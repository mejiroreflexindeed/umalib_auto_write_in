"""Pure UmaLib change-plan core (TASK-007).

Everything here is a pure function or value object: no files, no SQLite, no
network, no clock, no stdout/stderr. The result is a previewable, hashable
plan data object, never executable SQL and never a database write.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit

from umalib_ingest.handoff.package import ApprovedSubmissionPackage
from umalib_ingest.umalib_adapter.errors import AdapterContentError, AdapterError

PLAN_SCHEMA_VERSION = "1.0"

CONFIRMED_SCHEMA_FINGERPRINT_SHA256 = (
    "d7b1966992e75819e37d41ae9c091ec21c2cdf9c5b6727af78f95398d0179aa8"
)

VALID_TAG_TYPES = frozenset({0, 1, 2, 3, 4})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ZERO_SHA256 = "0" * 64
_ASCII_WHITESPACE = " \t\n\r\v\f"
_SOURCE_KEYS = ("nga", "original_series", "original", "translation_series", "translation")


def _stable_json(obj: object) -> str:
    """Serialize with stable key order for deterministic hashing."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(text: str) -> str:
    """Return the lowercase hex SHA-256 of a UTF-8 encoded string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _deep_freeze(value: object) -> object:
    """Return an immutable, detached representation of JSON-like data."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: object) -> object:
    """Return a fresh mutable JSON-like copy of frozen plan data."""
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return copy.deepcopy(value)


@dataclass(frozen=True)
class TargetTag:
    """One target-database tag with metadata only; usage_count is advisory."""

    id: int
    name: str
    type: int
    usage_count: int | None = None


@dataclass(frozen=True)
class TargetTagCatalog:
    """A snapshot of the target database identity and its tag metadata."""

    database_sha256: str
    schema_fingerprint_sha256: str
    tags: tuple[TargetTag, ...]


@dataclass(frozen=True)
class TagChoice:
    """One human decision for one requested tag or series value."""

    requested_kind: str  # "tag" / "series"
    requested_value: str
    existing_tag_id: int | None = None
    new_tag_type: int | None = None


@dataclass(frozen=True)
class AdapterChoices:
    """Human decisions required to build a change plan."""

    upload_time_seconds: int
    note: str | None
    ordered_sources: tuple[str, ...]
    tag_choices: tuple[TagChoice, ...] = ()
    # TASK-013 扩展（带默认值；旧调用保持纯文本行为）
    content_mode: str = "plain"  # plain / nga_ubb
    formatted_body: str | None = None


@dataclass(frozen=True)
class AdapterDiagnostic:
    """One machine-identifiable plan diagnostic."""

    severity: str  # blocker / warning
    code: str
    field: str
    message: str


def _diag(severity: str, code: str, field: str, message: str) -> AdapterDiagnostic:
    """Build one diagnostic value."""
    return AdapterDiagnostic(severity=severity, code=code, field=field, message=message)


@dataclass(frozen=True)
class UmaLibChangePlan:
    """A previewable, deterministic change plan; never executable SQL."""

    schema_version: str
    input: Mapping[str, object]
    ready: bool
    article: Mapping[str, object]
    tags: Mapping[str, object]
    diagnostics: Mapping[str, object]
    plan_sha256: str = ""

    def __post_init__(self) -> None:
        """Detach and deeply freeze all hash-covered structured fields."""
        object.__setattr__(self, "input", _deep_freeze(self.input))
        object.__setattr__(self, "article", _deep_freeze(self.article))
        object.__setattr__(self, "tags", _deep_freeze(self.tags))
        object.__setattr__(self, "diagnostics", _deep_freeze(self.diagnostics))

    def to_dict(self) -> dict[str, object]:
        """Return a fresh deep copy of the plan as a plain dict."""
        return {
            "schema_version": self.schema_version,
            "input": _deep_thaw(self.input),
            "ready": self.ready,
            "article": _deep_thaw(self.article),
            "tags": _deep_thaw(self.tags),
            "diagnostics": _deep_thaw(self.diagnostics),
            "plan_sha256": self.plan_sha256,
        }


def _render_formatted_body(
    choices: AdapterChoices,
    submission: dict[str, object],
    blockers: list[AdapterDiagnostic],
) -> str:
    """Render the nga_ubb formatted body with strict plain-text equivalence.

    formatted_body must parse cleanly, and its plain text must equal the
    approved body_text up to CRLF/CR->LF normalization; otherwise a blocker
    prevents any new text from being written.
    """
    from umalib_ingest.formatting.quill import render_quill_html
    from umalib_ingest.formatting.ubb import parse_basic_ubb

    formatted = choices.formatted_body
    if not isinstance(formatted, str) or formatted.strip() == "":
        blockers.append(_diag(
            "blocker", "formatted_body_missing", "formatted_body",
            "nga_ubb 模式必须提供 formatted_body",
        ))
        return ""
    parse = parse_basic_ubb(formatted)
    parse_blockers = [d for d in parse.diagnostics if d.severity == "blocker"]
    if parse_blockers:
        blockers.append(_diag(
            "blocker", "formatted_body_invalid", "formatted_body",
            f"格式正文无法解析: {', '.join(d.code for d in parse_blockers)}",
        ))
        return ""
    approved = submission["content"]["body_text"]
    approved_normalized = approved.replace("\r\n", "\n").replace("\r", "\n")
    if parse.plain_text != approved_normalized:
        blockers.append(_diag(
            "blocker", "formatted_body_mismatch", "formatted_body",
            "格式正文的纯文本与批准正文不一致，不能写入",
        ))
        return ""
    return render_quill_html(parse.document)


def render_plain_text_quill_html(body_text: str) -> str:
    """Render plain body text into minimal escaped Quill HTML.

    Normalizes CRLF/CR to LF, escapes &<> without interpreting any HTML,
    emits one <p> per non-empty logical line and <p><br></p> per empty line.
    Empty bodies and disallowed C0 controls raise AdapterContentError.
    """
    if not isinstance(body_text, str):
        raise AdapterContentError("正文必须是字符串")
    normalized = body_text.replace("\r\n", "\n").replace("\r", "\n")
    for char in normalized:
        if ord(char) < 32 and char not in ("\t", "\n"):
            raise AdapterContentError("正文包含不允许的控制字符")
    if normalized.strip() == "":
        raise AdapterContentError("正文为空")
    lines = normalized.split("\n")
    parts: list[str] = []
    for line in lines:
        if line == "":
            parts.append("<p><br></p>")
            continue
        escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        parts.append(f"<p>{escaped}</p>")
    return "".join(parts)


def build_umalib_change_plan(
    package: ApprovedSubmissionPackage,
    choices: AdapterChoices,
    target_catalog: TargetTagCatalog,
) -> UmaLibChangePlan:
    """Build a deterministic, previewable change plan without side effects.

    Human-choice gaps become blockers in diagnostics; only structurally wrong
    inputs raise AdapterError. Inputs are never modified.
    """
    blockers: list[AdapterDiagnostic] = []
    warnings: list[AdapterDiagnostic] = []

    submission = _submission_dict(package)
    inbox = _inbox_dict(package)
    _check_choices_shape(choices)

    package_sha256 = _check_package(package, submission, inbox, blockers)
    target_database_sha256, target_schema_fingerprint_sha256 = (
        _check_target_bindings(target_catalog, blockers)
    )
    catalog = _index_catalog(target_catalog, blockers)

    content = ""
    if choices.content_mode == "plain":
        try:
            content = render_plain_text_quill_html(submission["content"]["body_text"])
        except AdapterContentError as exc:
            blockers.append(_diag("blocker", "invalid_body_text", "content", str(exc)))
    elif choices.content_mode == "nga_ubb":
        content = _render_formatted_body(choices, submission, blockers)
    else:
        blockers.append(_diag(
            "blocker", "invalid_content_mode", "content_mode",
            f"content_mode 非法: {choices.content_mode!r}",
        ))

    upload_time = _check_upload_time(choices, blockers)
    note = _check_note(choices)
    source_text = _resolve_sources(submission, choices, blockers, warnings)
    existing_tags, create_tags = _resolve_tags(
        submission, choices, catalog, blockers, warnings
    )

    catalog_sha256 = _catalog_sha256(target_catalog)
    base = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "input": {
            "item_id": inbox["item_id"],
            "package_sha256": package_sha256,
            "target_database_sha256": target_database_sha256,
            "target_schema_fingerprint_sha256": target_schema_fingerprint_sha256,
            "target_tag_catalog_sha256": catalog_sha256,
        },
        "ready": not blockers,
        "article": {
            "name": submission["metadata"].get("title"),
            "note": note,
            "content": content,
            "author": submission["metadata"].get("author"),
            "translator": submission["metadata"].get("translator"),
            "upload_time": upload_time,
            "source": source_text,
        },
        "tags": {"existing": existing_tags, "create": create_tags},
        "diagnostics": _diagnostics_dict(blockers, warnings),
    }
    plan_sha256 = _sha256_hex(_stable_json(base))
    return UmaLibChangePlan(
        schema_version=PLAN_SCHEMA_VERSION,
        input=base["input"],
        ready=base["ready"],
        article=base["article"],
        tags=base["tags"],
        diagnostics=base["diagnostics"],
        plan_sha256=plan_sha256,
    )


def _submission_dict(package: ApprovedSubmissionPackage) -> dict[str, object]:
    """Return the package submission dict or raise on a wrong shape."""
    if not isinstance(package, ApprovedSubmissionPackage):
        raise AdapterError("package 必须是 ApprovedSubmissionPackage")
    submission = package.submission
    if not isinstance(submission, dict) or not isinstance(submission.get("metadata"), dict):
        raise AdapterError("package.submission 形状不正确")
    metadata = submission["metadata"]
    content = submission.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("body_text"), str):
        raise AdapterError("package.submission.content.body_text 形状不正确")
    for key in ("title", "author"):
        value = metadata.get(key)
        if not isinstance(value, str) or value.strip() == "":
            raise AdapterError(f"package.submission.metadata.{key} 形状不正确")
    for key in ("translator", "series"):
        value = metadata.get(key)
        if value is not None and not isinstance(value, str):
            raise AdapterError(f"package.submission.metadata.{key} 形状不正确")
    tags = metadata.get("tags")
    if not isinstance(tags, list) or any(
        not isinstance(value, str) or value.strip() == "" for value in tags
    ):
        raise AdapterError("package.submission.metadata.tags 形状不正确")
    sources = metadata.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(_SOURCE_KEYS):
        raise AdapterError("package.submission.metadata.sources 形状不正确")
    for key in _SOURCE_KEYS:
        values = sources[key]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or value == "" for value in values
        ):
            raise AdapterError(f"package.submission.metadata.sources.{key} 形状不正确")
    return submission


def _inbox_dict(package: ApprovedSubmissionPackage) -> dict[str, object]:
    """Return the package inbox dict or raise on a wrong shape."""
    inbox = package.inbox
    if (
        not isinstance(inbox, dict)
        or type(inbox.get("item_id")) is not int
        or inbox["item_id"] < 1
    ):
        raise AdapterError("package.inbox.item_id 形状不正确")
    return inbox


def _check_package(
    package: ApprovedSubmissionPackage,
    submission: dict[str, object],
    inbox: dict[str, object],
    blockers: list[AdapterDiagnostic],
) -> str:
    """Validate approved-package compatibility and return a contract-safe hash."""
    if package.schema_version != PLAN_SCHEMA_VERSION:
        blockers.append(
            _diag(
                "blocker",
                "approved_package_schema_mismatch",
                "package.schema_version",
                "批准包 Schema 版本不受支持",
            )
        )
    if submission.get("schema_version") != PLAN_SCHEMA_VERSION:
        blockers.append(
            _diag(
                "blocker",
                "submission_schema_mismatch",
                "submission.schema_version",
                "投稿 Schema 版本不受支持",
            )
        )
    if inbox.get("status") != "approved":
        blockers.append(
            _diag(
                "blocker",
                "approved_package_status_mismatch",
                "package.inbox.status",
                "批准包状态不是 approved",
            )
        )
    hash_is_valid = isinstance(package.package_sha256, str) and bool(
        _SHA256_RE.fullmatch(package.package_sha256)
    )
    if not hash_is_valid:
        blockers.append(
            _diag(
                "blocker",
                "approved_package_sha256_malformed",
                "package.package_sha256",
                "批准包 SHA-256 格式非法",
            )
        )
    try:
        expected = _sha256_hex(
            _stable_json(
                {
                    "schema_version": package.schema_version,
                    "inbox": package.inbox,
                    "submission": package.submission,
                }
            )
        )
    except (TypeError, ValueError) as exc:
        raise AdapterError("批准包包含不可序列化的数据") from exc
    if hash_is_valid and expected != package.package_sha256:
        blockers.append(
            _diag(
                "blocker",
                "approved_package_hash_mismatch",
                "package",
                "批准包哈希与内容不一致",
            )
        )
    return package.package_sha256 if hash_is_valid else _ZERO_SHA256


def _check_choices_shape(choices: AdapterChoices) -> None:
    """Reject programming-level choice container shape errors."""
    if not isinstance(choices, AdapterChoices):
        raise AdapterError("choices 必须是 AdapterChoices")
    if not isinstance(choices.ordered_sources, tuple):
        raise AdapterError("ordered_sources 必须是元组")
    if not isinstance(choices.tag_choices, tuple):
        raise AdapterError("tag_choices 必须是元组")


def _check_target_bindings(
    catalog: TargetTagCatalog, blockers: list[AdapterDiagnostic]
) -> tuple[str, str]:
    """Block on malformed target hashes or an unconfirmed schema fingerprint."""
    if not isinstance(catalog, TargetTagCatalog):
        raise AdapterError("target_catalog 必须是 TargetTagCatalog")
    database_sha256 = catalog.database_sha256
    if not isinstance(database_sha256, str) or not _SHA256_RE.fullmatch(database_sha256):
        blockers.append(
            _diag(
                "blocker",
                "target_database_sha256_malformed",
                "target_database_sha256",
                "目标数据库 SHA-256 格式非法",
            )
        )
        database_sha256 = _ZERO_SHA256
    schema_fingerprint_sha256 = catalog.schema_fingerprint_sha256
    if (
        not isinstance(schema_fingerprint_sha256, str)
        or not _SHA256_RE.fullmatch(schema_fingerprint_sha256)
    ):
        blockers.append(
            _diag(
                "blocker",
                "target_schema_fingerprint_malformed",
                "target_schema_fingerprint_sha256",
                "目标 Schema 指纹格式非法",
            )
        )
        schema_fingerprint_sha256 = _ZERO_SHA256
    elif schema_fingerprint_sha256 != CONFIRMED_SCHEMA_FINGERPRINT_SHA256:
        blockers.append(
            _diag(
                "blocker",
                "target_schema_fingerprint_mismatch",
                "target_schema_fingerprint_sha256",
                "目标 Schema 指纹与已确认契约不一致",
            )
        )
    return database_sha256, schema_fingerprint_sha256


def _index_catalog(
    catalog: TargetTagCatalog,
    blockers: list[AdapterDiagnostic],
) -> dict[str, object]:
    """Validate the catalog and return {by_id, by_name} index maps."""
    by_id: dict[int, TargetTag] = {}
    by_name: dict[str, list[TargetTag]] = {}
    if not isinstance(catalog.tags, tuple):
        raise AdapterError("catalog.tags 必须是元组")
    for tag in catalog.tags:
        if not isinstance(tag, TargetTag):
            raise AdapterError("catalog.tags 元素必须是 TargetTag")
        if (
            (type(tag.id) is not int and not isinstance(tag.id, bool))
            or not isinstance(tag.name, str)
            or (type(tag.type) is not int and not isinstance(tag.type, bool))
            or (
                tag.usage_count is not None
                and type(tag.usage_count) is not int
                and not isinstance(tag.usage_count, bool)
            )
        ):
            raise AdapterError("catalog.tags 字段类型不正确")
        invalid = (
            type(tag.id) is not int
            or tag.id < 1
            or tag.name.strip() == ""
            or type(tag.type) is not int
            or tag.type not in VALID_TAG_TYPES
            or (
                tag.usage_count is not None
                and (type(tag.usage_count) is not int or tag.usage_count < 0)
            )
        )
        if invalid:
            blockers.append(
                _diag(
                    "blocker",
                    "catalog_invalid_tag",
                    "target_tag_catalog",
                    f"目录含非法标签 (id={tag.id!r})",
                )
            )
            continue
        if tag.id in by_id:
            blockers.append(
                _diag(
                    "blocker",
                    "catalog_duplicate_id",
                    "target_tag_catalog",
                    f"目录存在重复标签 ID: {tag.id}",
                )
            )
            continue
        by_id[tag.id] = tag
        by_name.setdefault(tag.name, []).append(tag)
    for name in by_name:
        by_name[name].sort(key=lambda tag: tag.id)
    return {"by_id": by_id, "by_name": by_name}


def _catalog_sha256(catalog: TargetTagCatalog) -> str:
    """Hash the {id,name,type} tag array sorted by id (usage_count excluded)."""
    payload = [
        {"id": tag.id, "name": tag.name, "type": tag.type}
        for tag in sorted(catalog.tags, key=lambda tag: (tag.id, tag.name, tag.type))
    ]
    return _sha256_hex(_stable_json(payload))


def _check_upload_time(
    choices: AdapterChoices, blockers: list[AdapterDiagnostic]
) -> int:
    """Require an explicit non-negative integer upload time in seconds."""
    value = choices.upload_time_seconds
    if type(value) is not int:
        raise AdapterError("upload_time_seconds 必须是整数")
    if value < 0:
        blockers.append(
            _diag(
                "blocker",
                "invalid_upload_time",
                "upload_time",
                "upload_time 必须是非负整数秒",
            )
        )
        return 0
    return value


def _check_note(choices: AdapterChoices) -> str | None:
    """Return the human-provided note or None; wrong types raise."""
    if choices.note is not None and not isinstance(choices.note, str):
        raise AdapterError("note 必须是字符串或 None")
    return choices.note


def _resolve_sources(
    submission: dict[str, object],
    choices: AdapterChoices,
    blockers: list[AdapterDiagnostic],
    warnings: list[AdapterDiagnostic],
) -> str:
    """Flatten sources only from the human-ordered selection."""
    available: list[str] = []
    sources = submission["metadata"].get("sources")
    if isinstance(sources, dict):
        for key in _SOURCE_KEYS:
            values = sources.get(key)
            if not isinstance(values, list):
                raise AdapterError(f"submission.metadata.sources.{key} 形状不正确")
            for value in values:
                if isinstance(value, str) and value != "" and value not in available:
                    available.append(value)
    available_set = set(available)

    if available and not choices.ordered_sources:
        blockers.append(
            _diag(
                "blocker",
                "source_selection_required",
                "source",
                "批准包存在来源但未提供人工选择顺序",
            )
        )
        return ""

    selected: list[str] = []
    seen: set[str] = set()
    for index, token in enumerate(choices.ordered_sources):
        if not isinstance(token, str):
            blockers.append(
                _diag(
                    "blocker",
                    "source_invalid_token",
                    "source",
                    f"ordered_sources[{index}] 不是字符串",
                )
            )
            continue
        if any(char in _ASCII_WHITESPACE for char in token):
            blockers.append(
                _diag(
                    "blocker",
                    "source_invalid_token",
                    "source",
                    f"ordered_sources[{index}] 含 ASCII 空白",
                )
            )
            continue
        if token not in available_set:
            blockers.append(
                _diag(
                    "blocker",
                    "source_not_available",
                    "source",
                    f"ordered_sources[{index}] 不在批准包来源中",
                )
            )
            continue
        try:
            parsed = urlsplit(token)
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError:
            parsed = None
            hostname = None
        if (
            parsed is None
            or parsed.scheme.lower() not in ("http", "https")
            or hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            blockers.append(
                _diag(
                    "blocker",
                    "source_invalid_url",
                    "source",
                    f"ordered_sources[{index}] 不是有效的 HTTP(S) URL",
                )
            )
            continue
        if token in seen:
            warnings.append(
                _diag(
                    "warning",
                    "duplicate_source_selection",
                    "source",
                    "同一 URL 被重复选择，已去重并保留第一次位置",
                )
            )
            continue
        seen.add(token)
        selected.append(token)
    return " ".join(selected)


def _resolve_tags(
    submission: dict[str, object],
    choices: AdapterChoices,
    catalog: dict[str, object],
    blockers: list[AdapterDiagnostic],
    warnings: list[AdapterDiagnostic],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Resolve tag/series requests against choices and the catalog."""
    by_id: dict[int, TargetTag] = catalog["by_id"]
    by_name: dict[str, list[TargetTag]] = catalog["by_name"]

    requests: list[tuple[str, str]] = []
    tags_values = submission["metadata"].get("tags")
    if not isinstance(tags_values, list):
        raise AdapterError("submission.metadata.tags 形状不正确")
    for value in tags_values:
        if not isinstance(value, str):
            raise AdapterError("submission.metadata.tags 元素必须是字符串")
        if value != "":
            requests.append(("tag", value))
    series = submission["metadata"].get("series")
    if isinstance(series, str) and series.strip() != "":
        requests.append(("series", series))

    choices_map: dict[tuple[str, str], TagChoice] = {}
    for choice in choices.tag_choices:
        if not isinstance(choice, TagChoice):
            raise AdapterError("tag_choices 元素必须是 TagChoice")
        if choice.requested_kind not in ("tag", "series"):
            raise AdapterError("TagChoice.requested_kind 必须是 tag 或 series")
        if not isinstance(choice.requested_value, str):
            raise AdapterError("TagChoice.requested_value 必须是字符串")
        if choice.requested_value.strip() == "":
            blockers.append(
                _diag(
                    "blocker",
                    "choice_empty_value",
                    "tag_choice",
                    "choice 的请求值为空",
                )
            )
            continue
        key = (choice.requested_kind, choice.requested_value)
        if key in choices_map:
            blockers.append(
                _diag(
                    "blocker",
                    "duplicate_choice",
                    "tag_choice",
                    f"同一请求项存在多个 choice: {choice.requested_value}",
                )
            )
            continue
        choices_map[key] = choice

    request_set = set(requests)
    for key in sorted(choices_map.keys()):
        if key not in request_set:
            blockers.append(
                _diag(
                    "blocker",
                    "orphan_choice",
                    "tag_choice",
                    f"choice 指向批准包中不存在的请求项: {key[1]}",
                )
            )

    existing_planned: dict[int, dict[str, object]] = {}
    create_planned: dict[tuple[str, int], dict[str, object]] = {}
    for kind, value in requests:
        choice = choices_map.get((kind, value))
        matches = by_name.get(value, [])
        if choice is None:
            if len(matches) == 1:
                _plan_existing(existing_planned, matches[0], warnings)
            elif not matches:
                code = "tag_resolution_required" if kind == "tag" else "series_resolution_required"
                blockers.append(
                    _diag("blocker", code, value, f"未在目标目录中找到匹配，需人工选择: {value}")
                )
            else:
                code = "ambiguous_tag_name" if kind == "tag" else "ambiguous_series_name"
                candidates = ", ".join(
                    f"id={tag.id}/type={tag.type}/usage={tag.usage_count}"
                    for tag in matches
                )
                blockers.append(
                    _diag(
                        "blocker",
                        code,
                        value,
                        f"名称匹配多个候选，需人工指定 ID（候选: {candidates}）",
                    )
                )
            continue
        if (
            choice.existing_tag_id is not None
            and type(choice.existing_tag_id) is not int
        ):
            blockers.append(
                _diag(
                    "blocker",
                    "tag_choice_invalid_existing_id",
                    value,
                    "choice 指定的现有标签 ID 不是整数",
                )
            )
            continue
        if choice.new_tag_type is not None and type(choice.new_tag_type) is not int:
            blockers.append(
                _diag(
                    "blocker",
                    "invalid_new_tag_type",
                    value,
                    "新建标签类型不是整数",
                )
            )
            continue
        if choice.existing_tag_id is not None and choice.new_tag_type is not None:
            blockers.append(
                _diag(
                    "blocker",
                    "tag_choice_conflict",
                    value,
                    "同一 choice 不能同时指定现有 ID 和新建类型",
                )
            )
            continue
        if choice.existing_tag_id is not None:
            tag = by_id.get(choice.existing_tag_id)
            if tag is None:
                blockers.append(
                    _diag(
                        "blocker",
                        "tag_choice_invalid_existing_id",
                        value,
                        f"choice 指定的现有标签 ID 不存在: {choice.existing_tag_id}",
                    )
                )
                continue
            if tag.name != value:
                warnings.append(
                    _diag(
                        "warning",
                        "tag_alias_mapping",
                        value,
                        f"请求值映射到不同名称的现有标签 (id={tag.id}, name={tag.name})",
                    )
                )
            _plan_existing(existing_planned, tag, warnings)
            continue
        if choice.new_tag_type is not None:
            if choice.new_tag_type not in VALID_TAG_TYPES:
                blockers.append(
                    _diag(
                        "blocker",
                        "invalid_new_tag_type",
                        value,
                        f"新建标签类型非法: {choice.new_tag_type}",
                    )
                )
                continue
            if matches:
                blockers.append(
                    _diag(
                        "blocker",
                        "new_tag_name_exists",
                        value,
                        f"目标目录已存在同名标签，不能重复新建: {value}",
                    )
                )
                continue
            key = (value, choice.new_tag_type)
            if key in create_planned:
                warnings.append(
                    _diag(
                        "warning",
                        "duplicate_target_resolution",
                        value,
                        "多个请求归一为同一新建标签，已去重",
                    )
                )
                continue
            create_planned[key] = {"name": value, "type": choice.new_tag_type}
            continue
        blockers.append(
            _diag(
                "blocker",
                "tag_choice_incomplete",
                value,
                "choice 未指定现有 ID 或新建类型",
            )
        )

    existing_out = [dict(tag) for tag in existing_planned.values()]
    existing_out.sort(key=lambda tag: tag["id"])
    create_out = list(create_planned.values())
    create_out.sort(key=lambda tag: (tag["name"], tag["type"]))
    return existing_out, create_out


def _plan_existing(
    planned: dict[int, dict[str, object]],
    tag: TargetTag,
    warnings: list[AdapterDiagnostic],
) -> None:
    """Record an existing tag for reuse, deduped by id with a warning."""
    if tag.id in planned:
        warnings.append(
            _diag(
                "warning",
                "duplicate_target_resolution",
                tag.name,
                f"多个请求归一为同一现有标签 (id={tag.id})，已去重",
            )
        )
        return
    planned[tag.id] = {"id": tag.id, "name": tag.name, "type": tag.type}


def _diagnostics_dict(
    blockers: list[AdapterDiagnostic],
    warnings: list[AdapterDiagnostic],
) -> dict[str, object]:
    """Return diagnostics with stable ordering by (code, field)."""

    def sorted_list(items: list[AdapterDiagnostic]) -> list[dict[str, object]]:
        return [
            {"code": item.code, "field": item.field, "message": item.message}
            for item in sorted(items, key=lambda d: (d.code, d.field, d.message))
        ]

    return {"blockers": sorted_list(blockers), "warnings": sorted_list(warnings)}
