"""TASK-015 自主 debug：把 A 项需求中缺失的关键断言补成可执行用例。

覆盖：A015/A016/A017/A019/A022/A023/A024/A025/A026/A030/A033/A037/A039。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest

from tests.test_operator_closeout import (
    _Resp,
    _html,
    add_thread,
    init_confirm,
    make_factory,
    make_operator,
    wait_run,
)


def _submission(title: str) -> str:
    return f"[收录]\n[标题] {title}\n[作者] 合成作者\n[标签] 标签\n\n合成正文"


def _now_start() -> dict:
    return {"mode": "now", "page": None, "date": None,
            "timezone": "UTC+08:00", "lower_bound_utc": None}


class _MultiPage:
    """Dispatches fetches per tid with an optional per-page hook."""

    def __init__(self, mapping, hook=None):
        self._mapping = mapping
        self._hook = hook

    def goto(self, url, wait_until=None):
        tid = url.split("tid=")[1].split("&")[0]
        page = int(url.split("&page=")[1]) if "&page=" in url else 1
        if self._hook is not None:
            self._hook(tid, page)
        return _Resp(url, self._mapping[tid][page])

    def title(self):
        return "模拟"


class _MultiSession:
    def __init__(self, page):
        self.page = page

    def close(self):
        pass


class ThreadManagementAssertions(unittest.TestCase):
    def test_rename_and_toggle_keep_items_and_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            pages = {1: _html(tid, 1, 1, [
                ("91002001", _submission("改名稿"), "2026-08-10 10:00:00")])}
            ctx = make_operator(tmp, make_factory(tid, pages))
            try:
                svc = ctx.service
                add_thread(svc, tid)
                init_confirm(svc, tid, {"mode": "page", "page": 1, "date": None,
                                        "timezone": "UTC+08:00", "lower_bound_utc": None})
                st = svc.thread_status(tid)
                run = wait_run(svc, svc.scan(tid, False, True, st["state_sha256"]))
                self.assertEqual(run["status"], "succeeded", run.get("error"))
                items_before = len(ctx.repository.list_items())
                self.assertGreaterEqual(items_before, 1)
                state_path = svc.paths.state_json(svc.workspace_id, tid)
                state_sha = hashlib.sha256(state_path.read_bytes()).hexdigest()

                svc.patch_thread(tid, svc.list_threads()["revision"], name="改名后的帖子")
                entry = [e for e in svc.list_threads()["entries"] if e["tid"] == tid][0]
                self.assertEqual(entry["name"], "改名后的帖子")
                self.assertEqual(len(ctx.repository.list_items()), items_before)
                self.assertEqual(
                    hashlib.sha256(state_path.read_bytes()).hexdigest(), state_sha)

                svc.patch_thread(tid, svc.list_threads()["revision"], enabled=False)
                entry = [e for e in svc.list_threads()["entries"] if e["tid"] == tid][0]
                self.assertFalse(entry["enabled"])
                self.assertEqual(len(ctx.repository.list_items()), items_before)
                self.assertEqual(
                    hashlib.sha256(state_path.read_bytes()).hexdigest(), state_sha)

                svc.patch_thread(tid, svc.list_threads()["revision"], enabled=True)
                entry = [e for e in svc.list_threads()["entries"] if e["tid"] == tid][0]
                self.assertTrue(entry["enabled"])
            finally:
                ctx.shutdown()

    def test_per_thread_snapshots_and_run_logs_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid_a, tid_b = "12345678", "87654321"
            mapping = {
                tid_a: {1: _html(tid_a, 1, 1, [
                    ("91004001", _submission("甲帖稿"), "2026-08-10 10:00:00")])},
                tid_b: {1: _html(tid_b, 1, 1, [
                    ("91004002", _submission("乙帖稿"), "2026-08-10 10:00:00")])},
            }
            ctx = make_operator(tmp, lambda: _MultiSession(_MultiPage(mapping)))
            try:
                svc = ctx.service
                add_thread(svc, tid_a)
                add_thread(svc, tid_b)
                for tid in (tid_a, tid_b):
                    init_confirm(svc, tid, _now_start())
                    st = svc.thread_status(tid)
                    run = wait_run(svc, svc.scan(tid, False, True, st["state_sha256"]))
                    self.assertEqual(run["status"], "succeeded", run.get("error"))

                snap_a = svc.paths.snapshots_dir(svc.workspace_id, tid_a)
                snap_b = svc.paths.snapshots_dir(svc.workspace_id, tid_b)
                self.assertNotEqual(snap_a, snap_b)
                blob_a = b"".join(p.read_bytes() for p in snap_a.rglob("*") if p.is_file())
                blob_b = b"".join(p.read_bytes() for p in snap_b.rglob("*") if p.is_file())
                self.assertIn(b"91004001", blob_a)
                self.assertIn(b"91004002", blob_b)
                self.assertNotIn(b"91004002", blob_a)
                self.assertNotIn(b"91004001", blob_b)

                runs_a = list(svc.paths.runs_dir(svc.workspace_id, tid_a).glob("*.json"))
                runs_b = list(svc.paths.runs_dir(svc.workspace_id, tid_b).glob("*.json"))
                self.assertTrue(runs_a and runs_b)
                for path in runs_a:
                    self.assertEqual(
                        path.read_text(encoding="utf-8").find(tid_b), -1, path.name)
                for path in runs_b:
                    self.assertEqual(
                        path.read_text(encoding="utf-8").find(tid_a), -1, path.name)
            finally:
                ctx.shutdown()

    def test_late_old_preview_confirm_is_rejected(self) -> None:
        from umalib_ingest.operator.service import OperatorError

        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            pages = {1: _html(tid, 1, 1, [
                ("91005001", _submission("迟到确认稿"), "2026-08-10 10:00:00")])}
            ctx = make_operator(tmp, make_factory(tid, pages))
            try:
                svc = ctx.service
                add_thread(svc, tid)
                first = wait_run(svc, svc.initialize_preview(tid, _now_start(), 0, True))
                second = wait_run(svc, svc.initialize_preview(tid, _now_start(), 0, True))
                self.assertEqual(first["status"], "succeeded", first.get("error"))
                self.assertEqual(second["status"], "succeeded", second.get("error"))

                with self.assertRaises(OperatorError) as caught:
                    svc.initialize_confirm(
                        tid, first["run_id"], first["result"]["preview_sha256"], 0, True)
                self.assertEqual(caught.exception.code, "preview_superseded")
                self.assertFalse(
                    svc.paths.state_json(svc.workspace_id, tid).exists())

                svc.initialize_confirm(
                    tid, second["run_id"], second["result"]["preview_sha256"], 0, True)
                self.assertTrue(svc.paths.state_json(svc.workspace_id, tid).exists())
                svc.cancel(first["run_id"])
                self.assertEqual(svc.get_run(second["run_id"])["status"], "succeeded")
            finally:
                ctx.shutdown()


class CrossThreadIsolationAssertions(unittest.TestCase):
    def test_same_pid_and_body_across_threads_stay_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid_a, tid_b = "12345678", "87654321"
            text = "[收录]\n[标题] 同文稿\n[作者] 合成作者\n[标签] 标签\n\n完全相同的正文"
            mapping = {
                tid_a: {1: _html(tid_a, 1, 1, [("91006001", text, "2026-08-10 10:00:00")])},
                tid_b: {1: _html(tid_b, 1, 1, [("91006001", text, "2026-08-10 10:00:00")])},
            }
            ctx = make_operator(tmp, lambda: _MultiSession(_MultiPage(mapping)))
            try:
                svc = ctx.service
                add_thread(svc, tid_a)
                add_thread(svc, tid_b)
                for tid in (tid_a, tid_b):
                    init_confirm(svc, tid, {"mode": "page", "page": 1, "date": None,
                                            "timezone": "UTC+08:00",
                                            "lower_bound_utc": None})
                    st = svc.thread_status(tid)
                    run = wait_run(svc, svc.scan(tid, False, True, st["state_sha256"]))
                    self.assertEqual(run["status"], "succeeded", run.get("error"))

                summaries = ctx.repository.list_items()
                self.assertEqual(len(summaries), 2)
                origins = set()
                for summary in summaries:
                    item = ctx.repository.get(summary.id)
                    origins.add(item.original_draft["origin"]["thread_id"])
                self.assertEqual(origins, {tid_a, tid_b})
                self.assertEqual(len({s.id for s in summaries}), 2)
            finally:
                ctx.shutdown()

    def test_switching_active_thread_mid_scan_keeps_state_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid_a, tid_b = "12345678", "87654321"
            pages_a = {page: _html(tid_a, 3, page, [
                (f"91007{page:03d}", _submission(f"甲帖第{page}页稿"),
                 "2026-08-10 10:00:00")]) for page in (1, 2, 3)}
            pages_b = {1: _html(tid_b, 1, 1, [
                ("91008001", _submission("乙帖稿"), "2026-08-10 10:00:00")])}
            holder: dict = {}

            def hook(tid, page):
                if tid == tid_a and page == 2 and holder.get("svc") is not None:
                    svc = holder["svc"]
                    svc.patch_thread(tid_b, svc.list_threads()["revision"],
                                     make_active=True)

            mapping = {tid_a: pages_a, tid_b: pages_b}
            ctx = make_operator(
                tmp, lambda: _MultiSession(_MultiPage(mapping, hook=hook)))
            holder["svc"] = ctx.service
            try:
                svc = ctx.service
                add_thread(svc, tid_a)
                add_thread(svc, tid_b)
                init_confirm(svc, tid_a, {"mode": "page", "page": 1, "date": None,
                                          "timezone": "UTC+08:00",
                                          "lower_bound_utc": None})
                st = svc.thread_status(tid_a)
                run = wait_run(svc, svc.scan(tid_a, False, True, st["state_sha256"]))
                self.assertEqual(run["status"], "succeeded", run.get("error"))
                self.assertEqual(svc.list_threads()["active_tid"], tid_b)
                self.assertFalse(
                    svc.paths.state_json(svc.workspace_id, tid_b).exists())
                state = svc.thread_status(tid_a)["state"]
                self.assertEqual(state["frontier_page"], 3)
                titles = set()
                for summary in ctx.repository.list_items():
                    titles.add(ctx.review_service.load(summary.id).title)
                self.assertEqual(titles, {"甲帖第1页稿", "甲帖第2页稿", "甲帖第3页稿"})
            finally:
                ctx.shutdown()


class InitializationAssertions(unittest.TestCase):
    def test_single_page_scan_reuses_homepage_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            calls: list[int] = []
            session = make_factory(tid, {1: _html(tid, 1, 1, [
                ("91009001", _submission("单页稿"), "2026-08-10 10:00:00")])})()
            original = session.page.goto

            def goto(url, wait_until=None):
                calls.append(int(url.split("&page=")[1]) if "&page=" in url else 1)
                return original(url, wait_until=wait_until)

            session.page.goto = goto
            ctx = make_operator(tmp, lambda: session)
            try:
                svc = ctx.service
                add_thread(svc, tid)
                init_confirm(svc, tid, _now_start())
                calls.clear()
                st = svc.thread_status(tid)
                run = wait_run(svc, svc.scan(tid, False, True, st["state_sha256"]))
                self.assertEqual(run["status"], "succeeded", run.get("error"))
                self.assertEqual(calls, [1], "单页帖首页只应读取一次")
            finally:
                ctx.shutdown()

    def test_wrong_tid_init_fails_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            body = _html("99999999", 1, 1, [
                ("91010001", _submission("错帖稿"), "2026-08-10 10:00:00")])

            class _RedirectedPage:
                def goto(self, url, wait_until=None):
                    return _Resp("https://bbs.nga.cn/read.php?tid=99999999", body)

                def title(self):
                    return "模拟"

            class _RedirectedSession:
                def __init__(self):
                    self.page = _RedirectedPage()

                def close(self):
                    pass

            ctx = make_operator(tmp, lambda: _RedirectedSession())
            try:
                svc = ctx.service
                add_thread(svc, tid)
                run = wait_run(svc, svc.initialize_preview(tid, _now_start(), 0, True))
                self.assertEqual(run["status"], "failed")
                message = (run.get("error") or {}).get("message", "")
                self.assertIn("不一致", message, message)
                self.assertIn("99999999", message, message)
                self.assertFalse(
                    svc.paths.state_json(svc.workspace_id, tid).exists())
            finally:
                ctx.shutdown()

    def test_failed_init_keeps_existing_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            pages = {1: _html(tid, 2, 1, [
                ("91011001", _submission("旧稿"), "2026-08-10 10:00:00")])}
            ctx = make_operator(tmp, make_factory(tid, pages))
            try:
                svc = ctx.service
                add_thread(svc, tid)
                svc.manual_import(_submission("手工旧稿"))
                inbox = svc.paths.inbox_db(svc.workspace_id)
                before_sha = hashlib.sha256(inbox.read_bytes()).hexdigest()
                before_count = len(ctx.repository.list_items())

                run = wait_run(svc, svc.initialize_preview(tid, {
                    "mode": "page", "page": 5, "date": None,
                    "timezone": "UTC+08:00", "lower_bound_utc": None}, 0, True))
                self.assertEqual(run["status"], "failed", run)
                self.assertEqual(
                    hashlib.sha256(inbox.read_bytes()).hexdigest(), before_sha)
                self.assertEqual(len(ctx.repository.list_items()), before_count)
            finally:
                ctx.shutdown()

    def test_baseline_stores_tail_floors_with_real_page(self) -> None:
        from umalib_ingest.operator.state_v2 import read_state_file

        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            pages = {
                1: _html(tid, 3, 1, [("91012001", "首页讨论", "2026-08-10 10:00:00")]),
                2: _html(tid, 3, 2, [("91012002", "二页讨论", "2026-08-10 10:00:00")]),
                3: _html(tid, 3, 3, [("91012003", "尾页讨论", "2026-08-10 10:00:00")]),
            }
            ctx = make_operator(tmp, make_factory(tid, pages))
            try:
                svc = ctx.service
                add_thread(svc, tid)
                init_confirm(svc, tid, _now_start())
                state = read_state_file(
                    svc.paths.state_json(svc.workspace_id, tid))
                self.assertEqual(state.observed_last_page, 3)
                self.assertEqual(state.frontier_page, 3)
                self.assertEqual(state.next_scan_page, 3)
                self.assertEqual(set(state.known_floors), {"91012003"})
                self.assertEqual(
                    state.known_floors["91012003"]["last_seen_page"], 3)
            finally:
                ctx.shutdown()

    def test_page_mode_n_first_batch_includes_page_n_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            pages = {
                1: _html(tid, 3, 1, [("91013001", _submission("一页稿"), "2026-08-10 10:00:00")]),
                2: _html(tid, 3, 2, [("91013002", _submission("二页稿"), "2026-08-10 10:00:00")]),
                3: _html(tid, 3, 3, [("91013003", _submission("三页稿"), "2026-08-10 10:00:00")]),
            }
            ctx = make_operator(tmp, make_factory(tid, pages))
            try:
                svc = ctx.service
                add_thread(svc, tid)
                init_confirm(svc, tid, {"mode": "page", "page": 2, "date": None,
                                        "timezone": "UTC+08:00", "lower_bound_utc": None})
                state = svc.thread_status(tid)["state"]
                self.assertEqual(state["next_scan_page"], 2)
                self.assertNotIn("91013001", state["known_floors"])
                st = svc.thread_status(tid)
                run = wait_run(svc, svc.scan(tid, False, True, st["state_sha256"]))
                self.assertEqual(run["status"], "succeeded", run.get("error"))
                titles = set()
                for summary in ctx.repository.list_items():
                    titles.add(ctx.review_service.load(summary.id).title)
                self.assertEqual(titles, {"二页稿", "三页稿"})
                state2 = svc.thread_status(tid)["state"]
                self.assertNotIn("91013001", state2["known_floors"])
            finally:
                ctx.shutdown()

    def test_stale_or_duplicate_confirm_does_not_overwrite_state(self) -> None:
        from umalib_ingest.operator.service import OperatorError

        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            pages = {1: _html(tid, 1, 1, [
                ("91014001", _submission("确认稿"), "2026-08-10 10:00:00")])}
            ctx = make_operator(tmp, make_factory(tid, pages))
            try:
                svc = ctx.service
                add_thread(svc, tid)
                run = wait_run(svc, svc.initialize_preview(tid, _now_start(), 0, True))
                self.assertEqual(run["status"], "succeeded", run.get("error"))
                state_path = svc.paths.state_json(svc.workspace_id, tid)

                with self.assertRaises(OperatorError) as caught:
                    svc.initialize_confirm(tid, run["run_id"], "0" * 64, 0, True)
                self.assertEqual(caught.exception.code, "preview_stale")
                self.assertFalse(state_path.exists())

                svc.initialize_confirm(
                    tid, run["run_id"], run["result"]["preview_sha256"], 0, True)
                sha_after = hashlib.sha256(state_path.read_bytes()).hexdigest()

                with self.assertRaises(OperatorError) as caught:
                    svc.initialize_confirm(
                        tid, run["run_id"], run["result"]["preview_sha256"], 0, True)
                self.assertIn(caught.exception.code,
                              ("state_exists", "config_conflict", "preview_stale"))
                self.assertEqual(
                    hashlib.sha256(state_path.read_bytes()).hexdigest(), sha_after)
            finally:
                ctx.shutdown()


class SessionLifecycleAssertions(unittest.TestCase):
    def test_session_created_used_and_closed_on_one_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            events: list[tuple[str, int]] = []

            class _RecordingSession:
                def __init__(self, page):
                    self.page = page
                    events.append(("create", threading.get_ident()))

                def close(self):
                    events.append(("close", threading.get_ident()))

            class _RecordingPage:
                def __init__(self, pages):
                    self._pages = pages

                def goto(self, url, wait_until=None):
                    events.append(("goto", threading.get_ident()))
                    page = int(url.split("&page=")[1]) if "&page=" in url else 1
                    return _Resp(url, self._pages[page])

                def title(self):
                    return "模拟"

            pages = {1: _html(tid, 1, 1, [
                ("91015001", _submission("线程稿"), "2026-08-10 10:00:00")])}

            def factory():
                return _RecordingSession(_RecordingPage(pages))

            ctx = make_operator(tmp, factory)
            try:
                svc = ctx.service
                add_thread(svc, tid)
                init_confirm(svc, tid, _now_start())
                st = svc.thread_status(tid)
                run = wait_run(svc, svc.scan(tid, False, True, st["state_sha256"]))
                self.assertEqual(run["status"], "succeeded", run.get("error"))
                kinds = {kind for kind, _ in events}
                self.assertEqual(kinds, {"create", "goto", "close"})
                threads = {ident for _, ident in events}
                self.assertEqual(len(threads), 1,
                                 f"session crossed threads: {events}")
            finally:
                ctx.shutdown()


class LoginDiagnosticsAssertions(unittest.TestCase):
    def test_expired_challenge_and_network_are_reported_separately(self) -> None:
        def forbidden_factory(tid):
            return make_factory(tid, {1: {"body": _html(tid, 1, 1, []), "status": 403}})

        def captcha_factory(tid):
            body = ("<html><head><title>验证</title></head><body>"
                    "<div class='captcha'>请完成验证码</div></body></html>").encode("utf-8")
            return make_factory(tid, {1: {"body": body, "status": 200}})

        def broken_factory(tid):
            class _Broken:
                def goto(self, url, wait_until=None):
                    raise RuntimeError("network down")

                def title(self):
                    return "模拟"

            class _BrokenSession:
                def __init__(self):
                    self.page = _Broken()

                def close(self):
                    pass

            return lambda: _BrokenSession()

        results = {}
        for name, factory in (("forbidden", forbidden_factory),
                              ("captcha", captcha_factory),
                              ("network", broken_factory)):
            with tempfile.TemporaryDirectory() as tmp:
                tid = "12345678"
                ctx = make_operator(tmp, factory(tid))
                try:
                    svc = ctx.service
                    add_thread(svc, tid)
                    run = wait_run(svc, svc.login(tid, True))
                    self.assertNotEqual(run["status"], "succeeded", (name, run))
                    self.assertFalse(
                        (run.get("result") or {}).get("login_verified", False))
                    results[name] = (run.get("error") or {}).get("message", "")
                finally:
                    ctx.shutdown()

        self.assertIn("thread_forbidden", results["forbidden"])
        self.assertIn("captcha_or_challenge", results["captcha"])
        self.assertIn("network down", results["network"])
        self.assertEqual(len(set(results.values())), 3,
                         "三种故障必须给出互不相同的说明")


class SourceSwitchAssertions(unittest.TestCase):
    def test_changing_source_keeps_old_workspace_head(self) -> None:
        from pathlib import Path

        from umalib_ingest.demo.samples import create_synthetic_base
        from umalib_ingest.operator.app_factory import create_operator_app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_a = root / "source-a.db"
            source_b = root / "source-b.db"
            create_synthetic_base(source_a)
            create_synthetic_base(source_b)
            ctx = create_operator_app(root=root / "prod", new_workspace=True,
                                      nav_delay=0.0)
            try:
                svc = ctx.service
                verified = svc.verify_source(source_a)
                svc.bind_source(verified["source_id"], "甲区", True)
                ws_a = svc.workspace_id
                manifest_path = (ctx.workspace_service.paths.workspace_dir(ws_a)
                                 / "workspace.json")
                manifest_before = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                head_before = ctx.workspace_service.resolve_head(ws_a)[1].resolve()

                verified_b = svc.verify_source(source_b)
                created = svc.create_workspace_from_source(
                    verified_b["source_id"], "乙区", True)
                ws_b = created["workspace_id"]
                self.assertNotEqual(ws_b, ws_a)

                manifest_after = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                head_after = ctx.workspace_service.resolve_head(ws_a)[1].resolve()
                self.assertEqual(manifest_after, manifest_before, "旧区 manifest 不应被换源改写")
                self.assertEqual(head_after, head_before, "旧区 HEAD 不应换成新源祖先")
                binding = json.loads(
                    ctx.service.paths.source_binding(ws_b).read_text(encoding="utf-8"))
                self.assertEqual(binding["sha256"], verified_b["sha256"],
                                 "新区绑定必须是新副本")
                self.assertEqual(Path(binding["path"]).resolve(), source_b.resolve())
            finally:
                ctx.shutdown()

            reopened = create_operator_app(root=root / "prod", nav_delay=0.0)
            try:
                self.assertEqual(reopened.service.workspace_id, ws_b)
                reopened_head = reopened.workspace_service.resolve_head(
                    reopened.service.workspace_id)[1].resolve()
                self.assertEqual(reopened_head, source_b.resolve(),
                                 "重开进入新区后 HEAD 必须指向新副本")
            finally:
                reopened.shutdown()


class RestartStateAssertions(unittest.TestCase):
    def test_restart_restores_active_thread_and_enabled_state(self) -> None:
        from pathlib import Path

        from umalib_ingest.operator.app_factory import create_operator_app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "prod"
            ctx = create_operator_app(root=root, new_workspace=True, nav_delay=0.0)
            try:
                svc = ctx.service
                add_thread(svc, "12345678")
                add_thread(svc, "87654321")
                svc.patch_thread("87654321", svc.list_threads()["revision"],
                                 enabled=False, make_active=True)
            finally:
                ctx.shutdown()

            reopened = create_operator_app(root=root, nav_delay=0.0)
            try:
                threads = reopened.service.list_threads()
                self.assertEqual(threads["active_tid"], "87654321")
                by_tid = {entry["tid"]: entry for entry in threads["entries"]}
                self.assertFalse(by_tid["87654321"]["enabled"])
                self.assertTrue(by_tid["12345678"]["enabled"])
            finally:
                reopened.shutdown()


class OfflineEditingAssertions(unittest.TestCase):
    def test_manual_edit_still_works_after_network_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"

            class _BrokenPage:
                def goto(self, url, wait_until=None):
                    raise RuntimeError("network down")

                def title(self):
                    return "模拟"

            class _BrokenSession:
                def __init__(self):
                    self.page = _BrokenPage()

                def close(self):
                    pass

            ctx = make_operator(tmp, lambda: _BrokenSession())
            try:
                svc = ctx.service
                add_thread(svc, tid)
                item = svc.manual_import(_submission("离线稿"))
                run = wait_run(svc, svc.login(tid, True))
                self.assertEqual(run["status"], "failed", run)

                from umalib_ingest.review import ReviewFields, ReviewSources

                ctx.review_service.save(item["id"], ReviewFields(
                    title="离线修改后的标题", author="离线作者", translator=None,
                    series=None, tags=["标签"],
                    sources=ReviewSources(nga=[], original_series=[],
                                          original=["https://example.com/离线"],
                                          translation_series=[], translation=[]),
                    body_text="离线修改后的正文"))
                ctx.repository.transition(item["id"], "approved", "离线审核")
                self.assertEqual(ctx.review_service.load(item["id"]).title,
                                 "离线修改后的标题")
            finally:
                ctx.shutdown()


class PosterAuthorAssertions(unittest.TestCase):
    def test_poster_name_does_not_override_author_field(self) -> None:
        import tests.test_operator_closeout as closeout

        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            submission = ("[收录]\n[标题] 帖子稿\n[作者] 原作者乙\n[标签] 标签\n\n正文")

            def floor_with_poster(pid, content, posted="2026-08-10 10:00:00"):
                esc = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                return (f'<tr id="post1strow{pid}"><td><a id="pid{pid}Anchor"></a>'
                        f'<a class="postauthor" id="postauthor{pid}">发帖者甲</a>'
                        f'<span class="postdate" id="postdate{pid}">{posted}</span>'
                        f'<span class="postcontent" id="postcontent{pid}">{esc}</span></td></tr>')

            original_floor = closeout._floor
            closeout._floor = floor_with_poster
            try:
                pages = {1: closeout._html(tid, 1, 1, [
                    ("91016001", submission, "2026-08-10 10:00:00")])}
            finally:
                closeout._floor = original_floor

            ctx = make_operator(tmp, make_factory(tid, pages))
            try:
                svc = ctx.service
                add_thread(svc, tid)
                init_confirm(svc, tid, {"mode": "page", "page": 1, "date": None,
                                        "timezone": "UTC+08:00",
                                        "lower_bound_utc": None})
                st = svc.thread_status(tid)
                run = wait_run(svc, svc.scan(tid, False, True, st["state_sha256"]))
                self.assertEqual(run["status"], "succeeded", run.get("error"))
                summaries = ctx.repository.list_items()
                self.assertEqual(len(summaries), 1)
                fields = ctx.review_service.load(summaries[0].id)
                self.assertEqual(fields.author, "原作者乙")
                self.assertNotIn("发帖者甲", fields.author)
            finally:
                ctx.shutdown()


class UnsupportedFormatAssertions(unittest.TestCase):
    def test_attachment_format_preserved_with_warning(self) -> None:
        from umalib_ingest.formatting.ubb import parse_basic_ubb

        result = parse_basic_ubb("前[attach]12345[/attach]后")
        codes = [d.code for d in result.diagnostics]
        self.assertIn("unsupported_format", codes)
        self.assertIn("[attach]", result.plain_text)

    def test_table_format_preserved_with_warning(self) -> None:
        from umalib_ingest.formatting.ubb import parse_basic_ubb

        result = parse_basic_ubb("前[table][tr][td]格[/td][/tr][/table]后")
        codes = [d.code for d in result.diagnostics]
        self.assertIn("unsupported_format", codes)
        self.assertIn("[table]", result.plain_text)


class DateRescanResilienceAssertions(unittest.TestCase):
    def test_date_rescan_cancel_then_resume_keeps_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            pages = {page: _html(tid, 5, page, [
                (f"91017{page:03d}", _submission(f"日期稿{page}"),
                 "2026-08-10 10:00:00")]) for page in (1, 2, 3, 4, 5)}
            calls: list[int] = []
            holder: dict = {}

            def hook(tid_name, page):
                calls.append(page)
                if page == 3 and not holder.get("cancelled"):
                    holder["cancelled"] = True
                    svc = holder["svc"]
                    svc.cancel(svc._jobs.active_run().run_id)

            ctx = make_operator(
                tmp, lambda: _MultiSession(_MultiPage({tid: pages}, hook=hook)))
            holder["svc"] = ctx.service
            try:
                svc = ctx.service
                add_thread(svc, tid)
                init_confirm(svc, tid, {"mode": "date", "page": None,
                                        "date": "2026-08-01",
                                        "timezone": "UTC+08:00",
                                        "lower_bound_utc": None})
                calls.clear()
                st = svc.thread_status(tid)
                first = wait_run(svc, svc.rescan(tid, {
                    "confirmed": True, "kind": "date",
                    "lower_bound_date": "2026-08-01",
                    "expected_state_sha256": st["state_sha256"]}))
                self.assertEqual(first["status"], "cancelled", first)
                state = svc.thread_status(tid)["state"]
                active = state["active_rescan"]
                self.assertIsNotNone(active)
                self.assertEqual(active["kind"], "date")
                self.assertEqual(active["next_page"], 3)

                st2 = svc.thread_status(tid)
                second = wait_run(svc, svc.rescan(tid, {
                    "confirmed": True, "kind": "date",
                    "lower_bound_date": "2026-08-01",
                    "expected_state_sha256": st2["state_sha256"]}))
                self.assertEqual(second["status"], "succeeded", second)
                self.assertEqual(calls, [1, 2, 3, 3, 4, 5])
                final_state = svc.thread_status(tid)["state"]
                self.assertIsNone(final_state["active_rescan"])
                self.assertEqual(final_state["next_scan_page"],
                                 state["next_scan_page"])
            finally:
                ctx.shutdown()


class PostedTimeTraceabilityAssertions(unittest.TestCase):
    def test_imported_floor_keeps_original_posted_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            pages = {1: _html(tid, 1, 1, [
                ("91018001", _submission("原时间稿"), "2026-08-10 09:30:00")])}
            ctx = make_operator(tmp, make_factory(tid, pages))
            try:
                svc = ctx.service
                add_thread(svc, tid)
                init_confirm(svc, tid, {"mode": "page", "page": 1, "date": None,
                                        "timezone": "UTC+08:00",
                                        "lower_bound_utc": None})
                st = svc.thread_status(tid)
                run = wait_run(svc, svc.scan(tid, False, True, st["state_sha256"]))
                self.assertEqual(run["status"], "succeeded", run.get("error"))
                summaries = ctx.repository.list_items()
                self.assertEqual(len(summaries), 1)
                item = ctx.repository.get(summaries[0].id)
                recorded = item.original_draft["raw_fields"].get("楼层发表时间")
                self.assertEqual(recorded, ["2026-08-10 09:30:00"])
                metadata = item.original_draft["metadata"]
                self.assertIsNone(metadata.get("upload_time"))
                self.assertNotIn("upload_time_seconds", item.original_draft)
            finally:
                ctx.shutdown()


class PinnedAndQuotedDateAssertions(unittest.TestCase):
    def test_pinned_and_quoted_dates_do_not_skip_pages(self) -> None:
        import tests.test_operator_closeout as closeout

        with tempfile.TemporaryDirectory() as tmp:
            tid = "12345678"
            sticky = (
                '<tr class="topicrow" id="topicrow9"><td>'
                '<span class="postdate" id="postdate99999999">2025-01-01 00:00:00</span>'
                '<span class="postcontent" id="postcontent99999999">'
                "[收录]\\n[标题] 置顶稿\\n[作者] 作者\\n[标签] 标签\\n\\n置顶正文</span>"
                "</td></tr>").encode("utf-8")
            page1 = closeout._html(tid, 3, 1, [
                ("91019001", _submission("七月稿"), "2026-07-15 10:00:00")])
            page1 = page1.replace(b"</body>", sticky + b"</body>")
            pages = {
                1: page1,
                2: closeout._html(tid, 3, 2, [
                    ("91019002",
                     "[收录]\n[标题] 引用稿\n[作者] 作者\n[标签] 标签\n\n"
                     "[quote]2020-01-01 00:00:00 引用块内日期[/quote]\n正文",
                     "2026-08-05 10:00:00")]),
                3: closeout._html(tid, 3, 3, [
                    ("91019003", _submission("八月稿"), "2026-08-06 10:00:00")]),
            }
            calls: list[int] = []
            session = make_factory(tid, pages)()
            original = session.page.goto

            def goto(url, wait_until=None):
                calls.append(int(url.split("&page=")[1]) if "&page=" in url else 1)
                return original(url, wait_until=wait_until)

            session.page.goto = goto
            ctx = make_operator(tmp, lambda: session)
            try:
                svc = ctx.service
                add_thread(svc, tid)
                init_confirm(svc, tid, {"mode": "date", "page": None,
                                        "date": "2026-08-01",
                                        "timezone": "UTC+08:00",
                                        "lower_bound_utc": None})
                calls.clear()
                st = svc.thread_status(tid)
                run = wait_run(svc, svc.scan(tid, False, True, st["state_sha256"]))
                self.assertEqual(run["status"], "succeeded", run.get("error"))
                self.assertEqual(calls, [1, 2, 3], "日期筛选不得跳页")
                titles = set()
                for summary in ctx.repository.list_items():
                    titles.add(ctx.review_service.load(summary.id).title)
                self.assertEqual(titles, {"引用稿", "八月稿"})
                state = svc.thread_status(tid)["state"]
                self.assertIn("91019001", state["date_exclusions"])
                self.assertNotIn("99999999", state["known_floors"])
            finally:
                ctx.shutdown()


if __name__ == "__main__":
    unittest.main()
