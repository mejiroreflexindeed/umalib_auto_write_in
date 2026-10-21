"""TASK-012B W5：真实浏览器行为验收（本地假数据 + 临时 Edge profile）。

- 只用本地假数据和本任务新建的临时 profile，拦截/拒绝非 loopback 请求；
- 15+ 条真实 DOM 交互旅程（不是字符串搜索）；
- 3 分辨率 + 125% 等效缩放布局检查；
- 保留 6~10 张精选假数据截图到证据根，临时 profile/DB 清理。
- 缺少 msedge/playwright 时记录 UNAVAILABLE，不当 PASS。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TID = 30099686
T0 = "2026-09-02T00:00:00.000000+00:00"

VIEWPORTS = ((1024, 768), (1280, 720), (1920, 1080))
ZOOM_VIEWPORTS = ((1280, 720, 1.25),)


def _ensure_src():
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _watcher_state_dict() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "tid": TID,
        "canonical_thread_url": f"https://bbs.nga.cn/read.php?tid={TID}",
        "last_page": 173,
        "known_floors": {},
        "baseline_created_at": T0,
        "last_successful_scan_at": None,
        "last_result_summary": None,
    }


def _ok_result(dry_run: bool) -> Any:
    from umalib_ingest.watcher.result import WatcherRunResult

    return WatcherRunResult(
        mode="scan", tid=TID, previous_last_page=173, current_last_page=173,
        scanned_pages=(173,), new_floor_count=0, unchanged_floor_count=0,
        changed_floor_count=0, discussion_count=0, triggered_count=0,
        imported_items=(), changed_after_import=(),
        state_before_sha256="a" * 64, state_after_sha256="b" * 64,
        dry_run=dry_run, diagnostics=(),
    )


class UiFakeRunner:
    """可控 fake runner：只替代真实 run_scan，页面其余交互全部真实.

    behavior: success | login_wait | error | block
    seed_on_success: 成功时向真实 Inbox 导入一个假 candidate（真实公共 API）。
    """

    def __init__(self, behavior: str, seed_on_success: bool = False,
                 gate: threading.Event | None = None):
        self.behavior = behavior
        self.seed_on_success = seed_on_success
        self.gate = gate
        self.calls: list[dict[str, Any]] = []
        self.confirm_called = 0

    def __call__(self, config, *, dry_run=False, confirm_login=None,
                 login_wait_timeout_seconds=300.0, **kwargs):
        self.calls.append({"dry_run": dry_run})
        if self.behavior == "block":
            if self.gate is not None:
                self.gate.wait(timeout=60)
        elif self.behavior in ("login_wait", "login_failure"):
            self.confirm_called += 1
            if confirm_login is not None:
                confirm_login()
            if self.behavior == "login_failure":
                from umalib_ingest.nga_access.errors import NgaAccessError
                raise NgaAccessError("login_required", "登录确认后仍无法读取目标帖")
            return _ok_result(dry_run)
        elif self.behavior == "error":
            from umalib_ingest.nga_access.errors import NgaAccessError

            raise NgaAccessError("page_fetch_failed", "simulated page failure")
        if self.seed_on_success and not dry_run:
            from umalib_ingest.inbox import InboxRepository
            from umalib_ingest.parser import parse_submission
            from umalib_ingest.watcher.diff import build_floor_origin

            repo = InboxRepository(config.inbox_db)
            repo.initialize()
            text = "[收录]\n[标题] 界面测试稿\n[作者] 假作者\n[标签] 假标签\n\n假正文。"
            draft = parse_submission(text, origin=build_floor_origin(TID, "910009099", None))
            repo.import_draft(draft)
        return _ok_result(dry_run)


def _record(check: str, ok: bool, detail: Any) -> dict[str, Any]:
    return {"check": check, "ok": bool(ok), "detail": detail}


def _journey(journey_id: str, purpose: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "journey_id": journey_id,
        "purpose": purpose,
        "checks": checks,
        "verdict": "PASS" if all(c["ok"] for c in checks) else "FAIL",
    }


def _text(page, selector: str) -> str:
    node = page.locator(selector).first
    return node.text_content() or ""


def _wait_text(page, selector: str, needle: str, timeout: int = 8000) -> bool:
    try:
        page.wait_for_function(
            f"""() => {{
              const n = document.querySelector({json.dumps(selector)});
              return n && n.textContent && n.textContent.includes({json.dumps(needle)});
            }}""",
            timeout=timeout,
        )
        return True
    except Exception:
        return False


def _wait_visible_text(page, selector: str, needle: str, timeout: int = 8000) -> bool:
    try:
        page.wait_for_function(
            f"""() => {{
              const n = document.querySelector({json.dumps(selector)});
              return n && !n.hidden && n.textContent && n.textContent.includes({json.dumps(needle)});
            }}""",
            timeout=timeout,
        )
        return True
    except Exception:
        return False


def run_ui(output: Path) -> dict[str, Any]:
    _ensure_src()
    from tools.night_acceptance import LabUnavailable, check_environment
    environment = check_environment()["items"]
    if environment["playwright"] != "OK" or environment["msedge"] == "unavailable":
        raise LabUnavailable("UI 需要现有 Playwright 和系统 Edge；本阶段未运行")
    from playwright.sync_api import sync_playwright

    from umalib_ingest.inbox import InboxRepository
    from umalib_ingest.web import create_server
    from umalib_ingest.web.watcher_ui import WatcherUiConfig
    from umalib_ingest.watcher.state import atomic_write_json

    ui_root = output / "ui"
    if ui_root.exists() and any(ui_root.iterdir()):
        from tools.night_acceptance import LabError

        raise LabError(f"ui 证据目录已存在且非空（不覆盖）: {ui_root}")
    ui_root.mkdir(parents=True, exist_ok=True)
    screenshots_dir = ui_root / "screenshots"
    screenshots_dir.mkdir()

    journeys: list[dict[str, Any]] = []
    from umalib_ingest.watcher.state import PRIVATE_STATE_ROOT

    owned_tmp: list[Path] = []
    def _tmp(prefix: str) -> Path:
        path = Path(tempfile.mkdtemp(dir=str(PRIVATE_STATE_ROOT), prefix=prefix))
        owned_tmp.append(path)
        return path
    extra_tmp: list[Path] = []
    servers: list[tuple[Any, threading.Thread]] = []
    gate = threading.Event()
    try:
        profile = _tmp("night-ui-edge-")
        state_file = _tmp("night-ui-state-") / "watcher-state.json"
        inbox_db = _tmp("night-ui-inbox-") / "inbox.db"
        snapshot_dir = _tmp("night-ui-snaps-")
        ui_profile = _tmp("night-ui-profile-")
        atomic_write_json(state_file, _watcher_state_dict())
        repo = InboxRepository(inbox_db)
        repo.initialize()
        blocked: list[str] = []

        runner = UiFakeRunner("block", gate=gate)
        config = WatcherUiConfig(
            state_file, ui_profile, snapshot_dir, inbox_db,
            login_timeout_seconds=60.0, max_scan_pages=12,
        )
        server = create_server(repo, port=0, watcher_config=config, watcher_runner=runner)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        servers.append((server, server_thread))
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 720})

                def route_block(route):
                    url = urlsplit(route.request.url)
                    if url.scheme != "http" or url.hostname != "127.0.0.1":
                        blocked.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", route_block)
                page = context.new_page()
                page_errors: list[str] = []
                page.on("pageerror", lambda e: page_errors.append(str(e)))

                # Missing configuration and invalid initial state are different
                # UI failures, and neither may start a scan.
                unconfigured = create_server(repo, port=0)
                unconfigured_thread = threading.Thread(target=unconfigured.serve_forever, daemon=True)
                unconfigured_thread.start()
                servers.append((unconfigured, unconfigured_thread))
                page.goto(f"http://127.0.0.1:{unconfigured.server_address[1]}")
                missing_config = _wait_text(page, "#wch-config", "启动时未配置")
                journeys.append(_journey("J00-unconfigured", "未配置时扫描禁用、审核台仍可见", [
                    _record("config_message", missing_config, None),
                    _record("disabled", not page.locator("#wch-scan-btn").is_enabled(), None),
                    _record("inbox_available", page.locator("#list-panel").is_visible(), None)]))
                atomic_write_json(state_file, {"invalid": True})
                page.goto(base, wait_until="load")
                bad_state = _wait_text(page, "#wch-status", "状态不可用")
                journeys.append(_journey("J00-invalid-state", "初始状态读取失败不可扫描", [
                    _record("state_error", bad_state, None),
                    _record("disabled", not page.locator("#wch-scan-btn").is_enabled(), None),
                    _record("no_scan", len(runner.calls) == 0, len(runner.calls))]))
                atomic_write_json(state_file, _watcher_state_dict())
                page.goto(base, wait_until="load")
                page.wait_for_selector("#watcher-panel", timeout=15000)
                page.wait_for_selector("#list-panel", timeout=15000)

                # ---- J01 空闲：按钮状态正确，status 显示 state 摘要 ----
                ok_state = _wait_text(page, "#wch-status", "已知楼层")
                dry_enabled = page.locator("#wch-dryrun-btn").is_enabled()
                scan_enabled = page.locator("#wch-scan-btn").is_enabled()
                login_disabled = not page.locator("#wch-login-confirm-btn").is_enabled()
                journeys.append(_journey(
                    "J01-idle-state",
                    "空闲：状态摘要可见，扫描可用，登录按钮禁用",
                    [_record("status_summary", ok_state, None),
                     _record("dry_run_enabled", dry_enabled, None),
                     _record("scan_enabled", scan_enabled, None),
                     _record("login_button_disabled", login_disabled, None)],
                ))

                # ---- J02 扫描中（阻塞 runner）+ 按钮禁用 ----
                gate.clear()
                page.locator("#wch-dryrun-btn").click()
                ok_busy = _wait_text(page, "#wch-scan-state", "dry-run 扫描中")
                buttons_disabled = (
                    not page.locator("#wch-dryrun-btn").is_enabled()
                    and not page.locator("#wch-scan-btn").is_enabled()
                    and not page.locator("#wch-login-confirm-btn").is_enabled()
                )
                page.screenshot(path=str(screenshots_dir / "J02-scanning.png"))
                gate.set()
                ok_done = _wait_text(page, "#wch-scan-state", "扫描完成")
                journeys.append(_journey(
                    "J02-scanning",
                    "扫描中提示 + 按钮禁用，完成后显示扫描完成",
                    [_record("busy_text", ok_busy, None),
                     _record("buttons_disabled", buttons_disabled, None),
                     _record("done_text", ok_done, None)],
                ))

                # ---- J03 扫描期间按钮禁用，无法重复点击产生第二次扫描 ----
                gate.clear()
                runner.calls.clear()
                page.locator("#wch-dryrun-btn").click()
                page.wait_for_timeout(300)
                disabled_during = not page.locator("#wch-dryrun-btn").is_enabled()
                page.locator("#wch-dryrun-btn").dispatch_event("click")
                page.wait_for_timeout(100)
                gate.set()
                _wait_text(page, "#wch-scan-state", "扫描完成")
                single_call = len(runner.calls) == 1
                journeys.append(_journey(
                    "J03-no-double-scan",
                    "扫描期间按钮禁用，不产生第二次扫描",
                    [_record("button_disabled_during_scan", disabled_during, None),
                     _record("single_runner_call", single_call, len(runner.calls))],
                ))

                # ---- J04 未勾选正式确认被前端阻断，runner 不被调用 ----
                runner.calls.clear()
                page.locator("#wch-scan-btn").click()
                ok_warn = _wait_text(page, "#wch-scan-state", "请先勾选")
                no_call = len(runner.calls) == 0
                journeys.append(_journey(
                    "J04-confirm-required",
                    "未勾选确认被前端阻断且不调用 runner",
                    [_record("warning_shown", ok_warn, None),
                     _record("runner_not_called", no_call, len(runner.calls))],
                ))

                # ---- J05 stale 确认 → 409 → 刷新后重新确认成功 ----
                atomic_write_json(state_file, {**_watcher_state_dict(), "last_page": 174})
                page.locator("#wch-confirm-import").check()
                page.locator("#wch-scan-btn").click()
                ok_stale = _wait_text(page, "#wch-scan-state", "watcher_state_stale")
                _wait_text(page, "#wch-status", "上次页码: 174")
                page.wait_for_timeout(300)
                stale_kept = "watcher_state_stale" in _text(page, "#wch-scan-state")
                page.locator("#wch-confirm-import").check()
                atomic_write_json(state_file, _watcher_state_dict())
                page.locator("#wch-refresh-btn").click()
                _wait_text(page, "#wch-status", "上次页码: 173")
                confirmation_invalidated = not page.locator("#wch-confirm-import").is_checked()
                page.locator("#wch-confirm-import").check()
                runner.calls.clear()
                page.locator("#wch-scan-btn").click()
                ok_success = _wait_text(page, "#wch-scan-state", "扫描完成")
                journeys.append(_journey(
                    "J05-stale-confirm",
                    "stale 确认 409；刷新后重新确认成功",
                    [_record("stale_409", ok_stale, None),
                     _record("stale_error_persists", stale_kept, None),
                     _record("state_change_invalidates_confirmation", confirmation_invalidated, None),
                     _record("rescan_success", ok_success, None)],
                ))
                page.locator("#wch-confirm-import").uncheck()

                # ---- J06 成功扫描向 Inbox 导入假稿并刷新列表 ----
                gate.clear()
                seed_runner = UiFakeRunner("block", seed_on_success=True, gate=gate)
                server.watcher_service = _rebind_service(server, seed_runner, config)
                page.locator("#wch-refresh-btn").click()
                page.locator("#wch-confirm-import").check()
                page.locator("#wch-scan-btn").click()
                gate.set()
                _wait_text(page, "#wch-scan-state", "扫描完成")
                ok_row = _wait_text(page, "#items-body", "界面测试稿", timeout=20000)
                row_text = _text(page, "#items-body")[:120]
                list_msg = _text(page, "#list-message")[:120]
                page.screenshot(path=str(screenshots_dir / "J06-success.png"))
                page.locator("#wch-confirm-import").uncheck()
                journeys.append(_journey(
                    "J06-success-refreshes-inbox",
                    "成功扫描后 Inbox 列表出现新 candidate",
                    [_record("item_row_visible", ok_row, {"rows_text": row_text, "list_message": list_msg})],
                ))

                # ---- J07 失败：显示错误、busy 复位、按钮恢复 ----
                error_runner = UiFakeRunner("error")
                server.watcher_service = _rebind_service(server, error_runner, config)
                page.locator("#wch-refresh-btn").click()
                page.locator("#wch-dryrun-btn").click()
                ok_err = _wait_text(page, "#wch-scan-state", "page_fetch_failed")
                page.wait_for_timeout(500)
                busy_false = page.locator("#wch-dryrun-btn").is_enabled()
                journeys.append(_journey(
                    "J07-error-recovery",
                    "失败显示错误码，busy 复位按钮恢复",
                    [_record("error_shown", ok_err, None),
                     _record("buttons_recovered", busy_false, None)],
                ))

                # ---- J08 等待登录：提示可见、登录按钮可用、扫描禁用 ----
                login_runner = UiFakeRunner("login_wait")
                server.watcher_service = _rebind_service(server, login_runner, config)
                page.locator("#wch-refresh-btn").click()
                page.locator("#wch-dryrun-btn").click()
                ok_login = _wait_visible_text(page, "#wch-login-wait", "检测到需要登录", timeout=20000)
                login_enabled = page.locator("#wch-login-confirm-btn").is_enabled()
                scan_disabled = not page.locator("#wch-scan-btn").is_enabled()
                page.screenshot(path=str(screenshots_dir / "J08-login-wait.png"))
                page.locator("#wch-login-confirm-btn").click()
                ok_confirmed = _wait_text(page, "#wch-scan-state", "扫描完成")
                journeys.append(_journey(
                    "J08-login-wait",
                    "等待登录提示与按钮状态，确认后完成且只重试一次",
                    [_record("login_notice_visible", ok_login, None),
                     _record("login_enabled", login_enabled, None),
                     _record("scan_disabled", scan_disabled, None),
                     _record("confirmed_once", ok_confirmed and login_runner.confirm_called == 1,
                             login_runner.confirm_called)],
                ))

                # ---- J09 取消：shutdown 后显示已取消，不显示成功 ----
                login_runner2 = UiFakeRunner("login_wait")
                server.watcher_service = _rebind_service(server, login_runner2, config)
                page.locator("#wch-refresh-btn").click()
                page.locator("#wch-dryrun-btn").click()
                _wait_visible_text(page, "#wch-login-wait", "检测到需要登录", timeout=20000)
                server.watcher_service.shutdown()
                ok_cancel = _wait_text(page, "#wch-scan-state", "已取消")
                success_absent = "扫描完成" not in _text(page, "#wch-scan-state")
                page.screenshot(path=str(screenshots_dir / "J09-cancelled.png"))
                journeys.append(_journey(
                    "J09-cancelled",
                    "shutdown 取消等待：显示已取消，不显示成功",
                    [_record("cancelled_shown", ok_cancel, None),
                     _record("success_absent", success_absent, None)],
                ))

                # ---- J10 dirty 阻断扫描；扫描期间编辑不丢输入 ----
                server.watcher_service = _rebind_service(server, UiFakeRunner("block", gate=gate), config)
                page.locator("#status-filter").select_option("candidate")
                page.locator("#items-body tr").first.click()
                page.wait_for_selector("#detail-panel:not([hidden])", timeout=8000)
                page.locator("#f-title").fill("编辑中的标题")
                page.locator("#wch-scan-btn").click()
                ok_dirty = _wait_text(page, "#wch-scan-state", "有未保存审核修改")
                page.locator("#discard-btn").click()
                gate.clear()
                page.locator("#wch-dryrun-btn").click()
                _wait_text(page, "#wch-scan-state", "dry-run 扫描中")
                page.locator("#f-title").fill("扫描中编辑标题")
                page.locator("#f-author").fill("扫描中编辑作者")
                gate.set()
                _wait_text(page, "#wch-scan-state", "扫描完成")
                title_kept = page.locator("#f-title").input_value() == "扫描中编辑标题"
                author_kept = page.locator("#f-author").input_value() == "扫描中编辑作者"
                page.screenshot(path=str(screenshots_dir / "J10-dirty-kept.png"))
                page.locator("#discard-btn").click()
                journeys.append(_journey(
                    "J10-dirty-protection",
                    "dirty 阻断扫描；扫描期间编辑的输入在刷新后不丢失",
                    [_record("dirty_blocked", ok_dirty, None),
                     _record("inputs_kept", title_kept and author_kept, None)],
                ))

                # Real Writer-independent scan core: import commits but saving
                # state fails. The UI must report partial and preserve the row.
                partial_runner = _partial_runner
                server.watcher_service = _rebind_service(server, partial_runner, config)
                page.locator("#wch-refresh-btn").click()
                page.locator("#wch-confirm-import").check()
                page.locator("#wch-scan-btn").click()
                partial_shown = _wait_text(page, "#wch-scan-state", "已部分入队")
                partial_row = _wait_text(page, "#items-body", "部分入队测试稿")
                journeys.append(_journey("J16-partial", "真实扫描部分提交，不冒充取消或成功", [
                    _record("partial_message", partial_shown, None),
                    _record("row_retained", partial_row, None),
                    _record("state_not_advanced", json.loads(state_file.read_text(encoding="utf-8")) == _watcher_state_dict(), None)]))

                page.locator("#items-body tr", has_text="部分入队测试稿").click()
                page.wait_for_function("() => document.querySelector('#f-title').value === '部分入队测试稿'")
                page.locator("#f-title").fill("失败后仍保留的编辑")
                page.route("**/api/items/*/review", lambda route: route.fulfill(status=400,
                    content_type="application/json", body=json.dumps({"error": {
                        "code": "invalid_request", "message": "模拟保存失败"}})))
                page.locator("#save-btn").click()
                save_failed = _wait_text(page, "#detail-message", "模拟保存失败")
                journeys.append(_journey("J17-edit-failure", "编辑保存失败时保留输入", [
                    _record("error", save_failed, None),
                    _record("input_retained", page.locator("#f-title").input_value() == "失败后仍保留的编辑", None)]))
                page.unroute("**/api/items/*/review")
                page.locator("#discard-btn").click()

                failed_login = UiFakeRunner("login_failure")
                server.watcher_service = _rebind_service(server, failed_login, config)
                page.locator("#wch-refresh-btn").click()
                page.locator("#wch-dryrun-btn").click()
                _wait_visible_text(page, "#wch-login-wait", "检测到需要登录", timeout=20000)
                page.locator("#wch-login-confirm-btn").click()
                page.locator("#wch-login-confirm-btn").dispatch_event("click")
                login_failed = _wait_text(page, "#wch-scan-state", "login_required")
                journeys.append(_journey("J18-login-failure", "确认后仍失败，不重复确认或重扫", [
                    _record("error", login_failed, None),
                    _record("single_confirmation", failed_login.confirm_called == 1, failed_login.confirm_called),
                    _record("single_scan", len(failed_login.calls) == 1, len(failed_login.calls))]))

                # ---- J11 服务器断开后重新加载恢复 ----
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=10)
                server2 = create_server(repo, port=port, watcher_config=config,
                                        watcher_runner=UiFakeRunner("block", gate=gate))
                server_thread2 = threading.Thread(target=server2.serve_forever, daemon=True)
                server_thread2.start()
                servers.append((server2, server_thread2))
                page.reload(wait_until="load")
                page.wait_for_selector("#watcher-panel", timeout=15000)
                ok_recover = _wait_text(page, "#wch-status", "已知楼层", timeout=15000)
                page.screenshot(path=str(screenshots_dir / "J11-recovered.png"))
                journeys.append(_journey(
                    "J11-reload-recovery",
                    "服务器断开后重新加载页面恢复状态",
                    [_record("status_recovered", ok_recover, None)],
                ))

                # ---- J12 空列表与 40 项列表布局 ----
                server2.shutdown()
                server2.server_close()
                server_thread2.join(timeout=10)
                empty_dir = _tmp("night-ui-empty-")
                extra_tmp.append(empty_dir)
                empty_repo = InboxRepository(empty_dir / "empty.db")
                empty_repo.initialize()
                server3 = create_server(empty_repo, port=0, watcher_config=config,
                                        watcher_runner=UiFakeRunner("block", gate=gate))
                server_thread3 = threading.Thread(target=server3.serve_forever, daemon=True)
                server_thread3.start()
                servers.append((server3, server_thread3))
                base3 = f"http://127.0.0.1:{server3.server_address[1]}"
                page.goto(base3, wait_until="load")
                page.wait_for_selector("#list-panel", timeout=15000)
                empty_msg = _wait_text(page, "#items-body", "（空）")
                _seed_many(empty_repo, 40)
                page.reload(wait_until="load")
                page.wait_for_selector("#items-body tr", timeout=15000)
                rows = page.locator("#items-body tr").count()
                overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth > window.innerWidth + 1"
                )
                journeys.append(_journey(
                    "J12-list-empty-and-40",
                    "空列表提示与 40 项列表无横向溢出",
                    [_record("empty_message", empty_msg, None),
                     _record("rows_40", rows == 40, rows),
                     _record("no_horizontal_overflow", not overflow, overflow)],
                ))

                # ---- J13 键盘 Tab 可达 ----
                page.locator("#wch-refresh-btn").focus()
                page.keyboard.press("Tab")
                active = page.evaluate("() => document.activeElement && document.activeElement.id")
                journeys.append(_journey(
                    "J13-keyboard-tab",
                    "Tab 可移动焦点到相邻按钮",
                    [_record("tab_moved_focus", bool(active) and active != "wch-refresh-btn", active)],
                ))

                # ---- J14/J15 多分辨率 + 125% 缩放布局检查（server3 存活时执行） ----
                layout_checks: list[dict[str, Any]] = []
                for width, height in VIEWPORTS:
                    context2 = browser.new_context(viewport={"width": width, "height": height})
                    context2.route("**/*", route_block)
                    page2 = context2.new_page()
                    page2.goto(base3, wait_until="load")
                    page2.wait_for_selector("#watcher-panel", timeout=15000)
                    watcher = page2.locator("#watcher-panel").bounding_box()
                    lst = page2.locator("#list-panel").bounding_box()
                    overlap = _boxes_overlap(watcher, lst)
                    layout_checks.append(_record(f"viewport-{width}x{height}-no-overlap",
                                                 not overlap, None))
                    if (width, height) == (1024, 768):
                        page2.screenshot(path=str(screenshots_dir / "layout-1024x768.png"))
                    context2.close()
                # 1280x720 physical pixels at 125% == 1024x576 CSS pixels.
                context_zoom = browser.new_context(viewport={"width": 1024, "height": 576},
                                                   device_scale_factor=1.25)
                context_zoom.route("**/*", route_block)
                page_zoom = context_zoom.new_page()
                page_zoom.goto(base3, wait_until="load")
                page_zoom.wait_for_selector("#watcher-panel", timeout=15000)
                zoom_overlap = _boxes_overlap(
                    page_zoom.locator("#watcher-panel").bounding_box(),
                    page_zoom.locator("#list-panel").bounding_box(),
                )
                page_zoom.screenshot(path=str(screenshots_dir / "layout-125percent.png"))
                context_zoom.close()
                layout_checks.append(_record("zoom-125-no-overlap", not zoom_overlap, None))
                journeys.append(_journey(
                    "J14-layout",
                    "3 分辨率 + 125% 缩放：面板可见且不遮挡 Inbox",
                    layout_checks,
                ))
                server3.shutdown()
                server3.server_close()
                server_thread3.join(timeout=10)

                # ---- J15 页面加载不访问 NGA/非 loopback ----
                journeys.append(_journey(
                    "J15-no-external-requests",
                    "页面加载与交互不产生任何非 loopback 请求",
                    [_record("blocked_external_empty", blocked == [], blocked),
                     _record("no_page_errors", page_errors == [], page_errors)],
                ))
            except Exception:
                try:
                    snapshot = {
                        "journeys_so_far": journeys,
                        "scan_state": _text(page, "#wch-scan-state"),
                        "wch_status": _text(page, "#wch-status"),
                        "list_message": _text(page, "#list-message"),
                        "rows": page.locator("#items-body tr").count(),
                    }
                    (ui_root / "failure-snapshot.json").write_text(
                        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except Exception:
                    pass
                raise
            finally:
                browser.close()
    finally:
        gate.set()
        for server_obj, server_thr in reversed(servers):
            server_obj.watcher_service.shutdown()
            if server_thr.is_alive():
                server_obj.shutdown()
            server_obj.server_close()
            server_thr.join(timeout=5)
        for tmp in owned_tmp:
            # Only exact mkdtemp paths owned by this run; failures are not PASS.
            shutil.rmtree(tmp)

    failed = [j["journey_id"] for j in journeys if j["verdict"] != "PASS"]
    overall = "PASS" if not failed and len(journeys) >= 20 else "FAIL"
    summary = {
        "phase": "ui",
        "total_journeys": len(journeys),
        "failed": failed,
        "overall": overall,
    }
    (ui_root / "journeys.json").write_text(
        json.dumps({"journeys": journeys, "summary": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    kept = sorted(p.name for p in screenshots_dir.iterdir())
    (ui_root / "index.txt").write_text(
        "\n".join([f"ui/screenshots/{name}" for name in kept]), encoding="utf-8"
    )
    return summary


def _boxes_overlap(a: dict | None, b: dict | None) -> bool:
    if not a or not b:
        return True
    return not (
        a["x"] + a["width"] <= b["x"]
        or b["x"] + b["width"] <= a["x"]
        or a["y"] + a["height"] <= b["y"]
        or b["y"] + b["height"] <= a["y"]
    )


def _rebind_service(server, runner, config):
    from umalib_ingest.web.watcher_ui import WatcherUiService

    service = WatcherUiService(config, runner=runner)
    server.watcher_service = service
    server.RequestHandlerClass.watcher_service = service
    return service


def _partial_runner(config, **kwargs):
    """Exercise real scan/import/partial handling with synthetic responses."""
    from unittest import mock
    from umalib_ingest.watcher import scan
    from umalib_ingest.nga_access.fetch import FetchedThreadPage
    from umalib_ingest.nga_access.parsing import FloorSnapshot
    text = "[收录]\n[标题] 部分入队测试稿\n[作者] 假作者\n[标签] 假标签\n\n假正文。"
    floor = FloorSnapshot("910009088", None, True, T0, None, "text", text, text)
    def fetch(session, tid, page, snapshots):
        return FetchedThreadPage(tid, page, 173, 200,
            f"https://bbs.nga.cn/read.php?tid={tid}&page={page}",
            "text/html", "utf-8", "ab" * 32, 0, "written", (floor,) if page == 173 else (), ())
    class Session:
        def close(self):
            pass
    with mock.patch.object(scan, "atomic_write_json", side_effect=OSError("synthetic state write failure")):
        return scan.run_scan(config, session_factory=Session, fetch_fn=fetch,
                             sleep=lambda _: None, **kwargs)


def _seed_many(repo, count: int) -> None:
    from umalib_ingest.parser import parse_submission
    from umalib_ingest.watcher.diff import build_floor_origin

    for index in range(count):
        text = (
            f"[收录]\n[标题] 超长中文标题测试第{index + 1}篇——包含大量文字用于验证布局不溢出\n"
            f"[作者] 假作者{index + 1}\n[标签] 标签甲\n\n正文第{index + 1}篇。\n第二行正文。\n第三行正文。"
        )
        draft = parse_submission(text, origin=build_floor_origin(TID, f"91001{index:04d}", None))
        repo.import_draft(draft)
