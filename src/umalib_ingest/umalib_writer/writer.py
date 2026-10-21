"""Candidate-database Writer (TASK-008): dry-run already done, single transaction.

The source database is never opened for writing. The writer copies the source
bytes into a call-owned temporary file in the candidate directory, applies one
transaction there, verifies in-transaction and post-commit, and only then
publishes the final candidate with no-overwrite semantics.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path

from umalib_ingest.umalib_adapter import (
    CONFIRMED_SCHEMA_FINGERPRINT_SHA256,
    UmaLibChangePlan,
    inspect_umalib_database,
)
from umalib_ingest.umalib_adapter.errors import UmaLibInspectionError
from umalib_ingest.umalib_writer.approval import CandidateWriteApproval
from umalib_ingest.umalib_writer.errors import (
    UmaLibWriterError,
    WriterApprovalError,
    WriterDatabaseError,
    WriterExecutionError,
    WriterNotReadyError,
    WriterPathError,
)
from umalib_ingest.umalib_writer.file_state import (
    assert_unchanged,
    has_sidecars,
    read_only_connect,
    sha256_file,
    sha256_hex,
    snapshot_file,
    stable_json,
    validate_candidate_path,
    validate_source_path,
)
from umalib_ingest.umalib_writer.preview import (
    CandidateWritePreview,
    preview_candidate_write,
)
from umalib_ingest.umalib_writer.result import (
    RESULT_SCHEMA_VERSION,
    CandidateWriteResult,
)

_TEMP_PREFIX = ".umalib-writer-"
_TEMP_SUFFIX = ".tmp"

_INSERT_ARTICLE_SQL = (
    'INSERT INTO "article" (name, note, content, author, translator, uploadTime, source) '
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_TAG_SQL = 'INSERT INTO "tag" (name, type, cover, description) VALUES (?, ?, NULL, NULL)'
_INSERT_TAGGED_SQL = 'INSERT INTO "tagged" (artId, tagId) VALUES (?, ?)'


def write_candidate_database(
    plan: UmaLibChangePlan,
    source_db: str | Path,
    candidate_db: str | Path,
    approval: CandidateWriteApproval,
) -> CandidateWriteResult:
    """Write one approved article into a new candidate copy; never the source."""
    if not isinstance(plan, UmaLibChangePlan):
        raise UmaLibWriterError("plan 必须是 UmaLibChangePlan")
    if not isinstance(approval, CandidateWriteApproval):
        raise UmaLibWriterError("approval 必须是 CandidateWriteApproval")
    if type(approval.app_closed_confirmed) is not bool:
        raise WriterApprovalError("app_closed_confirmed 必须是布尔值")
    if approval.app_closed_confirmed is not True:
        raise WriterApprovalError("app_closed_confirmed 必须为 True（需人工确认大书库已关闭）")

    preview = preview_candidate_write(plan, source_db, candidate_db)
    if not preview.ready_to_write:
        raise WriterNotReadyError("Preview 存在 blockers，不能写入", preview)

    _check_approval_bindings(plan, preview, source_db, approval)

    source = validate_source_path(source_db)
    candidate = validate_candidate_path(source, candidate_db)
    source_state = snapshot_file(source)

    temp_path: Path | None = None
    temp_identity: tuple[int, int] | None = None
    candidate_identity: tuple[int, int] | None = None
    candidate_published = False
    plan_dict = plan.to_dict()
    try:
        temp_path, temp_identity = _create_temp_copy(source, candidate, source_state)
        _verify_temp_baseline(temp_path, preview)
        assert_unchanged(source, source_state)
        article_id, created_tags, tag_ids, tagged_ids = _apply_transaction(
            temp_path, plan_dict
        )
        if has_sidecars(temp_path):
            raise WriterExecutionError("临时副本残留 sidecar 文件")
        assert_unchanged(source, source_state)
        _post_commit_verify(
            temp_path, plan_dict, preview, article_id, created_tags, tag_ids, tagged_ids
        )
        assert_unchanged(source, source_state)
        candidate_identity = _publish(temp_path, candidate, temp_identity)
        candidate_published = True
        if not _unlink_owned_file(temp_path, temp_identity):
            raise WriterExecutionError("临时文件身份发生变化，拒绝清理")
        temp_path = None
        assert_unchanged(source, source_state)
        final_report, candidate_state = _final_candidate_inspection(
            candidate, candidate_identity, plan_dict, article_id,
            created_tags, tag_ids, tagged_ids,
        )
        source_after = assert_unchanged(source, source_state)
        return _build_result(
            plan, preview, source, source_state, source_after, candidate,
            candidate_state, final_report,
            article_id, created_tags, tag_ids, tagged_ids,
        )
    except (WriterNotReadyError, WriterApprovalError):
        raise
    except UmaLibWriterError:
        _cleanup(
            temp_path, temp_identity, candidate, candidate_published, candidate_identity
        )
        raise
    except sqlite3.Error:
        _cleanup(
            temp_path, temp_identity, candidate, candidate_published, candidate_identity
        )
        raise WriterExecutionError("数据库写入失败") from None
    except OSError:
        _cleanup(
            temp_path, temp_identity, candidate, candidate_published, candidate_identity
        )
        raise WriterExecutionError("文件操作失败") from None


def _check_approval_bindings(
    plan: UmaLibChangePlan,
    preview: CandidateWritePreview,
    source_db: str | Path,
    approval: CandidateWriteApproval,
) -> None:
    """Verify all four approval bindings before creating any file."""
    if approval.plan_sha256 != plan.plan_sha256:
        raise WriterApprovalError("approval.plan_sha256 与当前计划不一致")
    if approval.preview_sha256 != preview.preview_sha256:
        raise WriterApprovalError("approval.preview_sha256 与当前 Preview 不一致")
    try:
        current_source_sha = snapshot_file(Path(source_db)).sha256
    except UmaLibWriterError:
        raise WriterApprovalError("无法确认当前源数据库状态") from None
    if approval.source_database_sha256 != current_source_sha:
        raise WriterApprovalError("approval.source_database_sha256 与当前源库不一致")
    if approval.source_database_sha256 != plan.input["target_database_sha256"]:
        raise WriterApprovalError("approval.source_database_sha256 与计划绑定不一致")


def _path_identity(path: Path) -> tuple[int, int]:
    """Return a non-following identity for a regular file."""
    try:
        file_stat = os.stat(path, follow_symlinks=False)
    except OSError:
        raise WriterExecutionError("无法读取 Writer 文件身份") from None
    if not stat.S_ISREG(file_stat.st_mode):
        raise WriterExecutionError("Writer 文件不再是普通文件")
    return (file_stat.st_dev, file_stat.st_ino)


def _identity_matches(path: Path, expected: tuple[int, int]) -> bool:
    """Return whether path still names the expected regular file."""
    try:
        file_stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(file_stat.st_mode) and (
        file_stat.st_dev, file_stat.st_ino
    ) == expected


def _unlink_owned_file(path: Path, expected: tuple[int, int]) -> bool:
    """Unlink only when path still names the call-owned file."""
    if not path.exists() and not path.is_symlink():
        return True
    if not _identity_matches(path, expected):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _create_temp_copy(
    source: Path, candidate: Path, source_state
) -> tuple[Path, tuple[int, int]]:
    """Copy the source bytes into a call-owned temp file in the candidate dir."""
    descriptor, temp_name = tempfile.mkstemp(
        dir=str(candidate.parent), prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX
    )
    temp_path = Path(temp_name)
    try:
        descriptor_stat = os.fstat(descriptor)
        temp_identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
    except OSError:
        try:
            os.close(descriptor)
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise WriterExecutionError("无法记录临时文件身份") from None
    try:
        os.close(descriptor)
    except OSError:
        _unlink_owned_file(temp_path, temp_identity)
        raise WriterExecutionError("无法关闭临时文件句柄") from None
    try:
        if not _identity_matches(temp_path, temp_identity):
            raise WriterExecutionError("临时文件身份在复制前发生变化")
        shutil.copyfile(source, temp_path)
        if not _identity_matches(temp_path, temp_identity):
            raise WriterExecutionError("临时文件身份在复制期间发生变化")
        temp_state = snapshot_file(temp_path)
        if (
            temp_state.sha256 != source_state.sha256
            or temp_state.size_bytes != source_state.size_bytes
        ):
            raise WriterExecutionError("临时副本校验失败")
    except BaseException:
        _unlink_owned_file(temp_path, temp_identity)
        raise
    return temp_path, temp_identity


def _verify_temp_baseline(temp_path: Path, preview: CandidateWritePreview) -> None:
    """Run the inspector on the temp copy; schema/counts must match preview."""
    try:
        report = inspect_umalib_database(temp_path)
    except UmaLibInspectionError:
        raise WriterDatabaseError("无法检查临时副本") from None
    blockers = [issue for issue in report.issues if issue.severity == "blocker"]
    if blockers:
        raise WriterExecutionError("临时副本存在阻断性问题")
    if report.schema["schema_fingerprint_sha256"] != CONFIRMED_SCHEMA_FINGERPRINT_SHA256:
        raise WriterExecutionError("临时副本 Schema 指纹不一致")
    expected_counts = dict(preview.source["counts"])
    actual_counts = {
        "article": report.counts["article"],
        "tag": report.counts["tag"],
        "tagged": report.counts["tagged"],
    }
    if actual_counts != expected_counts:
        raise WriterExecutionError("临时副本行数与 Preview 基线不一致")


def _apply_transaction(
    temp_path: Path, plan_dict: dict[str, object]
) -> tuple[int, list[dict[str, object]], list[int], list[int]]:
    """Apply the single write transaction and return inserted identities."""
    conn = sqlite3.connect(str(temp_path))
    conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise WriterExecutionError("无法启用外键约束")
            conn.execute("BEGIN IMMEDIATE")
            try:
                _recheck_in_transaction(conn, plan_dict)
                article_id = _insert_article(conn, plan_dict)
                created_tags = _insert_create_tags(conn, plan_dict)
                tag_ids = _merged_tag_ids(plan_dict, created_tags)
                tagged_ids = _insert_tagged(conn, article_id, tag_ids)
                _verify_in_transaction(
                    conn, plan_dict, article_id, created_tags, tag_ids, tagged_ids
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        except sqlite3.Error:
            raise WriterExecutionError("数据库写入失败") from None
    finally:
        conn.close()
    return article_id, created_tags, tag_ids, tagged_ids


def _recheck_in_transaction(conn: sqlite3.Connection, plan_dict: dict[str, object]) -> None:
    """Re-verify duplicates and tag targets inside the write transaction."""
    article = plan_dict["article"]
    row = conn.execute(
        'SELECT id FROM "article" WHERE content = ? LIMIT 1', (article["content"],)
    ).fetchone()
    if row is not None:
        raise WriterExecutionError("事务内检测到正文精确重复")
    # 同标题同作者但正文不同是 Preview 中已展示并由 Approval 绑定的
    # warning，不得在写入阶段悄悄升级为 blocker。精确正文重复仍须阻断。
    for tag in plan_dict["tags"]["existing"]:
        row = conn.execute(
            'SELECT name, type FROM "tag" WHERE id = ?', (tag["id"],)
        ).fetchone()
        if row is None or (row["name"], row["type"]) != (tag["name"], tag["type"]):
            raise WriterExecutionError("事务内现有标签校验失败")
    create_names = [tag["name"] for tag in plan_dict["tags"]["create"]]
    if create_names:
        placeholders = ", ".join("?" for _ in create_names)
        rows = conn.execute(
            f'SELECT id FROM "tag" WHERE name IN ({placeholders})', create_names
        ).fetchall()
        if rows:
            raise WriterExecutionError("事务内检测到同名标签已存在")


def _insert_article(conn: sqlite3.Connection, plan_dict: dict[str, object]) -> int:
    """Insert the article row; returns the actual lastrowid."""
    article = plan_dict["article"]
    cursor = conn.execute(
        _INSERT_ARTICLE_SQL,
        (
            article["name"],
            article["note"],
            article["content"],
            article["author"],
            article["translator"],
            article["upload_time"],
            article["source"],
        ),
    )
    article_id = cursor.lastrowid
    if type(article_id) is not int or article_id < 1:
        raise WriterExecutionError("article 未返回有效自增 ID")
    return article_id


def _insert_create_tags(
    conn: sqlite3.Connection, plan_dict: dict[str, object]
) -> list[dict[str, object]]:
    """Insert planned new tags in stable order; returns their identities."""
    created: list[dict[str, object]] = []
    for tag in sorted(
        plan_dict["tags"]["create"], key=lambda item: (item["name"], item["type"])
    ):
        cursor = conn.execute(_INSERT_TAG_SQL, (tag["name"], tag["type"]))
        tag_id = cursor.lastrowid
        if type(tag_id) is not int or tag_id < 1:
            raise WriterExecutionError("tag 未返回有效自增 ID")
        created.append({"id": tag_id, "name": tag["name"], "type": tag["type"]})
    return created


def _merged_tag_ids(
    plan_dict: dict[str, object], created_tags: list[dict[str, object]]
) -> list[int]:
    """Merge existing and created tag ids in stable order."""
    existing = sorted(tag["id"] for tag in plan_dict["tags"]["existing"])
    created = [tag["id"] for tag in created_tags]
    merged: list[int] = []
    seen: set[int] = set()
    for tag_id in existing + created:
        if tag_id in seen:
            continue
        seen.add(tag_id)
        merged.append(tag_id)
    return merged


def _insert_tagged(
    conn: sqlite3.Connection, article_id: int, tag_ids: list[int]
) -> list[int]:
    """Insert tagged rows for the merged tag ids; returns tagged row ids."""
    tagged_ids: list[int] = []
    for tag_id in tag_ids:
        cursor = conn.execute(_INSERT_TAGGED_SQL, (article_id, tag_id))
        tagged_id = cursor.lastrowid
        if type(tagged_id) is not int or tagged_id < 1:
            raise WriterExecutionError("tagged 未返回有效自增 ID")
        tagged_ids.append(tagged_id)
    return tagged_ids


def _verify_in_transaction(
    conn: sqlite3.Connection,
    plan_dict: dict[str, object],
    article_id: int,
    created_tags: list[dict[str, object]],
    tag_ids: list[int],
    tagged_ids: list[int],
) -> None:
    """Verify every inserted row and integrity inside the transaction."""
    article = plan_dict["article"]
    row = conn.execute(
        'SELECT name, note, content, author, translator, uploadTime, source '
        'FROM "article" WHERE id = ?',
        (article_id,),
    ).fetchone()
    expected = (
        article["name"],
        article["note"],
        article["content"],
        article["author"],
        article["translator"],
        article["upload_time"],
        article["source"],
    )
    if row is None or tuple(row) != expected:
        raise WriterExecutionError("事务内文章回读不一致")
    for tag in created_tags:
        row = conn.execute(
            'SELECT name, type, cover, description FROM "tag" WHERE id = ?', (tag["id"],)
        ).fetchone()
        if (
            row is None
            or (row["name"], row["type"]) != (tag["name"], tag["type"])
            or row["cover"] is not None
            or row["description"] is not None
        ):
            raise WriterExecutionError("事务内新标签回读不一致")
    rows = conn.execute(
        'SELECT id, artId, tagId FROM "tagged" WHERE artId = ? ORDER BY id ASC',
        (article_id,),
    ).fetchall()
    if (
        [row["id"] for row in rows] != tagged_ids
        or [row["tagId"] for row in rows] != tag_ids
        or any(row["artId"] != article_id for row in rows)
    ):
        raise WriterExecutionError("事务内 tagged 回读不一致")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise WriterExecutionError("事务内外键检查失败")
    duplicate_pairs = conn.execute(
        'SELECT COUNT(*) FROM (SELECT artId, tagId FROM "tagged" WHERE artId = ? '
        "GROUP BY artId, tagId HAVING COUNT(*) > 1)",
        (article_id,),
    ).fetchone()[0]
    if duplicate_pairs:
        raise WriterExecutionError("事务内重复关联检查失败")
    integrity_rows = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
    if integrity_rows != ["ok"]:
        raise WriterExecutionError("事务内完整性检查失败")


def _post_commit_verify(
    temp_path: Path,
    plan_dict: dict[str, object],
    preview: CandidateWritePreview,
    article_id: int,
    created_tags: list[dict[str, object]],
    tag_ids: list[int],
    tagged_ids: list[int],
) -> None:
    """Read-only exact re-read and full inspector after commit."""
    try:
        with read_only_connect(temp_path) as conn:
            _verify_readonly_readback(
                conn, plan_dict, article_id, created_tags, tag_ids, tagged_ids
            )
        report = inspect_umalib_database(temp_path)
    except (UmaLibWriterError, UmaLibInspectionError):
        raise WriterExecutionError("提交后验证失败") from None
    blockers = [issue for issue in report.issues if issue.severity == "blocker"]
    if blockers:
        raise WriterExecutionError("提交后检查器发现阻断性问题")
    expected_counts = {
        "article": preview.source["counts"]["article"] + 1,
        "tag": preview.source["counts"]["tag"] + len(created_tags),
        "tagged": preview.source["counts"]["tagged"] + len(tagged_ids),
    }
    actual_counts = {
        "article": report.counts["article"],
        "tag": report.counts["tag"],
        "tagged": report.counts["tagged"],
    }
    if actual_counts != expected_counts:
        raise WriterExecutionError("提交后行数与预期不一致")


def _verify_readonly_readback(
    conn: sqlite3.Connection,
    plan_dict: dict[str, object],
    article_id: int,
    created_tags: list[dict[str, object]],
    tag_ids: list[int],
    tagged_ids: list[int],
) -> None:
    """Exact readback checks through a read-only connection."""
    article = plan_dict["article"]
    row = conn.execute(
        'SELECT name, note, content, author, translator, uploadTime, source '
        'FROM "article" WHERE id = ?',
        (article_id,),
    ).fetchone()
    expected = (
        article["name"],
        article["note"],
        article["content"],
        article["author"],
        article["translator"],
        article["upload_time"],
        article["source"],
    )
    if row is None or tuple(row) != expected:
        raise WriterExecutionError("提交后文章回读不一致")
    for tag in created_tags:
        row = conn.execute(
            'SELECT name, type, cover, description FROM "tag" WHERE id = ?', (tag["id"],)
        ).fetchone()
        if (
            row is None
            or (row["name"], row["type"]) != (tag["name"], tag["type"])
            or row["cover"] is not None
            or row["description"] is not None
        ):
            raise WriterExecutionError("提交后新标签回读不一致")
    rows = conn.execute(
        'SELECT id, artId, tagId FROM "tagged" WHERE artId = ? ORDER BY id ASC',
        (article_id,),
    ).fetchall()
    if (
        [row["id"] for row in rows] != tagged_ids
        or [row["tagId"] for row in rows] != tag_ids
        or any(row["artId"] != article_id for row in rows)
    ):
        raise WriterExecutionError("提交后 tagged 回读不一致")


def _publish(
    temp_path: Path, candidate: Path, temp_identity: tuple[int, int]
) -> tuple[int, int]:
    """Publish with no-overwrite semantics via a hard link."""
    if not _identity_matches(temp_path, temp_identity):
        raise WriterExecutionError("发布前临时文件身份发生变化")
    try:
        os.link(str(temp_path), str(candidate))
    except FileExistsError:
        raise WriterExecutionError("候选路径已被其他文件占用，已放弃") from None
    except OSError:
        raise WriterExecutionError("发布候选文件失败") from None
    try:
        candidate_identity = _path_identity(candidate)
        if candidate_identity != temp_identity:
            raise WriterExecutionError("发布后的候选文件身份校验失败")
        if sha256_file(candidate) != sha256_file(temp_path):
            raise WriterExecutionError("发布后的候选文件哈希校验失败")
        return candidate_identity
    except UmaLibWriterError:
        _unlink_owned_file(candidate, temp_identity)
        raise


def _final_candidate_inspection(
    candidate: Path,
    candidate_identity: tuple[int, int],
    plan_dict: dict[str, object],
    article_id: int,
    created_tags: list[dict[str, object]],
    tag_ids: list[int],
    tagged_ids: list[int],
):
    """Verify identity, exact rows and full inspector after publication."""
    if not _identity_matches(candidate, candidate_identity):
        raise WriterExecutionError("最终候选文件身份发生变化")
    before = snapshot_file(candidate)
    if (before.dev, before.ino) != candidate_identity or before.sidecars:
        raise WriterExecutionError("最终候选文件状态不安全")
    try:
        with read_only_connect(candidate) as conn:
            _verify_readonly_readback(
                conn, plan_dict, article_id, created_tags, tag_ids, tagged_ids
            )
        report = inspect_umalib_database(candidate)
    except UmaLibInspectionError:
        raise WriterExecutionError("候选文件最终检查失败") from None
    blockers = [issue for issue in report.issues if issue.severity == "blocker"]
    if blockers:
        raise WriterExecutionError("候选文件最终检查发现阻断性问题")
    if report.schema["schema_fingerprint_sha256"] != CONFIRMED_SCHEMA_FINGERPRINT_SHA256:
        raise WriterExecutionError("候选文件最终 Schema 指纹不一致")
    after = assert_unchanged(candidate, before)
    if (after.dev, after.ino) != candidate_identity:
        raise WriterExecutionError("最终候选文件身份发生变化")
    if (
        report.database["sha256"] != after.sha256
        or report.database["size_bytes"] != after.size_bytes
    ):
        raise WriterExecutionError("最终候选文件证据不一致")
    return report, after


def _build_result(
    plan: UmaLibChangePlan,
    preview: CandidateWritePreview,
    source: Path,
    source_state,
    source_after,
    candidate: Path,
    candidate_state,
    final_report,
    article_id: int,
    created_tags: list[dict[str, object]],
    tag_ids: list[int],
    tagged_ids: list[int],
) -> CandidateWriteResult:
    """Build the deep-immutable result after all verifications passed."""
    counts_before = {
        "article": preview.source["counts"]["article"],
        "tag": preview.source["counts"]["tag"],
        "tagged": preview.source["counts"]["tagged"],
    }
    counts_after = {
        "article": final_report.counts["article"],
        "tag": final_report.counts["tag"],
        "tagged": final_report.counts["tagged"],
    }
    expected_after = {
        "article": counts_before["article"] + 1,
        "tag": counts_before["tag"] + len(created_tags),
        "tagged": counts_before["tagged"] + len(tagged_ids),
    }
    if counts_after != expected_after:
        raise WriterExecutionError("最终候选行数与本次增量不一致")
    final_blockers = [
        issue for issue in final_report.issues if issue.severity == "blocker"
    ]
    if final_blockers:
        raise WriterExecutionError("最终候选检查存在 blockers")
    base = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "success": True,
        "plan_sha256": plan.plan_sha256,
        "preview_sha256": preview.preview_sha256,
        "source": {
            "file_name": source.name,
            "database_sha256_before": source_state.sha256,
            "database_sha256_after": source_after.sha256,
            "unchanged": True,
        },
        "candidate": {
            "file_name": candidate.name,
            "database_sha256": candidate_state.sha256,
            "size_bytes": candidate_state.size_bytes,
        },
        "inserted": {
            "article_id": article_id,
            "created_tags": created_tags,
            "reused_tag_ids": [
                tag_id for tag_id in tag_ids if tag_id not in {t["id"] for t in created_tags}
            ],
            "tagged_ids": tagged_ids,
            "tagged_count": len(tagged_ids),
        },
        "counts_before": counts_before,
        "counts_after": counts_after,
        "checks": {
            "article_readback": True,
            "created_tags_readback": True,
            "tagged_readback": True,
            "integrity_check": True,
            "foreign_key_violation_count": 0,
            "duplicate_tagged_pair_count": 0,
            "inspector_blocker_count": len(final_blockers),
        },
    }
    result_sha256 = sha256_hex(stable_json(base))
    return CandidateWriteResult(
        schema_version=RESULT_SCHEMA_VERSION,
        success=True,
        plan_sha256=plan.plan_sha256,
        preview_sha256=preview.preview_sha256,
        source=base["source"],
        candidate=base["candidate"],
        inserted=base["inserted"],
        counts_before=counts_before,
        counts_after=counts_after,
        checks=base["checks"],
        result_sha256=result_sha256,
    )


def _cleanup(
    temp_path: Path | None,
    temp_identity: tuple[int, int] | None,
    candidate: Path,
    candidate_published: bool,
    candidate_identity: tuple[int, int] | None,
) -> None:
    """Remove only files this call created; never foreign files."""
    if temp_path is not None and temp_identity is not None:
        _unlink_owned_file(temp_path, temp_identity)
    if candidate_published and candidate_identity is not None:
        _unlink_owned_file(candidate, candidate_identity)
