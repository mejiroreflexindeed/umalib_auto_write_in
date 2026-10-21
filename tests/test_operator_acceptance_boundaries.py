"""Independent TASK-015 regressions for interruption and multi-batch use."""
from __future__ import annotations

import hashlib
import datetime
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_operator_scan import _Session
from umalib_ingest.operator.app_factory import create_operator_app
from umalib_ingest.operator.service import OperatorError
from umalib_ingest.operator.scan_service import Transport, HARD_NAV_LIMIT


def _submission(title: str) -> str:
    return f"[收录]\n[标题] {title}\n[作者] 合成作者\n[标签] 标签\n\n合成正文"


class OperatorBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="operator-boundary-")
        self.scenario = {"tid": "12345678", "last_page": 1, "floors": {
            1: [("91000001", _submission("原稿"), "2026-08-10 12:00:00")],
        }}
        self.calls: list[int] = []
        self.before_fetch = None
        self.ctx = self._open()
        self.svc = self.ctx.service
        self.svc.add_thread("https://bbs.nga.cn/read.php?tid=12345678", None, 0)

    def _session(self):
        session = _Session(self.scenario)
        original = session.page.goto

        def goto(url, wait_until=None):
            page = int(url.split("&page=", 1)[1]) if "&page=" in url else 1
            self.calls.append(page)
            if self.before_fetch is not None:
                self.before_fetch(page)
            return original(url, wait_until=wait_until)

        session.page.goto = goto
        return session

    def _open(self):
        # Synthetic transport and no real source probe; all app/state/Inbox paths are real.
        with patch("umalib_ingest.operator.app_factory.detect_main_copy", return_value=None):
            return create_operator_app(root=Path(self.temp.name),
                                       session_factory=self._session, nav_delay=0.0)

    def tearDown(self) -> None:
        self.ctx.shutdown()
        self.temp.cleanup()

    def _wait(self, run_id: str) -> dict:
        """Wait for a terminal status; fail on real stalls, tolerate slow machines.

        The job is considered stalled only if the run record makes no progress
        for 15s; a 300s cap bounds the total wait while 35-page rescans may
        legitimately take longer than the old fixed 10s on a loaded host.
        """
        last_snapshot = None
        last_change = time.monotonic()
        deadline = last_change + 300
        while time.monotonic() < deadline:
            record = self.svc.get_run(run_id)
            if record["status"] in ("succeeded", "partial", "failed", "cancelled", "interrupted"):
                return record
            snapshot = json.dumps([record["status"], record.get("progress")], sort_keys=True)
            if snapshot != last_snapshot:
                last_snapshot = snapshot
                last_change = time.monotonic()
            elif time.monotonic() - last_change > 15:
                self.fail(f"job stalled: {self.svc.get_run(run_id)}")
            time.sleep(0.01)
        self.fail(f"job timeout: {self.svc.get_run(run_id)}")

    def _init(self, mode="page", date=None):
        run_id = self.svc.initialize_preview("12345678", {
            "mode": mode, "page": 1 if mode == "page" else None,
            "date": date, "timezone": "UTC+08:00", "lower_bound_utc": None,
        }, 0, True)
        run = self._wait(run_id)
        self.assertEqual(run["status"], "succeeded", run)
        self.svc.initialize_confirm("12345678", run_id, run["result"]["preview_sha256"], 0, True)

    def _state_path(self) -> Path:
        return self.svc.paths.state_json(self.svc.workspace_id, "12345678")

    def _scan(self):
        sha = hashlib.sha256(self._state_path().read_bytes()).hexdigest()
        return self._wait(self.svc.scan("12345678", False, True, sha))

    def _rescan(self, **dto):
        dto.update(confirmed=True, expected_state_sha256=hashlib.sha256(self._state_path().read_bytes()).hexdigest())
        return self._wait(self.svc.rescan("12345678", dto))

    def test_page_rescan_35_pages_finishes_after_restart_without_repeating_batches(self):
        self.scenario["last_page"] = 35
        self.scenario["floors"] = {
            page: [(str(91000000 + page), _submission(f"稿{page}"), "2026-08-10 12:00:00")]
            for page in range(1, 36)
        }
        self._init("now")
        original = self.svc.thread_status("12345678")["state"]
        self.calls.clear()
        run_ids = []
        for batch in range(4):
            run = self._rescan(kind="pages", from_page=1, to_page=35)
            run_ids.append(run["run_id"])
            self.assertEqual(run["status"], "partial" if batch < 3 else "succeeded", run)
            if batch == 1:
                self.ctx.shutdown()
                self.ctx = self._open()
                self.svc = self.ctx.service
        self.assertEqual(self.calls, list(range(1, 36)))
        after = self.svc.thread_status("12345678")["state"]
        self.assertIsNone(after["active_rescan"])
        self.assertEqual(after["next_scan_page"], original["next_scan_page"])
        self.assertEqual(after["frontier_page"], original["frontier_page"])

    def test_cancel_preserves_completed_page_and_resume_imports_once(self):
        self.scenario["last_page"] = 3
        self.scenario["floors"][2] = [("91000002", _submission("第二页"), "2026-08-10 12:00:00")]
        self.scenario["floors"][3] = [("91000003", _submission("第三页"), "2026-08-10 12:00:00")]
        self._init()

        def cancel_at_page_two(page):
            if page == 2:
                self.svc.cancel(self.svc._jobs.active_run().run_id)

        self.before_fetch = cancel_at_page_two
        run = self._scan()
        self.assertEqual(run["status"], "cancelled", run)
        state = self.svc.thread_status("12345678")["state"]
        self.assertEqual(state["frontier_page"], 1, "fully imported page must be persisted before cancellation")
        self.before_fetch = None
        self.assertEqual(self._scan()["status"], "succeeded")
        self.assertEqual(len(self.ctx.repository.list_items()), 3)

    def test_date_rescan_35_pages_covers_unordered_dates_and_survives_restart(self):
        self.scenario["last_page"] = 35
        # A future-looking first page must not cause skipped earlier-date later pages.
        dates = ("2026-08-10 00:00:00", "2026-08-09 23:59:59", "未知时间", "2026-08-11 12:00:00")
        self.scenario["floors"] = {
            p: [(str(91000000 + p), _submission(f"日期稿{p}"), dates[p % 4])]
            for p in range(1, 36)
        }
        self._init("date", "2026-08-12")
        for _ in range(4):
            self._scan()
        original = self.svc.thread_status("12345678")["state"]
        self.calls.clear()
        identity = None
        for batch in range(4):
            run = self._rescan(kind="date", lower_bound_date="2026-08-10")
            self.assertEqual(run["status"], "partial" if batch < 3 else "succeeded", run)
            state = self.svc.thread_status("12345678")["state"]
            if batch < 3:
                active = state["active_rescan"]
                identity = identity or active["rescan_id"]
                self.assertEqual(active["rescan_id"], identity)
                self.assertEqual(active["next_page"], (batch + 1) * 10 + 1)
            if batch == 1:
                self.ctx.shutdown()
                self.ctx = self._open()
                self.svc = self.ctx.service
        self.assertEqual(self.calls, list(range(1, 36)))
        self.assertEqual(len(self.ctx.repository.list_items()), sum(p % 4 != 1 for p in range(1, 36)))
        self.assertIsNone(state["active_rescan"])
        self.assertEqual(state["frontier_page"], original["frontier_page"])
        self.assertEqual(state["next_scan_page"], original["next_scan_page"])
        self.assertEqual(self.svc._registry.read_config("12345678").start.date, "2026-08-12")

    def test_metadata_edit_retried_after_state_write_failure_does_not_duplicate(self):
        self._init()
        self.assertEqual(self._scan()["status"], "succeeded")
        self.scenario["floors"][1] = [("91000001", _submission("仅标题修订"), "2026-08-10 12:00:00")]
        from umalib_ingest.operator import service as service_module
        original_write = service_module.atomic_write_json

        def fail_state(path, data):
            if Path(path) == self._state_path():
                raise OSError("injected state disk failure")
            return original_write(path, data)

        with patch.object(service_module, "atomic_write_json", side_effect=fail_state):
            self.assertEqual(self._scan()["status"], "failed")
        self.assertEqual(self._scan()["status"], "succeeded")
        self.assertEqual(len(self.ctx.repository.list_items()), 2, "one original and exactly one revision")

    def test_thread_configuration_rejected_during_maintenance(self):
        gate = self.ctx.workspace_service.coordinator()
        before = self.svc.list_threads()
        gate.enter_maintenance()
        try:
            with self.assertRaises(OperatorError):
                self.svc.add_thread("https://bbs.nga.cn/read.php?tid=87654321", None, before["revision"])
            with self.assertRaises(OperatorError):
                self.svc.patch_thread("12345678", before["revision"], name="维护中改名")
        finally:
            gate.leave_maintenance()
        self.assertEqual(self.svc.list_threads(), before)

    def test_invalid_rescan_range_rejected_before_job_or_navigation(self):
        self._init()
        before = self._state_path().read_bytes()
        self.calls.clear()
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 11, tzinfo=datetime.timezone.utc).astimezone(tz)

        for dto in ({"kind": "pages", "from_page": 0, "to_page": 1},
                    {"kind": "pages", "from_page": 2, "to_page": 1},
                    {"kind": "pages", "from_page": True, "to_page": 1},
                    {"kind": "date", "lower_bound_date": "bad-date"},
                    {"kind": "date", "lower_bound_date": "2999-01-01"}):
            with self.subTest(dto=dto):
                with patch("umalib_ingest.operator.service.datetime.datetime", FixedDateTime), \
                        self.assertRaises(OperatorError) as caught:
                    self.svc.rescan("12345678", {**dto, "confirmed": True,
                        "expected_state_sha256": hashlib.sha256(before).hexdigest()})
                self.assertIn(caught.exception.code, ("rescan_invalid", "start_invalid"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self._state_path().read_bytes(), before)


class NavigationBoundaryTests(unittest.TestCase):
    def test_failed_navigations_are_limited_and_spaced(self):
        clock = [0.0]
        calls = []

        def failure(*args):
            calls.append(clock[0])
            raise OSError("injected network failure")

        def sleep(seconds):
            clock[0] += seconds

        with tempfile.TemporaryDirectory() as tmp:
            transport = Transport(lambda: object(), Path(tmp), "12345678", clock=lambda: clock[0])
            with patch("umalib_ingest.nga_access.fetch.fetch_thread_page", side_effect=failure), \
                    patch("umalib_ingest.operator.scan_service.time.sleep", side_effect=sleep):
                for _ in range(HARD_NAV_LIMIT + 1):
                    try:
                        transport.fetch(1)
                    except Exception:
                        pass
            self.assertEqual(len(calls), HARD_NAV_LIMIT)
            self.assertTrue(all(b - a >= 3 for a, b in zip(calls, calls[1:])))


if __name__ == "__main__":
    unittest.main()
