"""Independent TASK-012B review regressions; synthetic, offline, temporary data."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import check_local_setup as doctor
from tools import night_acceptance as lab
from umalib_ingest.inbox import InboxRepository
from umalib_ingest.nga_access.errors import NgaAccessError
from umalib_ingest.watcher import scan as scan_module
from umalib_ingest.watcher import state as state_module
from umalib_ingest.watcher.result import WatcherRunResult
from umalib_ingest.web import watcher_ui
from tests.test_watcher_release_acceptance import (
    FakeSession, ReleaseAcceptanceTestCase, make_fetched, make_floor, make_state,
)

TEXT = "[收录]\n[标题] 标题甲\n[作者] 作者甲\n[标签] 标签甲\n[原作] https://example.com/a\n\n正文甲。"


class WatcherRevisionTests(ReleaseAcceptanceTestCase):
    def scan(self, **kwargs):
        return scan_module.run_scan(self.config(), session_factory=lambda: FakeSession([]),
            fetch_fn=kwargs.pop("fetch_fn", lambda s, t, p, d: make_fetched(p, 173)),
            sleep=lambda _: None, **kwargs)

    def test_cancelled_before_start_has_no_session_or_inbox(self):
        session = mock.Mock()
        with self.assertRaises(NgaAccessError) as caught:
            scan_module.run_scan(self.config(), session_factory=session, should_cancel=lambda: True,
                                 fetch_fn=lambda s, t, p, d: make_fetched(p, 173))
        self.assertEqual(caught.exception.code, "watcher_shutdown")
        session.assert_not_called()
        self.assertFalse(self.inbox_db.exists())

    def test_cancel_after_final_fetch_is_seen_by_dry_run(self):
        cancelled = False
        def fetch(s, t, p, d):
            nonlocal cancelled
            cancelled = p == 173
            return make_fetched(p, 173)
        with self.assertRaises(NgaAccessError) as caught:
            self.scan(dry_run=True, fetch_fn=fetch, should_cancel=lambda: cancelled)
        self.assertEqual(caught.exception.code, "watcher_shutdown")

    def test_cancel_after_each_import_reports_partial_without_advancing_state(self):
        for stop_after in (1, 2):
            with self.subTest(stop_after=stop_after):
                state_module.atomic_write_json(self.state_file, make_state().to_dict())
                before = self.state_file.read_bytes()
                cancelled = False
                committed = 0
                original = scan_module._import_floor
                def importing(*args):
                    nonlocal committed, cancelled
                    value = original(*args)
                    committed += 1
                    cancelled = committed == stop_after
                    return value
                floors = [make_floor("910009001", TEXT), make_floor("910009002", TEXT)]
                with mock.patch.object(scan_module, "_import_floor", side_effect=importing):
                    with self.assertRaises(NgaAccessError) as caught:
                        self.scan(fetch_fn=lambda s, t, p, d: make_fetched(p, 173, floors if p == 173 else []),
                                  should_cancel=lambda: cancelled)
                self.assertEqual(caught.exception.code, "watcher_partial_import")
                self.assertIn("已", caught.exception.message)
                self.assertEqual(self.state_file.read_bytes(), before)
                self.assertEqual(committed, stop_after)

    def test_shutdown_during_start_precheck_cannot_be_cleared(self):
        runner = mock.Mock(return_value=WatcherRunResult(mode="scan", tid=30099686,
            previous_last_page=173, current_last_page=173, scanned_pages=()))
        config = self.config()
        service = watcher_ui.WatcherUiService(watcher_ui.WatcherUiConfig(
            config.state_file, config.profile_dir, config.snapshot_dir, config.inbox_db), runner=runner)
        sha = state_module.state_file_sha256(self.state_file)
        def stop(path):
            service.shutdown()
            return sha
        with mock.patch.object(watcher_ui, "state_file_sha256", side_effect=stop):
            with self.assertRaises(watcher_ui.WatcherUiError) as caught:
                service.scan({"confirm_import": True, "expected_state_sha256": sha})
        self.assertEqual(caught.exception.code, "watcher_shutdown")
        runner.assert_not_called()
        self.assertTrue(service._cancelled)

    def test_recovery_requires_all_original_content_not_only_body(self):
        repo = InboxRepository(self.inbox_db)
        repo.initialize()
        item_id, _ = scan_module._import_floor(repo, 30099686, make_floor("910009001", TEXT), [])
        for old, new in (("标题甲", "标题乙"), ("作者甲", "作者乙"), ("标签甲", "标签乙"),
                         ("example.com/a", "example.com/b")):
            with self.subTest(field=old):
                with self.assertRaises(NgaAccessError) as caught:
                    scan_module._import_floor(repo, 30099686, make_floor("910009001", TEXT.replace(old, new)), [])
                self.assertEqual(caught.exception.code, "state_import_recovery_ambiguous")
        self.assertEqual(scan_module._import_floor(repo, 30099686, make_floor("910009001", TEXT), [])[0], item_id)
        self.assertEqual(len(repo.list_items()), 1)

    def test_state_and_sha_are_bound_to_a_single_byte_read(self):
        before = self.state_file.read_bytes()
        real_read = Path.read_bytes
        count = 0
        def replacing(path):
            nonlocal count
            data = real_read(path)
            if path == self.state_file:
                count += 1
                state_module.atomic_write_json(path, make_state(last_page=174).to_dict())
            return data
        with mock.patch.object(Path, "read_bytes", replacing):
            state, sha = state_module.load_state_snapshot(self.state_file)
        self.assertEqual(count, 1)
        self.assertEqual(state.last_page, 173)
        self.assertEqual(sha, hashlib.sha256(before).hexdigest())


class DoctorRevisionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def database(self, sql):
        db = self.root / "test.db"
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.executescript(sql)
        return db

    def test_v0_with_user_tables_is_blocked(self):
        db = self.database("CREATE TABLE unrelated(x)")
        self.assertEqual(doctor.check_inbox(db)["status"], "BLOCKED")

    def test_v1_empty_shell_tables_are_blocked(self):
        db = self.database("CREATE TABLE inbox_item(x); CREATE TABLE inbox_status_history(x); PRAGMA user_version=1")
        self.assertEqual(doctor.check_inbox(db)["status"], "BLOCKED")

    def test_umalib_inspector_blockers_are_preserved(self):
        db = self.database("CREATE TABLE unrelated(x)")
        self.assertEqual(doctor.check_umalib_db(db)[0]["status"], "BLOCKED")

    def test_clean_wal_and_live_wal_are_not_opened_or_mutated(self):
        db = self.root / "inbox.db"
        InboxRepository(db).initialize()
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        for fn in (doctor.check_inbox, lambda p: doctor.check_umalib_db(p)[0]):
            self.assertEqual(fn(db)["status"], "BLOCKED")
            self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE new_uncheckpointed(x)")
            conn.commit()
            before = {p.name: p.read_bytes() for p in self.root.iterdir()}
            self.assertEqual(doctor.check_inbox(db)["status"], "BLOCKED")
            self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_missing_index_is_blocked_without_initialize(self):
        db = self.root / "inbox.db"
        InboxRepository(db).initialize()
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.execute("DROP INDEX idx_inbox_item_status")
            conn.commit()
        with mock.patch.object(InboxRepository, "initialize", side_effect=AssertionError("must not initialize")):
            self.assertEqual(doctor.check_inbox(db)["status"], "BLOCKED")


class LabRevisionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_unavailable_phase_never_passes_all(self):
        with mock.patch.object(lab, "run_replay", return_value={"overall": "PASS"}), \
             mock.patch.object(lab, "run_recovery", return_value={"overall": "PASS"}), \
             mock.patch.object(lab, "run_ui", side_effect=ModuleNotFoundError("missing", name="playwright")), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = lab.main(["--phase", "all", "--output", str(self.root / "out")])
        result = json.loads((self.root / "out" / "all-summary.json").read_text(encoding="utf-8"))
        self.assertNotEqual(rc, 0)
        self.assertNotEqual(result["overall"], "PASS")

    def test_all_honors_missing_custom_scenarios(self):
        with mock.patch.object(lab, "run_replay", return_value={"overall": "PASS"}) as replay, \
             mock.patch.object(lab, "run_recovery", return_value={"overall": "PASS"}), \
             mock.patch.object(lab, "run_ui", return_value={"overall": "PASS"}), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = lab.main(["--phase", "all", "--scenarios", str(self.root / "missing.json"),
                           "--output", str(self.root / "out")])
        self.assertNotEqual(rc, 0)
        replay.assert_not_called()

    def test_phase_crash_collects_other_phase_evidence(self):
        with mock.patch.object(lab, "run_replay", return_value={"overall": "PASS"}), \
             mock.patch.object(lab, "run_recovery", side_effect=RuntimeError("synthetic crash")), \
             mock.patch.object(lab, "run_ui", return_value={"overall": "PASS"}) as ui, \
             contextlib.redirect_stdout(io.StringIO()):
            rc = lab.main(["--phase", "all", "--output", str(self.root / "out")])
        self.assertNotEqual(rc, 0)
        ui.assert_called_once()
        self.assertTrue((self.root / "out" / "all-summary.json").exists())

    def test_ui_early_browser_failure_cleans_server_and_workspace(self):
        from tools import night_ui
        from umalib_ingest import web
        servers = []
        directories = []
        original_server = web.create_server
        original_tmp = tempfile.mkdtemp
        def create(*args, **kwargs):
            server = original_server(*args, **kwargs)
            servers.append(server)
            return server
        def mkdtemp(*args, **kwargs):
            path = original_tmp(*args, **kwargs)
            directories.append(Path(path))
            return path
        with mock.patch.object(lab, "check_environment", return_value={"items": {
                "playwright": "OK", "msedge": "synthetic"}}), \
             mock.patch.object(web, "create_server", side_effect=create), \
             mock.patch.object(night_ui.tempfile, "mkdtemp", side_effect=mkdtemp), \
             mock.patch("playwright.sync_api.sync_playwright", side_effect=RuntimeError("synthetic launch failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic launch failure"):
                night_ui.run_ui(self.root)
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].fileno(), -1)
        self.assertTrue(directories)
        self.assertTrue(all(not path.exists() for path in directories))

    def test_replay_early_failure_cleans_workspace(self):
        directories = []
        original_tmp = tempfile.mkdtemp
        def mkdtemp(*args, **kwargs):
            path = original_tmp(*args, **kwargs)
            directories.append(Path(path))
            return path
        with mock.patch.object(tempfile, "mkdtemp", side_effect=mkdtemp), \
             mock.patch.object(lab, "_initial_state_dict", side_effect=RuntimeError("synthetic setup failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic setup failure"):
                lab.run_scenario(lab.load_scenarios()[0])
        self.assertTrue(directories)
        self.assertTrue(all(not path.exists() for path in directories))

    def test_reference_model_exception_is_not_a_pass(self):
        from tools import night_reference
        with mock.patch.object(night_reference, "generate_sequences", return_value=[[(910009999, "complete")]]), \
             mock.patch.object(scan_module, "run_scan", side_effect=NgaAccessError("synthetic", "test")):
            report = night_reference.run_reference_sequences(self.root)
        self.assertEqual(report["overall"], "FAIL")
        self.assertEqual(report["out_of_scope_count"], 1)
