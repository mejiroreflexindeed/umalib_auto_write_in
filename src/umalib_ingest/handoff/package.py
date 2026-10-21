"""Approved submission package core (TASK-005)."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field

from umalib_ingest.inbox.errors import InboxItemNotFoundError
from umalib_ingest.inbox.models import InboxItem, InboxStatusHistory
from umalib_ingest.inbox.repository import InboxRepository
from umalib_ingest.handoff.errors import HandoffNotReadyError
from umalib_ingest.review import ReviewService

SCHEMA_VERSION = "1.0"

_SUBMISSION_KEYS = {
    "schema_version",
    "parser_status",
    "origin",
    "trigger",
    "metadata",
    "content",
    "raw_fields",
    "diagnostics",
}
_SOURCE_KEYS = {
    "nga",
    "original_series",
    "original",
    "translation_series",
    "translation",
}
_MISSING_FIELDS = {"title", "author", "tags", "body"}


def _stable_json(obj: dict[str, object]) -> str:
    """Serialize with stable key order for deterministic hashing."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(text: str) -> str:
    """Return the lowercase hex SHA-256 of a UTF-8 encoded string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _contract_error(path: str) -> HandoffNotReadyError:
    return HandoffNotReadyError(f"working_draft 不符合 submission 契约: {path}")


def _object(value: object, path: str, keys: set[str] | None = None) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _contract_error(path)
    if keys is not None and set(value) != keys:
        raise _contract_error(path)
    return value


def _optional_string(value: object, path: str) -> None:
    if value is not None and not isinstance(value, str):
        raise _contract_error(path)


def _string_list(value: object, path: str, *, nonempty: bool) -> None:
    if not isinstance(value, list):
        raise _contract_error(path)
    for entry in value:
        if not isinstance(entry, str) or (nonempty and entry == ""):
            raise _contract_error(path)


def _validate_submission_contract(submission: dict[str, object]) -> None:
    """Validate the complete protected and editable SubmissionDraft shape."""
    root = _object(submission, "root", _SUBMISSION_KEYS)
    if root["schema_version"] != SCHEMA_VERSION:
        raise _contract_error("schema_version")
    if root["parser_status"] not in ("accepted", "ignored"):
        raise _contract_error("parser_status")

    origin = _object(
        root["origin"],
        "origin",
        {"platform", "thread_id", "post_id", "floor", "canonical_url"},
    )
    if origin["platform"] != "nga":
        raise _contract_error("origin.platform")
    for key in ("thread_id", "post_id", "canonical_url"):
        _optional_string(origin[key], f"origin.{key}")
    floor = origin["floor"]
    if floor is not None and (
        not isinstance(floor, int) or isinstance(floor, bool) or floor < 0
    ):
        raise _contract_error("origin.floor")

    trigger = _object(root["trigger"], "trigger", {"found", "marker"})
    if not isinstance(trigger["found"], bool):
        raise _contract_error("trigger.found")
    _optional_string(trigger["marker"], "trigger.marker")

    metadata = _object(
        root["metadata"],
        "metadata",
        {"title", "author", "translator", "series", "tags", "sources"},
    )
    for key in ("title", "author", "translator", "series"):
        _optional_string(metadata[key], f"metadata.{key}")
    _string_list(metadata["tags"], "metadata.tags", nonempty=True)
    sources = _object(metadata["sources"], "metadata.sources", _SOURCE_KEYS)
    for key in _SOURCE_KEYS:
        _string_list(sources[key], f"metadata.sources.{key}", nonempty=True)

    content = _object(root["content"], "content", {"raw_text", "body_text"})
    for key in ("raw_text", "body_text"):
        if not isinstance(content[key], str):
            raise _contract_error(f"content.{key}")

    raw_fields = _object(root["raw_fields"], "raw_fields")
    for key, values in raw_fields.items():
        if not isinstance(key, str):
            raise _contract_error("raw_fields")
        _string_list(values, f"raw_fields.{key}", nonempty=False)

    diagnostics = _object(
        root["diagnostics"],
        "diagnostics",
        {"missing_fields", "warnings", "unknown_fields"},
    )
    missing = diagnostics["missing_fields"]
    if (
        not isinstance(missing, list)
        or not all(
            isinstance(value, str) and value in _MISSING_FIELDS for value in missing
        )
        or len(set(missing)) != len(missing)
    ):
        raise _contract_error("diagnostics.missing_fields")

    warnings = diagnostics["warnings"]
    if not isinstance(warnings, list):
        raise _contract_error("diagnostics.warnings")
    for index, value in enumerate(warnings):
        warning = _object(
            value,
            f"diagnostics.warnings[{index}]",
            {"code", "message", "field", "line"},
        )
        if not isinstance(warning["code"], str) or warning["code"] == "":
            raise _contract_error(f"diagnostics.warnings[{index}].code")
        if not isinstance(warning["message"], str) or warning["message"] == "":
            raise _contract_error(f"diagnostics.warnings[{index}].message")
        _optional_string(warning["field"], f"diagnostics.warnings[{index}].field")
        line = warning["line"]
        if line is not None and (
            not isinstance(line, int) or isinstance(line, bool) or line < 1
        ):
            raise _contract_error(f"diagnostics.warnings[{index}].line")

    unknown_fields = diagnostics["unknown_fields"]
    if not isinstance(unknown_fields, list):
        raise _contract_error("diagnostics.unknown_fields")
    for index, value in enumerate(unknown_fields):
        unknown = _object(
            value,
            f"diagnostics.unknown_fields[{index}]",
            {"name", "value", "line"},
        )
        if not isinstance(unknown["name"], str) or unknown["name"] == "":
            raise _contract_error(f"diagnostics.unknown_fields[{index}].name")
        if not isinstance(unknown["value"], str):
            raise _contract_error(f"diagnostics.unknown_fields[{index}].value")
        line = unknown["line"]
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise _contract_error(f"diagnostics.unknown_fields[{index}].line")


@dataclass
class ApprovedSubmissionPackage:
    """A deterministic handoff package for one approved inbox item."""

    schema_version: str = SCHEMA_VERSION
    inbox: dict[str, object] = field(default_factory=dict)
    submission: dict[str, object] = field(default_factory=dict)
    package_sha256: str = ""

    def to_dict(self) -> dict[str, object]:
        """Return a fresh deep copy of the package as a plain dict."""
        return {
            "schema_version": self.schema_version,
            "inbox": copy.deepcopy(self.inbox),
            "submission": copy.deepcopy(self.submission),
            "package_sha256": self.package_sha256,
        }

    def to_json(self) -> str:
        """Return the stable JSON serialization of the full package."""
        return _stable_json(self.to_dict())


class ApprovedPackageService:
    """Builds deterministic approved submission packages from the Inbox."""

    def __init__(
        self,
        repository: InboxRepository,
        review_service: ReviewService | None = None,
    ) -> None:
        """Bind the service to an InboxRepository and optional ReviewService."""
        self._repository = repository
        self._review_service = review_service or ReviewService(repository)

    def build(self, item_id: int) -> ApprovedSubmissionPackage:
        """Build the approved package for an item, or raise a clear error.

        Read-only: never writes the Inbox, changes status or history, or calls
        the clock. Every call returns a fresh deep copy.
        """
        item = self._repository.get(item_id)
        if item is None:
            raise InboxItemNotFoundError(f"item {item_id} 不存在")
        if item.status != "approved":
            raise HandoffNotReadyError(
                f"当前状态 {item.status} 不是 approved，不能生成交接包"
            )
        _validate_submission_contract(item.working_draft)
        fields = self._review_service.load(item_id)
        validation = self._review_service.validate(fields)
        if validation.missing_fields:
            raise HandoffNotReadyError(
                f"必要字段缺失: {', '.join(validation.missing_fields)}"
            )
        history = self._repository.list_history(item_id)
        approved_at = self._latest_approved_at(history)
        if approved_at is None:
            raise HandoffNotReadyError("缺少进入 approved 的状态历史")
        final_item = self._repository.get(item_id)
        final_history = self._repository.list_history(item_id)
        if final_item != item or final_history != history:
            raise HandoffNotReadyError("构建期间 Inbox 记录发生变化，请重试")
        expected_snapshot_sha256 = _sha256_hex(_stable_json(item.original_draft))
        if item.snapshot_sha256 != expected_snapshot_sha256:
            raise HandoffNotReadyError("original_draft 与 snapshot_sha256 不一致")
        body_text = item.working_draft["content"]["body_text"]
        expected_content_sha256 = _sha256_hex(body_text) if body_text.strip() else None
        if item.content_sha256 != expected_content_sha256:
            raise HandoffNotReadyError("正文与 content_sha256 不一致")
        inbox = {
            "item_id": item.id,
            "status": "approved",
            "snapshot_sha256": item.snapshot_sha256,
            "content_sha256": item.content_sha256,
            "created_at": item.created_at,
            "approved_at": approved_at,
        }
        submission = copy.deepcopy(item.working_draft)
        package_sha256 = _sha256_hex(
            _stable_json(
                {
                    "schema_version": SCHEMA_VERSION,
                    "inbox": inbox,
                    "submission": submission,
                }
            )
        )
        return ApprovedSubmissionPackage(
            schema_version=SCHEMA_VERSION,
            inbox=inbox,
            submission=submission,
            package_sha256=package_sha256,
        )

    @staticmethod
    def _latest_approved_at(history: list[InboxStatusHistory]) -> str | None:
        """Return the changed_at of the latest history entry entering approved."""
        for entry in reversed(history):
            if entry.to_status == "approved":
                return entry.changed_at
        return None
