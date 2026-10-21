"""Inbox domain models (TASK-002)."""

from __future__ import annotations

from dataclasses import dataclass

VALID_STATUSES: tuple[str, ...] = (
    "candidate",
    "incomplete",
    "suspected_duplicate",
    "approved",
    "exported",
    "ignored",
)


@dataclass
class InboxItem:
    """One inbox item with deserialized draft dictionaries."""

    id: int
    status: str
    original_draft: dict[str, object]
    working_draft: dict[str, object]
    snapshot_sha256: str
    content_sha256: str | None
    created_at: str
    updated_at: str


@dataclass
class InboxStatusHistory:
    """One status transition audit record."""

    id: int
    item_id: int
    from_status: str | None
    to_status: str
    reason: str
    changed_at: str
