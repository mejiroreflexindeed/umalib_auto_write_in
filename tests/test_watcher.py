"""Offline tests for the NGA Watcher core (TASK-011).

No network, no Edge, no real profile/snapshots. All scans use injected fetch
functions and per-test temporary Inbox/state files under private-state.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from umalib_ingest.domain.models import SubmissionOrigin
from umalib_ingest.inbox import InboxRepository
from umalib_ingest.nga_access.errors import NgaAccessError
from umalib_ingest.nga_access.fetch import FetchedThreadPage
from umalib_ingest.nga_access.parsing import FloorSnapshot
from umalib_ingest.parser import parse_submission
from umalib_ingest.watcher import diff, plan
from umalib_ingest.watcher.baseline import baseline_from_snapshot
from umalib_ingest.watcher.scan import run_scan
from umalib_ingest.watcher.state import (
    PRIVATE_STATE_ROOT,
    KnownFloor,
    StateLock,
    WatcherConfig,
    WatcherState,
    atomic_write_json,
    load_state,
    sha256_bytes,
    state_file_sha256,
    state_from_dict,
    validate_state_path,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "watcher"
T0 = "2026-08-31T00:00:00+00:00"
T1 = "2026-08-31T01:00:00+00:00"


def fixed_clock(*values: str):
    index = 0

    def clock() -> str:
        nonlocal index
        value = values[min(index, len(values) - 1)]
        index += 1
        return value

    return clock


def make_floor(pid: str, content: str, floor_number: int | None = None) -> FloorSnapshot:
    return FloorSnapshot(
        pid=pid,
        floor_number=floor_number,
        author_present=True,
        posted_at_raw="2026-08-31 19:00",
        edited_at_raw=None,
        content_format="ubbcode",
        content_text=content,
        content_raw_fragment=content,
    )


def make_fetched(
    page: int,
    last_page: int,
    floors,
    diagnostics=(),
    status: int = 200,
) -> FetchedThreadPage:
    return FetchedThreadPage(
        tid=30099686,
        page=page,
        last_page=last_page,
        status=status,
        final_url=f"https://bbs.nga.cn/read.php?tid=30099686&page={page}",
        content_type="text/html; charset=utf-8",
        encoding="utf-8",
        body_sha256="ab" * 32,
        body_bytes=0,
        snapshot_outcome="written",
        floors=tuple(floors),
        diagnostics=tuple(diagnostics),
    )


def make_state(
    *,
    last_page: int = 173,
    floors: dict[str, KnownFloor] | None = None,
    baseline_at: str = T0,
) -> WatcherState:
    return WatcherState(
        schema_version="1",
        tid=30099686,
        canonical_thread_url="https://bbs.nga.cn/read.php?tid=30099686",
        last_page=last_page,
        known_floors=floors or {},
        baseline_created_at=baseline_at,
        last_successful_scan_at=None,
        last_result_summary=None,
    )


class WatcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        PRIVATE_STATE_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(PRIVATE_STATE_ROOT), prefix="nga-watch-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state_file = self.tmp / "watcher-state.json"
        self.snapshot_dir = self.tmp / "snaps"
        self.inbox_db = self.tmp / "inbox.db"

    def config(self, **kwargs) -> WatcherConfig:
        return WatcherConfig(
            state_file=kwargs.pop("state_file", self.state_file),
            snapshot_dir=kwargs.pop("snapshot_dir", self.snapshot_dir),
            inbox_db=kwargs.pop("inbox_db", self.inbox_db),
            **kwargs,
        )

    def write_state(self, state: WatcherState, path: Path | None = None) -> str:
        return atomic_write_json(path or self.state_file, state.to_dict())

    def run_cli(self, *args: str):
        env = dict(
            os.environ,
            PYTHONPATH=str(PROJECT_ROOT / "src"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        return subprocess.run(
            [sys.executable, "-B", "-m", "umalib_ingest.watcher", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )


class StateTests(WatcherTestCase):
    def test_roundtrip_and_stable_json(self) -> None:
        floors = {
            "123456789": KnownFloor(
                pid="123456789",
                content_sha256="a" * 64,
                last_seen_page=173,
                trigger_found=True,
                inbox_item_id=5,
                imported_content_sha256="b" * 64,
                changed_after_import=False,
            )
        }
        state = make_state(floors=floors)
        data = json.loads(state.to_json())
        restored = state_from_dict(data)
        self.assertEqual(restored.to_json(), state.to_json())
        self.assertEqual(state.sha256(), state.sha256())

    def test_invalid_fields_rejected(self) -> None:
        base = make_state().to_dict()
        bad = [
            dict(base, unknown_key=1),
            dict(base, schema_version="2"),
            dict(base, tid=1),
            dict(base, canonical_thread_url="https://evil.example.com/"),
            dict(base, last_page=True),
            dict(base, last_page=-1),
            dict(base, last_page=0),
            dict(base, known_floors="x"),
            dict(base, known_floors={"123": {"pid": "abc", "content_sha256": "x",
                                             "last_seen_page": 1, "trigger_found": 1,
                                             "inbox_item_id": None,
                                             "imported_content_sha256": None,
                                             "changed_after_import": False}}),
            dict(base, known_floors={"0": make_state().known_floors}),
            dict(base, baseline_created_at=1),
            dict(base, last_successful_scan_at=1),
            dict(base, last_result_summary="x"),
        ]
        for index, payload in enumerate(bad):
            with self.subTest(case=index):
                with self.assertRaises(NgaAccessError) as caught:
                    state_from_dict(payload)
                self.assertEqual(caught.exception.code, "watcher_state_invalid")

    def test_known_floor_validation(self) -> None:
        good = {
            "pid": "123456789",
            "content_sha256": "a" * 64,
            "last_seen_page": 173,
            "trigger_found": False,
            "inbox_item_id": None,
            "imported_content_sha256": None,
            "changed_after_import": False,
        }
        from umalib_ingest.watcher.state import known_floor_from_dict

        self.assertEqual(known_floor_from_dict(good).pid, "123456789")
        for bad_key in ("pid", "content_sha256", "last_seen_page", "trigger_found",
                        "inbox_item_id", "imported_content_sha256", "changed_after_import"):
            payload = dict(good)
            payload["extra"] = 1
            with self.assertRaises(NgaAccessError):
                known_floor_from_dict(payload)
            payload = dict(good)
            if bad_key == "inbox_item_id":
                payload[bad_key] = 0
            elif bad_key == "trigger_found":
                payload[bad_key] = 1
            elif bad_key == "changed_after_import":
                payload[bad_key] = 1
            elif bad_key == "last_seen_page":
                payload[bad_key] = True
            elif bad_key == "pid":
                payload[bad_key] = "0"
            elif bad_key in ("content_sha256", "imported_content_sha256"):
                payload[bad_key] = "not-a-hash"
            with self.assertRaises(NgaAccessError):
                known_floor_from_dict(payload)

    def test_state_path_validation(self) -> None:
        for bad in (
            "relative/state.json",
            str(PROJECT_ROOT / "state.json"),
            str(self.tmp / "state.json.lock"),
            str(self.tmp / "state.json.tmp"),
            str(Path("D:/")),
        ):
            with self.subTest(path=bad):
                with self.assertRaises(NgaAccessError):
                    validate_state_path(bad)

    def test_atomic_write_failure_no_half_file(self) -> None:
        from umalib_ingest.watcher import state as state_module

        original = state_module.os.replace
        state_module.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        try:
            with self.assertRaises(OSError):
                atomic_write_json(self.state_file, make_state().to_dict())
        finally:
            state_module.os.replace = original
        self.assertFalse(self.state_file.exists())
        leftovers = [p for p in self.tmp.iterdir() if p.name.startswith(".nga-watcher-")]
        self.assertEqual(leftovers, [])

    def test_lock_exclusive_and_released(self) -> None:
        with StateLock(self.state_file):
            with self.assertRaises(NgaAccessError) as caught:
                with StateLock(self.state_file):
                    pass
            self.assertEqual(caught.exception.code, "watcher_locked")
        self.assertFalse(Path(str(self.state_file) + ".lock").exists())
        with StateLock(self.state_file):
            pass


class BaselineTests(WatcherTestCase):
    def _build_snapshot_dir(self, html: str | None = None, expected_floors: int = 20) -> None:
        html = html if html is not None else (
            FIXTURES_DIR / "page-173.decoded.html"
        ).read_text("utf-8")
        body = html.encode("utf-8")
        body_sha = sha256_bytes(body)
        thread_dir = self.snapshot_dir / "tid-30099686"
        thread_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "tid": 30099686,
            "page_label": "173",
            "status": 200,
            "final_url": "https://bbs.nga.cn/read.php?tid=30099686&page=173",
            "content_type": "text/html; charset=utf-8",
            "body_bytes": len(body),
            "body_sha256": body_sha,
            "diagnostics": [],
        }
        (thread_dir / "page-173.response.bin").write_bytes(body)
        (thread_dir / "page-173.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        manifest = {
            "tid": 30099686,
            "pages": [
                {"page_label": "1", "status": 200, "body_sha256": "a" * 64, "last_page": 173},
                {"page_label": "173", "status": 200, "body_sha256": body_sha,
                 "last_page": 173, "diagnostics": []},
            ],
            "diagnostics": [],
        }
        (self.snapshot_dir / "probe-result.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )

    def test_baseline_creates_state_without_parser_inbox(self) -> None:
        self._build_snapshot_dir()
        with mock.patch("umalib_ingest.watcher.baseline.parsing.parse_html_thread",
                        wraps=None) as parse_spy:
            pass
        with mock.patch("umalib_ingest.watcher.baseline.diff.floor_to_known",
                        wraps=diff.floor_to_known) as floor_spy:
            result = baseline_from_snapshot(self.config(), clock=fixed_clock(T0))
        self.assertEqual(result.mode, "baseline")
        self.assertEqual(result.current_last_page, 173)
        self.assertEqual(result.new_floor_count, 20)
        state = load_state(self.state_file)
        self.assertEqual(len(state.known_floors), 20)
        self.assertEqual(state.last_page, 173)
        self.assertEqual(state.baseline_created_at, T0)
        self.assertTrue(all(floor.inbox_item_id is None for floor in state.known_floors.values()))
        self.assertTrue(all(floor.imported_content_sha256 is None for floor in state.known_floors.values()))
        self.assertEqual(state.sha256(), state_file_sha256(self.state_file))
        raw = state.to_json()
        self.assertNotIn("合成普通讨论", raw)
        self.assertNotIn("author", raw)
        self.assertNotIn("标题", raw)
        triggers = [f for f in state.known_floors.values() if f.trigger_found]
        self.assertEqual(len(triggers), 2)

    def test_baseline_rejects_bad_evidence(self) -> None:
        self._build_snapshot_dir()
        thread_dir = self.snapshot_dir / "tid-30099686"
        meta_path = thread_dir / "page-173.meta.json"
        meta = json.loads(meta_path.read_text("utf-8"))
        meta["body_sha256"] = "f" * 64
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        with self.assertRaises(NgaAccessError) as caught:
            baseline_from_snapshot(self.config(), clock=fixed_clock(T0))
        self.assertEqual(caught.exception.code, "baseline_snapshot_invalid")

        self._build_snapshot_dir()
        manifest = json.loads((self.snapshot_dir / "probe-result.json").read_text("utf-8"))
        manifest["pages"][1]["status"] = 403
        (self.snapshot_dir / "probe-result.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        with self.assertRaises(NgaAccessError):
            baseline_from_snapshot(self.config(), clock=fixed_clock(T0))

    def test_baseline_rejects_existing_state(self) -> None:
        self._build_snapshot_dir()
        baseline_from_snapshot(self.config(), clock=fixed_clock(T0))
        with self.assertRaises(NgaAccessError) as caught:
            baseline_from_snapshot(self.config(), clock=fixed_clock(T0))
        self.assertEqual(caught.exception.code, "watcher_state_exists")

    def test_baseline_rejects_unrecognized_structure(self) -> None:
        self._build_snapshot_dir(html="<html><body>no floors</body></html>")
        with self.assertRaises(NgaAccessError) as caught:
            baseline_from_snapshot(self.config(), clock=fixed_clock(T0))
        self.assertEqual(caught.exception.code, "baseline_snapshot_invalid")


class PagePlanTests(WatcherTestCase):
    def _state(self, last_page):
        return make_state(last_page=last_page)

    def test_plans(self) -> None:
        config = self.config(max_scan_pages=12)
        self.assertEqual(plan.build_page_plan(self._state(173), 173, config)[0], [173])
        self.assertEqual(plan.build_page_plan(self._state(173), 174, config)[0], [173, 174])
        self.assertEqual(
            plan.build_page_plan(self._state(173), 176, config)[0],
            [173, 174, 175, 176],
        )

    def test_regression_blocker(self) -> None:
        with self.assertRaises(NgaAccessError) as caught:
            plan.build_page_plan(self._state(174), 173, self.config())
        self.assertEqual(caught.exception.code, "thread_page_regressed")

    def test_range_limits(self) -> None:
        with self.assertRaises(NgaAccessError) as caught:
            plan.build_page_plan(self._state(173), 186, self.config(max_scan_pages=12))
        self.assertEqual(caught.exception.code, "scan_range_too_large")
        config = self.config(max_scan_pages=25)
        pages, _ = plan.build_page_plan(self._state(173), 197, config)
        self.assertEqual(len(pages), 25)
        with self.assertRaises(NgaAccessError):
            plan.build_page_plan(self._state(173), 199, self.config(max_scan_pages=25))


class DiffTests(WatcherTestCase):
    def test_has_trigger(self) -> None:
        self.assertTrue(diff.has_trigger("前缀\n[收录]\n后缀"))
        self.assertTrue(diff.has_trigger("  [收录]  "))
        self.assertFalse(diff.has_trigger("正文[收录]不是独立行"))
        self.assertFalse(diff.has_trigger("普通讨论"))

    def test_canonical_floor_url(self) -> None:
        url = diff.canonical_floor_url(30099686, "578248505")
        self.assertEqual(url, "https://bbs.nga.cn/read.php?tid=30099686&pid=578248505")
        self.assertNotIn("to=1", url)
        with self.assertRaises(ValueError):
            diff.canonical_floor_url(0, "1")
        with self.assertRaises(ValueError):
            diff.canonical_floor_url(1, "abc")

    def test_build_floor_origin(self) -> None:
        origin = diff.build_floor_origin(30099686, "578248505", 12)
        self.assertEqual(origin.platform, "nga")
        self.assertEqual(origin.thread_id, "30099686")
        self.assertEqual(origin.post_id, "578248505")
        self.assertEqual(origin.floor, 12)
        self.assertEqual(
            origin.canonical_url,
            "https://bbs.nga.cn/read.php?tid=30099686&pid=578248505",
        )

    def test_content_hash_stable(self) -> None:
        floor = make_floor("1", "正文[color=red]色[/color]")
        self.assertEqual(diff.floor_content_sha256(floor), diff.floor_content_sha256(floor))
        self.assertEqual(
            diff.floor_content_sha256(floor),
            sha256_bytes("正文[color=red]色[/color]".encode("utf-8")),
        )

    def test_adapt_preserves_raw_and_keeps_parsed_fields(self) -> None:
        raw = "[b]标题[/b]\n[收录]\n[标题] 标题甲\n[作者] 作者甲\n[标签] 角色\n\n正文含[color=#C00000]颜色[/color]与[url=https://bbs.nga.cn/read.php?tid=99999999]链接[/url]及[术语]注释。"
        text = "[收录]\n[标题] 标题甲\n[作者] 作者甲\n[标签] 角色\n\n正文含颜色与链接及[术语]注释。"
        floor = FloorSnapshot(
            pid="578248505",
            floor_number=1,
            author_present=True,
            posted_at_raw="2026-08-31 19:00",
            edited_at_raw=None,
            content_format="ubbcode",
            content_text=text,
            content_raw_fragment=raw,
        )
        draft = parse_submission(floor.content_text, origin=diff.build_floor_origin(30099686, floor.pid, 1))
        diff.adapt_draft_with_floor(draft, floor, 30099686)
        self.assertEqual(draft.content.raw_text, raw)
        self.assertIn("[color=#C00000]", draft.content.raw_text)
        self.assertIn("[url=https://bbs.nga.cn/read.php?tid=99999999]", draft.content.raw_text)
        self.assertIn("[术语]注释", draft.content.raw_text)
        self.assertNotIn("[color=", draft.content.body_text)
        self.assertNotIn("[url=", draft.content.body_text)
        self.assertIn("[术语]注释", draft.content.body_text)
        self.assertEqual(
            draft.metadata.sources.nga,
            ["https://bbs.nga.cn/read.php?tid=30099686&pid=578248505"],
        )
        self.assertEqual(draft.metadata.title, "标题甲")
        self.assertEqual(draft.diagnostics.missing_fields, [])

    def test_adapt_preserves_unknown_brackets(self) -> None:
        floor = make_floor("1", "普通讨论[未知注释]保留")
        draft = parse_submission(floor.content_text)
        diff.adapt_draft_with_floor(draft, floor, 30099686)
        self.assertIn("[未知注释]", draft.content.raw_text)
        self.assertIn("[未知注释]", draft.content.body_text)


class ScanTests(WatcherTestCase):
    def _state_with_floors(self, floors: dict[str, KnownFloor], last_page=173) -> Path:
        self.write_state(make_state(last_page=last_page, floors=floors))
        return self.state_file

    def _scan(self, state_file, pages, *, dry_run=False, sleep=None, inbox_db=None):
        config = self.config(state_file=state_file, inbox_db=inbox_db or self.inbox_db)
        calls = []
        synthetic_first = make_fetched(
            1, max(p.last_page for p in pages), []
        )

        def fetch_fn(session, tid, page, snapshot_dir):
            for candidate in pages:
                if candidate.page == page:
                    calls.append(page)
                    return candidate
            if page == 1:
                calls.append(1)
                return synthetic_first
            raise AssertionError(f"unexpected page {page}")

        class FakeSession:
            def close(self):
                pass

        return run_scan(
            config,
            dry_run=dry_run,
            session_factory=lambda: FakeSession(),
            fetch_fn=fetch_fn,
            sleep=sleep or (lambda seconds: None),
            clock=fixed_clock(T0, T1),
        ), calls

    def test_new_discussion_not_imported(self) -> None:
        state_file = self._state_with_floors({})
        pages = [
            make_fetched(173, 173, [make_floor("900000001", "普通讨论")]),
        ]
        result, calls = self._scan(state_file, pages)
        self.assertEqual(result.new_floor_count, 1)
        self.assertEqual(result.discussion_count, 1)
        self.assertEqual(result.triggered_count, 0)
        self.assertEqual(result.imported_items, ())
        state = load_state(state_file)
        floor = state.known_floors["900000001"]
        self.assertFalse(floor.trigger_found)
        self.assertIsNone(floor.inbox_item_id)
        repo = InboxRepository(self.inbox_db)
        repo.initialize()
        self.assertEqual(repo.list_items(), [])

    def test_new_complete_and_incomplete_import(self) -> None:
        state_file = self._state_with_floors({})
        complete = (
            "[收录]\n[标题] 标题甲\n[作者] 作者甲\n[标签] 角色\n\n正文甲。\n"
        )
        incomplete = "[收录]\n[标题] 标题乙\n\n正文乙。\n"
        pages = [
            make_fetched(
                173,
                173,
                [make_floor("900000001", complete), make_floor("900000002", incomplete)],
            ),
        ]
        result, _ = self._scan(state_file, pages)
        self.assertEqual(result.triggered_count, 2)
        self.assertEqual(len(result.imported_items), 2)
        by_id = {item.item_id: item.status for item in result.imported_items}
        self.assertEqual(sorted(by_id.values()), ["candidate", "incomplete"])
        state = load_state(state_file)
        self.assertTrue(state.known_floors["900000001"].trigger_found)
        self.assertTrue(state.known_floors["900000002"].trigger_found)
        self.assertIsNotNone(state.known_floors["900000001"].inbox_item_id)
        repo = InboxRepository(self.inbox_db)
        repo.initialize()
        self.assertEqual(len(repo.list_items()), 2)

    def test_known_unchanged_no_reimport(self) -> None:
        content = "普通讨论"
        floor = make_floor("900000001", content)
        known = diff.floor_to_known(floor, last_seen_page=173)
        state_file = self._state_with_floors({"900000001": known})
        with mock.patch("umalib_ingest.watcher.scan.parse_submission",
                        wraps=parse_submission) as parser_spy:
            result, _ = self._scan(state_file, [make_fetched(173, 173, [floor])])
        self.assertEqual(result.unchanged_floor_count, 1)
        self.assertEqual(result.triggered_count, 0)
        self.assertEqual(result.imported_items, ())
        parser_spy.assert_not_called()
        state = load_state(state_file)
        self.assertEqual(state.known_floors["900000001"].last_seen_page, 173)

    def test_discussion_edited_to_add_trigger_imports(self) -> None:
        old = make_floor("900000001", "普通讨论")
        known = diff.floor_to_known(old, last_seen_page=173)
        state_file = self._state_with_floors({"900000001": known})
        new_content = "[收录]\n[标题] 新标题\n[作者] 新作者\n[标签] 标签\n\n正文。\n"
        result, _ = self._scan(
            state_file, [make_fetched(173, 173, [make_floor("900000001", new_content)])]
        )
        self.assertEqual(result.changed_floor_count, 1)
        self.assertEqual(result.triggered_count, 1)
        self.assertEqual(len(result.imported_items), 1)
        state = load_state(state_file)
        self.assertTrue(state.known_floors["900000001"].trigger_found)
        self.assertIsNotNone(state.known_floors["900000001"].inbox_item_id)

    def test_imported_floor_edited_only_reports(self) -> None:
        content = "[收录]\n[标题] 标题\n[作者] 作者\n[标签] 标签\n\n正文。\n"
        floor = make_floor("900000001", content)
        known = KnownFloor(
            pid="900000001",
            content_sha256=diff.floor_content_sha256(floor),
            last_seen_page=173,
            trigger_found=True,
            inbox_item_id=7,
            imported_content_sha256="c" * 64,
            changed_after_import=False,
        )
        state_file = self._state_with_floors({"900000001": known})
        new_content = "[收录]\n[标题] 标题\n[作者] 作者\n[标签] 标签\n\n修改后正文。\n"
        result, _ = self._scan(
            state_file, [make_fetched(173, 173, [make_floor("900000001", new_content)])]
        )
        self.assertEqual(result.changed_floor_count, 1)
        self.assertEqual(result.changed_after_import, (7,))
        self.assertIn("floor_changed_after_import", result.diagnostics)
        self.assertEqual(result.imported_items, ())
        state = load_state(state_file)
        floor_state = state.known_floors["900000001"]
        self.assertTrue(floor_state.changed_after_import)
        self.assertEqual(floor_state.imported_content_sha256, "c" * 64)
        self.assertEqual(floor_state.inbox_item_id, 7)

    def test_dry_run_writes_nothing(self) -> None:
        state_file = self._state_with_floors({})
        content = "[收录]\n[标题] 标题\n[作者] 作者\n[标签] 标签\n\n正文。\n"
        pages = [make_fetched(173, 173, [make_floor("900000001", content)])]
        result, _ = self._scan(state_file, pages, dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.state_after_sha256, None)
        self.assertFalse(self.inbox_db.exists())
        self.assertEqual(state_file_sha256(state_file), state_file_sha256(state_file))

    def test_crash_recovery_reuses_same_item(self) -> None:
        content = "[收录]\n[标题] 标题\n[作者] 作者\n[标签] 标签\n\n正文。\n"
        floor = make_floor("900000001", content)
        repo = InboxRepository(self.inbox_db)
        repo.initialize()
        origin = diff.build_floor_origin(30099686, floor.pid, 1)
        draft = parse_submission(floor.content_text, origin=origin)
        diff.adapt_draft_with_floor(draft, floor, 30099686)
        first_item = repo.import_draft(draft)
        state_file = self._state_with_floors({})
        result, _ = self._scan(state_file, [make_fetched(173, 173, [floor])])
        self.assertEqual(len(result.imported_items), 1)
        self.assertEqual(result.imported_items[0].item_id, first_item.id)
        self.assertEqual(len(repo.list_items()), 1)
        state = load_state(state_file)
        self.assertEqual(state.known_floors["900000001"].inbox_item_id, first_item.id)

    def test_crash_recovery_ambiguous_when_content_changed(self) -> None:
        old = "[收录]\n[标题] 旧\n[作者] 作者\n[标签] 标签\n\n旧正文。\n"
        floor_old = make_floor("900000001", old)
        repo = InboxRepository(self.inbox_db)
        repo.initialize()
        origin = diff.build_floor_origin(30099686, floor_old.pid, 1)
        draft = parse_submission(floor_old.content_text, origin=origin)
        diff.adapt_draft_with_floor(draft, floor_old, 30099686)
        repo.import_draft(draft)
        state_file = self._state_with_floors({})
        new_content = "[收录]\n[标题] 新\n[作者] 作者\n[标签] 标签\n\n新正文。\n"
        with self.assertRaises(NgaAccessError) as caught:
            self._scan(state_file, [make_fetched(173, 173, [make_floor("900000001", new_content)])])
        self.assertEqual(caught.exception.code, "state_import_recovery_ambiguous")
        self.assertEqual(len(repo.list_items()), 1)

    def test_state_external_change_rejected(self) -> None:
        content = "[收录]\n[标题] 标题\n[作者] 作者\n[标签] 标签\n\n正文。\n"
        state_file = self._state_with_floors({})
        pages = [make_fetched(173, 173, [make_floor("900000001", content)])]
        original = state_file_sha256
        state_file.write_text('{"tampered": true}', encoding="utf-8")
        with self.assertRaises(NgaAccessError) as caught:
            self._scan(state_file, pages)
        self.assertEqual(caught.exception.code, "watcher_state_invalid")

    def test_duplicate_pid_same_hash_merged(self) -> None:
        floor = make_floor("900000001", "同一内容")
        state_file = self._state_with_floors({})
        pages = [
            make_fetched(173, 174, [floor]),
            make_fetched(174, 174, [floor]),
        ]
        result, calls = self._scan(state_file, pages)
        self.assertEqual(result.new_floor_count, 1)
        self.assertIn("duplicate_pid_across_pages", result.diagnostics)
        self.assertEqual(calls, [1, 173, 174])

    def test_duplicate_pid_different_hash_blocker(self) -> None:
        state_file = self._state_with_floors({})
        pages = [
            make_fetched(173, 174, [make_floor("900000001", "内容一")]),
            make_fetched(174, 174, [make_floor("900000001", "内容二")]),
        ]
        with self.assertRaises(NgaAccessError) as caught:
            self._scan(state_file, pages)
        self.assertEqual(caught.exception.code, "duplicate_pid_across_pages")

    def test_import_failure_does_not_advance_state(self) -> None:
        state_file = self._state_with_floors({})
        content = "[收录]\n[标题] 标题\n[作者] 作者\n[标签] 标签\n\n正文。\n"
        pages = [make_fetched(173, 173, [make_floor("900000001", content)])]
        before_sha = state_file_sha256(state_file)
        with mock.patch("umalib_ingest.watcher.scan._import_floor",
                        side_effect=NgaAccessError("page_fetch_failed", "simulated")):
            with self.assertRaises(NgaAccessError):
                self._scan(state_file, pages)
        self.assertEqual(state_file_sha256(state_file), before_sha)

    def test_inbox_schema_unchanged(self) -> None:
        state_file = self._state_with_floors({})
        content = "[收录]\n[标题] 标题\n[作者] 作者\n[标签] 标签\n\n正文。\n"
        self._scan(state_file, [make_fetched(173, 173, [make_floor("900000001", content)])])
        conn = sqlite3.connect(str(self.inbox_db))
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            conn.close()

    def test_scan_updates_last_page_and_summary(self) -> None:
        state_file = self._state_with_floors({}, last_page=173)
        pages = [
            make_fetched(173, 175, [make_floor("900000001", "普通讨论一")]),
            make_fetched(174, 175, [make_floor("900000002", "普通讨论二")]),
            make_fetched(175, 175, [make_floor("900000003", "普通讨论三")]),
        ]
        sleeps = []
        result, calls = self._scan(state_file, pages, sleep=sleeps.append)
        self.assertEqual(result.current_last_page, 175)
        self.assertEqual(calls, [1, 173, 174, 175])
        self.assertGreaterEqual(len(sleeps), 2)
        state = load_state(state_file)
        self.assertEqual(state.last_page, 175)
        self.assertEqual(state.last_successful_scan_at, T0)
        self.assertEqual(state.last_result_summary["new_floor_count"], 3)


class CliTests(WatcherTestCase):
    def test_help_and_status_do_not_import_playwright(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("baseline-from-snapshot", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("status", "--state-file", str(self.state_file))
        self.assertEqual(result.returncode, 3)
        self.assertIn("watcher_state_missing", result.stderr)

    def test_scan_without_playwright_reports_safe_error(self) -> None:
        self.write_state(make_state())
        result = self.run_cli(
            "scan",
            "--state-file", str(self.state_file),
            "--inbox-db", str(self.inbox_db),
            "--profile-dir", str(self.tmp / "profile"),
            "--snapshot-dir", str(self.snapshot_dir),
            "--max-scan-pages", "1",
            "--dry-run",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)

    def test_status_output_minimal(self) -> None:
        self.write_state(make_state(last_page=173))
        result = self.run_cli("status", "--state-file", str(self.state_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tid: 30099686", result.stdout)
        self.assertIn("last_page: 173", result.stdout)
        self.assertNotIn("900000001", result.stdout)
        self.assertNotIn("content", result.stdout)

    def test_console_entry_point_declared(self) -> None:
        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text("utf-8")
        self.assertIn('nga-watch = "umalib_ingest.watcher.__main__:main"', pyproject)
        import importlib

        module = importlib.import_module("umalib_ingest.watcher.__main__")
        self.assertTrue(callable(module.main))


class GateAReworkTests(WatcherTestCase):
    """TASK-011R+012 门 A：Codex 复现缺陷与一致性边界的回归测试."""

    def _fetch_for(self, pages):
        def fetch_fn(session, tid, page, snapshot_dir):
            for candidate in pages:
                if candidate.page == page:
                    return candidate
            if page == 1:
                return make_fetched(1, max(p.last_page for p in pages), [])
            raise AssertionError(f"unexpected page {page}")

        return fetch_fn

    def test_real_scan_without_inbox_fails_closed(self) -> None:
        """MISSING_INBOX_DB: no session, no network, no state change."""
        self.write_state(make_state())
        config = WatcherConfig(
            state_file=self.state_file,
            snapshot_dir=self.snapshot_dir,
            inbox_db=None,
        )
        before_sha = state_file_sha256(self.state_file)
        session_created = []
        fetch_calls = []

        def session_factory():
            session_created.append(1)
            raise AssertionError("session 不应被创建")

        with self.assertRaises(NgaAccessError) as caught:
            run_scan(
                config,
                dry_run=False,
                session_factory=session_factory,
                fetch_fn=lambda *a, **k: fetch_calls.append(1),
            )
        self.assertEqual(caught.exception.code, "watcher_inbox_required")
        self.assertEqual(session_created, [])
        self.assertEqual(fetch_calls, [])
        self.assertEqual(state_file_sha256(self.state_file), before_sha)

    def test_cli_missing_inbox_db_no_side_effects(self) -> None:
        self.write_state(make_state())
        result = self.run_cli(
            "scan",
            "--state-file", str(self.state_file),
            "--profile-dir", str(self.tmp / "profile"),
            "--snapshot-dir", str(self.snapshot_dir),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--inbox-db", result.stderr)
        self.assertFalse(self.inbox_db.exists())
        self.assertEqual(
            state_file_sha256(self.state_file),
            state_file_sha256(self.state_file),
        )

    def test_login_confirm_then_one_retry_success(self) -> None:
        """LOGIN_RETRY: login page → user confirms → exactly one retry succeeds."""
        self.write_state(make_state())
        login_page = make_fetched(
            1, 173, [], diagnostics=("manual_login_required",), status=200
        )
        good_page = make_fetched(1, 173, [])
        page_173 = make_fetched(173, 173, [make_floor("900000001", "普通讨论")])
        calls = []

        def fetch_fn(session, tid, page, snapshot_dir):
            calls.append(page)
            if page == 1:
                return login_page if len(calls) == 1 else good_page
            return page_173

        confirmed = []

        def confirm_login():
            confirmed.append(1)

        result = run_scan(
            self.config(),
            dry_run=True,
            session_factory=lambda: FakeSession(),
            fetch_fn=fetch_fn,
            sleep=lambda s: None,
            confirm_login=confirm_login,
        )
        self.assertEqual(calls, [1, 1, 173])
        self.assertEqual(confirmed, [1])
        self.assertEqual(result.new_floor_count, 1)
        self.assertEqual(result.diagnostics, ())

    def test_login_not_confirmed_timeout(self) -> None:
        self.write_state(make_state())
        login_page = make_fetched(
            1, 173, [], diagnostics=("manual_login_required",), status=200
        )
        calls = []

        def fetch_fn(session, tid, page, snapshot_dir):
            calls.append(page)
            return login_page

        with self.assertRaises(NgaAccessError) as caught:
            run_scan(
                self.config(),
                dry_run=True,
                session_factory=lambda: FakeSession(),
                fetch_fn=fetch_fn,
                sleep=lambda s: None,
                confirm_login=None,
                login_wait_file=self.tmp / "never-created",
                login_wait_timeout_seconds=0.5,
            )
        self.assertEqual(caught.exception.code, "login_not_confirmed")
        self.assertEqual(calls, [1])

    def test_login_confirmed_but_still_failing_no_third_attempt(self) -> None:
        self.write_state(make_state())
        login_page = make_fetched(
            1, 173, [], diagnostics=("manual_login_required",), status=200
        )
        calls = []

        def fetch_fn(session, tid, page, snapshot_dir):
            calls.append(page)
            return login_page

        with self.assertRaises(NgaAccessError) as caught:
            run_scan(
                self.config(),
                dry_run=True,
                session_factory=lambda: FakeSession(),
                fetch_fn=fetch_fn,
                sleep=lambda s: None,
                confirm_login=lambda: None,
            )
        self.assertEqual(caught.exception.code, "login_required")
        self.assertEqual(calls, [1, 1])

    def test_login_diagnostics_are_content_free(self) -> None:
        """登录/验证码诊断只落无正文 metadata（复用 TASK-010 语义）。"""
        from umalib_ingest.nga_access import snapshot as snapshot_module

        snapshot_module.write_diagnostic_meta(self.snapshot_dir, 30099686, "1",
                                              {"status": 200, "diagnostics": ["manual_login_required"]})
        self.assertTrue((self.snapshot_dir / "tid-30099686" / "page-1.diag.json").exists())
        self.assertFalse(
            (self.snapshot_dir / "tid-30099686" / "page-1.response.bin").exists()
        )

    def test_first_interval_sleep(self) -> None:
        """FIRST_INTERVAL: [1,173] 必须有一次 >=3s wait."""
        self.write_state(make_state())
        sleeps = []
        pages = [
            make_fetched(1, 173, []),
            make_fetched(173, 173, [make_floor("900000001", "普通讨论")]),
        ]
        run_scan(
            self.config(),
            dry_run=True,
            session_factory=lambda: FakeSession(),
            fetch_fn=self._fetch_for(pages),
            sleep=sleeps.append,
        )
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 3.0)

    def test_interval_between_plan_pages(self) -> None:
        self.write_state(make_state(last_page=173))
        sleeps = []
        pages = [
            make_fetched(1, 175, []),
            make_fetched(173, 175, [make_floor("900000001", "普通讨论一")]),
            make_fetched(174, 175, [make_floor("900000002", "普通讨论二")]),
            make_fetched(175, 175, [make_floor("900000003", "普通讨论三")]),
        ]
        run_scan(
            self.config(),
            dry_run=True,
            session_factory=lambda: FakeSession(),
            fetch_fn=self._fetch_for(pages),
            sleep=sleeps.append,
        )
        self.assertEqual(len(sleeps), 3)
        self.assertTrue(all(s >= 3.0 for s in sleeps))

    def test_unsafe_profile_rejected_at_config(self) -> None:
        """PROFILE_BOUNDARY: 日常 Edge User Data 在配置阶段被拒绝."""
        local_app_data = os.environ.get("LOCALAPPDATA", "C:\\")
        edge_default = Path(local_app_data) / "Microsoft" / "Edge" / "User Data"
        with self.assertRaises(NgaAccessError) as caught:
            WatcherConfig(
                state_file=self.state_file,
                snapshot_dir=self.snapshot_dir,
                profile_dir=edge_default,
            )
        self.assertEqual(caught.exception.code, "unsafe_profile_path")
        with self.assertRaises(NgaAccessError):
            WatcherConfig(
                state_file=self.state_file,
                snapshot_dir=self.snapshot_dir,
                profile_dir=str(PROJECT_ROOT / "profile"),
            )
        with self.assertRaises(NgaAccessError):
            WatcherConfig(
                state_file=self.state_file,
                snapshot_dir=self.snapshot_dir,
                profile_dir="relative/profile",
            )

    def test_profile_snapshot_same_rejected(self) -> None:
        shared = PRIVATE_STATE_ROOT / "nga-shared-test"
        with self.assertRaises(NgaAccessError):
            WatcherConfig(
                state_file=self.state_file,
                snapshot_dir=str(shared),
                profile_dir=str(shared),
            )

    def test_canonical_state_strictness(self) -> None:
        """STATE_STRICTNESS: 非法时间、未知 summary 键、body 键必须拒绝."""
        base = make_state().to_dict()
        bad_time = dict(base, baseline_created_at="2026-08-31 22:39")
        with self.assertRaises(NgaAccessError):
            state_from_dict(bad_time)
        bad_tz = dict(base, baseline_created_at="2026-08-31T22:39:29+08:00")
        with self.assertRaises(NgaAccessError):
            state_from_dict(bad_tz)
        summary = {
            "mode": "scan",
            "previous_last_page": 173,
            "current_last_page": 175,
            "scanned_pages": [173, 174, 175],
            "new_floor_count": 1,
            "unchanged_floor_count": 0,
            "changed_floor_count": 0,
            "discussion_count": 2,
            "triggered_count": 1,
            "imported_item_count": 1,
            "changed_after_import_count": 0,
            "dry_run": False,
        }
        with_summary = dict(base, last_result_summary=summary,
                            last_successful_scan_at="2026-09-01T00:00:00+00:00")
        restored = state_from_dict(with_summary)
        self.assertEqual(restored.last_result_summary, summary)
        unknown_key = dict(base, last_result_summary={**summary, "extra": 1})
        with self.assertRaises(NgaAccessError):
            state_from_dict(unknown_key)
        body_key = dict(base, last_result_summary={**summary, "body": "正文"})
        with self.assertRaises(NgaAccessError):
            state_from_dict(body_key)
        missing_key = dict(base, last_result_summary={k: v for k, v in summary.items()
                                                      if k != "dry_run"})
        with self.assertRaises(NgaAccessError):
            state_from_dict(missing_key)

    def test_multi_floors_import_in_page_floor_order(self) -> None:
        self.write_state(make_state())
        contents = [
            "[收录]\n[标题] 一\n[作者] 甲\n[标签] 标签\n\n正文一。\n",
            "[收录]\n[标题] 二\n[作者] 乙\n[标签] 标签\n\n正文二。\n",
        ]
        floors = [make_floor(f"90000000{i}", contents[i]) for i in range(2)]
        pages = [
            make_fetched(1, 173, []),
            make_fetched(173, 173, floors),
        ]
        import_order = []
        original = parse_submission

        def spy_parse(text, origin=None):
            import_order.append(text)
            return original(text, origin=origin)

        with mock.patch("umalib_ingest.watcher.scan.parse_submission",
                        side_effect=spy_parse):
            result = run_scan(
                self.config(),
                dry_run=False,
                session_factory=lambda: FakeSession(),
                fetch_fn=self._fetch_for(pages),
                sleep=lambda s: None,
            )
        self.assertEqual(len(result.imported_items), 2)
        self.assertEqual(len(import_order), 2)
        self.assertIn("[标题] 一", import_order[0])
        self.assertIn("[标题] 二", import_order[1])

    def test_state_changed_before_import_aborts(self) -> None:
        self.write_state(make_state())
        content = "[收录]\n[标题] 一\n[作者] 甲\n[标签] 标签\n\n正文。\n"
        pages = [
            make_fetched(1, 173, []),
            make_fetched(173, 173, [make_floor("900000001", content)]),
        ]
        normal_fetch = self._fetch_for(pages)
        def changing_fetch(session, tid, page, snapshot_dir):
            fetched = normal_fetch(session, tid, page, snapshot_dir)
            if page == 173:
                # Inject the real boundary, not the ordinal number of hash reads.
                atomic_write_json(self.state_file, make_state(last_page=174).to_dict())
            return fetched

        with mock.patch("umalib_ingest.watcher.scan._import_floor") as importing:
            with self.assertRaises(NgaAccessError) as caught:
                run_scan(
                    self.config(),
                    dry_run=False,
                    session_factory=lambda: FakeSession(),
                    fetch_fn=changing_fetch,
                    sleep=lambda s: None,
                )
            importing.assert_not_called()
        self.assertEqual(caught.exception.code, "watcher_state_changed")
        repo = InboxRepository(self.inbox_db)
        repo.initialize()
        self.assertEqual(repo.list_items(), [])


class FakeSession:
    def close(self):
        pass


if __name__ == "__main__":
    unittest.main()
