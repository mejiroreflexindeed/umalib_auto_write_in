"""Tests for the approved submission handoff package (TASK-005)."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from umalib_ingest.handoff import (
    ApprovedPackageService,
    HandoffNotReadyError,
)
from umalib_ingest.inbox import InboxItemNotFoundError, InboxRepository
from umalib_ingest.parser import parse_submission
from umalib_ingest.review import ReviewService

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"


class FixedClock:
    def __init__(self, *timestamps: str) -> None:
        self._timestamps = list(timestamps)
        self._index = 0

    def __call__(self) -> str:
        value = self._timestamps[min(self._index, len(self._timestamps) - 1)]
        self._index += 1
        return value


class CountingFixedClock(FixedClock):
    def __init__(self, *timestamps: str) -> None:
        super().__init__(*timestamps)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return super().__call__()


def stable_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def recompute_package_sha256(package: dict) -> str:
    payload = {
        "schema_version": package["schema_version"],
        "inbox": package["inbox"],
        "submission": package["submission"],
    }
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def full_draft():
    return parse_submission(
        "[收录]\n[标题] 星光下的测试稿\n[作者] 测试作者\n[译者] 测试译者\n"
        "[系列] 测试系列\n[标签] 角色, 短篇\n\n正文第一行。\n"
    )


def incomplete_draft():
    return parse_submission("[收录]\n\n只有正文。\n")


def ignored_draft():
    return parse_submission("普通讨论，没有投稿标记。\n")


class HandoffTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "inbox.db"
        self.clock = CountingFixedClock(T0, T1, T2, T3)
        self.repo = InboxRepository(self.path, clock=self.clock)
        self.repo.initialize()
        self.service = ApprovedPackageService(self.repo)

    def import_and_approve(self, draft=None, reason="确认"):
        item = self.repo.import_draft(draft if draft is not None else full_draft())
        self.repo.transition(item.id, "approved", reason)
        return self.repo.get(item.id)

    def table_names(self) -> set[str]:
        raw = sqlite3.connect(str(self.path))
        try:
            return {
                row[0]
                for row in raw.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            raw.close()

    def replace_working_draft(self, item_id: int, draft: object) -> None:
        raw = sqlite3.connect(str(self.path))
        try:
            raw.execute(
                "UPDATE inbox_item SET working_draft_json = ? WHERE id = ?",
                (stable_json(draft), item_id),
            )
            raw.commit()
        finally:
            raw.close()

    def replace_hash(self, item_id: int, column: str, value: str | None) -> None:
        if column not in {"snapshot_sha256", "content_sha256"}:
            raise AssertionError("unexpected hash column")
        raw = sqlite3.connect(str(self.path))
        try:
            raw.execute(
                f"UPDATE inbox_item SET {column} = ? WHERE id = ?",
                (value, item_id),
            )
            raw.commit()
        finally:
            raw.close()


class HandoffPackageTests(HandoffTestCase):
    def test_approved_complete_package_top_level_structure(self) -> None:
        item = self.import_and_approve()
        package = self.service.build(item.id).to_dict()
        self.assertEqual(
            set(package.keys()),
            {"schema_version", "inbox", "submission", "package_sha256"},
        )
        self.assertEqual(package["schema_version"], "1.0")
        inbox = package["inbox"]
        self.assertEqual(
            set(inbox.keys()),
            {"item_id", "status", "snapshot_sha256", "content_sha256",
             "created_at", "approved_at"},
        )
        self.assertEqual(inbox["item_id"], item.id)
        self.assertEqual(inbox["status"], "approved")
        self.assertEqual(inbox["snapshot_sha256"], item.snapshot_sha256)
        self.assertEqual(inbox["content_sha256"], item.content_sha256)
        self.assertEqual(inbox["created_at"], item.created_at)
        self.assertEqual(inbox["approved_at"], T1)
        self.assertRegex(package["package_sha256"], r"^[0-9a-f]{64}$")

    def test_submission_equals_working_draft_deep_copy(self) -> None:
        item = self.import_and_approve()
        package = self.service.build(item.id).to_dict()
        working = self.repo.get(item.id).working_draft
        self.assertEqual(package["submission"], working)
        self.assertIsNot(package["submission"], working)
        for key in ("schema_version", "origin", "trigger", "raw_fields", "diagnostics"):
            self.assertIn(key, package["submission"])
        self.assertIn("raw_text", package["submission"]["content"])

    def test_approved_at_is_latest_approval_time(self) -> None:
        item = self.import_and_approve()
        self.repo.transition(item.id, "candidate", "退回")
        self.repo.transition(item.id, "approved", "再次批准")
        package = self.service.build(item.id).to_dict()
        self.assertEqual(package["inbox"]["approved_at"], T3)

    def test_stable_json_and_hash_independently_recomputable(self) -> None:
        item = self.import_and_approve()
        package = self.service.build(item.id)
        data = package.to_dict()
        self.assertEqual(recompute_package_sha256(data), data["package_sha256"])
        self.assertEqual(
            package.to_json(),
            stable_json(data),
        )
        self.assertRegex(package.to_json(), "package_sha256")

    def test_repeat_build_identical_without_clock(self) -> None:
        item = self.import_and_approve()
        calls_before = self.clock.calls
        first = self.service.build(item.id)
        second = self.service.build(item.id)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(self.clock.calls, calls_before)

    def test_mutating_to_dict_does_not_pollute(self) -> None:
        item = self.import_and_approve()
        first = self.service.build(item.id).to_dict()
        first["submission"]["metadata"]["title"] = "被污染"
        first["inbox"]["status"] = "exported"
        first["inbox"]["approved_at"] = "被污染"
        second = self.service.build(item.id).to_dict()
        self.assertEqual(second["submission"]["metadata"]["title"], "星光下的测试稿")
        self.assertEqual(second["inbox"]["status"], "approved")
        self.assertEqual(second["inbox"]["approved_at"], T1)
        self.assertEqual(second["package_sha256"], self.service.build(item.id).to_dict()["package_sha256"])

    def test_missing_item_raises(self) -> None:
        with self.assertRaises(InboxItemNotFoundError):
            self.service.build(999)

    def test_non_approved_statuses_rejected(self) -> None:
        candidate = self.repo.import_draft(full_draft())
        incomplete = self.repo.import_draft(incomplete_draft())
        suspected = self.repo.transition(
            self.repo.import_draft(parse_submission("[收录]\n\n另一条。\n")).id,
            "suspected_duplicate",
            "标记",
        )
        ignored = self.repo.import_draft(ignored_draft())
        exported_item = self.repo.import_draft(full_draft())
        self.repo.transition(exported_item.id, "approved", "确认")
        self.repo.transition(exported_item.id, "exported", "导出")
        for item in (candidate, incomplete, suspected, ignored, self.repo.get(exported_item.id)):
            with self.subTest(status=item.status):
                with self.assertRaises(HandoffNotReadyError):
                    self.service.build(item.id)

    def test_missing_fields_or_history_rejected(self) -> None:
        incomplete = self.repo.import_draft(incomplete_draft())
        self.repo.transition(incomplete.id, "approved", "直接批准")
        with self.assertRaises(HandoffNotReadyError):
            self.service.build(incomplete.id)
        candidate = self.repo.import_draft(full_draft())
        raw = sqlite3.connect(str(self.path))
        try:
            raw.execute("UPDATE inbox_item SET status = 'approved' WHERE id = ?", (candidate.id,))
            raw.commit()
        finally:
            raw.close()
        with self.assertRaises(HandoffNotReadyError):
            self.service.build(candidate.id)

    def test_malformed_protected_submission_contract_rejected(self) -> None:
        mutations = {
            "wrong_schema": lambda draft: draft.__setitem__("schema_version", "9.9"),
            "extra_root_key": lambda draft: draft.__setitem__("unexpected", True),
            "boolean_floor": lambda draft: draft["origin"].__setitem__("floor", True),
            "empty_tag": lambda draft: draft["metadata"].__setitem__("tags", [""]),
            "invalid_warning": lambda draft: draft["diagnostics"]["warnings"].append(
                {"code": "", "message": "bad", "field": None, "line": 1}
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                draft = parse_submission(
                    "[收录]\n[标题] 契约测试 " + name
                    + "\n[作者] 测试作者\n[标签] 测试\n\n正文 " + name + "。\n"
                )
                item = self.import_and_approve(draft)
                malformed = deepcopy(item.working_draft)
                mutate(malformed)
                self.replace_working_draft(item.id, malformed)
                with self.assertRaises(HandoffNotReadyError):
                    self.service.build(item.id)

    def test_inbox_integrity_hash_mismatch_rejected(self) -> None:
        snapshot_item = self.import_and_approve()
        self.replace_hash(snapshot_item.id, "snapshot_sha256", "0" * 64)
        with self.assertRaises(HandoffNotReadyError):
            self.service.build(snapshot_item.id)

        content_item = self.import_and_approve()
        self.replace_hash(content_item.id, "content_sha256", "f" * 64)
        with self.assertRaises(HandoffNotReadyError):
            self.service.build(content_item.id)

    def test_state_change_during_build_is_rejected(self) -> None:
        item = self.import_and_approve()
        review = ReviewService(self.repo)

        def racing_load(item_id: int):
            fields = review.load(item_id)
            self.repo.transition(item_id, "candidate", "模拟并发退回")
            return fields

        racing_review = SimpleNamespace(load=racing_load, validate=review.validate)
        service = ApprovedPackageService(self.repo, racing_review)
        with self.assertRaises(HandoffNotReadyError):
            service.build(item.id)
        self.assertEqual(self.repo.get(item.id).status, "candidate")

    def test_reapprove_after_edit_updates_package(self) -> None:
        item = self.import_and_approve()
        first = self.service.build(item.id).to_dict()
        self.repo.transition(item.id, "candidate", "退回修改")
        from umalib_ingest.review import ReviewService

        review = ReviewService(self.repo)
        fields = review.load(item.id)
        fields.title = "修改后的标题"
        review.save(item.id, fields)
        self.repo.transition(item.id, "approved", "再次批准")
        second = self.service.build(item.id).to_dict()
        self.assertEqual(second["inbox"]["approved_at"], T3)
        self.assertEqual(second["submission"]["metadata"]["title"], "修改后的标题")
        self.assertNotEqual(second["package_sha256"], first["package_sha256"])

    def test_build_does_not_change_inbox(self) -> None:
        item = self.import_and_approve()
        tables_before = self.table_names()
        history_before = [
            (h.id, h.from_status, h.to_status, h.reason, h.changed_at)
            for h in self.repo.list_history(item.id)
        ]
        calls_before = self.clock.calls
        self.service.build(item.id)
        after = self.repo.get(item.id)
        self.assertEqual(after.status, "approved")
        self.assertEqual(after.updated_at, item.updated_at)
        self.assertEqual(after.working_draft, item.working_draft)
        self.assertEqual(after.snapshot_sha256, item.snapshot_sha256)
        history_after = [
            (h.id, h.from_status, h.to_status, h.reason, h.changed_at)
            for h in self.repo.list_history(item.id)
        ]
        self.assertEqual(history_after, history_before)
        self.assertEqual(self.table_names(), tables_before)
        self.assertEqual(self.clock.calls, calls_before)


if __name__ == "__main__":
    unittest.main()
