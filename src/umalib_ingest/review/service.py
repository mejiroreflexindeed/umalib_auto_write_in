"""Deterministic review editing core (TASK-003)."""

from __future__ import annotations

import copy

from umalib_ingest.inbox.errors import InboxItemNotFoundError
from umalib_ingest.inbox.models import InboxItem
from umalib_ingest.inbox.repository import InboxRepository
from umalib_ingest.review.errors import InvalidReviewFieldsError
from umalib_ingest.review.models import ReviewFields, ReviewSources, ReviewValidation

_REQUIRED_MISSING_ORDER = ("title", "author", "tags", "body")

_SOURCE_KEYS = (
    "nga",
    "original_series",
    "original",
    "translation_series",
    "translation",
)


class ReviewService:
    """Local review form backend over an InboxRepository."""

    def __init__(self, repository: InboxRepository) -> None:
        """Bind the service to an InboxRepository."""
        self._repository = repository

    def load(self, item_id: int) -> ReviewFields:
        """Extract the current editable fields from the item's working draft."""
        item = self._repository.get(item_id)
        if item is None:
            raise InboxItemNotFoundError(f"item {item_id} 不存在")
        return _fields_from_working_draft(item.working_draft)

    def validate(self, fields: ReviewFields) -> ReviewValidation:
        """Normalize and validate review fields without database side effects."""
        normalized = _normalize_fields(fields)
        missing = _missing_fields(normalized)
        return ReviewValidation(
            fields=normalized,
            missing_fields=missing,
            can_approve=not missing,
        )

    def save(self, item_id: int, fields: ReviewFields) -> InboxItem:
        """Validate, apply and atomically persist the review fields."""
        validation = self.validate(fields)
        item = self._repository.get(item_id)
        if item is None:
            raise InboxItemNotFoundError(f"item {item_id} 不存在")
        working = copy.deepcopy(item.working_draft)
        _working_structure(working)
        _apply_fields(working, validation.fields, validation.missing_fields)
        return self._repository.replace_working_draft(item_id, working)


def _working_structure(
    working: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    """Return (metadata, content, sources, diagnostics) or raise on malformed structure."""
    if not isinstance(working, dict):
        raise InvalidReviewFieldsError("working draft 必须是字典")
    metadata = working.get("metadata")
    content = working.get("content")
    diagnostics = working.get("diagnostics")
    if not isinstance(metadata, dict) or not isinstance(content, dict):
        raise InvalidReviewFieldsError("working draft 缺少 metadata 或 content")
    if not isinstance(diagnostics, dict):
        raise InvalidReviewFieldsError("working draft 缺少 diagnostics")
    sources = metadata.get("sources")
    if not isinstance(sources, dict):
        raise InvalidReviewFieldsError("working draft 缺少 metadata.sources")
    return metadata, content, sources, diagnostics


def _fields_from_working_draft(working: dict[str, object]) -> ReviewFields:
    """Build a ReviewFields copy from a working draft, validating its structure."""
    metadata, content, sources, _ = _working_structure(working)
    return ReviewFields(
        title=_require_optional_str(metadata.get("title"), "metadata.title"),
        author=_require_optional_str(metadata.get("author"), "metadata.author"),
        translator=_require_optional_str(metadata.get("translator"), "metadata.translator"),
        series=_require_optional_str(metadata.get("series"), "metadata.series"),
        tags=_require_str_list(metadata.get("tags"), "metadata.tags"),
        sources=ReviewSources(
            nga=_require_str_list(sources.get("nga"), "sources.nga"),
            original_series=_require_str_list(
                sources.get("original_series"), "sources.original_series"
            ),
            original=_require_str_list(sources.get("original"), "sources.original"),
            translation_series=_require_str_list(
                sources.get("translation_series"), "sources.translation_series"
            ),
            translation=_require_str_list(
                sources.get("translation"), "sources.translation"
            ),
        ),
        body_text=_require_str(content.get("body_text"), "content.body_text"),
    )


def _apply_fields(
    working: dict[str, object],
    fields: ReviewFields,
    missing_fields: list[str],
) -> None:
    """Replace the editable fields inside a working draft dict (structure checked)."""
    metadata, content, sources, diagnostics = _working_structure(working)
    metadata["title"] = fields.title
    metadata["author"] = fields.author
    metadata["translator"] = fields.translator
    metadata["series"] = fields.series
    metadata["tags"] = list(fields.tags)
    for key in _SOURCE_KEYS:
        sources[key] = list(getattr(fields.sources, key))
    content["body_text"] = fields.body_text
    diagnostics["missing_fields"] = list(missing_fields)


def _normalize_fields(fields: ReviewFields) -> ReviewFields:
    """Return a new normalized ReviewFields, rejecting wrong types."""
    if not isinstance(fields, ReviewFields):
        raise InvalidReviewFieldsError("fields 必须是 ReviewFields")
    if not isinstance(fields.sources, ReviewSources):
        raise InvalidReviewFieldsError("sources 必须是 ReviewSources")
    if not isinstance(fields.body_text, str):
        raise InvalidReviewFieldsError("body_text 必须是字符串")
    return ReviewFields(
        title=_normalize_optional(fields.title, "title"),
        author=_normalize_optional(fields.author, "author"),
        translator=_normalize_optional(fields.translator, "translator"),
        series=_normalize_optional(fields.series, "series"),
        tags=_normalize_string_list(fields.tags, "tags", dedup=True),
        sources=ReviewSources(
            nga=_normalize_string_list(fields.sources.nga, "sources.nga", dedup=False),
            original_series=_normalize_string_list(
                fields.sources.original_series, "sources.original_series", dedup=False
            ),
            original=_normalize_string_list(
                fields.sources.original, "sources.original", dedup=False
            ),
            translation_series=_normalize_string_list(
                fields.sources.translation_series,
                "sources.translation_series",
                dedup=False,
            ),
            translation=_normalize_string_list(
                fields.sources.translation, "sources.translation", dedup=False
            ),
        ),
        body_text=fields.body_text,
    )


def _normalize_optional(value: object, field_name: str) -> str | None:
    """Strip a single value; empty strings become None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidReviewFieldsError(f"{field_name} 必须是字符串或 None")
    stripped = value.strip()
    return stripped if stripped else None


def _normalize_string_list(value: object, field_name: str, *, dedup: bool) -> list[str]:
    """Strip list items, drop empties, optionally stable-dedup, keep order."""
    if not isinstance(value, list):
        raise InvalidReviewFieldsError(f"{field_name} 必须是字符串列表")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise InvalidReviewFieldsError(f"{field_name}[{index}] 必须是字符串")
        stripped = item.strip()
        if not stripped:
            continue
        if dedup and stripped in seen:
            continue
        if dedup:
            seen.add(stripped)
        result.append(stripped)
    return result


def _missing_fields(fields: ReviewFields) -> list[str]:
    """Return missing required fields in fixed order title, author, tags, body."""
    missing: list[str] = []
    if fields.title is None:
        missing.append("title")
    if fields.author is None:
        missing.append("author")
    if not fields.tags:
        missing.append("tags")
    if fields.body_text.strip() == "":
        missing.append("body")
    return [name for name in _REQUIRED_MISSING_ORDER if name in missing]


def _require_optional_str(value: object, field_name: str) -> str | None:
    """Require a str or None working-draft value without leaking type errors."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidReviewFieldsError(f"working draft 的 {field_name} 必须是字符串或 None")
    return value


def _require_str(value: object, field_name: str) -> str:
    """Require a str working-draft value without leaking type errors."""
    if not isinstance(value, str):
        raise InvalidReviewFieldsError(f"working draft 的 {field_name} 必须是字符串")
    return value


def _require_str_list(value: object, field_name: str) -> list[str]:
    """Require a list of strings, returning a fresh copy."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InvalidReviewFieldsError(f"working draft 的 {field_name} 必须是字符串列表")
    return list(value)
