"""Candidate-database Writer errors (TASK-008)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from umalib_ingest.umalib_writer.preview import CandidateWritePreview


class UmaLibWriterError(Exception):
    """Base class for all candidate-database Writer errors."""


class WriterPathError(UmaLibWriterError):
    """Raised for invalid, missing or unsafe source/candidate paths."""


class WriterDatabaseError(UmaLibWriterError):
    """Raised for source database open, inspect or read failures."""


class WriterNotReadyError(UmaLibWriterError):
    """Raised when the preview has blockers or the plan is not ready to write."""

    def __init__(self, message: str, preview: CandidateWritePreview) -> None:
        super().__init__(message)
        self.preview = preview


class WriterApprovalError(UmaLibWriterError):
    """Raised when the human approval bindings do not match."""


class WriterExecutionError(UmaLibWriterError):
    """Raised for failures during copy, transaction, publish or verification."""
