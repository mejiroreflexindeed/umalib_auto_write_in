"""TASK-011R2+012A 发布候选验收测试（门 A 红灯 → 门 B 绿灯）。

所有测试离线：fake response/session/runner、临时 state/Inbox，不打开真实 Edge。
"""

from __future__ import annotations

import http.client
import json
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from umalib_ingest.nga_access.errors import NgaAccessError
from umalib_ingest.nga_access.fetch import FetchedThreadPage, fetch_thread_page
from umalib_ingest.nga_access.parsing import FloorSnapshot
from umalib_ingest.watcher.result import ImportedItem, WatcherRunResult
from umalib_ingest.watcher.scan import run_scan
from umalib_ingest.watcher.state import (
    PRIVATE_STATE_ROOT,
    WatcherConfig,
    WatcherState,
    atomic_write_json,
    state_file_sha256,
)
from umalib_ingest.web.watcher_ui import WatcherUiConfig, WatcherUiService

T0 = "2026-08-31T14:39:29.509386+00:00"


def make_state(*, last_page: int = 173) -> WatcherState:
    return WatcherState(
        schema_version="1",
        tid=30099686,
        canonical_thread_url="https://bbs.nga.cn/read.php?tid=30099686",
        last_page=last_page,
        known_floors={},
        baseline_created_at=T0,
        last_successful_scan_at=None,
        last_result_summary=None,
    )


def make_floor(pid: str, content: str) -> FloorSnapshot:
    return FloorSnapshot(
        pid=pid, floor_number=None, author_present=True,
        posted_at_raw="2026-08-31 19:00", edited_at_raw=None,
        content_format="ubbcode", content_text=content, content_raw_fragment=content,
    )


def make_fetched(page: int, last_page: int, floors=(), diagnostics=(),
                 status: int = 200, url: str | None = None) -> FetchedThreadPage:
    return FetchedThreadPage(
        tid=30099686, page=page, last_page=last_page, status=status,
        final_url=url or f"https://bbs.nga.cn/read.php?tid=30099686&page={page}",
        content_type="text/html; charset=utf-8", encoding="utf-8",
        body_sha256="ab" * 32, body_bytes=0, snapshot_outcome="written",
        floors=tuple(floors), diagnostics=tuple(diagnostics),
    )


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.close_count = 0

    def close(self):
        self.close_count += 1


class ReleaseAcceptanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        PRIVATE_STATE_ROOT.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=str(PRIVATE_STATE_ROOT), prefix="nga-r2-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state_file = self.tmp / "watcher-state.json"
        self.inbox_db = self.tmp / "inbox.db"
        self.snapshot_dir = self.tmp / "snaps"
        self.profile_dir = self.tmp / "profile"
        atomic_write_json(self.state_file, make_state().to_dict())

    def config(self, **kwargs) -> WatcherConfig:
        return WatcherConfig(
            state_file=kwargs.pop("state_file", self.state_file),
            profile_dir=kwargs.pop("profile_dir", self.profile_dir),
            snapshot_dir=kwargs.pop("snapshot_dir", self.snapshot_dir),
            inbox_db=kwargs.pop("inbox_db", self.inbox_db),
            navigation_delay_seconds=kwargs.pop("navigation_delay_seconds", 3.0),
            max_scan_pages=kwargs.pop("max_scan_pages", 12),
        )


class F1LoginRedirectTests(ReleaseAcceptanceTestCase):
    """官方登录重定向必须得到 manual_login_required，而不是 unexpected_redirect."""

    def _fake_session(self, final_url, title, body, status=200):
        class FakeResponse:
            def __init__(self):
                self.headers = {"content-type": "text/html; charset=utf-8"}
                self.url = final_url
                self.status = status

            def body(self):
                return body

        class FakePage:
            def goto(self, url, wait_until=None):
                return FakeResponse()

            def title(self):
                return title

        class FakeContext:
            pages = []

        class FakeSession:
            def __init__(self):
                self.page = FakePage()
                self._context = FakeContext()

            def close(self):
                pass

        return FakeSession()

    def test_official_login_redirect_is_manual_login_required(self) -> None:
        session = self._fake_session(
            "https://bbs.nga.cn/login.php", "NGA 登录",
            b"<html><title>NGA login</title></html>",
        )
        fetched = fetch_thread_page(session, 30099686, 1, self.snapshot_dir)
        self.assertIn("manual_login_required", fetched.diagnostics)
        self.assertNotIn("unexpected_redirect", fetched.diagnostics)
        self.assertEqual(fetched.snapshot_outcome, "skipped")
        self.assertFalse(
            (self.snapshot_dir / "tid-30099686" / "page-1.response.bin").exists()
        )
        self.assertTrue(
            (self.snapshot_dir / "tid-30099686" / "page-1.diag.json").exists()
        )

    def test_official_host_non_login_path_still_blocked(self) -> None:
        session = self._fake_session(
            "https://bbs.nga.cn/other.php", "任意页面",
            b"<html>other</html>",
        )
        with self.assertRaises(NgaAccessError) as caught:
            fetch_thread_page(session, 30099686, 1, self.snapshot_dir)
        self.assertEqual(caught.exception.code, "unexpected_redirect")
        self.assertFalse(
            self.snapshot_dir.exists() and any(self.snapshot_dir.iterdir())
        )

    def test_external_http_and_wrong_tid_still_unexpected_redirect(self) -> None:
        for url in (
            "https://evil.example.com/login.php",
            "http://bbs.nga.cn/login.php",
            "https://bbs.nga.cn/read.php?tid=1",
        ):
            with self.subTest(url=url):
                session = self._fake_session(url, "登录", b"<html>x</html>")
                with self.assertRaises(NgaAccessError) as caught:
                    fetch_thread_page(session, 30099686, 1, self.snapshot_dir)
                self.assertEqual(caught.exception.code, "unexpected_redirect")
                self.assertFalse(
                    self.snapshot_dir.exists() and any(self.snapshot_dir.iterdir())
                )

    def test_watcher_login_flow_confirm_once_then_stable_login_required(self) -> None:
        login_page = make_fetched(
            1, 173, [], diagnostics=("manual_login_required",), status=200
        )
        calls = []
        confirmed = []

        def fetch_fn(session, tid, page, snapshot_dir):
            calls.append(page)
            return login_page

        with self.assertRaises(NgaAccessError) as caught:
            run_scan(
                self.config(),
                dry_run=True,
                session_factory=lambda: FakeSession([]),
                fetch_fn=fetch_fn,
                sleep=lambda s: None,
                confirm_login=lambda: confirmed.append(1),
            )
        self.assertEqual(caught.exception.code, "login_required")
        self.assertEqual(calls, [1, 1])
        self.assertEqual(len(confirmed), 1)

    def test_login_timeout_session_closed_once(self) -> None:
        login_page = make_fetched(
            1, 173, [], diagnostics=("manual_login_required",), status=200
        )
        session = FakeSession([])
        created = []

        def session_factory():
            created.append(1)
            return session

        with self.assertRaises(NgaAccessError) as caught:
            run_scan(
                self.config(),
                dry_run=True,
                session_factory=session_factory,
                fetch_fn=lambda s, t, p, d: login_page,
                sleep=lambda s: None,
                login_wait_file=self.tmp / "never-created",
                login_wait_timeout_seconds=0.5,
            )
        self.assertEqual(caught.exception.code, "login_not_confirmed")
        self.assertEqual(session.close_count, 1)

    def test_login_url_confusion_still_rejected(self) -> None:
        """W1-1: userinfo/端口/后缀混淆 host 不得被识别为官方登录页."""
        from umalib_ingest.nga_access import urls as urls_module

        for bad in (
            "https://evil.com@bbs.nga.cn/login.php",
            "https://bbs.nga.cn:8443/login.php",
            "https://bbs.nga.cn@evil.com/login.php",
            "https://bbs.nga.cn.evil.com/login.php",
            "https://evilbbs.nga.cn.evil.com/login.php",
            "http://bbs.nga.cn/login.php",
        ):
            with self.subTest(url=bad):
                self.assertFalse(urls_module.is_official_login_url(bad))

    def test_login_diag_meta_strips_query_tokens(self) -> None:
        """W1-2: 登录诊断 meta 不得持久化 query token 等凭证数据."""
        session = self._fake_session(
            "https://bbs.nga.cn/login.php?token=secret123&next=%2Fread.php",
            "NGA 登录", b"<html><title>NGA login</title></html>",
        )
        fetched = fetch_thread_page(session, 30099686, 1, self.snapshot_dir)
        self.assertIn("manual_login_required", fetched.diagnostics)
        diag = self.snapshot_dir / "tid-30099686" / "page-1.diag.json"
        data = json.loads(diag.read_text("utf-8"))
        raw = json.dumps(data, ensure_ascii=False)
        self.assertEqual(data["final_url"], "https://bbs.nga.cn/login.php")
        self.assertNotIn("token", raw)
        self.assertNotIn("secret123", raw)


class F2NavigationGateTests(ReleaseAcceptanceTestCase):
    """所有顶层导航（含登录重试）统一经过 sleep 门."""

    def _run(self, pages_by_page, login_retry=False, delay=3.0, dry_run=True):
        self.state_file = self.tmp / "watcher-state.json"
        atomic_write_json(self.state_file, make_state().to_dict())
        config = WatcherConfig(
            state_file=self.state_file,
            profile_dir=self.profile_dir,
            snapshot_dir=self.snapshot_dir,
            inbox_db=self.inbox_db,
            navigation_delay_seconds=delay,
            max_scan_pages=12,
        )
        calls = []
        sleeps = []

        def fetch_fn(session, tid, page, snapshot_dir):
            calls.append(page)
            return pages_by_page[page]

        if login_retry:
            original = pages_by_page[1]

            def retrying(session, tid, page, snapshot_dir):
                calls.append(page)
                return (
                    make_fetched(1, 173, [], diagnostics=("manual_login_required",))
                    if len([c for c in calls if c == 1]) == 1
                    else original
                )

            fetch_fn = retrying

        result = run_scan(
            config,
            dry_run=dry_run,
            session_factory=lambda: FakeSession([]),
            fetch_fn=fetch_fn,
            sleep=sleeps.append,
            confirm_login=lambda: None,
        )
        return calls, sleeps, result

    def test_first_interval(self) -> None:
        calls, sleeps, _ = self._run(
            {1: make_fetched(1, 173, []),
             173: make_fetched(173, 173, [make_floor("900000001", "讨论")])}
        )
        self.assertEqual(calls, [1, 173])
        self.assertEqual(sleeps, [3.0])

    def test_login_retry_gets_two_sleeps(self) -> None:
        calls, sleeps, _ = self._run(
            {1: make_fetched(1, 173, []),
             173: make_fetched(173, 173, [make_floor("900000001", "讨论")])},
            login_retry=True,
        )
        self.assertEqual(calls, [1, 1, 173])
        self.assertEqual(sleeps, [3.0, 3.0])

    def test_two_plan_pages_two_sleeps(self) -> None:
        self.state_file = self.tmp / "watcher-state.json"
        atomic_write_json(
            self.state_file, make_state(last_page=173).to_dict()
        )
        pages = {
            1: make_fetched(1, 175, []),
            173: make_fetched(173, 175, [make_floor("900000001", "一")]),
            174: make_fetched(174, 175, [make_floor("900000002", "二")]),
            175: make_fetched(175, 175, [make_floor("900000003", "三")]),
        }
        config = self.config()
        calls = []
        sleeps = []

        def fetch_fn(session, tid, page, snapshot_dir):
            calls.append(page)
            return pages[page]

        run_scan(
            config,
            dry_run=True,
            session_factory=lambda: FakeSession([]),
            fetch_fn=fetch_fn,
            sleep=sleeps.append,
        )
        self.assertEqual(calls, [1, 173, 174, 175])
        self.assertEqual(sleeps, [3.0, 3.0, 3.0])

    def test_custom_delay_used(self) -> None:
        calls, sleeps, _ = self._run(
            {1: make_fetched(1, 173, []),
             173: make_fetched(173, 173, [make_floor("900000001", "讨论")])},
            delay=4.5,
        )
        self.assertEqual(calls, [1, 173])
        self.assertEqual(sleeps, [4.5])

    def test_login_retry_failure_still_has_pre_retry_sleep(self) -> None:
        """登录确认后重试即使立即失败，重试导航前也必须经过一次 3s 门."""
        calls = []
        sleeps = []
        login_page = make_fetched(
            1, 173, [], diagnostics=("manual_login_required",), status=200
        )

        def fetch_fn(session, tid, page, snapshot_dir):
            calls.append(page)
            return login_page

        with self.assertRaises(NgaAccessError) as caught:
            run_scan(
                self.config(),
                dry_run=True,
                session_factory=lambda: FakeSession([]),
                fetch_fn=fetch_fn,
                sleep=sleeps.append,
                confirm_login=lambda: None,
            )
        self.assertEqual(caught.exception.code, "login_required")
        self.assertEqual(calls, [1, 1])
        self.assertEqual(sleeps, [3.0])


class F3ExpectedShaTests(ReleaseAcceptanceTestCase):
    """expected_state_sha256 必须在 run_scan 锁内绑定."""

    def test_stale_expected_sha_rejected_before_side_effects(self) -> None:
        session_created = []
        fetch_calls = []

        def session_factory():
            session_created.append(1)
            return FakeSession([])

        with self.assertRaises(NgaAccessError) as caught:
            run_scan(
                self.config(),
                dry_run=False,
                session_factory=session_factory,
                fetch_fn=lambda *a, **k: fetch_calls.append(1),
                expected_state_sha256="f" * 64,
            )
        self.assertEqual(caught.exception.code, "watcher_state_stale")
        self.assertEqual(session_created, [])
        self.assertEqual(fetch_calls, [])
        self.assertEqual(
            state_file_sha256(self.state_file),
            state_file_sha256(self.state_file),
        )

    def test_correct_expected_sha_proceeds(self) -> None:
        current = state_file_sha256(self.state_file)
        result = run_scan(
            self.config(),
            dry_run=False,
            session_factory=lambda: FakeSession([]),
            fetch_fn=lambda s, t, p, d: make_fetched(p, 173, []),
            sleep=lambda s: None,
            expected_state_sha256=current,
        )
        self.assertEqual(result.mode, "scan")
        self.assertIsNotNone(result.state_after_sha256)

    def test_missing_or_malformed_expected_sha_rejected_by_service(self) -> None:
        from umalib_ingest.web.watcher_ui import WatcherUiError

        runner_calls = []
        service = WatcherUiService(
            WatcherUiConfig(
                self.state_file, self.profile_dir, self.snapshot_dir, self.inbox_db,
                login_timeout_seconds=60.0, max_scan_pages=12,
            ),
            runner=lambda *a, **k: runner_calls.append(1),
        )
        for bad in (None, "", "xyz", "f" * 63):
            with self.subTest(sha=bad):
                with self.assertRaises(WatcherUiError) as caught:
                    service.scan({
                        "dry_run": False,
                        "confirm_import": True,
                        "expected_state_sha256": bad,
                    })
                self.assertEqual(caught.exception.code, "watcher_state_stale")
        self.assertEqual(runner_calls, [])

    def test_state_changed_after_request_rejected_in_lock(self) -> None:
        """请求时 SHA 有效（A），锁内加载前 state 已变成 B：必须 watcher_state_stale."""
        state_a = make_state()
        atomic_write_json(self.state_file, state_a.to_dict())
        a_sha = state_file_sha256(self.state_file)
        atomic_write_json(self.state_file, make_state(last_page=174).to_dict())
        b_sha = state_file_sha256(self.state_file)
        self.assertNotEqual(a_sha, b_sha)
        session_created = []
        fetch_calls = []

        def session_factory():
            session_created.append(1)
            return FakeSession([])

        with self.assertRaises(NgaAccessError) as caught:
            run_scan(
                self.config(),
                dry_run=False,
                session_factory=session_factory,
                fetch_fn=lambda *a, **k: fetch_calls.append(1),
                expected_state_sha256=a_sha,
            )
        self.assertEqual(caught.exception.code, "watcher_state_stale")
        self.assertEqual(session_created, [])
        self.assertEqual(fetch_calls, [])
        self.assertFalse(self.inbox_db.exists())


class F4ShutdownTests(ReleaseAcceptanceTestCase):
    """shutdown 取消扫描，不得被解释为登录确认成功."""

    def _blocking_runner(self, gate: threading.Event, cancelled):
        def runner(config, *, dry_run=False, confirm_login=None,
                   login_wait_timeout_seconds=300.0, **kwargs):
            try:
                confirm_login()
            except NgaAccessError as exc:
                cancelled.append(exc.code)
                raise
            gate.wait(timeout=5)
            return WatcherRunResult(
                mode="scan", tid=30099686, previous_last_page=173,
                current_last_page=173, scanned_pages=(173,),
                dry_run=dry_run, diagnostics=(),
            )

        return runner

    def test_shutdown_cancels_login_wait_not_success(self) -> None:
        ui_config = WatcherUiConfig(
            self.state_file, self.profile_dir, self.snapshot_dir, self.inbox_db,
            login_timeout_seconds=60.0, max_scan_pages=12,
        )
        cancelled = []
        gate = threading.Event()
        service = WatcherUiService(
            ui_config, runner=self._blocking_runner(gate, cancelled)
        )
        result = {}

        def do_scan():
            try:
                data = service.scan({"dry_run": True})
                result["data"] = data
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=do_scan)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not service._awaiting_login:
            time.sleep(0.02)
        self.assertTrue(service._awaiting_login)
        service.shutdown()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(cancelled, ["watcher_shutdown"])
        self.assertIn("error", result)
        self.assertFalse(service._busy)
        self.assertFalse(service._awaiting_login)

    def test_shutdown_idempotent(self) -> None:
        service = WatcherUiService(
            WatcherUiConfig(None, None, None, None), runner=lambda *a, **k: None
        )
        service.shutdown()
        service.shutdown()

    def test_cancel_arriving_during_sleep_aborts_before_next_fetch(self) -> None:
        """W1-6: 导航 sleep 期间到达的取消必须在后续 fetch 前可见."""
        self.state_file = self.tmp / "watcher-state.json"
        atomic_write_json(self.state_file, make_state().to_dict())
        pages = {
            1: make_fetched(1, 173, []),
            173: make_fetched(173, 173, [make_floor("900000001", "讨论")]),
        }
        calls = []
        cancel_flag = {"value": False}

        def fetch_fn(session, tid, page, snapshot_dir):
            calls.append(page)
            return pages[page]

        def sleep(seconds):
            cancel_flag["value"] = True

        with self.assertRaises(NgaAccessError) as caught:
            run_scan(
                self.config(),
                dry_run=True,
                session_factory=lambda: FakeSession([]),
                fetch_fn=fetch_fn,
                sleep=sleep,
                should_cancel=lambda: cancel_flag["value"],
            )
        self.assertEqual(caught.exception.code, "watcher_shutdown")
        self.assertEqual(calls, [1])

    def test_stopped_service_refuses_new_scan_fresh_instance_works(self) -> None:
        """W1-7: shutdown 后同实例拒绝新扫描，新实例可正常扫描."""
        from umalib_ingest.web.watcher_ui import WatcherUiError

        ui_config = WatcherUiConfig(
            self.state_file, self.profile_dir, self.snapshot_dir, self.inbox_db,
            login_timeout_seconds=60.0, max_scan_pages=12,
        )
        service = WatcherUiService(ui_config, runner=lambda *a, **k: None)
        service.shutdown()
        with self.assertRaises(WatcherUiError) as caught:
            service.scan({"dry_run": True})
        self.assertEqual(caught.exception.code, "watcher_shutdown")

        calls = []

        def ok_runner(config, *, dry_run=False, confirm_login=None,
                      login_wait_timeout_seconds=300.0, **kwargs):
            calls.append(1)
            return WatcherRunResult(
                mode="scan", tid=30099686, previous_last_page=173,
                current_last_page=173, scanned_pages=(), dry_run=dry_run,
                diagnostics=(),
            )

        fresh = WatcherUiService(ui_config, runner=ok_runner)
        fresh.scan({"dry_run": True})
        self.assertEqual(calls, [1])

    def test_login_confirm_after_shutdown_rejected(self) -> None:
        ui_config = WatcherUiConfig(
            self.state_file, self.profile_dir, self.snapshot_dir, self.inbox_db,
            login_timeout_seconds=60.0, max_scan_pages=12,
        )
        cancelled = []
        gate = threading.Event()
        service = WatcherUiService(
            ui_config, runner=self._blocking_runner(gate, cancelled)
        )
        from umalib_ingest.web.watcher_ui import WatcherUiError
        errors = []
        def scan_until_cancelled():
            try:
                service.scan({"dry_run": True})
            except WatcherUiError as exc:
                errors.append(exc.code)
        thread = threading.Thread(target=scan_until_cancelled)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not service._awaiting_login:
            time.sleep(0.02)
        service.shutdown()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["watcher_shutdown"])

        with self.assertRaises(WatcherUiError) as caught:
            service.login_confirm()
        self.assertEqual(caught.exception.code, "watcher_not_awaiting_login")

    def test_cancel_between_pages_stops_fetching(self) -> None:
        self.state_file = self.tmp / "watcher-state.json"
        atomic_write_json(self.state_file, make_state(last_page=173).to_dict())
        pages = {
            1: make_fetched(1, 175, []),
            173: make_fetched(173, 175, [make_floor("900000001", "一")]),
            174: make_fetched(174, 175, [make_floor("900000002", "二")]),
            175: make_fetched(175, 175, [make_floor("900000003", "三")]),
        }
        calls = []
        cancel_flag = {"value": False}
        session = FakeSession([])

        def fetch_fn(session_arg, tid, page, snapshot_dir):
            calls.append(page)
            if page == 174:
                cancel_flag["value"] = True
            return pages[page]

        def should_cancel():
            return cancel_flag["value"]

        config = self.config()
        with self.assertRaises(NgaAccessError) as caught:
            run_scan(
                config,
                dry_run=True,
                session_factory=lambda: session,
                fetch_fn=fetch_fn,
                sleep=lambda s: None,
                should_cancel=should_cancel,
            )
        self.assertEqual(caught.exception.code, "watcher_shutdown")
        self.assertLess(len(calls), 4)
        self.assertEqual(session.close_count, 1)

    def test_confirm_then_shutdown_aborts_without_success(self) -> None:
        """确认已接受后 shutdown 到达：取消，不重试、不写 Inbox/state、session 关一次."""
        self.state_file = self.tmp / "watcher-state.json"
        atomic_write_json(self.state_file, make_state().to_dict())
        state_before = state_file_sha256(self.state_file)
        login_page = make_fetched(
            1, 173, [], diagnostics=("manual_login_required",), status=200
        )
        calls = []
        cancelled = {"value": False}
        session = FakeSession([])

        def fetch_fn(session_arg, tid, page, snapshot_dir):
            calls.append(page)
            return login_page

        def confirm_login():
            cancelled["value"] = True

        with self.assertRaises(NgaAccessError) as caught:
            run_scan(
                self.config(),
                dry_run=False,
                session_factory=lambda: session,
                fetch_fn=fetch_fn,
                sleep=lambda s: None,
                confirm_login=confirm_login,
                should_cancel=lambda: cancelled["value"],
            )
        self.assertEqual(caught.exception.code, "watcher_shutdown")
        self.assertEqual(calls, [1])
        self.assertEqual(session.close_count, 1)
        self.assertEqual(state_file_sha256(self.state_file), state_before)
        from umalib_ingest.inbox import InboxRepository

        repo = InboxRepository(self.inbox_db)
        repo.initialize()
        self.assertEqual(repo.list_items(), [])

    def test_app_run_exit_notifies_watcher_service_before_close(self) -> None:
        from umalib_ingest.web import app as app_module

        class FakeWatcherService:
            def __init__(self, events):
                self._events = events

            def shutdown(self):
                self._events.append("shutdown")

        class FakeServer:
            def __init__(self):
                self.events = []
                self.server_address = ("127.0.0.1", 0)
                self.watcher_service = FakeWatcherService(self.events)

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                self.events.append("server_close")

        fake = FakeServer()
        with mock.patch.object(app_module, "create_server", return_value=fake):
            app_module.run(str(self.tmp / "app-run-inbox.db"), initialize=True)
        self.assertEqual(fake.events, ["shutdown", "server_close"])


class ReleaseLoopbackTests(ReleaseAcceptanceTestCase):
    """门 D1：临时 127.0.0.1 loopback API 操作流（无真实 Edge/网络/profile）."""

    def _start(self, runner):
        from umalib_ingest.inbox import InboxRepository
        from umalib_ingest.web import create_server

        repo = InboxRepository(self.inbox_db)
        repo.initialize()
        config = WatcherUiConfig(
            self.state_file, self.profile_dir, self.snapshot_dir, self.inbox_db,
            login_timeout_seconds=60.0, max_scan_pages=12,
        )
        server = create_server(repo, port=0, watcher_config=config, watcher_runner=runner)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, thread

    @staticmethod
    def _request(port, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            headers = {}
            if body is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=body, headers=headers)
            res = conn.getresponse()
            payload = res.read()
            return res.status, json.loads(payload.decode("utf-8")) if payload else {}
        finally:
            conn.close()

    @staticmethod
    def _ok_result(dry_run):
        return WatcherRunResult(
            mode="scan", tid=30099686, previous_last_page=173,
            current_last_page=173, scanned_pages=(), dry_run=dry_run,
            diagnostics=(),
        )

    def test_config_status_and_dry_run_loopback(self) -> None:
        from umalib_ingest.inbox import InboxRepository
        from umalib_ingest.watcher.scan import run_scan as real_run_scan

        def dry_runner(config, *, dry_run=False, confirm_login=None,
                       login_wait_timeout_seconds=300.0, **kwargs):
            return real_run_scan(
                config, dry_run=True,
                session_factory=lambda: FakeSession([]),
                fetch_fn=lambda s, t, p, d: make_fetched(p, 173, []),
                sleep=lambda s: None, confirm_login=confirm_login,
            )

        server, _ = self._start(dry_runner)
        port = server.server_address[1]
        status, body = self._request(port, "GET", "/api/watcher/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"], {"enabled": True, "tid": 30099686})
        status, body = self._request(port, "GET", "/api/watcher/status")
        self.assertEqual(status, 200)
        self.assertFalse(body["data"]["busy"])
        state_before = state_file_sha256(self.state_file)
        status, body = self._request(
            port, "POST", "/api/watcher/scan",
            json.dumps({"dry_run": True}).encode("utf-8"),
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["mode"], "scan")
        self.assertEqual(state_file_sha256(self.state_file), state_before)
        repo = InboxRepository(self.inbox_db)
        self.assertEqual(repo.list_items(), [])

    def test_shutdown_cancels_awaiting_login_not_success(self) -> None:
        def blocking_runner(config, *, dry_run=False, confirm_login=None,
                            login_wait_timeout_seconds=300.0, **kwargs):
            confirm_login()
            return self._ok_result(dry_run)

        server, _ = self._start(blocking_runner)
        port = server.server_address[1]
        scan_result = {}

        def do_scan():
            scan_result["status"], scan_result["body"] = self._request(
                port, "POST", "/api/watcher/scan",
                json.dumps({"dry_run": True}).encode("utf-8"),
            )

        thread = threading.Thread(target=do_scan)
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _, body = self._request(port, "GET", "/api/watcher/status")
            if body["data"].get("awaiting_login"):
                break
            time.sleep(0.05)
        _, body = self._request(port, "GET", "/api/watcher/status")
        self.assertTrue(body["data"]["awaiting_login"])
        server.watcher_service.shutdown()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(scan_result["status"], 409)
        self.assertEqual(scan_result["body"]["error"]["code"], "watcher_shutdown")
        _, body = self._request(port, "GET", "/api/watcher/status")
        self.assertFalse(body["data"]["busy"])
        self.assertFalse(body["data"]["awaiting_login"])
        self.assertIsNone(body["data"]["last_result"])
        self.assertEqual(body["data"]["last_error"]["code"], "watcher_shutdown")
        status, body = self._request(port, "POST", "/api/watcher/login-confirm", b"{}")
        self.assertEqual(status, 409)

    def test_server_close_releases_port_and_session_closed_once(self) -> None:
        class SessionRunner:
            def __init__(self):
                self.close_count = 0

            def __call__(self, config, *, dry_run=False, confirm_login=None,
                         login_wait_timeout_seconds=300.0, **kwargs):
                try:
                    confirm_login()
                finally:
                    self.close_count += 1
                return WatcherRunResult(
                    mode="scan", tid=30099686, previous_last_page=173,
                    current_last_page=173, scanned_pages=(), dry_run=dry_run,
                    diagnostics=(),
                )

        runner = SessionRunner()
        server, thread = self._start(runner)
        port = server.server_address[1]
        scan_thread = threading.Thread(target=self._request, args=(
            port, "POST", "/api/watcher/scan",
            json.dumps({"dry_run": True}).encode("utf-8"),
        ))
        scan_thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _, body = self._request(port, "GET", "/api/watcher/status")
            if body["data"].get("awaiting_login"):
                break
            time.sleep(0.05)
        server.watcher_service.shutdown()
        scan_thread.join(timeout=10)
        self.assertEqual(runner.close_count, 1)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        with self.assertRaises(OSError):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/watcher/status")

    def test_oversized_body_rejected(self) -> None:
        import socket

        server, _ = self._start(lambda *a, **k: None)
        port = server.server_address[1]
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            sock.sendall(
                b"POST /api/watcher/scan HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 2097153\r\n"
                b"\r\n"
            )
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            header_block, _, rest = buf.partition(b"\r\n\r\n")
            self.assertIn(b" 413 ", header_block.split(b"\r\n", 1)[0])
            length = 0
            for line in header_block.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip())
            body_bytes = rest
            while len(body_bytes) < length:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                body_bytes += chunk
            payload = json.loads(body_bytes.decode("utf-8"))
            self.assertEqual(payload["error"]["code"], "payload_too_large")
        finally:
            sock.close()


class ReleaseUiStaticTests(ReleaseAcceptanceTestCase):
    """门 D2：Windows 7 UI 状态机静态检查（本地，不起服务）."""

    STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "umalib_ingest" / "web" / "static"

    def test_seven_states_and_buttons(self) -> None:
        js = (self.STATIC_DIR / "app.js").read_text("utf-8")
        html = (self.STATIC_DIR / "index.html").read_text("utf-8")
        for text in (
            "启动时未配置 Watcher",
            "扫描中…",
            "检测到需要登录",
            "我已在官方 NGA 页面完成登录，重试一次",
            "扫描完成",
            "有未保存审核修改，请先保存或放弃后再扫描。",
            "我确认把本次发现的投稿加入当前 Inbox，并推进扫描状态",
        ):
            self.assertIn(text, js + html)
        # 取消状态：前端识别后端 watcher_shutdown 码并显示明确文案，不作为一般失败
        self.assertIn("watcher_shutdown", js)
        self.assertIn("已取消：", js)

    def test_waiting_state_login_button_enabled_and_scan_disabled(self) -> None:
        js = (self.STATIC_DIR / "app.js").read_text("utf-8")
        self.assertIn('$("wch-login-confirm-btn").disabled = !status.awaiting_login', js)
        self.assertIn('$(id).disabled = status.busy || watcherState.requestPending || !status.enabled', js)

    def test_safe_dom_and_responsive_layout(self) -> None:
        js = (self.STATIC_DIR / "app.js").read_text("utf-8")
        css = (self.STATIC_DIR / "styles.css").read_text("utf-8")
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                          "eval(", "new Function", "document.write",
                          "localStorage", "sessionStorage"):
            self.assertNotIn(forbidden, js)
        self.assertIn("@media (max-width: 1024px)", css)
        self.assertIn("@media (max-width: 1280px)", css)
        for forbidden in ("https://fonts", "http://fonts", "@import", "url(http"):
            self.assertNotIn(forbidden, css)


class ReleaseFixtureIntegrationTests(ReleaseAcceptanceTestCase):
    """门 D3：完全离线 Watcher → Parser → Inbox 联调（fake fetch，真实公共 API）."""

    def test_full_flow_fixture(self) -> None:
        from umalib_ingest.inbox import InboxRepository
        from umalib_ingest.watcher.state import KnownFloor, load_state

        state_file = self.tmp / "watcher-state.json"
        atomic_write_json(state_file, make_state().to_dict())
        discussion = make_floor("910000001", "普通讨论楼层")
        complete = make_floor("910000002", "[收录]\n[标题] 完整标题\n[作者] 作者甲\n[标签] 角色\n\n正文。\n")
        incomplete = make_floor("910000003", "[收录]\n[标题] 缺字段标题\n\n正文。\n")
        pages = {
            1: make_fetched(1, 173, []),
            173: make_fetched(173, 173, [discussion, complete, incomplete]),
        }
        config = WatcherConfig(
            state_file=state_file,
            profile_dir=self.profile_dir,
            snapshot_dir=self.snapshot_dir,
            inbox_db=self.inbox_db,
            navigation_delay_seconds=3.0,
            max_scan_pages=12,
        )

        def fetch_fn(session, tid, page, snapshot_dir):
            return pages[page]

        result = run_scan(
            config,
            dry_run=False,
            session_factory=lambda: FakeSession([]),
            fetch_fn=fetch_fn,
            sleep=lambda s: None,
        )
        self.assertEqual(result.discussion_count, 1)
        self.assertEqual(result.triggered_count, 2)
        self.assertEqual(len(result.imported_items), 2)
        repo = InboxRepository(self.inbox_db)
        repo.initialize()
        items = repo.list_items()
        self.assertEqual(len(items), 2)
        statuses = sorted(item.status for item in items)
        self.assertEqual(statuses, ["candidate", "incomplete"])
        state = load_state(state_file)
        self.assertEqual(
            state.known_floors["910000001"].inbox_item_id, None
        )
        self.assertIsNotNone(state.known_floors["910000002"].inbox_item_id)
        self.assertIsNotNone(state.known_floors["910000003"].inbox_item_id)
        self.assertEqual(
            state.known_floors["910000002"].content_sha256,
            state.known_floors["910000002"].imported_content_sha256,
        )

    def test_rerun_idempotent_and_edited_imported_floor_reports(self) -> None:
        from umalib_ingest.inbox import InboxRepository
        from umalib_ingest.watcher.state import KnownFloor, load_state

        state_file = self.tmp / "watcher-state.json"
        atomic_write_json(state_file, make_state().to_dict())
        content = "[收录]\n[标题] 标题\n[作者] 作者\n[标签] 标签\n\n正文。\n"
        floor = make_floor("910000009", content)
        pages = {
            1: make_fetched(1, 173, []),
            173: make_fetched(173, 173, [floor]),
        }
        config = WatcherConfig(
            state_file=state_file,
            profile_dir=self.profile_dir,
            snapshot_dir=self.snapshot_dir,
            inbox_db=self.inbox_db,
            navigation_delay_seconds=3.0,
            max_scan_pages=12,
        )

        def fetch_fn(session, tid, page, snapshot_dir):
            return pages[page]

        first = run_scan(
            config, dry_run=False, session_factory=lambda: FakeSession([]),
            fetch_fn=fetch_fn, sleep=lambda s: None,
        )
        self.assertEqual(len(first.imported_items), 1)
        item_id = first.imported_items[0].item_id
        repo = InboxRepository(self.inbox_db)
        repo.initialize()
        self.assertEqual(len(repo.list_items()), 1)

        second = run_scan(
            config, dry_run=False, session_factory=lambda: FakeSession([]),
            fetch_fn=fetch_fn, sleep=lambda s: None,
        )
        self.assertEqual(second.unchanged_floor_count, 1)
        self.assertEqual(second.imported_items, ())
        self.assertEqual(len(repo.list_items()), 1)

        edited_content = "[收录]\n[标题] 标题\n[作者] 作者\n[标签] 标签\n\n修改后正文。\n"
        edited = make_floor("910000009", edited_content)
        pages[173] = make_fetched(173, 173, [edited])
        third = run_scan(
            config, dry_run=False, session_factory=lambda: FakeSession([]),
            fetch_fn=fetch_fn, sleep=lambda s: None,
        )
        self.assertEqual(third.changed_after_import, (item_id,))
        self.assertIn("floor_changed_after_import", third.diagnostics)
        self.assertEqual(third.imported_items, ())
        self.assertEqual(len(repo.list_items()), 1)
        state = load_state(state_file)
        floor_state = state.known_floors["910000009"]
        self.assertEqual(floor_state.inbox_item_id, item_id)
        self.assertTrue(floor_state.changed_after_import)


if __name__ == "__main__":
    unittest.main()
