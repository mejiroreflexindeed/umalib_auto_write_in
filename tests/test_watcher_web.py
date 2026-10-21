"""Offline tests for the Watcher Windows 7 UI/API (TASK-011R+012 门 B).

All tests use fake Watcher runners and per-test temporary state/Inbox under
private-state; no Edge, no network, no real snapshot/profile access.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from umalib_ingest.inbox import InboxRepository
from umalib_ingest.nga_access.errors import NgaAccessError
from umalib_ingest.web import create_server
from umalib_ingest.web.watcher_ui import WatcherUiConfig
from umalib_ingest.watcher.result import ImportedItem, WatcherRunResult
from umalib_ingest.watcher.state import (
    PRIVATE_STATE_ROOT,
    WatcherState,
    atomic_write_json,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "src" / "umalib_ingest" / "web" / "static"


def make_state(*, last_page: int = 173) -> WatcherState:
    return WatcherState(
        schema_version="1",
        tid=30099686,
        canonical_thread_url="https://bbs.nga.cn/read.php?tid=30099686",
        last_page=last_page,
        known_floors={},
        baseline_created_at="2026-08-31T14:39:29.509386+00:00",
        last_successful_scan_at=None,
        last_result_summary=None,
    )


class FakeRunner:
    """Fake scan runner; records calls and can simulate login waits/failures."""

    def __init__(self, *, simulate_login: bool = False, error_code: str | None = None,
                 login_gate=None):
        self.calls = []
        self.simulate_login = simulate_login
        self.error_code = error_code
        self.login_gate = login_gate

    def __call__(self, config, *, dry_run=False, confirm_login=None,
                 login_wait_timeout_seconds=300.0, **kwargs):
        self.calls.append(
            {"dry_run": dry_run, "inbox_db": config.inbox_db,
             "max_scan_pages": config.max_scan_pages}
        )
        if self.simulate_login:
            confirm_login()
        if self.error_code:
            raise NgaAccessError(self.error_code, "simulated")
        return WatcherRunResult(
            mode="scan",
            tid=30099686,
            previous_last_page=173,
            current_last_page=174,
            scanned_pages=(173, 174),
            new_floor_count=1,
            unchanged_floor_count=0,
            changed_floor_count=0,
            discussion_count=0,
            triggered_count=1,
            imported_items=(ImportedItem(item_id=7, status="candidate"),),
            changed_after_import=(),
            state_before_sha256="a" * 64,
            state_after_sha256="b" * 64,
            dry_run=dry_run,
            diagnostics=(),
        )


class WatcherWebTestCase(unittest.TestCase):
    def setUp(self) -> None:
        PRIVATE_STATE_ROOT.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=str(PRIVATE_STATE_ROOT), prefix="nga-watcher-web-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state_file = self.tmp / "watcher-state.json"
        self.inbox_db = self.tmp / "inbox.db"
        self.snapshot_dir = self.tmp / "snaps"
        self.profile_dir = self.tmp / "profile"
        atomic_write_json(self.state_file, make_state().to_dict())
        self.repo = InboxRepository(self.inbox_db)
        self.repo.initialize()

    def start_server(self, watcher_config=None, runner=None, writer_config=None):
        self.server = create_server(
            self.repo,
            port=0,
            writer_config=writer_config,
            watcher_config=watcher_config,
            watcher_runner=runner,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def watcher_config(self, **kwargs) -> WatcherUiConfig:
        return WatcherUiConfig(
            state_file=kwargs.pop("state_file", self.state_file),
            profile_dir=kwargs.pop("profile_dir", self.profile_dir),
            snapshot_dir=kwargs.pop("snapshot_dir", self.snapshot_dir),
            inbox_db=kwargs.pop("inbox_db", self.inbox_db),
            login_timeout_seconds=kwargs.pop("login_timeout_seconds", 60.0),
            max_scan_pages=kwargs.pop("max_scan_pages", 12),
        )

    def request(self, method, path, body=None, headers=None):
        headers = dict(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            conn.request(method, path, body=body, headers=headers)
            res = conn.getresponse()
            payload = res.read()
            return res.status, dict(res.getheaders()), payload
        finally:
            conn.close()

    def get_json(self, path):
        status, _, payload = self.request("GET", path)
        return status, json.loads(payload.decode("utf-8"))

    def post_json(self, path, payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _, data = self.request(
            "POST", path, body=body, headers={"Content-Type": "application/json"}
        )
        return status, json.loads(data.decode("utf-8")) if data else {}


class WatcherConfigApiTests(WatcherWebTestCase):
    def test_unconfigured_keeps_review_working(self) -> None:
        self.start_server(watcher_config=WatcherUiConfig(None, None, None, None))
        status, body = self.get_json("/api/watcher/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"], {"enabled": False})
        status, body = self.get_json("/api/watcher/status")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "watcher_not_configured")
        status, body = self.get_json("/api/items")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["items"], [])
        status, body = self.post_json(
            "/api/import", {"raw_text": "[收录]\n[标题] t\n[作者] a\n[标签] x\n\n正文\n"}
        )
        self.assertEqual(status, 200)

    def test_config_and_status_never_leak_paths_or_content(self) -> None:
        self.start_server(watcher_config=self.watcher_config())
        status, body = self.get_json("/api/watcher/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"], {"enabled": True, "tid": 30099686})
        status, body = self.get_json("/api/watcher/status")
        self.assertEqual(status, 200)
        status_data = body["data"]
        self.assertEqual(status_data["tid"], 30099686)
        self.assertEqual(status_data["last_page"], 173)
        self.assertEqual(status_data["known_floor_count"], 0)
        raw = json.dumps(status_data, ensure_ascii=False)
        self.assertNotIn(str(self.tmp).replace("\\", "/"), raw)
        self.assertNotIn("profile", raw)
        self.assertNotIn("author", raw)
        self.assertNotIn("content", raw.replace("content", "") if "content" not in raw else raw)
        self.assertRegex(status_data["state_sha256"], r"^[0-9a-f]{64}$")

    def test_status_is_offline_read_only(self) -> None:
        runner = FakeRunner()
        self.start_server(watcher_config=self.watcher_config(), runner=runner)
        before_sha = json.loads(self.state_file.read_text("utf-8"))
        self.get_json("/api/watcher/status")
        after = json.loads(self.state_file.read_text("utf-8"))
        self.assertEqual(after, before_sha)
        self.assertEqual(runner.calls, [])
        self.assertTrue(self.inbox_db.exists())

    def test_cli_all_or_none_and_bounds(self) -> None:
        env = dict(
            os.environ,
            PYTHONPATH=str(PROJECT_ROOT / "src"),
            PYTHONDONTWRITEBYTECODE="1",
        )

        def run_cli(*args):
            return subprocess.run(
                [sys.executable, "-B", "-m", "umalib_ingest.web", "--db", str(self.inbox_db), *args],
                capture_output=True, text=True, env=env, timeout=60,
            )

        result = run_cli("--watcher-state", str(self.state_file))
        self.assertNotEqual(result.returncode, 0)
        result = run_cli(
            "--watcher-state", str(self.state_file),
            "--watcher-profile", str(self.profile_dir),
        )
        self.assertNotEqual(result.returncode, 0)
        result = run_cli(
            "--watcher-state", str(self.state_file),
            "--watcher-profile", str(self.profile_dir),
            "--watcher-snapshot-dir", str(self.snapshot_dir),
            "--watcher-login-timeout-seconds", "-1",
        )
        self.assertNotEqual(result.returncode, 0)
        result = run_cli(
            "--watcher-state", str(self.state_file),
            "--watcher-profile", str(self.profile_dir),
            "--watcher-snapshot-dir", str(self.snapshot_dir),
            "--watcher-max-scan-pages", "99",
        )
        self.assertNotEqual(result.returncode, 0)
        unsafe_profile = Path(os.environ.get("LOCALAPPDATA", "C:\\")) / "Microsoft" / "Edge" / "User Data"
        result = run_cli(
            "--watcher-state", str(self.state_file),
            "--watcher-profile", str(unsafe_profile),
            "--watcher-snapshot-dir", str(self.snapshot_dir),
        )
        self.assertNotEqual(result.returncode, 0)


class WatcherScanApiTests(WatcherWebTestCase):
    def test_dry_run_does_not_write_state_or_inbox(self) -> None:
        runner = FakeRunner()
        self.start_server(watcher_config=self.watcher_config(), runner=runner)
        state_before = self.state_file.read_bytes()
        status, body = self.post_json(
            "/api/watcher/scan", {"dry_run": True, "confirm_import": False,
                                  "expected_state_sha256": None}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(runner.calls, [{"dry_run": True, "inbox_db": self.inbox_db,
                                         "max_scan_pages": 12}])
        self.assertEqual(self.state_file.read_bytes(), state_before)

    def test_actual_scan_requires_confirmation_and_sha(self) -> None:
        runner = FakeRunner()
        self.start_server(watcher_config=self.watcher_config(), runner=runner)
        status, body = self.post_json("/api/watcher/scan", {"dry_run": False})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "watcher_confirm_required")
        status, body = self.post_json(
            "/api/watcher/scan",
            {"dry_run": False, "confirm_import": True, "expected_state_sha256": "f" * 64},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "watcher_state_stale")
        self.assertEqual(runner.calls, [])
        current_sha = self.get_json("/api/watcher/status")[1]["data"]["state_sha256"]
        status, body = self.post_json(
            "/api/watcher/scan",
            {"dry_run": False, "confirm_import": True, "expected_state_sha256": current_sha},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(runner.calls[-1]["dry_run"], False)
        result = body["data"]
        self.assertEqual(result["imported_items"], [{"item_id": 7, "status": "candidate"}])
        self.assertEqual(result["current_last_page"], 174)
        self.assertNotIn("raw_text", json.dumps(result))

    def test_concurrent_scan_rejected(self) -> None:
        gate = threading.Event()
        runner = FakeRunner(login_gate=gate)

        def blocking_runner(config, *, dry_run=False, confirm_login=None,
                            login_wait_timeout_seconds=300.0, **kwargs):
            runner.calls.append({"dry_run": dry_run})
            gate.wait(timeout=30)
            return WatcherRunResult(
                mode="scan", tid=30099686, previous_last_page=173,
                current_last_page=174, scanned_pages=(173, 174),
                dry_run=dry_run, diagnostics=(),
            )

        self.start_server(watcher_config=self.watcher_config(), runner=blocking_runner)
        first_result = {}

        def first_scan():
            status, body = self.post_json("/api/watcher/scan", {"dry_run": True})
            first_result["status"] = status

        t = threading.Thread(target=first_scan)
        t.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not runner.calls:
            time.sleep(0.05)
        status, body = self.post_json("/api/watcher/scan", {"dry_run": True})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "watcher_scan_busy")
        gate.set()
        t.join(timeout=10)
        self.assertEqual(first_result["status"], 200)

    def test_login_waiting_confirm_retry_flow(self) -> None:
        self.start_server(
            watcher_config=self.watcher_config(),
            runner=FakeRunner(simulate_login=True),
        )
        scan_result = {}

        def run_scan():
            status, body = self.post_json("/api/watcher/scan", {"dry_run": True})
            scan_result["status"] = status
            scan_result["body"] = body

        t = threading.Thread(target=run_scan)
        t.start()
        deadline = time.monotonic() + 10
        awaiting = False
        while time.monotonic() < deadline:
            status, body = self.get_json("/api/watcher/status")
            if body["data"].get("awaiting_login"):
                awaiting = True
                break
            time.sleep(0.05)
        self.assertTrue(awaiting)
        status, body = self.post_json("/api/watcher/login-confirm", {})
        self.assertEqual(status, 200)
        t.join(timeout=10)
        self.assertEqual(scan_result["status"], 200)

    def test_login_confirm_rejected_when_not_waiting(self) -> None:
        self.start_server(watcher_config=self.watcher_config(), runner=FakeRunner())
        status, body = self.post_json("/api/watcher/login-confirm", {})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "watcher_not_awaiting_login")

    def test_runner_failure_releases_busy(self) -> None:
        self.start_server(
            watcher_config=self.watcher_config(),
            runner=FakeRunner(error_code="page_fetch_failed"),
        )
        status, body = self.post_json("/api/watcher/scan", {"dry_run": True})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "page_fetch_failed")
        status, body = self.get_json("/api/watcher/status")
        self.assertFalse(body["data"]["busy"])
        self.assertEqual(body["data"]["last_error"]["code"], "page_fetch_failed")

    def test_unknown_keys_and_non_json_rejected(self) -> None:
        self.start_server(watcher_config=self.watcher_config(), runner=FakeRunner())
        status, body = self.post_json(
            "/api/watcher/scan", {"dry_run": True, "extra": 1}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "watcher_invalid_request")
        status, _, payload = self.request(
            "POST", "/api/watcher/scan", body="{bad",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)

    def test_foreign_origin_rejected(self) -> None:
        self.start_server(watcher_config=self.watcher_config(), runner=FakeRunner())
        status, _, _ = self.request(
            "POST", "/api/watcher/scan",
            body=json.dumps({"dry_run": True}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": "http://evil.example.com"},
        )
        self.assertEqual(status, 403)


class WatcherFrontendStructureTests(WatcherWebTestCase):
    def test_dom_and_texts_present(self) -> None:
        html = (STATIC_DIR / "index.html").read_text("utf-8")
        for marker in (
            "watcher-panel",
            "wch-refresh-btn",
            "wch-dryrun-btn",
            "wch-scan-btn",
            "wch-confirm-import",
            "wch-login-confirm-btn",
            "wch-scan-state",
            "wch-result",
            "我确认把本次发现的投稿加入当前 Inbox，并推进扫描状态",
            "我已在官方 NGA 页面完成登录，重试一次",
            "只检查，不导入（dry-run）",
            "扫描并加入当前 Inbox",
        ):
            self.assertIn(marker, html)

    def test_js_state_machine_and_safety(self) -> None:
        js = (STATIC_DIR / "app.js").read_text("utf-8")
        for marker in (
            "/api/watcher/config",
            "/api/watcher/status",
            "/api/watcher/scan",
            "/api/watcher/login-confirm",
            "expected_state_sha256",
            "confirm_import",
            "有未保存审核修改",
            "setInterval",
            "clearInterval",
        ):
            self.assertIn(marker, js)
        for forbidden in (
            "innerHTML", "outerHTML", "insertAdjacentHTML", "eval(",
            "new Function", "document.write", "localStorage", "sessionStorage",
        ):
            self.assertNotIn(forbidden, js)
        html = (STATIC_DIR / "index.html").read_text("utf-8")
        for forbidden in ("onclick=", "onerror=", "onload=", "<script>"):
            self.assertNotIn(forbidden, html)

    def test_dirty_blocks_scan(self) -> None:
        js = (STATIC_DIR / "app.js").read_text("utf-8")
        self.assertIn("请先保存或放弃后再扫描", js)
        self.assertIn("if (dirty)", js)


if __name__ == "__main__":
    unittest.main()
