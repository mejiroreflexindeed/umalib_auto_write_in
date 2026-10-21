"""Human approval object for candidate writing (TASK-008)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateWriteApproval:
    """Explicit human approval binding plan, preview and source evidence."""

    plan_sha256: str
    preview_sha256: str
    source_database_sha256: str
    app_closed_confirmed: bool
