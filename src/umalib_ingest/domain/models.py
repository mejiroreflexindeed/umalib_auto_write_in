"""Domain models for submission drafts, conforming to contracts/submission.schema.json."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SubmissionOrigin:
    """Origin context of the raw floor text; defaults to an empty NGA origin."""

    platform: str = "nga"
    thread_id: str | None = None
    post_id: str | None = None
    floor: int | None = None
    canonical_url: str | None = None


@dataclass
class ParseWarning:
    """A non-fatal issue found while parsing a field."""

    code: str
    message: str
    field: str | None
    line: int | None


@dataclass
class UnknownField:
    """A field name that the parser does not recognize."""

    name: str
    value: str
    line: int


@dataclass
class SubmissionTrigger:
    """Whether the [收录] trigger marker was found and its marker text."""

    found: bool = False
    marker: str | None = None


@dataclass
class SubmissionSources:
    """Structured sources, one ordered list per source category."""

    nga: list[str] = field(default_factory=list)
    original_series: list[str] = field(default_factory=list)
    original: list[str] = field(default_factory=list)
    translation_series: list[str] = field(default_factory=list)
    translation: list[str] = field(default_factory=list)


@dataclass
class SubmissionMetadata:
    """Extracted metadata fields; missing fields stay None."""

    title: str | None = None
    author: str | None = None
    translator: str | None = None
    series: str | None = None
    tags: list[str] = field(default_factory=list)
    sources: SubmissionSources = field(default_factory=SubmissionSources)


@dataclass
class SubmissionContent:
    """Raw input text and the extracted body text."""

    raw_text: str
    body_text: str


@dataclass
class SubmissionDiagnostics:
    """Diagnostics collected during parsing."""

    missing_fields: list[str] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)
    unknown_fields: list[UnknownField] = field(default_factory=list)


@dataclass
class SubmissionDraft:
    """A parsed submission draft; to_dict() matches contracts/submission.schema.json."""

    schema_version: str = "1.0"
    parser_status: str = "ignored"
    origin: SubmissionOrigin = field(default_factory=SubmissionOrigin)
    trigger: SubmissionTrigger = field(default_factory=SubmissionTrigger)
    metadata: SubmissionMetadata = field(default_factory=SubmissionMetadata)
    content: SubmissionContent | None = None
    raw_fields: dict[str, list[str]] = field(default_factory=dict)
    diagnostics: SubmissionDiagnostics = field(default_factory=SubmissionDiagnostics)

    def to_dict(self) -> dict[str, object]:
        """Return the draft as a plain dict matching the JSON Schema."""
        content = self.content
        if content is None:
            content = SubmissionContent(raw_text="", body_text="")
        return {
            "schema_version": self.schema_version,
            "parser_status": self.parser_status,
            "origin": {
                "platform": self.origin.platform,
                "thread_id": self.origin.thread_id,
                "post_id": self.origin.post_id,
                "floor": self.origin.floor,
                "canonical_url": self.origin.canonical_url,
            },
            "trigger": {
                "found": self.trigger.found,
                "marker": self.trigger.marker,
            },
            "metadata": {
                "title": self.metadata.title,
                "author": self.metadata.author,
                "translator": self.metadata.translator,
                "series": self.metadata.series,
                "tags": list(self.metadata.tags),
                "sources": {
                    "nga": list(self.metadata.sources.nga),
                    "original_series": list(self.metadata.sources.original_series),
                    "original": list(self.metadata.sources.original),
                    "translation_series": list(self.metadata.sources.translation_series),
                    "translation": list(self.metadata.sources.translation),
                },
            },
            "content": {
                "raw_text": content.raw_text,
                "body_text": content.body_text,
            },
            "raw_fields": {name: list(values) for name, values in self.raw_fields.items()},
            "diagnostics": {
                "missing_fields": list(self.diagnostics.missing_fields),
                "warnings": [
                    {
                        "code": warning.code,
                        "message": warning.message,
                        "field": warning.field,
                        "line": warning.line,
                    }
                    for warning in self.diagnostics.warnings
                ],
                "unknown_fields": [
                    {
                        "name": unknown.name,
                        "value": unknown.value,
                        "line": unknown.line,
                    }
                    for unknown in self.diagnostics.unknown_fields
                ],
            },
        }
