"""Watcher run result model (TASK-011)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ImportedItem:
    """One imported Inbox item."""

    item_id: int
    status: str


@dataclass(frozen=True)
class WatcherRunResult:
    """Stable scan/baseline outcome; never holds content/authors/pids lists."""

    mode: str  # "baseline" | "scan"
    tid: int
    previous_last_page: int | None
    current_last_page: int | None
    scanned_pages: tuple[int, ...] = ()
    new_floor_count: int = 0
    unchanged_floor_count: int = 0
    changed_floor_count: int = 0
    discussion_count: int = 0
    triggered_count: int = 0
    imported_items: tuple[ImportedItem, ...] = ()
    changed_after_import: tuple[int, ...] = ()
    state_before_sha256: str | None = None
    state_after_sha256: str | None = None
    dry_run: bool = False
    diagnostics: tuple[str, ...] = ()
