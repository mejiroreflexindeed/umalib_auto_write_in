"""Shared domain models. Implementations are introduced by assigned tasks."""

from __future__ import annotations

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

__all__ = [
    "ParseWarning",
    "SubmissionContent",
    "SubmissionDiagnostics",
    "SubmissionDraft",
    "SubmissionMetadata",
    "SubmissionOrigin",
    "SubmissionSources",
    "SubmissionTrigger",
    "UnknownField",
]
