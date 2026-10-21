"""Pure scan page planning (TASK-011)."""

from __future__ import annotations

from umalib_ingest.watcher.state import WatcherConfig, WatcherState
from umalib_ingest.nga_access.errors import NgaAccessError


def build_page_plan(
    state: WatcherState,
    current_last_page: int,
    config: WatcherConfig,
) -> tuple[list[int], tuple[str, ...]]:
    """Plan pages from state.last_page to the current last page, inclusive.

    Re-reads the previous last page so floors appended to it before pagination
    are captured. Returns (pages, diagnostics); diagnostics are empty on success.
    """
    diagnostics: list[str] = []
    if current_last_page is None or current_last_page <= 0:
        raise NgaAccessError("page_fetch_failed", "无法确定当前最后一页")
    if current_last_page < state.last_page:
        raise NgaAccessError(
            "thread_page_regressed",
            f"当前最后一页 {current_last_page} 小于状态记录的 {state.last_page}，不推进",
        )
    pages = list(range(state.last_page, current_last_page + 1))
    if len(pages) > config.hard_max_scan_pages:
        diagnostics.append("scan_range_too_large")
    if len(pages) > config.max_scan_pages:
        raise NgaAccessError(
            "scan_range_too_large",
            f"扫描页数 {len(pages)} 超过配置上限 {config.max_scan_pages}，不抓取不推进",
        )
    return pages, tuple(diagnostics)
