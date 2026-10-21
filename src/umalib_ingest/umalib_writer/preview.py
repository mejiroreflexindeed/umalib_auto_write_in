"""Dry-run candidate write preview (TASK-008).

preview_candidate_write never creates any file, never opens the source for
writing, and returns a deterministic, deep-immutable, minimal-disclosure
preview object.
"""

from __future__ import annotations

import ipaddress
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from umalib_ingest.umalib_adapter import (
    CONFIRMED_SCHEMA_FINGERPRINT_SHA256,
    UmaLibChangePlan,
    inspect_umalib_database,
)
from umalib_ingest.umalib_adapter.change_plan import PLAN_SCHEMA_VERSION
from umalib_ingest.umalib_adapter.errors import UmaLibInspectionError
from umalib_ingest.umalib_writer.errors import UmaLibWriterError
from umalib_ingest.umalib_writer.file_state import (
    has_sidecars,
    path_sha256,
    read_only_connect,
    sha256_hex,
    snapshot_file,
    stable_json,
    validate_candidate_path,
    validate_source_path,
)

PREVIEW_SCHEMA_VERSION = "1.0"

_MAX_ARTICLE_NAME = 50
_MAX_ARTICLE_NOTE = 100
_MAX_ARTICLE_AUTHOR = 50
_MAX_ARTICLE_TRANSLATOR = 50
_MAX_TAG_NAME = 50
_MAX_UPLOAD_TIME = 2147483647
_DUPLICATE_ID_LIMIT = 20

_QUILL_TEXT_RE = re.compile(r"(?:[^&<>]|&(?:amp|lt|gt);)+")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ZERO_SHA256 = "0" * 64
_ASCII_WHITESPACE = " \t\n\r\v\f"

_TOP_LEVEL_KEYS = {
    "schema_version", "input", "ready", "article", "tags", "diagnostics", "plan_sha256",
}
_INPUT_KEYS = {
    "item_id", "package_sha256", "target_database_sha256",
    "target_schema_fingerprint_sha256", "target_tag_catalog_sha256",
}
_ARTICLE_KEYS = {"name", "note", "content", "author", "translator", "upload_time", "source"}
_TAGS_KEYS = {"existing", "create"}
_DIAGNOSTICS_KEYS = {"blockers", "warnings"}


def _deep_freeze(value: object) -> object:
    """Return an immutable, detached representation of JSON-like data."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: object) -> object:
    """Return a fresh mutable JSON-like copy of frozen preview data."""
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class WriterIssue:
    """One machine-identifiable preview diagnostic."""

    severity: str  # blocker / warning
    code: str
    field: str
    message: str


def _diag(severity: str, code: str, field: str, message: str) -> WriterIssue:
    """Build one diagnostic value."""
    return WriterIssue(severity=severity, code=code, field=field, message=message)


@dataclass(frozen=True)
class CandidateWritePreview:
    """A deterministic, deep-immutable dry-run preview; never authorizes writes."""

    schema_version: str
    plan_sha256: str
    source: Mapping[str, object]
    candidate: Mapping[str, object]
    operations: Mapping[str, object]
    duplicates: Mapping[str, object]
    ready_to_write: bool
    diagnostics: Mapping[str, object]
    preview_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _deep_freeze(self.source))
        object.__setattr__(self, "candidate", _deep_freeze(self.candidate))
        object.__setattr__(self, "operations", _deep_freeze(self.operations))
        object.__setattr__(self, "duplicates", _deep_freeze(self.duplicates))
        object.__setattr__(self, "diagnostics", _deep_freeze(self.diagnostics))

    def to_dict(self) -> dict[str, object]:
        """Return a fresh deep copy of the preview as a plain dict."""
        return {
            "schema_version": self.schema_version,
            "plan_sha256": self.plan_sha256,
            "source": _deep_thaw(self.source),
            "candidate": _deep_thaw(self.candidate),
            "operations": _deep_thaw(self.operations),
            "duplicates": _deep_thaw(self.duplicates),
            "ready_to_write": self.ready_to_write,
            "diagnostics": _deep_thaw(self.diagnostics),
            "preview_sha256": self.preview_sha256,
        }


def preview_candidate_write(
    plan: UmaLibChangePlan,
    source_db: str | Path,
    candidate_db: str | Path,
) -> CandidateWritePreview:
    """Build a deterministic dry-run preview without creating any file.

    Expected business gaps become blockers in diagnostics; only structurally
    wrong plan objects raise UmaLibWriterError.
    """
    if not isinstance(source_db, (str, Path)) or not isinstance(candidate_db, (str, Path)):
        raise UmaLibWriterError("source_db 和 candidate_db 必须是路径")
    blockers: list[WriterIssue] = []
    warnings: list[WriterIssue] = []

    plan_dict = _validate_plan_shape(plan)
    _check_plan_bindings(plan, plan_dict, blockers)

    source_state = _inspect_source(source_db, blockers, warnings)
    catalog_map = _load_catalog(source_db, blockers) if source_state is not None else {}
    _check_plan_against_source(plan, plan_dict, source_state, catalog_map, blockers)
    _check_article_fields(plan_dict, blockers)
    _check_tags(plan_dict, catalog_map, blockers)
    duplicate_evidence = _collect_duplicates(
        source_db, plan_dict, blockers, warnings
    ) if source_state is not None else {
        "exact_content_count": 0,
        "exact_content_article_ids": [],
        "same_title_author_count": 0,
        "same_title_author_article_ids": [],
    }
    _recheck_preview_source(source_db, source_state, blockers)

    source_section = (
        _source_section(source_state)
        if source_state is not None
        else {"file_name": Path(source_db).name or "unknown.db", "path_sha256": path_sha256(source_db),
              "database_sha256": _ZERO_SHA256, "schema_fingerprint_sha256": _ZERO_SHA256,
              "size_bytes": 0, "counts": {"article": 0, "tag": 0, "tagged": 0}}
    )
    candidate_section = _candidate_section(source_db, candidate_db, blockers)
    operations = _operations_section(plan_dict)
    diagnostics = _diagnostics_dict(blockers, warnings)
    ready = not blockers

    base = {
        "schema_version": PREVIEW_SCHEMA_VERSION,
        "plan_sha256": plan.plan_sha256,
        "source": source_section,
        "candidate": candidate_section,
        "operations": operations,
        "duplicates": duplicate_evidence,
        "ready_to_write": ready,
        "diagnostics": diagnostics,
    }
    preview_sha256 = sha256_hex(stable_json(base))
    return CandidateWritePreview(
        schema_version=PREVIEW_SCHEMA_VERSION,
        plan_sha256=plan.plan_sha256,
        source=source_section,
        candidate=candidate_section,
        operations=operations,
        duplicates=duplicate_evidence,
        ready_to_write=ready,
        diagnostics=diagnostics,
        preview_sha256=preview_sha256,
    )


def _validate_plan_shape(plan: UmaLibChangePlan) -> dict[str, object]:
    """Reject programming-level plan shape errors; return a thawed dict."""
    if not isinstance(plan, UmaLibChangePlan):
        raise UmaLibWriterError("plan 必须是 UmaLibChangePlan")
    try:
        plan_dict = plan.to_dict()
    except Exception:
        raise UmaLibWriterError("plan 无法转换为稳定数据") from None
    if not isinstance(plan_dict, dict) or set(plan_dict) != _TOP_LEVEL_KEYS:
        raise UmaLibWriterError("plan 顶层字段不正确")
    if not isinstance(plan.plan_sha256, str) or _SHA256_RE.fullmatch(plan.plan_sha256) is None:
        raise UmaLibWriterError("plan_sha256 必须是小写 SHA-256")
    try:
        stable_json(plan_dict)
    except (TypeError, ValueError):
        raise UmaLibWriterError("plan 包含无法稳定序列化的数据") from None
    return plan_dict


def _check_plan_bindings(
    plan: UmaLibChangePlan,
    plan_dict: dict[str, object],
    blockers: list[WriterIssue],
) -> None:
    """Re-verify the plan's shape, own hash and ready flag."""
    if plan_dict.get("schema_version") != PLAN_SCHEMA_VERSION:
        blockers.append(
            _diag("blocker", "plan_schema_mismatch", "schema_version", "计划 Schema 版本不受支持")
        )
    if plan.ready is not True or plan_dict.get("ready") is not True:
        blockers.append(
            _diag("blocker", "plan_not_ready", "ready", "计划未就绪（ready != true）")
        )
    inputs = plan_dict.get("input")
    if not isinstance(inputs, dict):
        blockers.append(_diag("blocker", "plan_input_shape_invalid", "input", "计划 input 必须是对象"))
    else:
        if set(inputs) != _INPUT_KEYS:
            blockers.append(
                _diag("blocker", "plan_input_shape_invalid", "input", "计划 input 字段不正确")
            )
        item_id = inputs.get("item_id")
        if type(item_id) is not int or item_id < 1:
            blockers.append(
                _diag("blocker", "plan_item_id_invalid", "input.item_id", "item_id 必须是正整数")
            )
        for key in (
            "package_sha256", "target_database_sha256",
            "target_schema_fingerprint_sha256", "target_tag_catalog_sha256",
        ):
            value = inputs.get(key)
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                blockers.append(
                    _diag("blocker", "plan_input_hash_invalid", f"input.{key}", f"{key} 必须是小写 SHA-256")
                )

    article = plan_dict.get("article")
    if isinstance(article, dict) and set(article) != _ARTICLE_KEYS:
        blockers.append(
            _diag("blocker", "article_shape_invalid", "article", "article 字段不正确，禁止预分配 ID")
        )
    tags = plan_dict.get("tags")
    if isinstance(tags, dict) and set(tags) != _TAGS_KEYS:
        blockers.append(
            _diag("blocker", "tags_shape_invalid", "tags", "tags 字段不正确，禁止预分配 tagged ID")
        )

    diagnostics = plan_dict.get("diagnostics")
    diagnostics_valid = (
        isinstance(diagnostics, dict)
        and set(diagnostics) == _DIAGNOSTICS_KEYS
        and isinstance(diagnostics.get("blockers"), list)
        and isinstance(diagnostics.get("warnings"), list)
    )
    if not diagnostics_valid or diagnostics["blockers"]:
        blockers.append(
            _diag("blocker", "plan_has_blockers", "diagnostics", "计划自身仍含 blockers")
        )
    elif any(
        not isinstance(issue, dict)
        or set(issue) != {"code", "field", "message"}
        or any(not isinstance(issue.get(key), str) for key in ("code", "field", "message"))
        for group in (diagnostics["blockers"], diagnostics["warnings"])
        for issue in group
    ):
        blockers.append(
            _diag("blocker", "plan_diagnostics_shape_invalid", "diagnostics", "计划 diagnostics 形状不正确")
        )
    payload = {key: value for key, value in plan_dict.items() if key != "plan_sha256"}
    recomputed = sha256_hex(stable_json(payload))
    if plan_dict.get("plan_sha256") != plan.plan_sha256 or recomputed != plan.plan_sha256:
        blockers.append(
            _diag("blocker", "plan_hash_mismatch", "plan_sha256", "计划哈希与内容不一致")
        )


def _inspect_source(
    source_db: str | Path,
    blockers: list[WriterIssue],
    warnings: list[WriterIssue],
) -> dict[str, object] | None:
    """Read-only inspect the source; return evidence or append blockers."""
    try:
        source = validate_source_path(source_db)
    except UmaLibWriterError as exc:
        blockers.append(_diag("blocker", "source_path_invalid", "source", str(exc)))
        return None
    if has_sidecars(source):
        blockers.append(
            _diag("blocker", "source_sidecar_present", "source", "源数据库存在 -wal/-shm/-journal")
        )
        return None
    before = snapshot_file(source)
    try:
        report = inspect_umalib_database(source)
    except UmaLibInspectionError:
        blockers.append(
            _diag("blocker", "source_inspection_failed", "source", "无法只读检查源数据库")
        )
        return None
    report_blockers = [i for i in report.issues if i.severity == "blocker"]
    for issue in report_blockers:
        blockers.append(
            _diag("blocker", "source_inspector_blocker", "source", issue.message)
        )
    if report.schema["schema_fingerprint_sha256"] != CONFIRMED_SCHEMA_FINGERPRINT_SHA256:
        blockers.append(
            _diag(
                "blocker",
                "source_schema_fingerprint_mismatch",
                "source",
                "源数据库 Schema 指纹与已确认契约不一致",
            )
        )
    try:
        snapshot_file(source)
    except UmaLibWriterError:
        blockers.append(
            _diag("blocker", "source_changed_during_preview", "source", "源数据库在预览期间发生变化")
        )
        return None
    after = snapshot_file(source)
    if after != before:
        blockers.append(
            _diag("blocker", "source_changed_during_preview", "source", "源数据库在预览期间发生变化")
        )
        return None
    warnings.extend(
        _diag("warning", "source_inspector_warning", "source", issue.message)
        for issue in report.issues
        if issue.severity != "blocker"
    )
    return {
        "file_name": report.database["file_name"],
        "path_sha256": path_sha256(source),
        "database_sha256": report.database["sha256"],
        "schema_fingerprint_sha256": report.schema["schema_fingerprint_sha256"],
        "size_bytes": report.database["size_bytes"],
        "counts": {
            "article": report.counts["article"],
            "tag": report.counts["tag"],
            "tagged": report.counts["tagged"],
        },
        "_file_state": after,
    }


def _recheck_preview_source(
    source_db: str | Path,
    source_state: dict[str, object] | None,
    blockers: list[WriterIssue],
) -> None:
    """Bind the complete preview to one unchanged source-file state."""
    if source_state is None:
        return
    before = source_state.get("_file_state")
    try:
        source = validate_source_path(source_db)
        if before is None:
            raise UmaLibWriterError("缺少源文件状态")
        current = snapshot_file(source)
    except UmaLibWriterError:
        blockers.append(
            _diag("blocker", "source_changed_during_preview", "source", "源数据库在预览期间发生变化")
        )
        return
    relevant = lambda state: (
        state.sha256, state.size_bytes, state.mtime_ns, state.dev, state.ino,
        state.sidecars, state.directory_names,
    )
    if relevant(current) != relevant(before):
        blockers.append(
            _diag("blocker", "source_changed_during_preview", "source", "源数据库在预览期间发生变化")
        )


def _load_catalog(source_db: str | Path, blockers: list[WriterIssue]) -> dict[int, dict[str, object]]:
    """Read-only load current tag evidence keyed by id and by name."""
    from umalib_ingest.umalib_writer.catalog import load_target_tag_catalog

    try:
        catalog = load_target_tag_catalog(source_db)
    except UmaLibWriterError as exc:
        blockers.append(_diag("blocker", "catalog_load_failed", "source", str(exc)))
        return {}
    by_id: dict[int, dict[str, object]] = {}
    by_name: dict[str, list[dict[str, object]]] = {}
    for tag in catalog.tags:
        by_id[tag.id] = {"id": tag.id, "name": tag.name, "type": tag.type}
        by_name.setdefault(tag.name, []).append({"id": tag.id, "name": tag.name, "type": tag.type})
    return {"__by_id": by_id, "__by_name": by_name}


def _check_plan_against_source(
    plan: UmaLibChangePlan,
    plan_dict: dict[str, object],
    source_state: dict[str, object] | None,
    catalog_map: dict[int, dict[str, object]],
    blockers: list[WriterIssue],
) -> None:
    """Bind the plan's input hashes to the current source evidence."""
    if source_state is None:
        return
    inputs = plan_dict.get("input")
    if not isinstance(inputs, dict):
        return
    target_db = inputs.get("target_database_sha256")
    if target_db != source_state["database_sha256"]:
        blockers.append(
            _diag("blocker", "target_database_sha256_mismatch", "input", "计划绑定的目标数据库 SHA 与当前源库不一致")
        )
    fingerprint = inputs.get("target_schema_fingerprint_sha256")
    if fingerprint != source_state["schema_fingerprint_sha256"]:
        blockers.append(
            _diag("blocker", "target_schema_fingerprint_mismatch", "input", "计划绑定的 Schema 指纹与当前源库不一致")
        )
    catalog_hash = inputs.get("target_tag_catalog_sha256")
    if catalog_map:
        current_catalog_hash = _catalog_hash_from_map(catalog_map)
        if catalog_hash != current_catalog_hash:
            blockers.append(
                _diag("blocker", "target_catalog_hash_mismatch", "input", "计划绑定的标签目录哈希与当前源库不一致")
            )


def _catalog_hash_from_map(catalog_map: dict[int, dict[str, object]]) -> str:
    """Recompute the {id,name,type} catalog hash from loaded evidence."""
    by_id: dict[int, dict[str, object]] = catalog_map["__by_id"]
    payload = [
        {"id": item["id"], "name": item["name"], "type": item["type"]}
        for _, item in sorted(by_id.items(), key=lambda kv: kv[0])
    ]
    return sha256_hex(stable_json(payload))


def _check_article_fields(plan_dict: dict[str, object], blockers: list[WriterIssue]) -> None:
    """Re-verify article field values and conservative length bounds."""
    article = plan_dict.get("article")
    if not isinstance(article, dict):
        blockers.append(_diag("blocker", "article_missing", "article", "计划缺少 article 字段"))
        return
    name = article.get("name")
    if not isinstance(name, str) or name.strip() == "":
        blockers.append(_diag("blocker", "article_name_invalid", "name", "标题必须是非空字符串"))
    elif len(name) > _MAX_ARTICLE_NAME:
        blockers.append(_diag("blocker", "article_name_too_long", "name", f"标题超过 {_MAX_ARTICLE_NAME} 字符"))
    author = article.get("author")
    if not isinstance(author, str) or author.strip() == "":
        blockers.append(_diag("blocker", "article_author_invalid", "author", "作者必须是非空字符串"))
    elif len(author) > _MAX_ARTICLE_AUTHOR:
        blockers.append(_diag("blocker", "article_author_too_long", "author", f"作者超过 {_MAX_ARTICLE_AUTHOR} 字符"))
    note = article.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > _MAX_ARTICLE_NOTE):
        blockers.append(_diag("blocker", "article_note_invalid", "note", f"备注必须是字符串且不超过 {_MAX_ARTICLE_NOTE} 字符"))
    translator = article.get("translator")
    if translator is not None and (not isinstance(translator, str) or len(translator) > _MAX_ARTICLE_TRANSLATOR):
        blockers.append(
            _diag("blocker", "article_translator_invalid", "translator", f"译者必须是字符串且不超过 {_MAX_ARTICLE_TRANSLATOR} 字符")
        )
    content = article.get("content")
    if not (_valid_minimal_quill(content) or _valid_basic_format_quill(content)):
        blockers.append(
            _diag("blocker", "article_content_invalid", "content", "正文不是合法的 TASK-007 最小 Quill HTML 或安全基本格式子集")
        )
    upload_time = article.get("upload_time")
    if type(upload_time) is not int or upload_time < 0 or upload_time > _MAX_UPLOAD_TIME:
        blockers.append(
            _diag("blocker", "article_upload_time_invalid", "upload_time", "upload_time 必须是 0..2147483647 的整数")
        )
    source = article.get("source")
    if not _valid_source_text(source):
        blockers.append(
            _diag("blocker", "article_source_invalid", "source", "source 必须是空串或单个 ASCII 空格连接的绝对 HTTP(S) URL")
        )


def _valid_minimal_quill(content: object) -> bool:
    """Deterministic structural check for TASK-007 minimal Quill HTML."""
    if not isinstance(content, str) or content == "":
        return False
    if any(ord(char) < 32 and char != "\t" for char in content):
        return False
    position = 0
    has_text = False
    while position < len(content):
        if not content.startswith("<p>", position):
            return False
        close = content.find("</p>", position + 3)
        if close < 0:
            return False
        inner = content[position + 3:close]
        if inner == "<br>":
            position = close + 4
            continue
        if inner == "" or _QUILL_TEXT_RE.fullmatch(inner) is None:
            return False
        if inner.strip() != "":
            has_text = True
        position = close + 4
    return position == len(content) and has_text


def _valid_basic_format_quill(content: object) -> bool:
    """Accept the TASK-013 basic-format Quill subset (strong/em/u/s/color span)."""
    if not isinstance(content, str) or content == "":
        return False
    from umalib_ingest.formatting.quill import validate_quill_subset

    return validate_quill_subset(content)


def _valid_source_text(source: object) -> bool:
    """Validate the flattened source string."""
    if source == "":
        return True
    if not isinstance(source, str):
        return False
    if source != source.strip():
        return False
    if "  " in source:
        return False
    tokens = source.split(" ")
    if any(token == "" for token in tokens):
        return False
    for token in tokens:
        if not _valid_absolute_url(token):
            return False
    return True


def _valid_absolute_url(token: str) -> bool:
    """Validate one absolute http(s) URL without credentials."""
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token):
        return False
    try:
        parsed = urlsplit(token)
        _ = parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if hostname is None or hostname == "":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.port is not None and not (0 < parsed.port <= 65535):
        return False
    return _valid_hostname(hostname)


def _valid_hostname(hostname: str) -> bool:
    """Accept a valid IP literal or DNS-style host; reject parser-only hosts."""
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    if hostname.endswith("."):
        hostname = hostname[:-1]
    if hostname == "" or len(hostname) > 253:
        return False
    if hostname.replace(".", "").isdigit():
        return False
    for label in hostname.split("."):
        try:
            ascii_label = label.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        if not (1 <= len(ascii_label) <= 63):
            return False
        if ascii_label.startswith("-") or ascii_label.endswith("-"):
            return False
        if any(not (char.isalnum() or char == "-") for char in ascii_label):
            return False
    return True


def _check_tags(
    plan_dict: dict[str, object],
    catalog_map: dict[int, dict[str, object]],
    blockers: list[WriterIssue],
) -> None:
    """Re-verify existing/create tag targets against the current database."""
    tags = plan_dict.get("tags")
    if not isinstance(tags, dict):
        blockers.append(_diag("blocker", "tags_missing", "tags", "计划缺少 tags 字段"))
        return
    existing = tags.get("existing")
    create = tags.get("create")
    if not isinstance(existing, list) or not isinstance(create, list):
        blockers.append(_diag("blocker", "tags_shape_invalid", "tags", "tags.existing/create 必须是列表"))
        return
    by_id: dict[int, dict[str, object]] = catalog_map.get("__by_id", {})
    by_name: dict[str, list[dict[str, object]]] = catalog_map.get("__by_name", {})

    seen_existing_ids: set[int] = set()
    for index, item in enumerate(existing):
        if not isinstance(item, dict) or set(item) != {"id", "name", "type"}:
            blockers.append(_diag("blocker", "existing_tag_shape_invalid", "tags.existing", f"existing[{index}] 形状不正确"))
            continue
        tag_id = item["id"]
        if type(tag_id) is not int or tag_id < 1:
            blockers.append(_diag("blocker", "existing_tag_invalid", "tags.existing", f"existing[{index}] 的 ID 非法"))
            continue
        name = item["name"]
        tag_type = item["type"]
        if not isinstance(name, str) or name.strip() == "" or type(tag_type) is not int or tag_type not in (0, 1, 2, 3, 4):
            blockers.append(
                _diag("blocker", "existing_tag_invalid", "tags.existing", f"existing[{index}] 的 name/type 非法")
            )
            continue
        if tag_id in seen_existing_ids:
            blockers.append(_diag("blocker", "existing_tag_duplicate", "tags.existing", f"existing 中 ID 重复: {tag_id}"))
            continue
        seen_existing_ids.add(tag_id)
        current = by_id.get(tag_id)
        if current is None:
            blockers.append(_diag("blocker", "existing_tag_mismatch", "tags.existing", f"现有标签 ID {tag_id} 不在当前数据库中"))
        elif (current["name"], current["type"]) != (name, tag_type):
            blockers.append(
                _diag("blocker", "existing_tag_mismatch", "tags.existing", f"现有标签 {tag_id} 的 name/type 与当前数据库不一致")
            )

    seen_creates: set[tuple[str, int]] = set()
    planned_names: set[str] = set()
    for index, item in enumerate(create):
        if not isinstance(item, dict) or set(item) != {"name", "type"}:
            blockers.append(_diag("blocker", "create_tag_shape_invalid", "tags.create", f"create[{index}] 形状不正确"))
            continue
        name = item["name"]
        tag_type = item["type"]
        if not isinstance(name, str) or name.strip() == "":
            blockers.append(_diag("blocker", "create_tag_name_invalid", "tags.create", f"create[{index}] 名称为空"))
            continue
        if len(name) > _MAX_TAG_NAME:
            blockers.append(_diag("blocker", "create_tag_name_too_long", "tags.create", f"新标签名超过 {_MAX_TAG_NAME} 字符"))
            continue
        if type(tag_type) is not int or tag_type not in (0, 1, 2, 3, 4):
            blockers.append(_diag("blocker", "create_tag_type_invalid", "tags.create", f"create[{index}] 类型非法"))
            continue
        key = (name, tag_type)
        if key in seen_creates or name in planned_names:
            blockers.append(_diag("blocker", "create_tag_duplicate", "tags.create", f"create 中存在重复目标: {name}/{tag_type}"))
            continue
        seen_creates.add(key)
        if name in by_name:
            blockers.append(_diag("blocker", "new_tag_name_exists", "tags.create", f"当前数据库已存在同名标签，不能新建: {name}"))
        planned_names.add(name)


def _collect_duplicates(
    source_db: str | Path,
    plan_dict: dict[str, object],
    blockers: list[WriterIssue],
    warnings: list[WriterIssue],
) -> dict[str, object]:
    """Read-only duplicate evidence: counts plus at most 20 article IDs."""
    article = plan_dict.get("article")
    if not isinstance(article, dict):
        return {
            "exact_content_count": 0,
            "exact_content_article_ids": [],
            "same_title_author_count": 0,
            "same_title_author_article_ids": [],
        }
    content = article.get("content")
    name = article.get("name")
    author = article.get("author")
    if not isinstance(content, str) or not isinstance(name, str) or not isinstance(author, str):
        return {
            "exact_content_count": 0,
            "exact_content_article_ids": [],
            "same_title_author_count": 0,
            "same_title_author_article_ids": [],
        }
    try:
        source = validate_source_path(source_db)
        with read_only_connect(source) as conn:
            exact_count = conn.execute(
                'SELECT COUNT(*) FROM "article" WHERE content = ?', (content,)
            ).fetchone()[0]
            exact_ids = [
                row["id"]
                for row in conn.execute(
                    'SELECT id FROM "article" WHERE content = ? ORDER BY id ASC LIMIT ?',
                    (content, _DUPLICATE_ID_LIMIT),
                ).fetchall()
            ]
            similar_count = conn.execute(
                'SELECT COUNT(*) FROM "article" WHERE name = ? AND author = ? '
                'AND (content IS NULL OR content != ?)',
                (name, author, content),
            ).fetchone()[0]
            similar_ids = [
                row["id"]
                for row in conn.execute(
                    'SELECT id FROM "article" WHERE name = ? AND author = ? '
                    "AND (content IS NULL OR content != ?) ORDER BY id ASC LIMIT ?",
                    (name, author, content, _DUPLICATE_ID_LIMIT),
                ).fetchall()
            ]
    except (UmaLibWriterError, sqlite3.Error):
        blockers.append(
            _diag("blocker", "duplicate_check_failed", "duplicates", "无法完成重复检查")
        )
        return {
            "exact_content_count": 0,
            "exact_content_article_ids": [],
            "same_title_author_count": 0,
            "same_title_author_article_ids": [],
        }
    if exact_count:
        blockers.append(
            _diag("blocker", "exact_content_duplicate", "duplicates", "已存在正文完全相同的文章")
        )
    if similar_count:
        warnings.append(
            _diag("warning", "same_title_author", "duplicates", "已存在同标题同作者但正文不同的文章")
        )
    return {
        "exact_content_count": exact_count,
        "exact_content_article_ids": exact_ids,
        "same_title_author_count": similar_count,
        "same_title_author_article_ids": similar_ids,
    }


def _source_section(state: dict[str, object]) -> dict[str, object]:
    """Build the minimal-disclosure source section."""
    return {
        "file_name": state["file_name"],
        "path_sha256": state["path_sha256"],
        "database_sha256": state["database_sha256"],
        "schema_fingerprint_sha256": state["schema_fingerprint_sha256"],
        "size_bytes": state["size_bytes"],
        "counts": {
            "article": state["counts"]["article"],
            "tag": state["counts"]["tag"],
            "tagged": state["counts"]["tagged"],
        },
    }


def _candidate_section(
    source_db: str | Path, candidate_db: str | Path, blockers: list[WriterIssue]
) -> dict[str, object]:
    """Validate the candidate path and report its existence status."""
    from umalib_ingest.umalib_writer.errors import WriterPathError

    candidate = Path(candidate_db)
    file_name = candidate.name or "candidate.db"
    exists = candidate.exists() or candidate.is_symlink()
    if exists:
        blockers.append(_diag("blocker", "candidate_exists", "candidate", "候选文件已存在，禁止覆盖"))
    if has_sidecars(candidate):
        blockers.append(_diag("blocker", "candidate_sidecar_present", "candidate", "候选路径存在 sidecar 文件"))
    if not exists:
        try:
            validate_candidate_path(source_db, candidate_db)
        except WriterPathError as exc:
            blockers.append(_diag("blocker", "candidate_path_invalid", "candidate", str(exc)))
    return {
        "file_name": file_name,
        "path_sha256": path_sha256(candidate_db),
        "exists": exists,
    }


def _operations_section(plan_dict: dict[str, object]) -> dict[str, object]:
    """Build the deterministic operations summary."""
    tags = plan_dict.get("tags") or {}
    if not isinstance(tags, dict):
        tags = {}
    existing = tags.get("existing") if isinstance(tags.get("existing"), list) else []
    create = tags.get("create") if isinstance(tags.get("create"), list) else []
    existing_ids = sorted({
        item["id"]
        for item in existing
        if isinstance(item, dict)
        and set(item) == {"id", "name", "type"}
        and type(item["id"]) is int
        and item["id"] >= 1
    })
    create_tags = sorted(
        (
            {"name": item["name"], "type": item["type"]}
            for item in create
            if isinstance(item, dict)
            and set(item) == {"name", "type"}
            and isinstance(item["name"], str)
            and item["name"].strip() != ""
            and type(item["type"]) is int
            and item["type"] in (0, 1, 2, 3, 4)
        ),
        key=lambda item: (item["name"], item["type"]),
    )
    return {
        "article_insert_count": 1,
        "existing_tag_ids": existing_ids,
        "create_tags": create_tags,
        "tagged_insert_count": len(existing_ids) + len(create_tags),
    }


def _diagnostics_dict(
    blockers: list[WriterIssue], warnings: list[WriterIssue]
) -> dict[str, object]:
    """Return diagnostics with stable ordering by (code, field, message)."""

    def sorted_list(items: list[WriterIssue]) -> list[dict[str, object]]:
        return [
            {"code": item.code, "field": item.field, "message": item.message}
            for item in sorted(items, key=lambda d: (d.code, d.field, d.message))
        ]

    return {"blockers": sorted_list(blockers), "warnings": sorted_list(warnings)}
