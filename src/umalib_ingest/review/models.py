"""Review editing domain models (TASK-003)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReviewSources:
    """Five ordered source string lists, mirroring metadata.sources."""

    nga: list[str] = field(default_factory=list)
    original_series: list[str] = field(default_factory=list)
    original: list[str] = field(default_factory=list)
    translation_series: list[str] = field(default_factory=list)
    translation: list[str] = field(default_factory=list)


@dataclass
class ReviewFields:
    """The human-editable review fields."""

    title: str | None = None
    author: str | None = None
    translator: str | None = None
    series: str | None = None
    tags: list[str] = field(default_factory=list)
    sources: ReviewSources = field(default_factory=ReviewSources)
    body_text: str = ""


@dataclass
class ReviewValidation:
    """Normalized review fields plus recalculated missing-field diagnostics."""

    fields: ReviewFields
    missing_fields: list[str]
    can_approve: bool
