"""NGA Watcher core: state, baseline, page plan, diff, scan, CLI (TASK-011)."""

from __future__ import annotations

from umalib_ingest.watcher.baseline import baseline_from_snapshot
from umalib_ingest.watcher.diff import (
    adapt_draft_with_floor,
    build_floor_origin,
    canonical_floor_url,
    floor_content_sha256,
    has_trigger,
)
from umalib_ingest.watcher.plan import build_page_plan
from umalib_ingest.watcher.result import ImportedItem, WatcherRunResult
from umalib_ingest.watcher.scan import run_scan
from umalib_ingest.watcher.state import (
    KnownFloor,
    StateLock,
    WatcherConfig,
    WatcherState,
    load_state,
)

__all__ = [
    "ImportedItem",
    "KnownFloor",
    "StateLock",
    "WatcherConfig",
    "WatcherRunResult",
    "WatcherState",
    "adapt_draft_with_floor",
    "baseline_from_snapshot",
    "build_floor_origin",
    "build_page_plan",
    "canonical_floor_url",
    "floor_content_sha256",
    "has_trigger",
    "load_state",
    "run_scan",
]
