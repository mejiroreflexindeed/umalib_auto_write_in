"""TASK-015 C5：真实浏览器验证 R06（未保存保护）与 R08（补查/中文状态/翻页/布局）。"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from tests.test_operator_closeout import _html, make_factory, wait_run  # noqa: E402


def _complete(title):
    return f"[收录]\n[标题] {title}\n[作者] 演示作者\n[标签] 演示标签\n\n正文。"


class OperatorUiFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="op-ui-")
        self.tid = "12345678"
        pages = {
            1: _html(self.tid, 1, 1, [
                ("91001001", _complete("界面稿一"), "2026-08-10 10:00:00"),
                ("91001002", "普通讨论", "2026-08-10 10:00:00"),
                ("91001003", _complete("界面稿二"), "2026-08-11 10:00:00"),
            ]),
        }
        from umalib_ingest.operator.app_factory import create_operator_app

        self.ctx = create_operator_app(
            root=Path(self.tmp.name), new_workspace=True,
            session_factory=make_factory(self.tid, pages), nav_delay=0.0,
        )
        self.thread = threading.Thread(target=self.ctx.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.ctx.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        # 通过真实服务准备：绑定源副本 + 添加帖子 + 初始化 + 扫描出两条待审
        svc = self.ctx.service
        main_copy = Path(r"D:\codex\13\umalib-test-data\main-copy.db")
        if not main_copy.exists():
            self.skipTest("main-copy.db 不存在，跳过 UI 联调")
        verified = svc.verify_source(main_copy)
        svc.bind_source(verified["source_id"], "界面测试工作区", True)
        svc.add_thread(f"https://bbs.nga.cn/read.php?tid={self.tid}", None, 0)
        run_id = svc.initialize_preview(self.tid, {
            "mode": "page", "page": 1, "date": None,
            "timezone": "UTC+08:00", "lower_bound_utc": None}, 0, True)
        run = wait_run(svc, run_id)
        assert run["status"] == "succeeded", run.get("error")
        svc.initialize_confirm(self.tid, run_id, run["result"]["preview_sha256"], 0, True)
        st = svc.thread_status(self.tid)
        scan = svc.scan(self.tid, False, True, st["state_sha256"])
        scan_run = wait_run(svc, scan)
        assert scan_run["status"] == "succeeded", scan_run.get("error")

    def tearDown(self) -> None:
        self.ctx.shutdown()
        self.tmp.cleanup()

    def test_dirty_protection_and_rescan_ui(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1366, "height": 768})
                page = ctx.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(self.base, wait_until="load")
                page.wait_for_selector("#thread-entries .thread-entry", timeout=15000)

                # R08：补查 UI 存在且可见（初始化后）
                page.locator("#main-nav button", has_text="待审稿件").click()
                page.wait_for_selector("#items-body tr", timeout=15000)
                rescan_controls = page.locator("#rescan-panel button").count()
                self.assertGreaterEqual(rescan_controls, 1, "补查按钮未接线")
                # 中文状态
                rows_text = page.locator("#items-body").text_content()
                self.assertIn("待审", rows_text)
                self.assertNotIn("candidate", rows_text)
                # 翻页控件存在（当前仅一页，按钮隐藏但 DOM 存在）
                self.assertEqual(page.locator("#filter-prev").count(), 1)
                self.assertEqual(page.locator("#filter-next").count(), 1)

                # R06：编辑稿一后切稿二，出现确认；选择“留在当前页”保留编辑
                row_one = page.locator("#items-body tr", has_text="界面稿一")
                row_two = page.locator("#items-body tr", has_text="界面稿二")
                row_one.click()
                page.wait_for_selector("#review-form [data-field=title]", timeout=15000)
                page.locator("#review-form [data-field=title]").fill("未保存的修改")
                dialogs = []

                def on_dialog(dialog):
                    dialogs.append(dialog.message)
                    dialog.dismiss()  # 留在当前页

                page.on("dialog", on_dialog)
                row_two.click()
                page.wait_for_timeout(600)
                self.assertTrue(dialogs, "切稿未提示未保存修改")
                self.assertEqual(
                    page.locator("#review-form [data-field=title]").input_value(),
                    "未保存的修改", "选择留下时不应丢失输入")

                # 选择“放弃修改”后允许切换
                page.remove_listener("dialog", on_dialog)

                def accept_dialog(dialog):
                    dialogs.append(dialog.message)
                    dialog.accept()

                page.on("dialog", accept_dialog)
                row_two.click()
                page.wait_for_timeout(1000)
                self.assertEqual(
                    page.locator("#review-form [data-field=title]").input_value(),
                    "界面稿二", "确认放弃后应载入另一稿")

                # 两档全页截图
                shots = Path(self.tmp.name) / "shots"
                shots.mkdir(exist_ok=True)
                page.screenshot(path=str(shots / "review-1366.png"), full_page=True)
                ctx.close()
                ctx2 = browser.new_context(viewport={"width": 1024, "height": 768})
                page2 = ctx2.new_page()
                page2.goto(self.base, wait_until="load")
                page2.wait_for_selector("#thread-entries .thread-entry", timeout=15000)
                page2.screenshot(path=str(shots / "home-1024.png"), full_page=True)
                overflow = page2.evaluate(
                    "() => document.documentElement.scrollWidth > window.innerWidth + 1")
                self.assertFalse(overflow, "1024 宽出现水平溢出")
                ctx2.close()
                self.assertEqual(errors, [], errors)
            finally:
                browser.close()


    def test_status_buttons_recover_and_rescan_date_backfill(self) -> None:
        """Q0 F008/F009：退回后按钮立即恢复；日期按 UTC+08:00 回填。"""
        import json as _json

        from playwright.sync_api import sync_playwright

        from umalib_ingest.operator.thread_registry import atomic_write_json

        svc = self.ctx.service
        state_path = svc.paths.state_json(svc.workspace_id, self.tid)
        state = _json.loads(state_path.read_text(encoding="utf-8"))
        state["active_rescan"] = {
            "rescan_id": "11111111-1111-4111-8111-111111111111", "kind": "date",
            "from_page": 1, "to_page": state["observed_last_page"],
            "lower_bound_utc": "2026-08-09T16:00:00+00:00", "next_page": 1,
            "created_at": "2026-09-13T00:00:00+00:00",
        }
        atomic_write_json(state_path, state)

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_context(viewport={"width": 1366, "height": 768}).new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(self.base, wait_until="load")
                page.wait_for_selector("#thread-entries .thread-entry", timeout=15000)
                # F009：UTC 下限 2026-08-09T16:00:00Z 应回填本地日历 2026-08-10
                page.wait_for_selector("#rescan-panel:not(.hidden)", timeout=15000)
                page.wait_for_timeout(800)
                self.assertEqual(page.locator("#rescan-date").input_value(), "2026-08-10")
                self.assertFalse(page.locator("#rescan-continue-btn").is_hidden())

                # F008：批准→切走再回来→退回→无需切稿即可保存/重新批准
                page.locator("#main-nav button", has_text="待审稿件").click()
                page.wait_for_selector("#items-body tr", timeout=15000)
                page.locator("#items-body tr", has_text="界面稿一").click()
                page.wait_for_selector("#review-form [data-field=title]", timeout=15000)
                page.locator("#review-form button", has_text="批准稿件").click()
                page.wait_for_function(
                    "() => document.querySelector('#status-diagnostic').textContent.includes('已批准')",
                    timeout=15000)
                self.assertTrue(
                    page.locator("#review-form button", has_text="保存修改").is_disabled())
                self.assertFalse(
                    page.locator("#review-form button", has_text="退回待审").is_hidden())
                page.locator("#items-body tr", has_text="界面稿二").click()
                page.wait_for_function(
                    "() => document.querySelector('#review-form [data-field=title]').value === '界面稿二'",
                    timeout=15000)
                page.locator("#items-body tr", has_text="界面稿一").click()
                page.wait_for_function(
                    "() => document.querySelector('#review-form [data-field=title]').value === '界面稿一'",
                    timeout=15000)
                self.assertTrue(
                    page.locator("#review-form button", has_text="保存修改").is_disabled())

                def on_dialog(dialog):
                    if dialog.type == "prompt":
                        dialog.accept("继续修改")
                    else:
                        dialog.accept()

                page.on("dialog", on_dialog)
                page.locator("#review-form button", has_text="退回待审").click()
                page.wait_for_function(
                    "() => document.querySelector('#status-diagnostic').textContent.includes('状态已更新：待审')",
                    timeout=15000)
                # 不切稿、不刷新，保存与重新批准立即可用
                self.assertFalse(
                    page.locator("#review-form button", has_text="保存修改").is_disabled())
                self.assertFalse(
                    page.locator("#review-form button", has_text="批准稿件").is_disabled())
                self.assertEqual(errors, [], errors)
            finally:
                browser.close()


    def test_refresh_resumes_active_run(self) -> None:
        """F010：扫描运行中刷新页面后重新接入同一任务（取消按钮/提示/轮询）。"""
        import http.client
        import json as _json

        from playwright.sync_api import sync_playwright

        svc = self.ctx.service
        gate = threading.Event()

        def blocking_scan(record, controller):
            controller.set_status("running")
            gate.wait(timeout=60)
            controller.set_status("succeeded", result={
                "pages_scanned": 1, "floors_seen": 0, "discussion_count": 0,
                "triggered_count": 0, "imported_count": 0, "incomplete_count": 0,
                "updated_count": 0, "date_excluded_count": 0,
                "date_unresolved_count": 0, "remaining_pages": 0, "state_sha256": ""})

        svc._jobs.register_executor("scan", blocking_scan)

        def req(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            conn.request(method, path,
                         body=_json.dumps(body).encode("utf-8") if body is not None else None,
                         headers={"Content-Type": "application/json"} if body is not None else {})
            res = conn.getresponse()
            data = res.read().decode("utf-8")
            conn.close()
            return res.status, (_json.loads(data) if data else {})

        st = svc.thread_status(self.tid)
        status, body = req("POST", f"/api/operator/threads/{self.tid}/scan", {
            "dry_run": False, "confirm_import": True,
            "expected_state_sha256": st["state_sha256"]})
        self.assertEqual(status, 200, body)
        active_id = body["data"]["run_id"]
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="msedge", headless=True)
                try:
                    page = browser.new_context(viewport={"width": 1366, "height": 768}).new_page()
                    errors = []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.goto(self.base, wait_until="load")
                    page.wait_for_selector("#thread-entries .thread-entry", timeout=20000)
                    page.reload(wait_until="load")
                    page.wait_for_selector("#thread-entries .thread-entry", timeout=20000)
                    page.wait_for_timeout(1800)
                    self.assertFalse(page.locator("#cancel-btn").is_hidden())
                    self.assertIn("重新接入",
                                  page.locator("#scan-msg").text_content() or "")
                    page.locator("#cancel-btn").click()
                    deadline = time.monotonic() + 15
                    cancelled = False
                    while time.monotonic() < deadline:
                        if svc.get_run(active_id)["status"] in ("cancelling", "cancelled"):
                            cancelled = True
                            break
                        time.sleep(0.1)
                    self.assertTrue(cancelled)
                    self.assertEqual(errors, [], errors)
                finally:
                    browser.close()
        finally:
            gate.set()


if __name__ == "__main__":
    unittest.main()
