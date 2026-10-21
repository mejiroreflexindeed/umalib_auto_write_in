"""Strictly read-only TargetTagCatalog loader (TASK-008)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from umalib_ingest.umalib_adapter import (
    CONFIRMED_SCHEMA_FINGERPRINT_SHA256,
    TargetTag,
    TargetTagCatalog,
    inspect_umalib_database,
)
from umalib_ingest.umalib_adapter.errors import UmaLibInspectionError
from umalib_ingest.umalib_writer.errors import WriterDatabaseError
from umalib_ingest.umalib_writer.file_state import (
    assert_unchanged,
    read_only_connect,
    snapshot_file,
    validate_source_path,
)


def load_target_tag_catalog(source_db: str | Path) -> TargetTagCatalog:
    """Load the target tag catalog strictly read-only from the source database.

    Never copies, creates or writes any file; refuses on inspection blockers,
    schema fingerprint mismatch or any source change during the load.
    """
    target = validate_source_path(source_db)
    before = snapshot_file(target)
    try:
        report = inspect_umalib_database(target)
    except UmaLibInspectionError:
        raise WriterDatabaseError("无法只读检查源数据库") from None
    blockers = [issue for issue in report.issues if issue.severity == "blocker"]
    if blockers:
        raise WriterDatabaseError("源数据库存在阻断性问题，无法加载标签目录")
    fingerprint = report.schema["schema_fingerprint_sha256"]
    if fingerprint != CONFIRMED_SCHEMA_FINGERPRINT_SHA256:
        raise WriterDatabaseError("源数据库 Schema 指纹与已确认契约不一致")

    tags: list[TargetTag] = []
    try:
        with read_only_connect(target) as conn:
            usage: dict[int, int] = {}
            for row in conn.execute('SELECT tagId, COUNT(*) AS n FROM "tagged" GROUP BY tagId'):
                usage[row["tagId"]] = row["n"]
            for row in conn.execute('SELECT id, name, type FROM "tag" ORDER BY id ASC'):
                tags.append(_validated_tag(row, usage))
    except sqlite3.Error:
        raise WriterDatabaseError("读取标签目录失败") from None
    assert_unchanged(target, before)
    return TargetTagCatalog(
        database_sha256=report.database["sha256"],
        schema_fingerprint_sha256=fingerprint,
        tags=tuple(tags),
    )


def _validated_tag(row: sqlite3.Row, usage: dict[int, int]) -> TargetTag:
    """Build one TargetTag or raise on evidence inconsistent with the contract."""
    tag_id = row["id"]
    name = row["name"]
    tag_type = row["type"]
    if type(tag_id) is not int or tag_id < 1:
        raise WriterDatabaseError("标签 ID 不是正整数")
    if not isinstance(name, str) or name.strip() == "":
        raise WriterDatabaseError("标签名称不是非空字符串")
    if type(tag_type) is not int or tag_type not in (0, 1, 2, 3, 4):
        raise WriterDatabaseError("标签类型不在 0..4")
    count = usage.get(tag_id, 0)
    if type(count) is not int or count < 0:
        raise WriterDatabaseError("标签关联数不是非负整数")
    return TargetTag(id=tag_id, name=name, type=tag_type, usage_count=count)
