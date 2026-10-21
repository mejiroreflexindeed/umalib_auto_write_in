"""Watcher scan orchestration: pages → diff → Inbox import → atomic state (TASK-011)."""

from __future__ import annotations

import datetime
import time
import warnings
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

from umalib_ingest.inbox import InboxItem, InboxRepository
from umalib_ingest.nga_access.errors import NgaAccessError
from umalib_ingest.nga_access.fetch import FetchedThreadPage, fetch_thread_page
from umalib_ingest.parser import parse_submission
from umalib_ingest.watcher import diff
from umalib_ingest.watcher.plan import build_page_plan
from umalib_ingest.watcher.result import ImportedItem, WatcherRunResult
from umalib_ingest.watcher.state import (
    KnownFloor,
    StateLock,
    WatcherConfig,
    WatcherState,
    atomic_write_json,
    load_state_snapshot,
    state_file_sha256,
)

_BLOCKER_DIAGNOSTICS = (
    "thread_forbidden",
    "thread_not_found",
    "captcha_or_challenge",
    "manual_login_required",
    "response_not_captured",
    "floor_structure_unrecognized",
    "unexpected_redirect",
    "encoding_uncertain",
)

DEFAULT_LOGIN_WAIT_TIMEOUT_SECONDS = 300.0


def _utc_clock() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _open_session(config: WatcherConfig):
    from umalib_ingest.nga_access.browser import launch_probe_session

    if config.profile_dir is None:
        raise NgaAccessError("login_required", "缺少 profile-dir，无法启动登录态会话")
    return launch_probe_session(config.profile_dir)


def _confirm_user_login(
    confirm_login: Callable[[], None] | None,
    login_wait_file: str | Path | None,
    timeout_seconds: float,
) -> None:
    """Wait for manual login confirmation on the official page; no credential I/O."""
    if confirm_login is not None:
        confirm_login()
        return
    if login_wait_file is not None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if Path(login_wait_file).exists():
                return
            time.sleep(2.0)
        raise NgaAccessError(
            "login_not_confirmed",
            "等待用户在官方 NGA 页面完成登录超时；请确认已登录后重试。",
        )
    input("请只在已打开的官方 NGA 页面完成手工登录；完成后按 Enter 继续…")


def _find_inbox_item_for_pid(repo: InboxRepository, pid: str) -> InboxItem | None:
    """Search Inbox public API for an item bound to this NGA post id."""
    for summary in repo.list_items():
        item = repo.get(summary.id)
        if item is None:
            continue
        origin = item.original_draft.get("origin") or {}
        if origin.get("post_id") == pid:
            return item
    return None


def _import_floor(
    repo: InboxRepository,
    tid: int,
    floor,
    diagnostics: list[str],
) -> tuple[int, str]:
    """Import one triggered floor via the existing Parser/Inbox public APIs."""
    existing = _find_inbox_item_for_pid(repo, floor.pid)
    if existing is not None:
        # Recovery compares the immutable original fragment, not the reviewed
        # body or a metadata-stripped parse. Titles/tags/sources can change too.
        original_raw = (existing.original_draft.get("content") or {}).get("raw_text")
        if isinstance(original_raw, str) and original_raw == floor.content_raw_fragment:
            return existing.id, existing.status
        raise NgaAccessError(
            "state_import_recovery_ambiguous",
            "该楼层在崩溃后内容又变化，无法安全绑定已有 Inbox 项，请人工处理",
        )
    origin = diff.build_floor_origin(tid, floor.pid, floor.floor_number)
    draft = parse_submission(floor.content_text, origin=origin)
    diff.adapt_draft_with_floor(draft, floor, tid)
    item = repo.import_draft(draft)
    return item.id, item.status


def _check_page_blockers(fetched: FetchedThreadPage) -> None:
    blockers = [d for d in fetched.diagnostics if d in _BLOCKER_DIAGNOSTICS]
    if not blockers:
        return
    if "manual_login_required" in blockers:
        raise NgaAccessError(
            "login_required", "登录确认后仍无法读取目标帖，请人工检查后重试"
        )
    raise NgaAccessError(
        "page_fetch_failed",
        f"页面读取失败: {', '.join(sorted(set(blockers)))}",
    )


def _diff_floors(
    state: WatcherState,
    pages: list[FetchedThreadPage],
    *,
    tid: int,
    dry_run: bool,
    diagnostics: list[str],
) -> tuple[dict[str, KnownFloor], list[tuple[str, int, Any, str]], WatcherRunResult]:
    """Pure diff: returns updated floors, pending imports in page/floor order, result."""
    pid_to_sha: dict[str, str] = {}
    for page in pages:
        for floor in page.floors:
            sha = diff.floor_content_sha256(floor)
            if floor.pid in pid_to_sha:
                if pid_to_sha[floor.pid] != sha:
                    raise NgaAccessError(
                        "duplicate_pid_across_pages",
                        f"PID {floor.pid} 在不同页内容哈希不一致，已阻断",
                    )
                diagnostics.append("duplicate_pid_across_pages")
            else:
                pid_to_sha[floor.pid] = sha

    ordered: list[tuple[str, int, Any, str]] = []
    seen: set[str] = set()
    for page in pages:
        for floor in page.floors:
            if floor.pid in seen:
                continue
            seen.add(floor.pid)
            ordered.append((floor.pid, page.page, floor, pid_to_sha[floor.pid]))

    updated: dict[str, KnownFloor] = dict(state.known_floors)
    pending: list[tuple[str, int, Any, str]] = []
    new_count = 0
    unchanged_count = 0
    changed_count = 0
    discussion_count = 0
    triggered_count = 0
    changed_after_import: list[int] = []

    for pid, page_number, floor, sha in ordered:
        trigger_now = diff.has_trigger(floor.content_text)
        previous = state.known_floors.get(pid)
        if previous is None:
            new_count += 1
            if trigger_now:
                triggered_count += 1
                pending.append((pid, page_number, floor, sha))
                if dry_run:
                    updated[pid] = KnownFloor(
                        pid=pid, content_sha256=sha, last_seen_page=page_number,
                        trigger_found=True, inbox_item_id=None,
                        imported_content_sha256=None, changed_after_import=False,
                    )
            else:
                discussion_count += 1
                updated[pid] = KnownFloor(
                    pid=pid, content_sha256=sha, last_seen_page=page_number,
                    trigger_found=False, inbox_item_id=None,
                    imported_content_sha256=None, changed_after_import=False,
                )
            continue

        if previous.content_sha256 == sha:
            unchanged_count += 1
            updated[pid] = KnownFloor(
                pid=pid, content_sha256=previous.content_sha256,
                last_seen_page=max(previous.last_seen_page, page_number),
                trigger_found=previous.trigger_found,
                inbox_item_id=previous.inbox_item_id,
                imported_content_sha256=previous.imported_content_sha256,
                changed_after_import=previous.changed_after_import,
            )
            continue

        changed_count += 1
        if previous.inbox_item_id is not None:
            changed_after_import.append(previous.inbox_item_id)
            diagnostics.append("floor_changed_after_import")
            updated[pid] = KnownFloor(
                pid=pid, content_sha256=sha, last_seen_page=page_number,
                trigger_found=previous.trigger_found,
                inbox_item_id=previous.inbox_item_id,
                imported_content_sha256=previous.imported_content_sha256,
                changed_after_import=True,
            )
            continue
        if trigger_now:
            triggered_count += 1
            pending.append((pid, page_number, floor, sha))
            if dry_run:
                updated[pid] = KnownFloor(
                    pid=pid, content_sha256=sha, last_seen_page=page_number,
                    trigger_found=True, inbox_item_id=None,
                    imported_content_sha256=None, changed_after_import=False,
                )
            continue
        discussion_count += 1
        updated[pid] = KnownFloor(
            pid=pid, content_sha256=sha, last_seen_page=page_number,
            trigger_found=False, inbox_item_id=None,
            imported_content_sha256=None, changed_after_import=False,
        )

    result = WatcherRunResult(
        mode="scan",
        tid=tid,
        previous_last_page=state.last_page,
        current_last_page=max(p.last_page or state.last_page for p in pages),
        scanned_pages=tuple(p.page for p in pages),
        new_floor_count=new_count,
        unchanged_floor_count=unchanged_count,
        changed_floor_count=changed_count,
        discussion_count=discussion_count,
        triggered_count=triggered_count,
        changed_after_import=tuple(changed_after_import),
        dry_run=dry_run,
        diagnostics=tuple(diagnostics),
    )
    return updated, pending, result


def run_scan(
    config: WatcherConfig,
    *,
    dry_run: bool = False,
    session_factory: Callable[[], Any] | None = None,
    fetch_fn: Callable[..., FetchedThreadPage] = fetch_thread_page,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], str] = _utc_clock,
    confirm_login: Callable[[], None] | None = None,
    login_wait_file: str | Path | None = None,
    login_wait_timeout_seconds: float = DEFAULT_LOGIN_WAIT_TIMEOUT_SECONDS,
    expected_state_sha256: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> WatcherRunResult:
    """Load state, fetch planned pages, diff, import, atomically advance.

    Fail-closed: a real scan without an Inbox raises before any session,
    network, file creation or state change. For real scans, the state is
    loaded and the expected SHA is bound inside the state lock, before any
    session/network/import side effect. Every top-level navigation after the
    first passes the injected sleep gate (>= delay). should_cancel is checked
    at each safe point. Cancellation before import leaves Inbox/state alone;
    after any import, report partial commit and leave state unadvanced.
    """
    if not dry_run and config.inbox_db is None:
        raise NgaAccessError(
            "watcher_inbox_required", "正式 scan 必须绑定 Inbox（--inbox-db）"
        )
    diagnostics: list[str] = []
    imported: list[ImportedItem] = []
    state_committed = False

    def check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise NgaAccessError("watcher_shutdown", "扫描已取消，state 未推进")

    check_cancel()
    lock_ctx = StateLock(config.state_file) if not dry_run else nullcontext()
    session = None
    with lock_ctx:
        state, state_before_sha = load_state_snapshot(config.state_file)
        if (
            not dry_run
            and expected_state_sha256 is not None
            and expected_state_sha256 != state_before_sha
        ):
            raise NgaAccessError(
                "watcher_state_stale",
                "state 已变化，请刷新状态后重新确认再扫描",
            )
        repo: InboxRepository | None = None
        check_cancel()
        if not dry_run:
            repo = InboxRepository(config.inbox_db)
            repo.initialize()
        try:
            check_cancel()
            session = session_factory() if session_factory else _open_session(config)
        except NgaAccessError:
            raise
        try:
            navigations = 0

            def navigate(fetch):
                nonlocal navigations
                check_cancel()
                if navigations > 0:
                    sleep(config.navigation_delay_seconds)
                    check_cancel()
                navigations += 1
                fetched = fetch()
                check_cancel()
                return fetched

            first = navigate(
                lambda: fetch_fn(session, config.tid, 1, config.snapshot_dir)
            )
            if "manual_login_required" in first.diagnostics:
                _confirm_user_login(
                    confirm_login, login_wait_file, login_wait_timeout_seconds
                )
                check_cancel()
                first = navigate(
                    lambda: fetch_fn(session, config.tid, 1, config.snapshot_dir)
                )
            _check_page_blockers(first)
            pages_plan, plan_diagnostics = build_page_plan(
                state, first.last_page, config
            )
            diagnostics.extend(plan_diagnostics)
            pages: list[FetchedThreadPage] = []
            for page_number in pages_plan:
                if page_number == first.page:
                    pages.append(first)
                    continue
                check_cancel()
                fetched = navigate(
                    lambda p=page_number: fetch_fn(
                        session, config.tid, p, config.snapshot_dir
                    )
                )
                _check_page_blockers(fetched)
                pages.append(fetched)
            pages.sort(key=lambda p: p.page)

            updated, pending, result = _diff_floors(
                state, pages, tid=config.tid, dry_run=dry_run, diagnostics=diagnostics
            )
            check_cancel()
            if dry_run:
                return WatcherRunResult(
                    mode="scan",
                    tid=result.tid,
                    previous_last_page=result.previous_last_page,
                    current_last_page=result.current_last_page,
                    scanned_pages=result.scanned_pages,
                    new_floor_count=result.new_floor_count,
                    unchanged_floor_count=result.unchanged_floor_count,
                    changed_floor_count=result.changed_floor_count,
                    discussion_count=result.discussion_count,
                    triggered_count=result.triggered_count,
                    imported_items=(),
                    changed_after_import=result.changed_after_import,
                    state_before_sha256=state_before_sha,
                    state_after_sha256=None,
                    dry_run=True,
                    diagnostics=result.diagnostics,
                )

            check_cancel()
            if state_file_sha256(config.state_file) != state_before_sha:
                raise NgaAccessError(
                    "watcher_state_changed",
                    "import 前 state 被外部修改，已中止且不推进",
                )
            for pid, page_number, floor, sha in pending:
                check_cancel()
                item_id, status = _import_floor(repo, config.tid, floor, diagnostics)
                imported.append(ImportedItem(item_id, status))
                updated[pid] = KnownFloor(
                    pid=pid,
                    content_sha256=sha,
                    last_seen_page=page_number,
                    trigger_found=True,
                    inbox_item_id=item_id,
                    imported_content_sha256=sha,
                    changed_after_import=False,
                )
            check_cancel()
            if state_file_sha256(config.state_file) != state_before_sha:
                raise NgaAccessError(
                    "watcher_state_changed",
                    "import 后 state 被外部修改，已中止且不推进",
                )
            final_result = WatcherRunResult(
                mode="scan",
                tid=result.tid,
                previous_last_page=result.previous_last_page,
                current_last_page=result.current_last_page,
                scanned_pages=result.scanned_pages,
                new_floor_count=result.new_floor_count,
                unchanged_floor_count=result.unchanged_floor_count,
                changed_floor_count=result.changed_floor_count,
                discussion_count=result.discussion_count,
                triggered_count=result.triggered_count,
                imported_items=tuple(imported),
                changed_after_import=result.changed_after_import,
                dry_run=False,
                diagnostics=result.diagnostics,
            )
            new_state = WatcherState(
                schema_version=state.schema_version,
                tid=state.tid,
                canonical_thread_url=state.canonical_thread_url,
                last_page=final_result.current_last_page or state.last_page,
                known_floors=updated,
                baseline_created_at=state.baseline_created_at,
                last_successful_scan_at=clock(),
                last_result_summary=diff.summary_payload(final_result),
            )
            check_cancel()
            state_after_sha = atomic_write_json(config.state_file, new_state.to_dict())
            state_committed = True
            return WatcherRunResult(
                mode="scan",
                tid=final_result.tid,
                previous_last_page=final_result.previous_last_page,
                current_last_page=final_result.current_last_page,
                scanned_pages=final_result.scanned_pages,
                new_floor_count=final_result.new_floor_count,
                unchanged_floor_count=final_result.unchanged_floor_count,
                changed_floor_count=final_result.changed_floor_count,
                discussion_count=final_result.discussion_count,
                triggered_count=final_result.triggered_count,
                imported_items=final_result.imported_items,
                changed_after_import=final_result.changed_after_import,
                state_before_sha256=state_before_sha,
                state_after_sha256=state_after_sha,
                dry_run=False,
                diagnostics=final_result.diagnostics,
            )
        except Exception as exc:
            if imported and not state_committed:
                cause = exc.code if isinstance(exc, NgaAccessError) else type(exc).__name__
                raise NgaAccessError(
                    "watcher_partial_import",
                    f"本次已入队或复用 {len(imported)} 条，state 未推进（{cause}）。"
                    "请重新扫描恢复并核对 Inbox；不要删除已有条目或当作完全未写入。",
                ) from exc
            raise
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    # Preserve the original error/committed result, but do not
                    # claim that resource cleanup succeeded.
                    warnings.warn("Watcher 会话关闭失败，请人工检查遗留窗口", RuntimeWarning)
                session = None
