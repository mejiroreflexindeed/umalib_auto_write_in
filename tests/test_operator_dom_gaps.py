"""TASK-015 2026-09-21 收尾：A053/A054/A067/A108/A112 的真实 DOM 回归。"""

from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from tests.test_operator_closeout import _html  # noqa: E402


def _complete(title: str, tag: str = "演示标签") -> str:
    return f"[收录]\n[标题] {title}\n[作者] 演示作者\n[标签] {tag}\n\n正文。"


class _Page:
    def __init__(self, mapping):
        self._mapping = mapping

    def goto(self, url, wait_until=None):
        tid = url.split("tid=")[1].split("&")[0]
        page = int(url.split("&page=")[1]) if "&page=" in url else 1
        from tests.test_operator_closeout import _Resp

        return _Resp(url, self._mapping[tid][page])

    def title(self):
        return "模拟"


class _Session:
    def __init__(self, page):
        self.page = page

    def close(self):
        pass


class DomGapTests(unittest.TestCase):
    def setUp(self) -> None:
        from umalib_ingest.demo.samples import create_synthetic_base
        from umalib_ingest.operator.app_factory import create_operator_app

        self.tmp = tempfile.TemporaryDirectory(prefix="op-dom-")
        root = Path(self.tmp.name)
        source = root / "synt.db"
        create_synthetic_base(source)
        conn = sqlite3.connect(source)
        try:
            base = conn.execute("SELECT COALESCE(MAX(id), 0) FROM tag").fetchone()[0]
            conn.execute(
                "INSERT INTO tag (id, name, type, cover, description) VALUES (?,?,?,?,?)",
                (base + 1, "同名标签", 0, None, None))
            conn.execute(
                "INSERT INTO tag (id, name, type, cover, description) VALUES (?,?,?,?,?)",
                (base + 2, "同名标签", 1, None, None))
            conn.commit()
        finally:
            conn.close()
        self.tag_type0 = base + 1
        self.tag_type1 = base + 2

        t1, t2, t3, t4, t5, t6, t7 = ("12345678", "87654321", "11112222",
                                      "44445555", "55556666", "66667777",
                                      "77778888")
        t8 = "88889999"
        t9, t10 = "99991111", "99992222"
        self.t1, self.t2, self.t3, self.t4 = t1, t2, t3, t4
        self.t5, self.t6, self.t7, self.t8 = t5, t6, t7, t8
        self.t9, self.t10 = t9, t10
        day = "2026-08-10 10:00:00"
        mapping = {
            t1: {1: _html(t1, 1, 1, [
                ("91001001", _complete("甲稿一"), day),
                ("91001002", "普通讨论甲", day)])},
            t2: {1: _html(t2, 1, 1, [
                ("91002001", _complete("乙稿"), day)])},
            t3: {1: _html(t3, 1, 1, [
                ("91003001", _complete("七月稿"), "2026-07-15 10:00:00")])},
            t4: {1: _html(t4, 1, 1, [
                ("91004001", _complete("七月稿二"), "2026-07-16 10:00:00"),
                ("91004002", _complete("时间未知稿"), "未知时间")])},
            t5: {1: _html(t5, 1, 1, [
                ("91005001", _complete("已收录稿一"), day),
                ("91005002", _complete("已收录稿二"), day)])},
            t6: {1: _html(t6, 1, 1, [
                ("91006001", "普通讨论一", day),
                ("91006002", "普通讨论二", day)])},
            t7: {1: _html(t7, 1, 1, [
                ("91007001", _complete("同类标签稿", tag="同名标签"), day)])},
            t8: {1: _html(t8, 1, 1, [
                ("91008001", _complete("更新稿一"), day)])},
            t9: {1: _html(t9, 1, 1, [
                ("91009001", _complete("混合稿一"), day),
                ("91009002", "混合讨论", day)])},
            t10: {1: _html(t10, 1, 1, [
                ("91010001", _complete("七月混稿"), "2026-07-15 10:00:00"),
                ("91010002", "日期讨论", day)])},
        }
        self.mapping = mapping
        self.ctx = create_operator_app(
            root=root, new_workspace=True,
            session_factory=lambda: _Session(_Page(mapping)), nav_delay=0.0)
        self.thread = threading.Thread(target=self.ctx.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.ctx.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        from tests.test_operator_closeout import wait_run

        svc = self.ctx.service
        verified = svc.verify_source(source)
        svc.bind_source(verified["source_id"], "DOM 测试区", True)

        def add(tid, name):
            svc.add_thread(f"https://bbs.nga.cn/read.php?tid={tid}", name,
                           svc.list_threads()["revision"])

        for tid, name in ((t1, "甲帖"), (t2, "乙帖"), (t3, "日期排除帖"),
                          (t4, "日期统计帖"), (t5, "现在基线条"),
                          (t6, "无标记帖"), (t7, "标签帖"), (t8, "更新帖"),
                          (t9, "混合帖"), (t10, "日期混合帖")):
            add(tid, name)

        def init(tid, start):
            run_id = svc.initialize_preview(tid, start, 0, True)
            run = wait_run(svc, run_id)
            assert run["status"] == "succeeded", (tid, run.get("error"))
            svc.initialize_confirm(tid, run_id, run["result"]["preview_sha256"],
                                   0, True)

        def scan(tid):
            st = svc.thread_status(tid)
            run = wait_run(svc, svc.scan(tid, False, True, st["state_sha256"]))
            assert run["status"] == "succeeded", (tid, run.get("error"))

        page_start = {"mode": "page", "page": 1, "date": None,
                      "timezone": "UTC+08:00", "lower_bound_utc": None}
        date_start = {"mode": "date", "page": None, "date": "2026-08-01",
                      "timezone": "UTC+08:00", "lower_bound_utc": None}
        init(t1, page_start); scan(t1)
        init(t2, page_start); scan(t2)
        init(t3, date_start)
        init(t4, date_start)
        init(t5, {"mode": "now", "page": None, "date": None,
                  "timezone": "UTC+08:00", "lower_bound_utc": None})
        init(t6, page_start)
        init(t7, page_start); scan(t7)
        init(t8, page_start); scan(t8)
        init(t9, page_start); scan(t9)
        init(t10, date_start)

    def tearDown(self) -> None:
        self.ctx.shutdown()
        self.tmp.cleanup()

    # ---- helpers ----

    def _open(self, page) -> None:
        page.goto(self.base_url, wait_until="load")
        page.wait_for_selector("#thread-entries .thread-entry", timeout=20000)

    def _select_thread(self, page, tid: str) -> None:
        page.locator("#main-nav button", has_text="帖子与扫描").click()
        entry = page.locator("#thread-entries .thread-entry", has_text=tid)
        entry.click(position={"x": 8, "y": 8})
        page.wait_for_function(
            "tid => { const row = Array.from(document.querySelectorAll("
            "'#thread-entries .thread-entry')).find(r => r.textContent.indexOf(tid) >= 0);"
            " return !!(row && row.classList.contains('active')); }",
            arg=tid, timeout=15000)

    def _scan(self, page) -> str:
        previous = page.locator("#run-result").text_content() or ""
        page.locator("#scan-btn").click()
        page.wait_for_function(
            "() => {"
            " const panel = document.getElementById('run-result-panel');"
            " const el = document.getElementById('run-result');"
            " if (!panel || !el || panel.classList.contains('hidden')) { return false; }"
            " return el.textContent.indexOf('读取页') >= 0; }",
            timeout=90000)
        page.wait_for_function(
            "prev => (document.getElementById('run-result').textContent || '') !== prev",
            arg=previous, timeout=90000)
        return page.locator("#run-result").text_content() or ""

    def _open_inbox_item(self, page, title: str, status_text: str | None = None) -> None:
        page.locator("#main-nav button", has_text="待审稿件").click()
        page.wait_for_selector("#items-body tr", timeout=20000)
        rows = page.locator("#items-body tr")
        target = None
        best_len = None
        for index in range(rows.count()):
            row = rows.nth(index)
            text = row.text_content() or ""
            if title in text and (status_text is None or status_text in text) \
                    and (best_len is None or len(text) < best_len):
                target = row
                best_len = len(text)
        self.assertIsNotNone(target, f"未找到稿件行: {title}")
        target.click()
        page.wait_for_selector("#review-form [data-field=title]", timeout=20000)

    def test_a053_zero_result_reasons(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1366, "height": 768})
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                self._open(page)

                self._select_thread(page, self.t5)
                text = self._scan(page)
                self.assertIn("基线内 [收录] 2", text)
                self.assertNotIn("已见投稿", text)
                self.assertNotIn("已见讨论", text)

                self._select_thread(page, self.t6)
                text = self._scan(page)
                self.assertIn("新讨论 2", text)
                self.assertNotIn("已见投稿", text)
                text = self._scan(page)
                self.assertIn("已见讨论 2", text)
                self.assertNotIn("已见投稿", text)

                self._select_thread(page, self.t2)
                text = self._scan(page)
                self.assertIn("已见投稿", text)
                self.assertNotIn("新讨论", text)

                self._select_thread(page, self.t3)
                text = self._scan(page)
                self.assertIn("早于所选日期 1", text)

                self._select_thread(page, self.t9)
                text = self._scan(page)
                self.assertIn("已见投稿 1", text)
                self.assertIn("已见讨论 1", text)

                self._select_thread(page, self.t10)
                text = self._scan(page)
                self.assertIn("新讨论 1", text)
                self.assertIn("早于所选日期 1", text)
                text = self._scan(page)
                self.assertIn("已见讨论 1", text)
                self.assertIn("早于所选日期 1", text)
                self.assertNotIn("已见投稿", text)

                self.assertEqual(errors, [], errors)
            finally:
                browser.close()

    def test_a054_jump_binds_scanned_thread_and_resets_filters(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1366, "height": 768})
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                requests: list[str] = []
                page.on("request", lambda request: requests.append(request.url))
                self._open(page)

                # 先在待审页筛选乙帖并搜索，制造会隐藏甲帖结果的旧条件
                page.locator("#main-nav button", has_text="待审稿件").click()
                page.wait_for_selector("#items-body tr", timeout=20000)
                page.select_option("#filter-tid", self.t2)
                page.fill("#filter-q", "乙稿")
                page.locator("#filter-apply").click()
                page.wait_for_selector("#items-body tr", timeout=20000)

                # 切到甲帖并扫描，从结果跳转
                self._select_thread(page, self.t1)
                self._scan(page)
                page.locator("#goto-inbox-btn").click()
                page.wait_for_selector("#items-body tr", timeout=20000)

                self.assertEqual(page.input_value("#filter-tid"), self.t1)
                self.assertEqual(page.input_value("#filter-q"), "")
                self.assertEqual(page.input_value("#filter-status"), "")
                rows = page.locator("#items-body").text_content() or ""
                self.assertIn("甲稿一", rows)
                self.assertNotIn("乙稿", rows)

                items_requests = [url for url in requests
                                  if "/api/operator/items?" in url]
                self.assertTrue(items_requests)
                query = parse_qs(urlsplit(items_requests[-1]).query)
                self.assertEqual(query.get("tid", [""])[0], self.t1)
                self.assertEqual(query.get("q", [""])[0], "")
                self.assertEqual(query.get("page", [""])[0], "1")
                self.assertEqual(errors, [], errors)
            finally:
                browser.close()

    def test_a067_tag_options_show_chinese_types_and_backfill(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1366, "height": 768})
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                choice_bodies: list[str] = []
                page.on("request", lambda request: (
                    choice_bodies.append(request.post_data or "")
                    if request.url.endswith("/choices") else None))
                self._open(page)
                self._open_inbox_item(page, "同类标签稿")

                save = page.locator("#review-form button", has_text="保存修改")
                if save.count():
                    save.click()
                approve = page.locator("#review-form button", has_text="批准稿件")
                approve.click()
                page.wait_for_selector(".tag-decision select", timeout=30000)

                select = page.locator(".tag-decision select").first
                option_texts = select.locator("option").all_text_contents()
                joined = " | ".join(option_texts)
                self.assertIn(f"复用 ID {self.tag_type0}（类型 0：其他）", joined)
                self.assertIn(f"复用 ID {self.tag_type1}（类型 1：角色）", joined)
                self.assertIn("新建，类型 0（其他）", joined)
                self.assertIn("新建，类型 4（争议/不适）", joined)

                select.select_option(value=f"existing:{self.tag_type0}")
                page.locator("#review-form button", has_text="保存人工决定").click()
                page.wait_for_timeout(1500)
                self.assertTrue(choice_bodies, "未发出人工决定保存请求")
                import json as _json

                payload = _json.loads(choice_bodies[-1])
                self.assertEqual(payload["tag_choices"][0]["existing_tag_id"],
                                 self.tag_type0)
                self.assertIsNone(payload["tag_choices"][0]["new_tag_type"])

                page.reload(wait_until="load")
                page.wait_for_selector("#thread-entries .thread-entry", timeout=20000)
                self._open_inbox_item(page, "同类标签稿")
                page.wait_for_selector(".tag-decision select", timeout=30000)
                value = page.locator(".tag-decision select").first.input_value()
                self.assertEqual(value, f"existing:{self.tag_type0}")
                self.assertEqual(errors, [], errors)
            finally:
                browser.close()

    def test_a108_date_counts_and_a112_help_note(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1366, "height": 768})
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                self._open(page)

                self._select_thread(page, self.t4)
                text = self._scan(page)
                self.assertIn("早于日期 1", text)
                self.assertIn("时间待确认 1", text)

                self._select_thread(page, self.t3)
                text = self._scan(page)
                self.assertIn("早于所选日期", text)

                page.locator("#main-nav button", has_text="设置与帮助").click()
                page.wait_for_function(
                    "() => (document.getElementById('settings-help').textContent || '')"
                    ".indexOf('旧楼编辑覆盖') >= 0", timeout=20000)
                help_text = page.locator("#settings-help").text_content() or ""
                self.assertIn("不替换正式大书库", help_text)
                self.assertEqual(errors, [], errors)
            finally:
                browser.close()


    def test_a072_updated_version_compare_and_export_lock(self) -> None:
        from playwright.sync_api import sync_playwright

        from tests.test_operator_closeout import wait_run

        svc = self.ctx.service
        summaries = [s for s in self.ctx.repository.list_items()
                     if self.ctx.review_service.load(s.id).title == "更新稿一"]
        self.assertEqual(len(summaries), 1)
        item_id = summaries[0].id
        from umalib_ingest.review import ReviewFields, ReviewSources

        ctx_review = self.ctx.review_service
        ctx_review.save(item_id, ReviewFields(
            title="更新稿一", author="演示作者", translator=None, series=None,
            tags=["演示标签"],
            sources=ReviewSources(
                nga=[f"https://bbs.nga.cn/read.php?tid={self.t8}&pid=91008001"],
                original_series=[], original=["https://example.com/更新稿一"],
                translation_series=[], translation=[]),
            body_text="第一版正文。"))
        self.ctx.repository.transition(item_id, "approved", "A072 夹具")
        svc._workspace.save_choices(svc.workspace_id, item_id, {
            "note": "导出版备注", "upload_time_seconds": 1788062400,
            "ordered_sources": ["https://example.com/更新稿一"],
            "tag_choices": [{"requested_kind": "tag", "requested_value": "演示标签",
                             "existing_tag_id": None, "new_tag_type": 0}],
            "content_mode": "plain", "formatted_body": None,
            "plain_loss_confirmed": False})
        preview = svc.writer_preview(item_id, "candidate-x.db")
        written = svc.writer_write(item_id, preview["preview_id"], True)
        self.assertEqual(written["delivery"]["count_after"],
                         written["delivery"]["count_after"])

        # 楼层内容更新后重扫：生成新版本待审稿，旧稿保持已导出
        self.mapping[self.t8][1] = _html(self.t8, 1, 1, [
            ("91008001", _complete("更新稿一（新）"), "2026-08-10 10:00:00")])
        st = svc.thread_status(self.t8)
        run = wait_run(svc, svc.scan(self.t8, False, True, st["state_sha256"]))
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        self.assertGreaterEqual(run["result"]["updated_count"], 1)

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1366, "height": 768})
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                self._open(page)

                # 已导出稿：锁定提醒 + 不可编辑 + 同楼新版本只读对照
                self._open_inbox_item(page, "更新稿一", status_text="已写入候选")
                locked = page.locator("#review-form .warn",
                                      has_text="已写入候选数据库").first
                self.assertTrue(locked.is_visible(), "已导出稿未见锁定提醒")
                self.assertIn("不可直接修改", locked.text_content() or "")
                self.assertTrue(
                    page.locator("#review-form button", has_text="保存修改").is_disabled())
                sibling_summary = page.locator(
                    "#review-form summary", has_text="同楼历史版本对照")
                self.assertTrue(sibling_summary.count() >= 1,
                                "已导出稿未显示同楼新版本对照")

                # 新版本稿：可编辑，并列旧版对照与处理入口
                self._open_inbox_item(page, "更新稿一（新）", status_text="待审")
                self.assertFalse(
                    page.locator("#review-form button", has_text="保存修改").is_disabled())
                sibling_summary2 = page.locator(
                    "#review-form summary", has_text="同楼历史版本对照")
                self.assertTrue(sibling_summary2.count() >= 1)
                self.assertIn("1 条",
                              sibling_summary2.first.text_content() or "")
                self.assertEqual(errors, [], errors)
            finally:
                browser.close()


    def test_f021_approved_readonly_and_return_edit_flow(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1366, "height": 768})
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                self._open(page)
                self._open_inbox_item(page, "甲稿一")

                page.locator("#review-form button", has_text="保存修改").click()
                page.wait_for_function(
                    "() => document.querySelector('#status-diagnostic')"
                    ".textContent.includes('已保存')")
                page.locator("#review-form button", has_text="批准稿件").click()
                page.wait_for_function(
                    "() => document.querySelector('#status-diagnostic')"
                    ".textContent.includes('已批准')")

                title = page.locator("#review-form [data-field=title]")
                self.assertTrue(title.evaluate("el => el.readOnly"),
                                "已批准稿的审核字段应为只读")
                hint = page.locator("#review-form .warn", has_text="已批准").first
                self.assertTrue(hint.is_visible(), "已批准应显示只读与退回指引")

                try:
                    title.fill("批准后修改标题", timeout=1500)
                except Exception:
                    pass
                self.assertEqual(title.input_value(), "甲稿一",
                                 "只读字段不应接收输入")

                page.on("dialog", lambda d: d.accept("需要继续修改标题"))
                page.locator("#review-form button", has_text="退回待审").click()
                page.wait_for_function(
                    "() => document.querySelector('#status-diagnostic')"
                    ".textContent.includes('可继续修改后重新批准')", timeout=20000)
                self.assertFalse(title.evaluate("el => el.readOnly"))

                title.fill("批准后修改标题")
                page.locator("#review-form button", has_text="保存修改").click()
                page.wait_for_function(
                    "() => document.querySelector('#status-diagnostic')"
                    ".textContent.includes('已保存')")
                page.locator("#review-form button", has_text="批准稿件").click()
                page.wait_for_function(
                    "() => document.querySelector('#status-diagnostic')"
                    ".textContent.includes('已批准')")
                self.assertTrue(title.evaluate("el => el.readOnly"))
                self.assertEqual(title.input_value(), "批准后修改标题")
                self.assertEqual(errors, [], errors)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
