"""Candidate-database Writer package (TASK-008)."""

from __future__ import annotations

from umalib_ingest.umalib_writer.approval import CandidateWriteApproval
from umalib_ingest.umalib_writer.catalog import load_target_tag_catalog
from umalib_ingest.umalib_writer.errors import (
    UmaLibWriterError,
    WriterApprovalError,
    WriterDatabaseError,
    WriterExecutionError,
    WriterNotReadyError,
    WriterPathError,
)
from umalib_ingest.umalib_writer.preview import (
    CandidateWritePreview,
    WriterIssue,
    preview_candidate_write,
)
from umalib_ingest.umalib_writer.result import CandidateWriteResult
from umalib_ingest.umalib_writer.writer import write_candidate_database

__all__ = [
    "CandidateWriteApproval",
    "CandidateWritePreview",
    "CandidateWriteResult",
    "UmaLibWriterError",
    "WriterApprovalError",
    "WriterDatabaseError",
    "WriterExecutionError",
    "WriterIssue",
    "WriterNotReadyError",
    "WriterPathError",
    "load_target_tag_catalog",
    "preview_candidate_write",
    "write_candidate_database",
]
