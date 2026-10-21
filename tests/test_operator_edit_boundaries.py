"""Browser regressions for unsaved decisions and delayed HTTP responses."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from tests.test_operator_closeout import wait_run
from tests.test_operator_scan import _Session
from umalib_ingest.demo.samples import create_synthetic_base
from umalib_ingest.operator.app_factory import create_operator_app


class OperatorEditBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="op-edit-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name) / "synthetic.db"
        create_synthetic_base(base)
        scenario = {"tid": "12345678", "last_page": 1, "floors": {1: [
            (str(91000001 + i), f"[收录]\n[标题] 界面稿{i + 1}\n[作者] 合成作者\n"
             f"[标签] 唯一演示标签\n\n第{i + 1}篇正文", "2026-08-10 12:00:00")
            for i in range(2)
        ]}}
        with patch("umalib_ingest.operator.app_factory.detect_main_copy", return_value=None):
            self.app = create_operator_app(root=Path(self.tmp.name) / "operator",
                                          session_factory=lambda: _Session(scenario), nav_delay=0)
        self.addCleanup(self.app.shutdown)
        svc = self.app.service
        svc.bind_source(svc.verify_source(base)["source_id"], "合成界面验收", True)
        svc.add_thread("https://bbs.nga.cn/read.php?tid=12345678", None, 0)
        run = wait_run(svc, svc.initialize_preview("12345678", {
            "mode": "page", "page": 1, "date": None, "timezone": "UTC+08:00",
        }, 0, True))
        svc.initialize_confirm("12345678", run["run_id"], run["result"]["preview_sha256"], 0, True)
        scan = wait_run(svc, svc.scan("12345678", False, True,
                                     svc.thread_status("12345678")["state_sha256"]))
        self.assertEqual(scan["status"], "succeeded", scan)
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(channel="msedge", headless=True)
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={"width": 1366, "height": 900})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        # Real HTTP and DOM; forbid all non-loopback requests.
        self.page.route("**/*", lambda route: route.continue_()
                        if route.request.url.startswith("http://127.0.0.1:") else route.abort())
        self.page.goto(f"http://127.0.0.1:{self.app.server.server_address[1]}")
        self.page.locator("#main-nav button", has_text="待审稿件").click()
        self.page.locator("#items-body tr", has_text="界面稿1").click()
        expect(self.page.locator(".tag-decision select")).to_have_count(1)

    def tearDown(self) -> None:
        self.assertEqual(self.errors, [])

    def _button(self, name):
        return self.page.get_by_role("button", name=name, exact=True)

    def _ready(self):
        self._button("批准稿件").click()
        expect(self.page.locator("#status-diagnostic")).to_contain_text("已批准")
        self.page.locator('.writer-decisions input[type="datetime-local"]').fill("2026-08-10T12:00")
        self.page.get_by_label("我确认选择纯文本时可能丢失颜色/粗体等格式").check()
        self._button("保存人工决定").click()
        expect(self.page.locator(".writer-decisions .msg").first).to_contain_text("人工决定已保存")

    def test_unsaved_writer_decisions_guard_switch_and_discard(self):
        note = self.page.locator('.writer-decisions input[type="text"]')
        note.fill("不能丢失的备注")
        dialogs = []

        def dismiss(dialog):
            dialogs.append(dialog.message)
            dialog.dismiss()

        self.page.on("dialog", dismiss)
        self.page.locator("#items-body tr", has_text="界面稿2").click()
        self.assertTrue(dialogs, "人工决定编辑必须触发未保存保护")
        expect(note).to_have_value("不能丢失的备注")
        self.page.remove_listener("dialog", dismiss)
        self.page.on("dialog", lambda dialog: dialog.accept())
        self.page.locator("#main-nav button", has_text="设置").click()
        self.page.locator("#main-nav button", has_text="待审稿件").click()
        self.page.locator("#items-body tr", has_text="界面稿1").click()
        expect(note).to_have_value("")

    def test_unsaved_edit_invalidates_ready_candidate_and_cannot_preview(self):
        self._ready()
        self._button("生成并查看预览").click()
        expect(self._button("确认生成候选数据库")).to_be_enabled()
        # F021：已批准稿审核字段只读；未保存的人工决定修改同样使预览失效。
        expect(self.page.locator("[data-field=title]")).to_have_attribute(
            "readonly", "")
        self.page.locator('.writer-decisions input[type="text"]').fill(
            "尚未保存的备注")
        expect(self._button("确认生成候选数据库")).to_be_disabled()
        expect(self._button("生成并查看预览")).to_be_disabled()

    def test_saved_tags_refresh_choices_without_discarding_note(self):
        self.page.locator('.writer-decisions input[type="text"]').fill("保留备注")
        self.page.locator("[data-field=tags]").fill("新的合成标签")
        self._button("保存修改").click()
        expect(self.page.locator(".tag-decision")).to_contain_text("新的合成标签")
        expect(self.page.locator('.writer-decisions input[type="text"]')).to_have_value("保留备注")
        self.page.locator(".tag-decision select").select_option("new:0")
        self._ready()
        self._button("生成并查看预览").click()
        expect(self._button("确认生成候选数据库")).to_be_enabled()
        expect(self.page.locator(".writer-preview-inline")).to_contain_text("新的合成标签")

    def test_late_preview_does_not_restore_old_candidate(self):
        self._ready()
        pending = []

        def hold(route):
            pending.append((route, route.fetch()))

        self.page.route("**/writer/preview", hold)
        self._button("生成并查看预览").click()
        deadline = time.monotonic() + 5
        while not pending and time.monotonic() < deadline:
            self.page.wait_for_timeout(10)
        self.assertEqual(len(pending), 1)
        self.page.locator('.writer-decisions input[type="text"]').fill("预览返回前的新备注")
        with self.page.expect_response("**/writer/preview"):
            pending[0][0].fulfill(response=pending[0][1])
        # Drain the promise chain through a later real HTTP response.
        self.page.evaluate("() => fetch('/api/operator/status').then(r => r.json())")
        expect(self._button("确认生成候选数据库")).to_be_disabled()

    def test_late_save_keeps_new_edits_dirty(self):
        pending = []

        def hold(route):
            pending.append((route, route.fetch()))

        self.page.route("**/review", hold)
        title = self.page.locator("[data-field=title]")
        title.fill("已提交保存的标题")
        self._button("保存修改").click()
        deadline = time.monotonic() + 5
        while not pending and time.monotonic() < deadline:
            self.page.wait_for_timeout(10)
        self.assertEqual(len(pending), 1)
        title.fill("保存过程中继续编辑")
        with self.page.expect_response("**/review"):
            pending[0][0].fulfill(response=pending[0][1])
        self.page.evaluate("() => fetch('/api/operator/status').then(r => r.json())")
        dialogs = []
        self.page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
        self.page.locator("#items-body tr", has_text="界面稿2").click()
        self.assertTrue(dialogs, "迟到的保存响应不能清掉后来输入的未保存标记")
        expect(title).to_have_value("保存过程中继续编辑")


if __name__ == "__main__":
    unittest.main()
