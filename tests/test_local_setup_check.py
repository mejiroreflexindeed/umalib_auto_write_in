"""TASK-012B W6：只读环境检查工具测试（全部离线、假数据、无写副作用断言）。"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tools.check_local_setup import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_OK,
    check_inbox,
    main,
    run_checks,
)

from umalib_ingest.inbox import InboxRepository  # noqa: E402
from umalib_ingest.watcher.state import (  # noqa: E402
    PRIVATE_STATE_ROOT,
    WatcherState,
    atomic_write_json,
)


_KEEP_TMP: list[tempfile.TemporaryDirectory] = []


def _tmp(prefix: str) -> Path:
    holder = tempfile.TemporaryDirectory(dir=str(PRIVATE_STATE_ROOT), prefix=prefix)
    _KEEP_TMP.append(holder)
    return Path(holder.name)


def _make_state(state_file: Path) -> None:
    state = WatcherState(
        schema_version="1",
        tid=30099686,
        canonical_thread_url="https://bbs.nga.cn/read.php?tid=30099686",
        last_page=173,
        known_floors={},
        baseline_created_at="2026-09-02T00:00:00.000000+00:00",
        last_successful_scan_at=None,
        last_result_summary=None,
    )
    atomic_write_json(state_file, state.to_dict())


class LocalSetupCheckTests(unittest.TestCase):
    def test_valid_setup_overall_ok(self) -> None:
        work = _tmp("night-check-ok-")
        state = work / "watcher-state.json"
        _make_state(state)
        inbox = work / "inbox.db"
        InboxRepository(inbox).initialize()
        items, overall = run_checks(inbox, state, work / "profile", work / "snaps", None)
        self.assertEqual(overall, "OK", items)
        statuses = {i["name"]: i["status"] for i in items}
        self.assertEqual(statuses["watcher-state"], "OK")
        self.assertEqual(statuses["inbox-db"], "OK")

    def test_missing_state_blocked(self) -> None:
        work = _tmp("night-check-missingstate-")
        inbox = work / "inbox.db"
        InboxRepository(inbox).initialize()
        items, overall = run_checks(inbox, work / "no-state.json",
                                    work / "profile", work / "snaps", None)
        self.assertEqual(overall, "BLOCKED")
        self.assertEqual(next(i for i in items if i["name"] == "watcher-state")["status"], "BLOCKED")

    def test_wrong_inbox_schema_blocked(self) -> None:
        work = _tmp("night-check-badschema-")
        inbox = work / "fake.db"
        conn = sqlite3.connect(str(inbox))
        conn.execute("PRAGMA user_version = 99")
        conn.close()
        self.assertEqual(check_inbox(inbox)["status"], "BLOCKED")

    def test_uninitialized_inbox_warn(self) -> None:
        work = _tmp("night-check-uninit-")
        inbox = work / "uninit.db"
        conn = sqlite3.connect(str(inbox))
        conn.close()
        self.assertEqual(check_inbox(inbox)["status"], "WARN")

    def test_missing_inbox_blocked(self) -> None:
        work = _tmp("night-check-noinbox-")
        self.assertEqual(check_inbox(work / "absent.db")["status"], "BLOCKED")

    def test_watcher_args_all_or_none(self) -> None:
        work = _tmp("night-check-allornone-")
        inbox = work / "inbox.db"
        InboxRepository(inbox).initialize()
        items, overall = run_checks(inbox, work / "state.json", None, None, None)
        self.assertEqual(overall, "BLOCKED")
        self.assertEqual(next(i for i in items if i["name"] == "watcher-args")["status"], "BLOCKED")

    def test_profile_equals_snapshot_blocked(self) -> None:
        work = _tmp("night-check-samepath-")
        state = work / "watcher-state.json"
        _make_state(state)
        inbox = work / "inbox.db"
        InboxRepository(inbox).initialize()
        items, overall = run_checks(inbox, state, work / "same", work / "same", None)
        self.assertEqual(overall, "BLOCKED")
        self.assertEqual(next(i for i in items if i["name"] == "watcher-paths")["status"], "BLOCKED")

    def test_check_creates_no_files(self) -> None:
        work = _tmp("night-check-nowrite-")
        inbox = work / "absent.db"
        before = sorted(p.name for p in work.iterdir())
        check_inbox(inbox)
        after = sorted(p.name for p in work.iterdir())
        self.assertEqual(before, after)

    def test_umalib_db_fake_does_not_crash(self) -> None:
        work = _tmp("night-check-umalib-")
        fake = work / "umalib.db"
        conn = sqlite3.connect(str(fake))
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
        conn.close()
        from tools.check_local_setup import check_umalib_db

        item = check_umalib_db(fake)[0]
        self.assertIn(item["status"], ("OK", "WARN", "BLOCKED"))

    def test_main_json_aligned_and_exit_codes(self) -> None:
        work = _tmp("night-check-main-")
        inbox = work / "inbox.db"
        InboxRepository(inbox).initialize()
        code = main(["--inbox-db", str(inbox), "--json"])
        self.assertEqual(code, EXIT_OK)
        missing = work / "absent.db"
        code = main(["--inbox-db", str(missing), "--json"])
        self.assertEqual(code, EXIT_BLOCKED)

    def test_no_args_prints_usage_exit_blocked(self) -> None:
        self.assertEqual(main([]), EXIT_BLOCKED)


if __name__ == "__main__":
    unittest.main()
