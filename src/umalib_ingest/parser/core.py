"""Deterministic submission parsing (Parser 临时语法 v1)."""

from __future__ import annotations

import re

from umalib_ingest.domain.models import (
    ParseWarning,
    SubmissionContent,
    SubmissionDiagnostics,
    SubmissionDraft,
    SubmissionMetadata,
    SubmissionOrigin,
    SubmissionSources,
    SubmissionTrigger,
    UnknownField,
)
from umalib_ingest.formatting.models import is_format_tag

_TRIGGER_RE = re.compile(r"^\s*\[收录\]\s*$")
_FIELD_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(.*)$")
_LINE_SPLIT_RE = re.compile(r"\r\n|\n|\r")

_SINGLETON_FIELDS: dict[str, str] = {
    "标题": "title",
    "作者": "author",
    "译者": "translator",
    "系列": "series",
}

_SOURCE_FIELDS: dict[str, str] = {
    "原作系列": "original_series",
    "原作": "original",
    "译作系列": "translation_series",
    "译作": "translation",
}

_TAG_FIELD = "标签"
_TAG_SPLIT_RE = re.compile(r"[,，、]")

_REQUIRED_FIELDS = ("title", "author", "tags", "body")


def parse_submission(raw_text: str, origin: SubmissionOrigin | None = None) -> SubmissionDraft:
    """Parse a raw NGA floor text into a SubmissionDraft.

    No network or database access; never guesses missing information.
    """
    lines = _LINE_SPLIT_RE.split(raw_text)
    trigger_line_index = _find_trigger_line(lines)

    if trigger_line_index is None:
        return SubmissionDraft(
            parser_status="ignored",
            origin=_default_origin(origin),
            trigger=SubmissionTrigger(found=False, marker=None),
            metadata=SubmissionMetadata(),
            content=SubmissionContent(
                raw_text=raw_text,
                body_text="\n".join(_strip_trailing_empty(lines)),
            ),
            raw_fields={},
            diagnostics=SubmissionDiagnostics(),
        )

    metadata = SubmissionMetadata()
    raw_fields: dict[str, list[str]] = {}
    warnings: list[ParseWarning] = []
    unknown_fields: list[UnknownField] = []
    singleton_seen: dict[str, bool] = {}
    body_lines: list[str] = list(lines[:trigger_line_index])

    body_started = False
    for index in range(trigger_line_index + 1, len(lines)):
        line = lines[index]
        line_number = index + 1
        if not body_started:
            if line.strip() == "":
                continue
            # Optional standalone separator. It is deliberately not a field:
            # everything after it is body, including lines such as [术语]注释.
            if line.strip() == "[正文]":
                body_started = True
                continue
            match = _FIELD_RE.match(line)
            if match is not None:
                field_name = match.group(1).strip()
                # A known UBB opening tag starts the body instead of becoming an
                # unknown metadata field (R07). Unknown [字段]值 stays diagnosed.
                if is_format_tag(field_name) and not field_name.startswith("/"):
                    body_started = True
                    body_lines.append(line)
                    continue
                _handle_field(
                    name=match.group(1),
                    value=match.group(2).strip(),
                    line_number=line_number,
                    metadata=metadata,
                    raw_fields=raw_fields,
                    warnings=warnings,
                    unknown_fields=unknown_fields,
                    singleton_seen=singleton_seen,
                )
                continue
            body_started = True
        body_lines.append(line)

    body_text = "\n".join(_strip_trailing_empty(body_lines))
    missing_fields = _missing_fields(metadata, body_text)

    return SubmissionDraft(
        parser_status="accepted",
        origin=_default_origin(origin),
        trigger=SubmissionTrigger(found=True, marker="收录"),
        metadata=metadata,
        content=SubmissionContent(raw_text=raw_text, body_text=body_text),
        raw_fields=raw_fields,
        diagnostics=SubmissionDiagnostics(
            missing_fields=missing_fields,
            warnings=warnings,
            unknown_fields=unknown_fields,
        ),
    )


def _default_origin(origin: SubmissionOrigin | None) -> SubmissionOrigin:
    """Return the given origin, or the Schema-required default NGA origin."""
    return origin if origin is not None else SubmissionOrigin()


def _find_trigger_line(lines: list[str]) -> int | None:
    """Return the 1-based-independent index of the first standalone [收录] line, if any."""
    for index, line in enumerate(lines):
        if _TRIGGER_RE.match(line):
            return index
    return None


def _handle_field(
    *,
    name: str,
    value: str,
    line_number: int,
    metadata: SubmissionMetadata,
    raw_fields: dict[str, list[str]],
    warnings: list[ParseWarning],
    unknown_fields: list[UnknownField],
    singleton_seen: dict[str, bool],
) -> None:
    """Process one parsed field occurrence."""
    raw_fields.setdefault(name, []).append(value)

    if value == "":
        warnings.append(
            ParseWarning(
                code="empty_field",
                message=f"字段 {name} 的值为空",
                field=name,
                line=line_number,
            )
        )

    if name in _SINGLETON_FIELDS:
        is_first_occurrence = name not in singleton_seen
        singleton_seen[name] = True
        if not is_first_occurrence:
            warnings.append(
                ParseWarning(
                    code="duplicate_field",
                    message=f"单值字段 {name} 重复，保留第一个非空值",
                    field=name,
                    line=line_number,
                )
            )
        if value != "" and (
            is_first_occurrence or getattr(metadata, _SINGLETON_FIELDS[name]) is None
        ):
            setattr(metadata, _SINGLETON_FIELDS[name], value)
        return

    if name == _TAG_FIELD:
        if value:
            for part in _TAG_SPLIT_RE.split(value):
                part = part.strip()
                if part and part not in metadata.tags:
                    metadata.tags.append(part)
        return

    if name in _SOURCE_FIELDS:
        if value:
            getattr(metadata.sources, _SOURCE_FIELDS[name]).append(value)
        return

    unknown_fields.append(UnknownField(name=name, value=value, line=line_number))


def _missing_fields(metadata: SubmissionMetadata, body_text: str) -> list[str]:
    """Return the missing required field names in schema enum order."""
    missing: list[str] = []
    if metadata.title is None:
        missing.append("title")
    if metadata.author is None:
        missing.append("author")
    if not metadata.tags:
        missing.append("tags")
    if body_text.strip() == "":
        missing.append("body")
    return [name for name in _REQUIRED_FIELDS if name in missing]


def _strip_trailing_empty(lines: list[str]) -> list[str]:
    """Return lines with trailing empty lines removed."""
    end = len(lines)
    while end > 0 and lines[end - 1] == "":
        end -= 1
    return lines[:end]
