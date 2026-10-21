"""TASK-014 W06/W07：初始化、分批扫描、日期过滤、补查（transport 注入）。"""

from __future__ import annotations

import datetime
import time
from pathlib import Path
from typing import Any, Callable

from umalib_ingest.nga_access.errors import NgaAccessError
from umalib_ingest.operator.models import ThreadConfig, utc_now
from umalib_ingest.operator.state_v2 import (
    ActiveRescan,
    DateExclusion,
    WatcherStateV2,
    parse_nga_posted_at,
)

NAV_MIN_INTERVAL = 3.0
DEFAULT_BATCH_PAGES = 10
HARD_NAV_LIMIT = 25


class Transport:
    """Injected browser/session transport; the worker owns one per run."""

    def __init__(self, session_factory: Callable, snapshot_dir: Path, tid: str,
                 delay: float = NAV_MIN_INTERVAL, clock=None, controller=None) -> None:
        self.session = session_factory()
        self.snapshot_dir = snapshot_dir
        self.tid = tid
        self.delay = delay
        self._clock = clock or (lambda: time.monotonic())
        self.navigations = 0
        self._last_nav = None
        self._controller = controller

    def fetch(self, page: int):
        from umalib_ingest.nga_access.fetch import fetch_thread_page

        if self._controller is not None:
            self._controller.check_cancel()
        if self.navigations >= HARD_NAV_LIMIT:
            raise NgaAccessError("navigation_budget_exceeded", "导航次数超出预算")
        if self.navigations > 0:
            now = self._clock()
            if self._last_nav is not None:
                elapsed = now - self._last_nav
                if elapsed < self.delay:
                    time.sleep(self.delay - elapsed)
        self.navigations += 1
        self._last_nav = self._clock()
        if self._controller is not None:
            self._controller.check_cancel()
        result = fetch_thread_page(self.session, int(self.tid), page, self.snapshot_dir)
        if self._controller is not None:
            self._controller.check_cancel()
        return result

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass


def initialize_preview(
    config: ThreadConfig,
    transport: Transport,
    *,
    start=None,
) -> dict[str, Any]:
    """Read page 1 + (for now/date) the tail page; build an initialization preview."""
    from umalib_ingest.watcher import diff

    effective_start = start or config.start

    first = transport.fetch(1)
    if first.last_page is None:
        raise NgaAccessError("page_read_failed", "无法确定当前总页数")
    if int(first.tid) != int(config.tid):
        raise NgaAccessError("tid_mismatch", "页面实际 tid 与请求不一致")
    last_page = first.last_page
    pages = [first]
    if effective_start.mode == "now" and last_page > 1:
        pages.append(transport.fetch(last_page))
    elif effective_start.mode == "page" and last_page < effective_start.page:
        raise NgaAccessError("start_page_beyond_last", f"起始页 {effective_start.page} 超过尾页 {last_page}")
    tail = pages[-1]
    tail_floor_hashes = [
        {"pid": floor.pid, "content_sha256": diff.floor_content_sha256(floor),
         "last_seen_page": tail.page,
         "has_trigger": bool(diff.has_trigger(floor.content_text))}
        for floor in tail.floors
    ]
    return {
        "tid": config.tid,
        "observed_last_page": last_page,
        "pages_read": [page.page for page in pages],
        "floor_pids": sorted(f.pid for page in pages for f in page.floors),
        "tail_floor_hashes": tail_floor_hashes,
        "floors_seen": sum(len(page.floors) for page in pages),
        "skip_note": ("跳过现有历史投稿，只以当前尾页楼层作为初始已见"
                      if effective_start.mode == "now" else
                      ("将从第 %d 页开始分批检查" % effective_start.page
                       if effective_start.mode == "page" else
                       "将从第 1 页开始分批读取并按发表时间筛选日期")),
    }


def _floor_hash_import_chain(
    repo, tid, floor, diagnostics, *, updated: bool = False,
    posted_at_unresolved: bool = False,
) -> tuple[int, str]:
    """复用现有 Parser/Inbox 导入并做崩溃恢复复用（镜像 scan._import_floor）。"""
    from umalib_ingest.domain import ParseWarning
    from umalib_ingest.parser import parse_submission
    from umalib_ingest.watcher import diff

    if not updated:
        existing = None
        for summary in repo.list_items():
            item = repo.get(summary.id)
            if item is None:
                continue
            origin = (item.original_draft.get("origin") or {})
            if (str(origin.get("thread_id")) == str(tid)
                    and origin.get("post_id") == floor.pid):
                existing = item
                break
        if existing is not None:
            content = (existing.original_draft.get("content") or {})
            body = content.get("raw_text") or content.get("body_text")
            parsed = parse_submission(floor.content_text).content.body_text
            if body == floor.content_raw_fragment or body == parsed:
                return existing.id, existing.status
            raise NgaAccessError("state_import_recovery_ambiguous",
                                 "该楼层在崩溃后内容又变化，请人工处理")
    origin = diff.build_floor_origin(int(tid), floor.pid, floor.floor_number)
    draft = parse_submission(floor.content_text, origin=origin)
    diff.adapt_draft_with_floor(draft, floor, int(tid))
    # A103：楼层原发表时间留痕（可追溯），不使用抓取/编辑/录入时间替代。
    if floor.posted_at_raw:
        recorded = draft.raw_fields.setdefault("楼层发表时间", [])
        if floor.posted_at_raw not in recorded:
            recorded.append(floor.posted_at_raw)
    if posted_at_unresolved:
        draft.diagnostics.warnings.append(ParseWarning(
            code="posted_at_unresolved",
            message="无法确定 NGA 楼层发表时间，请人工确认是否在扫描日期范围内",
            field=None,
            line=None,
        ))
    item = repo.import_draft(draft)
    return item.id, item.status


def run_batch_scan(
    state: WatcherStateV2,
    config: ThreadConfig,
    transport: Transport,
    inbox_db: Path,
    *,
    dry_run: bool,
    controller,
    batch_pages: int = DEFAULT_BATCH_PAGES,
    date_lower_bound: datetime.datetime | None = None,
    from_page: int | None = None,
    to_page: int | None = None,
    rescan: bool = False,
    rescan_id: str | None = None,
    rescan_kind: str | None = None,
    rescan_from_page: int | None = None,
    write_gate=None,
    persist_page=None,
    prefetched=None,
    ) -> tuple[WatcherStateV2, dict[str, Any]]:
    """Batch-scan pages, commit frontier per page, import triggered floors.

    date_lower_bound / from_page/to_page define a rescan scope; otherwise the
    normal frontier scan uses state.next_scan_page .. observed tail.
    rescan=True 时不推进实时 frontier/next_scan_page，补查进度写入 active_rescan。
    """
    from umalib_ingest.inbox import InboxRepository
    from umalib_ingest.watcher import diff

    repo = InboxRepository(inbox_db)
    repo.initialize()
    diagnostics: list[str] = []
    counters = {
        "pages_scanned": 0, "floors_seen": 0, "discussion_count": 0,
        "triggered_count": 0, "imported_count": 0, "incomplete_count": 0,
        "updated_count": 0, "date_excluded_count": 0, "date_unresolved_count": 0,
        "known_skip_count": 0, "known_baseline_trigger_count": 0,
        "known_discussion_skip_count": 0,
        "remaining_pages": 0,
    }

    if from_page is None:
        from_page = state.next_scan_page
    observed = state.observed_last_page
    if to_page is None:
        if observed is None:
            raise NgaAccessError("no_observed_last_page", "尚未初始化扫描进度")
        to_page = observed
    start = from_page
    frontier = state.frontier_page
    known = dict(state.known_floors)
    exclusions = dict(state.date_exclusions)
    if write_gate is None:
        import contextlib

        write_gate = contextlib.nullcontext

    page = start

    def snapshot(next_page: int) -> WatcherStateV2:
        """Capture a complete page boundary while the write gate is held."""
        remaining = max(0, to_page - next_page + 1)
        active_rescan = state.active_rescan
        if rescan:
            if remaining:
                same = active_rescan is not None and active_rescan.rescan_id == rescan_id
                active_rescan = ActiveRescan(
                    rescan_id=rescan_id or str(__import__("uuid").uuid4()),
                    kind=rescan_kind or ("date" if date_lower_bound is not None else "pages"),
                    from_page=rescan_from_page if rescan_from_page is not None else start,
                    to_page=to_page,
                    lower_bound_utc=date_lower_bound.isoformat() if date_lower_bound else None,
                    next_page=next_page,
                    created_at=active_rescan.created_at if same else utc_now(),
                )
            else:
                active_rescan = None
        return WatcherStateV2(
            schema_version="2", tid=state.tid,
            canonical_thread_url=config.canonical_url, frontier_page=frontier,
            observed_last_page=observed,
            next_scan_page=state.next_scan_page if rescan else (next_page if remaining else to_page),
            known_floors=dict(known), date_exclusions=dict(exclusions),
            baseline_created_at=state.baseline_created_at or utc_now(),
            last_successful_scan_at=state.last_successful_scan_at if (dry_run or rescan) else utc_now(),
            last_result_summary={"pages_scanned": counters["pages_scanned"]},
            active_rescan=active_rescan,
        )

    while page <= to_page and transport.navigations < HARD_NAV_LIMIT:
        controller.check_cancel()
        controller.update_progress(page=page, observed_last_page=observed,
                                   navigations=transport.navigations)
        if prefetched is not None and page == start and page == prefetched.page:
            fetched = prefetched
        else:
            fetched = transport.fetch(page)
        _check_blockers(fetched)
        counters["pages_scanned"] += 1
        # 写入阶段（楼层导入 + frontier 提交）服从共享维护门闩；
        # 网络等待（transport.fetch）不持有门闩。
        with write_gate():
            for floor in fetched.floors:
                controller.check_cancel()
                counters["floors_seen"] += 1
                content_sha = diff.floor_content_sha256(floor)
                previous = known.get(floor.pid)
                excluded = exclusions.get(floor.pid)
                if previous is not None and previous.get("content_sha256") == content_sha \
                        and excluded is None:
                    # 已见楼层按语义分开：此前导入过的投稿 / 起点基线内未导入的
                    # [收录] 楼层 / 仅看过的讨论层，避免把部分原因写成全批结论。
                    if previous.get("trigger_found"):
                        counters["known_skip_count"] += 1
                    elif previous.get("has_trigger"):
                        counters["known_baseline_trigger_count"] += 1
                    else:
                        counters["known_discussion_skip_count"] += 1
                    continue
                changed = previous is not None and previous.get("content_sha256") != content_sha
                changed_after_import = bool(
                    changed and previous.get("inbox_item_id") is not None)
                if not diff.has_trigger(floor.content_text):
                    counters["discussion_count"] += 1
                    if changed:
                        counters["updated_count"] += 1
                    known[floor.pid] = {
                        "pid": floor.pid, "content_sha256": content_sha,
                        "last_seen_page": page, "trigger_found": False,
                        "inbox_item_id": previous.get("inbox_item_id") if previous else None,
                        "imported_content_sha256": (
                            previous.get("imported_content_sha256") if previous else None),
                        "changed_after_import": changed_after_import,
                        "has_trigger": False,
                    }
                    if changed_after_import:
                        diagnostics.append("floor_changed_after_import")
                    continue
                # 有 [收录]：日期筛选
                posted = parse_nga_posted_at(floor.posted_at_raw)
                if date_lower_bound is not None and posted is not None and posted < date_lower_bound:
                    counters["date_excluded_count"] += 1
                    exclusions[floor.pid] = DateExclusion(
                        pid=floor.pid, content_sha256=content_sha,
                        posted_at_raw=floor.posted_at_raw,
                        posted_at_utc=posted.isoformat(), last_seen_page=page,
                        excluded_lower_bound_utc=date_lower_bound.isoformat(),
                    )
                    known[floor.pid] = {
                        "pid": floor.pid, "content_sha256": content_sha,
                        "last_seen_page": page, "trigger_found": False,
                        "inbox_item_id": None, "imported_content_sha256": None,
                        "changed_after_import": False,
                    }
                    continue
                counters["triggered_count"] += 1
                exclusions.pop(floor.pid, None)
                posted_unresolved = date_lower_bound is not None and posted is None
                if posted_unresolved:
                    counters["date_unresolved_count"] += 1
                    diagnostics.append("posted_at_unresolved")
                if dry_run:
                    continue
                try:
                    item_id, status = _floor_hash_import_chain(repo, int(config.tid),
                                                               floor, diagnostics,
                                                               updated=changed_after_import,
                                                               posted_at_unresolved=posted_unresolved)
                except NgaAccessError as exc:
                    if exc.code == "state_import_recovery_ambiguous":
                        raise
                    diagnostics.append(exc.code)
                    raise
                counters["imported_count"] += 1
                if changed:
                    counters["updated_count"] += 1
                if changed_after_import:
                    diagnostics.append("floor_changed_after_import")
                if status == "incomplete":
                    counters["incomplete_count"] += 1
                known[floor.pid] = {
                    "pid": floor.pid, "content_sha256": content_sha,
                    "last_seen_page": page, "trigger_found": True,
                    "inbox_item_id": item_id, "imported_content_sha256": content_sha,
                    "changed_after_import": False,
                    "has_trigger": True,
                }
            # 整页处理成功后提交 frontier（补查不推进实时 frontier）
            if not dry_run and not rescan:
                frontier = max(frontier, page)
            if not dry_run and persist_page is not None:
                persist_page(snapshot(page + 1))
        page += 1
        if counters["pages_scanned"] >= batch_pages and page <= to_page:
            break

    remaining = max(0, to_page - page + 1)
    counters["remaining_pages"] = remaining
    return snapshot(page), counters


def _check_blockers(fetched) -> None:
    blockers = [d for d in fetched.diagnostics if d in (
        "thread_forbidden", "thread_not_found", "captcha_or_challenge",
        "manual_login_required", "response_not_captured",
        "floor_structure_unrecognized", "unexpected_redirect", "encoding_uncertain",
    )]
    if blockers:
        raise NgaAccessError("page_fetch_failed", "页面读取失败: " + ", ".join(blockers))


def _updated_floor(previous: dict, new_sha: str, page: int) -> dict:
    return {
        "pid": previous["pid"], "content_sha256": new_sha,
        "last_seen_page": page, "trigger_found": previous.get("trigger_found", False),
        "inbox_item_id": previous.get("inbox_item_id"),
        "imported_content_sha256": previous.get("imported_content_sha256"),
        "changed_after_import": True,
    }
