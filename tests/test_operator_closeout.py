"""TASK-015 收尾：R01—R07 红灯回归（正确预期）。"""

from __future__ import annotations

import datetime
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


# ---- 字节级假 transport ----

class _Resp:
    def __init__(self, url, body, status=200):
        self.url = url
        self.status = status
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self._body = body

    def body(self):
        return self._body


class _Page:
    def __init__(self, tid, pages):
        self._pages = pages

    def goto(self, url, wait_until=None):
        page = 1
        if "&page=" in url:
            page = int(url.split("&page=")[1])
        entry = self._pages.get(page)
        if entry is None:
            raise RuntimeError(f"no page {page}")
        if isinstance(entry, dict):
            body = entry.get("body", b"")
            status = entry.get("status", 200)
        else:
            body = entry
            status = 200
        return _Resp(url, body, status=status)

    def title(self):
        return "模拟帖子"


class _Session:
    def __init__(self, page):
        self.page = page

    def close(self):
        pass


def _floor(pid, content, posted="2026-08-10 10:00:00"):
    esc = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (f'<tr id="post1strow{pid}"><td><a id="pid{pid}Anchor"></a>'
            f'<span class="postdate" id="postdate{pid}">{posted}</span>'
            f'<span class="postcontent" id="postcontent{pid}">{esc}</span></td></tr>')


def _html(tid, last_page, page, floors, status=200):
    head = (f"<script>var __PAGE = {{0:'/read.php?tid={tid}',1:{last_page},2:1,3:20}};"
            "commonui.pageBtn(document.getElementById('pageBtnHere').parentNode,__PAGE,true)</script>")
    return (f"<html><head><title>模拟</title></head><body>{head}"
            + "".join(_floor(p, c, d) for p, c, d in floors)
            + "</body></html>").encode("utf-8")


def make_factory(tid, pages):
    def factory():
        return _Session(_Page(tid, pages))
    return factory


def make_operator(tmp, session_factory, nav_delay=0.0):
    from umalib_ingest.operator.app_factory import create_operator_app

    return create_operator_app(root=Path(tmp), new_workspace=True,
                               session_factory=session_factory, nav_delay=nav_delay)


def add_thread(svc, tid):
    revision = svc.list_threads()["revision"]
    svc.add_thread(f"https://bbs.nga.cn/read.php?tid={tid}", None, revision)


def init_confirm(svc, tid, start):
    run_id = svc.initialize_preview(tid, start, 0, True)
    for _ in range(100):
        run = svc.get_run(run_id)
        if run["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert run["status"] == "succeeded", run.get("error")
    svc.initialize_confirm(tid, run_id, run["result"]["preview_sha256"], 0, True)
    return svc.thread_status(tid)


def wait_run(svc, run_id):
    for _ in range(200):
        run = svc.get_run(run_id)
        if run["status"] in ("succeeded", "partial", "cancelled", "failed",
                             "interrupted", "awaiting_login"):
            return run
        time.sleep(0.05)
    return svc.get_run(run_id)


class R07ParserBodyStartTests(unittest.TestCase):
    def test_ubb_first_paragraph_is_body_not_unknown_field(self) -> None:
        from umalib_ingest.parser import parse_submission

        raw = "[收录]\n[标题] 标题甲\n[作者] 作者甲\n[标签] 标签甲\n\n[b]首段正文[/b]"
        draft = parse_submission(raw)
        self.assertEqual(draft.content.body_text, "[b]首段正文[/b]")
        self.assertEqual(draft.diagnostics.unknown_fields, [])
        self.assertEqual(draft.content.raw_text, raw)

    def test_color_first_paragraph_is_body(self) -> None:
        from umalib_ingest.parser import parse_submission

        raw = "[收录]\n[标题] 标题甲\n[作者] 作者甲\n[标签] 标签甲\n[color=#3366ff]彩色[/color]"
        draft = parse_submission(raw)
        self.assertEqual(draft.content.body_text, "[color=#3366ff]彩色[/color]")

    def test_unknown_metadata_still_diagnosed(self) -> None:
        from umalib_ingest.parser import parse_submission

        raw = "[收录]\n[标题] 标题甲\n[作者] 作者甲\n[标签] 标签甲\n[出处] 某处\n\n正文。"
        draft = parse_submission(raw)
        self.assertEqual([u.name for u in draft.diagnostics.unknown_fields], ["出处"])
        self.assertEqual(draft.content.body_text, "正文。")


class R01PageRescanCursorTests(unittest.TestCase):
    def test_page_rescan_does_not_corrupt_daily_cursor(self) -> None:
        from umalib_ingest.operator.state_v2 import read_state_file

        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            pages = {
                1: _html(tid, 2, 1, [("91001001", "普通讨论", "2026-08-10 10:00:00")]),
                2: _html(tid, 2, 2, [("91001002", "普通讨论", "2026-08-11 10:00:00")]),
            }
            ctx = make_operator(tmp, make_factory(tid, pages))
            try:
                svc = ctx.service
                add_thread(svc, tid)
                init_confirm(svc, tid, {"mode": "page", "page": 1, "date": None,
                                        "timezone": "UTC+08:00", "lower_bound_utc": None})
                # 日常扫到第 2 页
                st = svc.thread_status(tid)
                scan = svc.scan(tid, False, True, st["state_sha256"])
                run = wait_run(svc, scan)
                self.assertEqual(run["status"], "succeeded", run.get("error"))
                before = read_state_file(ctx.service.paths.state_json(svc.workspace_id, tid))
                self.assertEqual(before.frontier_page, 2)
                # 补查第 1 页
                st2 = svc.thread_status(tid)
                rescan = svc.rescan(tid, {"confirmed": True, "kind": "pages",
                                          "from_page": 1, "to_page": 1,
                                          "expected_state_sha256": st2["state_sha256"]})
                run2 = wait_run(svc, rescan)
                self.assertEqual(run2["status"], "succeeded", run2.get("error"))
                after = read_state_file(ctx.service.paths.state_json(svc.workspace_id, tid))
                # 日常游标不被补查破坏
                self.assertEqual(after.frontier_page, 2)
                self.assertEqual(after.next_scan_page, 2)
                # 状态仍可读（读本身不抛异常）
                self.assertEqual(after.observed_last_page, 2)
            finally:
                ctx.shutdown()


class R02EarlierDateRescanTests(unittest.TestCase):
    def test_earlier_date_rescan_recovers_excluded_floor(self) -> None:
        from umalib_ingest.operator.state_v2 import read_state_file

        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            pages = {
                1: _html(tid, 2, 1, [
                    ("91001001", "[收录]\n[标题] 七月稿\n[作者] 甲\n[标签] 标签\n\n七月。",
                     "2026-07-30 10:00:00")]),
                2: _html(tid, 2, 2, [
                    ("91001002", "[收录]\n[标题] 八月稿\n[作者] 甲\n[标签] 标签\n\n八月。",
                     "2026-08-02 10:00:00")]),
            }
            ctx = make_operator(tmp, make_factory(tid, pages))
            try:
                svc = ctx.service
                add_thread(svc, tid)
                init_confirm(svc, tid, {"mode": "date", "page": None, "date": "2026-08-01",
                                        "timezone": "UTC+08:00",
                                        "lower_bound_utc": "2026-07-31T16:00:00+00:00"})
                st = svc.thread_status(tid)
                scan = svc.scan(tid, False, True, st["state_sha256"])
                run = wait_run(svc, scan)
                self.assertEqual(run["status"], "succeeded", run.get("error"))
                state = read_state_file(ctx.service.paths.state_json(svc.workspace_id, tid))
                # 七月稿被排除
                self.assertIn("91001001", state.date_exclusions)
                # 改为 7 月补查：必须从第 1 页开始覆盖，补入七月稿
                st2 = svc.thread_status(tid)
                rescan = svc.rescan(tid, {"confirmed": True, "kind": "date",
                                          "lower_bound_date": "2026-07-01",
                                          "expected_state_sha256": st2["state_sha256"]})
                run2 = wait_run(svc, rescan)
                self.assertEqual(run2["status"], "succeeded", run2.get("error"))
                self.assertGreaterEqual(run2["result"]["imported_count"], 1,
                                        run2["result"])
                state2 = read_state_file(ctx.service.paths.state_json(svc.workspace_id, tid))
                self.assertNotIn("91001001", state2.date_exclusions)
                # 八月稿不重复入队
                items = [s for s in ctx.repository.list_items()]
                pids = {(i.original_draft.get("origin") or {}).get("post_id")
                        for i in [ctx.repository.get(s.id) for s in items] if i}
                self.assertEqual(pids, {"91001001", "91001002"})
            finally:
                ctx.shutdown()


class R03LoginTruthTests(unittest.TestCase):
    def test_forbidden_login_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            pages = {1: {"body": _html(tid, 1, 1, []), "status": 403}}
            ctx = make_operator(tmp, make_factory(tid, pages))
            try:
                svc = ctx.service
                add_thread(svc, tid)
                run_id = svc.login(tid, True)
                run = wait_run(svc, run_id)
                self.assertNotEqual(run["status"], "succeeded", run)
                self.assertFalse((run.get("result") or {}).get("login_verified", False))
                self.assertIsNotNone(run.get("error"))
            finally:
                ctx.shutdown()

    def test_captcha_login_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            body = ("<html><head><title>验证</title></head><body>"
                    "<div class='captcha'>请完成验证码</div></body></html>").encode("utf-8")
            pages = {1: {"body": body, "status": 200}}
            ctx = make_operator(tmp, make_factory(tid, pages))
            try:
                svc = ctx.service
                add_thread(svc, tid)
                run = wait_run(svc, svc.login(tid, True))
                self.assertNotEqual(run["status"], "succeeded", run)
                self.assertFalse((run.get("result") or {}).get("login_verified", False))
            finally:
                ctx.shutdown()


class R04CrossThreadConfirmTests(unittest.TestCase):
    def test_cannot_confirm_other_thread_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid_a = "12345678"
            tid_b = "87654321"
            pages = {1: _html(tid_a, 1, 1, [
                ("91001001", "普通讨论", "2026-08-10 10:00:00")])}
            ctx = make_operator(tmp, make_factory(tid_a, pages))
            try:
                svc = ctx.service
                add_thread(svc, tid_a)
                add_thread(svc, tid_b)
                run_id = svc.initialize_preview(tid_a, {"mode": "now", "page": None,
                                                        "date": None, "timezone": "UTC+08:00",
                                                        "lower_bound_utc": None}, 0, True)
                run = wait_run(svc, run_id)
                self.assertEqual(run["status"], "succeeded", run.get("error"))
                from umalib_ingest.operator.service import OperatorError

                with self.assertRaises(OperatorError):
                    svc.initialize_confirm(tid_b, run_id,
                                           run["result"]["preview_sha256"], 0, True)
                # B 帖 config/state 未被写入
                self.assertFalse(ctx.service.paths.state_json(svc.workspace_id, tid_b).exists())
            finally:
                ctx.shutdown()


class R05MaintenanceImportTests(unittest.TestCase):
    def test_import_rejected_during_maintenance(self) -> None:
        from umalib_ingest.operator.service import OperatorError

        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_operator(tmp, lambda: None)
            try:
                svc = ctx.service
                ctx.workspace_service.coordinator().enter_maintenance()
                try:
                    with self.assertRaises(OperatorError) as caught:
                        svc.manual_import("[收录]\n[标题] 标题\n[作者] 作者\n[标签] 标签\n\n正文。")
                    self.assertIn(caught.exception.code, ("maintenance_active", "workspace_busy"))
                finally:
                    ctx.workspace_service.coordinator().leave_maintenance()
                # 维护结束后可导入
                item = svc.manual_import("[收录]\n[标题] 标题\n[作者] 作者\n[标签] 标签\n\n正文。")
                self.assertGreater(item["id"], 0)
            finally:
                ctx.shutdown()


if __name__ == "__main__":
    unittest.main()
