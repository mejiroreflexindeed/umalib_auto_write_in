"""TASK-014 W03/W04/W06 模型与状态测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umalib_ingest.operator.models import (
    JsonValidationError,
    SourceBinding,
    ThreadConfig,
    ThreadEntry,
    ThreadIndex,
    ThreadStart,
    canonical_json,
    utc_to_shanghai_lower_bound,
    validate_tid,
)
from umalib_ingest.operator.state_v2 import (
    WatcherStateV2,
    from_v1_state,
    parse_nga_posted_at,
    read_state_file,
)


class ModelTests(unittest.TestCase):
    def test_source_binding_roundtrip_and_unknown_key(self) -> None:
        data = {
            "schema_version": "1", "source_id": "sha256:abcd" + "0" * 56,
            "path": "D:/x/main-copy.db", "file_name": "main-copy.db",
            "sha256": "4e152ea3" + "0" * 56, "size": 82296832,
            "bound_at": "2026-09-04T00:00:00+00:00",
            "schema_fingerprint_sha256": "d7b19669" + "0" * 56,
        }
        binding = SourceBinding.from_dict(data)
        self.assertEqual(binding.to_dict(), data)
        with self.assertRaises(JsonValidationError):
            SourceBinding.from_dict({**data, "extra": 1})

    def test_thread_index_active_must_exist(self) -> None:
        entry = ThreadEntry(tid="30099686", name="帖", enabled=True,
                            canonical_url="https://bbs.nga.cn/read.php?tid=30099686",
                            config_revision=0, created_at="2026-09-04T00:00:00+00:00",
                            updated_at="2026-09-04T00:00:00+00:00")
        index = ThreadIndex(schema_version="1", revision=0, active_tid="30099686",
                            entries=(entry,))
        self.assertEqual(ThreadIndex.from_dict(index.to_dict()).active_tid, "30099686")
        bad = index.to_dict()
        bad["active_tid"] = "99999999"
        with self.assertRaises(JsonValidationError):
            ThreadIndex.from_dict(bad)

    def test_start_three_shapes(self) -> None:
        now = ThreadStart(mode="now", page=None, date=None, timezone="UTC+08:00",
                          lower_bound_utc=None)
        self.assertEqual(ThreadStart.from_dict(now.to_dict()), now)
        page = ThreadStart(mode="page", page=5, date=None, timezone="UTC+08:00",
                           lower_bound_utc=None)
        self.assertEqual(ThreadStart.from_dict(page.to_dict()).page, 5)
        with self.assertRaises(JsonValidationError):
            ThreadStart.from_dict({"mode": "date", "page": 1, "date": "2026-08-01",
                                   "timezone": "UTC+08:00", "lower_bound_utc": None})
        with self.assertRaises(JsonValidationError):
            ThreadStart.from_dict({"mode": "date", "page": None, "date": "2026-08-01",
                                   "timezone": "UTC+09:00", "lower_bound_utc": None})

    def test_utc_to_shanghai_lower_bound(self) -> None:
        self.assertEqual(utc_to_shanghai_lower_bound("2026-08-01"),
                         "2026-07-31T16:00:00+00:00")

    def test_tid_validation(self) -> None:
        self.assertEqual(validate_tid("30099686"), "30099686")
        for bad in ("0", "abc", "-1", "1.5", "30099686x"):
            with self.assertRaises(ValueError):
                validate_tid(bad)


class StateV2Tests(unittest.TestCase):
    def test_parse_nga_posted_at(self) -> None:
        self.assertIsNone(parse_nga_posted_at("2026/08/01"))
        self.assertIsNone(parse_nga_posted_at("今天 10:00"))
        dt = parse_nga_posted_at("2026-08-01 00:00:00")
        self.assertEqual(dt.isoformat(), "2026-07-31T16:00:00+00:00")
        self.assertEqual(parse_nga_posted_at("2026-08-01").isoformat(),
                         "2026-07-31T16:00:00+00:00")
        self.assertIsNotNone(parse_nga_posted_at("2026-08-01 12:34"))

    def test_from_v1_maps_last_page(self) -> None:
        v1 = {
            "schema_version": "1", "tid": 30099686,
            "canonical_thread_url": "https://bbs.nga.cn/read.php?tid=30099686",
            "last_page": 173, "known_floors": {
                "910001001": {"content_sha256": "a" * 64, "last_seen_page": 173,
                              "trigger_found": False, "inbox_item_id": None,
                              "imported_content_sha256": None, "changed_after_import": False},
            },
            "baseline_created_at": "2026-08-31T14:39:29+00:00",
            "last_successful_scan_at": None, "last_result_summary": None,
        }
        state = from_v1_state(v1)
        self.assertEqual(state.frontier_page, 173)
        self.assertEqual(state.next_scan_page, 173)
        self.assertEqual(state.observed_last_page, 173)
        self.assertIn("pid", state.known_floors["910001001"])

    def test_v2_roundtrip_and_validation(self) -> None:
        state = WatcherStateV2(
            schema_version="2", tid="30099686",
            canonical_thread_url="https://bbs.nga.cn/read.php?tid=30099686",
            frontier_page=0, observed_last_page=None, next_scan_page=1,
            known_floors={}, date_exclusions={},
            baseline_created_at="2026-09-04T00:00:00+00:00",
            last_successful_scan_at=None, last_result_summary=None, active_rescan=None,
        )
        self.assertEqual(WatcherStateV2.from_dict(state.to_dict()).tid, "30099686")
        bad = state.to_dict()
        bad["next_scan_page"] = 5  # frontier=0 -> 只允许 1
        with self.assertRaises(JsonValidationError):
            WatcherStateV2.from_dict(bad)
        bad2 = state.to_dict()
        bad2["extra"] = 1
        with self.assertRaises(JsonValidationError):
            WatcherStateV2.from_dict(bad2)

    def test_unknown_version_blocked(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"schema_version": "9", "tid": 1}),
                            encoding="utf-8")
            with self.assertRaises(JsonValidationError):
                read_state_file(path)


if __name__ == "__main__":
    unittest.main()
