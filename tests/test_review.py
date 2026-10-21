"""Tests for the review editing core (TASK-003)."""

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from umalib_ingest.inbox import (
    InboxError,
    InboxItemLockedError,
    InboxItemNotFoundError,
    InboxRepository,
)
from umalib_ingest.parser import parse_submission
from umalib_ingest.review import (
    InvalidReviewFieldsError,
    ReviewFields,
    ReviewService,
)

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"


class CountingFixedClock:
    """Returns fixed timestamps in call order (repeating the last), counting calls."""

    def __init__(self, timestamps: list[str]) -> None:
        self._timestamps = list(timestamps)
        self.calls = 0

    def __call__(self) -> str:
        value = self._timestamps[min(self.calls, len(self._timestamps) - 1)]
        self.calls += 1
        return value


def full_draft():
    return parse_submission(
        "[收录]\n"
        "[标题] 原标题\n"
        "[作者] 原作者\n"
        "[译者] 原译者\n"
        "[系列] 原系列\n"
        "[标签] 角色, 短篇\n"
        "[原作系列] os-url\n"
        "[原作] https://example.com/original?tid=1&to=1\n"
        "[译作系列] ts-url\n"
        "[译作] https://bbs.nga.cn/read.php?tid=1&pid=2&to=1\n"
        "\n"
        "正文第一行。\n"
        "正文中的[术语]保持。\n"
    )


def incomplete_draft():
    return parse_submission("[收录]\n\n只有正文。\n")


def ignored_draft():
    return parse_submission("普通讨论，没有投稿标记。\n")


def draft_with_diagnostics():
    return parse_submission(
        "[收录]\n"
        "[标题] 第一标题\n"
        "[标题] 第二标题\n"
        "[作者] 作者\n"
        "[标签] 标签\n"
        "[未来字段] 未来值\n"
        "\n"
        "正文。\n"
    )


class ReviewServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "inbox.db"
        self.clock = CountingFixedClock([T0, T1, T2])
        self.repo = InboxRepository(self.path, clock=self.clock)
        self.repo.initialize()
        self.service = ReviewService(self.repo)

    def import_item(self, draft):
        return self.repo.import_draft(draft)

    def write_working_draft(self, item_id: int, working: dict) -> None:
        raw = sqlite3.connect(str(self.path))
        try:
            raw.execute(
                "UPDATE inbox_item SET working_draft_json = ? WHERE id = ?",
                (json.dumps(working, ensure_ascii=False), item_id),
            )
            raw.commit()
        finally:
            raw.close()


class ReviewLoadTests(ReviewServiceTestCase):
    def test_load_extracts_all_fields_without_shared_lists(self) -> None:
        item = self.import_item(full_draft())
        fields = self.service.load(item.id)
        self.assertEqual(fields.title, "原标题")
        self.assertEqual(fields.author, "原作者")
        self.assertEqual(fields.translator, "原译者")
        self.assertEqual(fields.series, "原系列")
        self.assertEqual(fields.tags, ["角色", "短篇"])
        self.assertEqual(fields.sources.original_series, ["os-url"])
        self.assertEqual(
            fields.sources.original, ["https://example.com/original?tid=1&to=1"]
        )
        self.assertEqual(fields.sources.translation_series, ["ts-url"])
        self.assertEqual(
            fields.sources.translation,
            ["https://bbs.nga.cn/read.php?tid=1&pid=2&to=1"],
        )
        self.assertEqual(fields.sources.nga, [])
        self.assertEqual(fields.body_text, "正文第一行。\n正文中的[术语]保持。")
        fields.tags.append("被修改")
        fields.sources.original.append("被修改")
        fields.sources.nga.append("被修改")
        again = self.service.load(item.id)
        self.assertEqual(again.tags, ["角色", "短篇"])
        self.assertEqual(
            again.sources.original, ["https://example.com/original?tid=1&to=1"]
        )
        self.assertEqual(again.sources.nga, [])

    def test_load_missing_item_raises(self) -> None:
        with self.assertRaises(InboxItemNotFoundError):
            self.service.load(999)

    def test_load_malformed_working_draft_raises(self) -> None:
        item = self.import_item(full_draft())
        working = copy.deepcopy(item.working_draft)
        variants = []
        missing_metadata = copy.deepcopy(working)
        del missing_metadata["metadata"]
        variants.append(missing_metadata)
        missing_content = copy.deepcopy(working)
        del missing_content["content"]
        variants.append(missing_content)
        missing_diagnostics = copy.deepcopy(working)
        del missing_diagnostics["diagnostics"]
        variants.append(missing_diagnostics)
        bad_tags = copy.deepcopy(working)
        bad_tags["metadata"]["tags"] = "不是列表"
        variants.append(bad_tags)
        bad_body = copy.deepcopy(working)
        bad_body["content"]["body_text"] = None
        variants.append(bad_body)
        bad_sources = copy.deepcopy(working)
        bad_sources["metadata"]["sources"] = None
        variants.append(bad_sources)
        bad_nga = copy.deepcopy(working)
        bad_nga["metadata"]["sources"]["nga"] = "不是列表"
        variants.append(bad_nga)
        for index, bad in enumerate(variants):
            with self.subTest(variant=index):
                self.write_working_draft(item.id, bad)
                with self.assertRaises(InvalidReviewFieldsError):
                    self.service.load(item.id)

    def test_save_malformed_working_draft_raises(self) -> None:
        item = self.import_item(full_draft())
        working = copy.deepcopy(item.working_draft)
        del working["metadata"]
        self.write_working_draft(item.id, working)
        fields = ReviewFields(title="x")
        with self.assertRaises(InvalidReviewFieldsError):
            self.service.save(item.id, fields)


class ReviewNormalizationTests(ReviewServiceTestCase):
    def test_single_value_strip_and_empty_becomes_none(self) -> None:
        item = self.import_item(full_draft())
        fields = self.service.load(item.id)
        fields.title = "  你好  "
        fields.author = "   "
        fields.series = ""
        fields.translator = None
        saved = self.service.save(item.id, fields)
        metadata = saved.working_draft["metadata"]
        self.assertEqual(metadata["title"], "你好")
        self.assertIsNone(metadata["author"])
        self.assertIsNone(metadata["series"])
        self.assertIsNone(metadata["translator"])

    def test_tags_stripped_deduped_in_order(self) -> None:
        item = self.import_item(full_draft())
        fields = self.service.load(item.id)
        fields.tags = [" 角色 ", "", "角色", "短篇", " 短篇 ", "", "日常"]
        saved = self.service.save(item.id, fields)
        self.assertEqual(saved.working_draft["metadata"]["tags"], ["角色", "短篇", "日常"])

    def test_sources_stripped_keep_order_duplicates_not_normalized(self) -> None:
        item = self.import_item(full_draft())
        fields = self.service.load(item.id)
        fields.sources.nga = [
            " https://bbs.nga.cn/read.php?tid=1&to=1 ",
            "",
            " 同一条 ",
            " 同一条 ",
        ]
        fields.sources.original = [" example.com/a ", " example.com/a "]
        fields.sources.translation = [None]
        with self.assertRaises(InvalidReviewFieldsError):
            self.service.save(item.id, fields)
        fields.sources.translation = [" http://x.example/b/../c "]
        saved = self.service.save(item.id, fields)
        sources = saved.working_draft["metadata"]["sources"]
        self.assertEqual(
            sources["nga"],
            ["https://bbs.nga.cn/read.php?tid=1&to=1", "同一条", "同一条"],
        )
        self.assertEqual(sources["original"], ["example.com/a", "example.com/a"])
        self.assertEqual(sources["translation"], ["http://x.example/b/../c"])

    def test_body_text_saved_verbatim(self) -> None:
        item = self.import_item(full_draft())
        fields = self.service.load(item.id)
        verbatim = "  第一行  \n第二行[术语]保持颜色\n\t结尾空白  "
        fields.body_text = verbatim
        saved = self.service.save(item.id, fields)
        self.assertEqual(saved.working_draft["content"]["body_text"], verbatim)
        self.assertEqual(self.service.load(item.id).body_text, verbatim)

    def test_missing_fields_recalculated_in_fixed_order(self) -> None:
        item = self.import_item(incomplete_draft())
        fields = self.service.load(item.id)
        validation = self.service.validate(fields)
        self.assertEqual(validation.missing_fields, ["title", "author", "tags"])
        self.assertFalse(validation.can_approve)
        self.assertIsNot(validation.fields, fields)
        fields.title = "补标题"
        fields.author = "补作者"
        fields.tags = ["短篇"]
        validation = self.service.validate(fields)
        self.assertEqual(validation.missing_fields, [])
        self.assertTrue(validation.can_approve)
        fields.body_text = "   \n\t"
        validation = self.service.validate(fields)
        self.assertEqual(validation.missing_fields, ["body"])
        self.assertFalse(validation.can_approve)

    def test_all_missing_fields_order(self) -> None:
        fields = ReviewFields(title=None, author=None, tags=[], body_text="  ")
        validation = self.service.validate(fields)
        self.assertEqual(validation.missing_fields, ["title", "author", "tags", "body"])
        self.assertFalse(validation.can_approve)

    def test_type_errors_raise_invalid_review_fields(self) -> None:
        item = self.import_item(full_draft())
        base = self.service.load(item.id)
        bad_title = copy.deepcopy(base)
        bad_title.title = 123
        bad_author = copy.deepcopy(base)
        bad_author.author = ["列表"]
        bad_tags_str = copy.deepcopy(base)
        bad_tags_str.tags = "不是列表"
        bad_tags_item = copy.deepcopy(base)
        bad_tags_item.tags = ["好", 1]
        bad_nga = copy.deepcopy(base)
        bad_nga.sources.nga = "不是列表"
        bad_source_item = copy.deepcopy(base)
        bad_source_item.sources.original = [None]
        bad_body = copy.deepcopy(base)
        bad_body.body_text = None
        bad_body_bytes = copy.deepcopy(base)
        bad_body_bytes.body_text = b"bytes"
        for index, bad in enumerate(
            [bad_title, bad_author, bad_tags_str, bad_tags_item, bad_nga,
             bad_source_item, bad_body, bad_body_bytes]
        ):
            with self.subTest(bad=index):
                with self.assertRaises(InvalidReviewFieldsError):
                    self.service.save(item.id, bad)
                with self.assertRaises(InvalidReviewFieldsError):
                    self.service.validate(bad)


class ReviewSaveTests(ReviewServiceTestCase):
    def test_save_preserves_untouched_fields(self) -> None:
        draft = draft_with_diagnostics()
        item = self.import_item(draft)
        fields = self.service.load(item.id)
        fields.title = "修正标题"
        saved = self.service.save(item.id, fields)
        working = saved.working_draft
        self.assertEqual(working["schema_version"], "1.0")
        self.assertEqual(working["parser_status"], "accepted")
        self.assertEqual(working["origin"], draft.to_dict()["origin"])
        self.assertEqual(working["trigger"], draft.to_dict()["trigger"])
        self.assertEqual(working["content"]["raw_text"], draft.to_dict()["content"]["raw_text"])
        self.assertEqual(working["raw_fields"], draft.to_dict()["raw_fields"])
        self.assertEqual(working["diagnostics"]["warnings"], draft.to_dict()["diagnostics"]["warnings"])
        self.assertEqual(working["diagnostics"]["unknown_fields"], draft.to_dict()["diagnostics"]["unknown_fields"])
        self.assertEqual(working["metadata"]["title"], "修正标题")

    def test_repository_rejects_direct_changes_to_protected_fields(self) -> None:
        item = self.import_item(draft_with_diagnostics())
        variants = (
            ("schema_version", lambda draft: draft.__setitem__("schema_version", "9.9")),
            (
                "origin",
                lambda draft: draft["origin"].__setitem__(
                    "canonical_url", "https://example.invalid/tampered"
                ),
            ),
            ("trigger", lambda draft: draft["trigger"].__setitem__("found", False)),
            (
                "raw_text",
                lambda draft: draft["content"].__setitem__("raw_text", "tampered"),
            ),
            (
                "raw_fields",
                lambda draft: draft["raw_fields"].__setitem__("额外字段", ["tampered"]),
            ),
            (
                "warnings",
                lambda draft: draft["diagnostics"]["warnings"].append("tampered"),
            ),
            (
                "unknown_fields",
                lambda draft: draft["diagnostics"]["unknown_fields"].append(
                    "tampered"
                ),
            ),
        )
        for name, mutate in variants:
            with self.subTest(field=name):
                proposed = copy.deepcopy(item.working_draft)
                mutate(proposed)
                with self.assertRaises(InboxError):
                    self.repo.replace_working_draft(item.id, proposed)

        reloaded = self.repo.get(item.id)
        self.assertEqual(reloaded.working_draft, item.working_draft)
        self.assertEqual(reloaded.updated_at, T0)
        self.assertEqual(len(self.repo.list_history(item.id)), 1)
        self.assertEqual(self.clock.calls, 1)

    def test_repository_locks_before_status_check_and_update(self) -> None:
        item = self.import_item(full_draft())
        lock_errors: list[str] = []
        unexpected_lock_acquisitions: list[bool] = []

        def clock_while_editing() -> str:
            competing = sqlite3.connect(str(self.path), timeout=0)
            try:
                try:
                    competing.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    lock_errors.append(str(exc))
                else:
                    unexpected_lock_acquisitions.append(True)
                    competing.rollback()
            finally:
                competing.close()
            return T1

        editing_repo = InboxRepository(self.path, clock=clock_while_editing)
        proposed = copy.deepcopy(item.working_draft)
        proposed["metadata"]["title"] = "事务内编辑"
        saved = editing_repo.replace_working_draft(item.id, proposed)

        self.assertEqual(saved.working_draft["metadata"]["title"], "事务内编辑")
        self.assertEqual(saved.status, "candidate")
        self.assertEqual(saved.updated_at, T1)
        self.assertEqual(unexpected_lock_acquisitions, [])
        self.assertEqual(len(lock_errors), 1)
        self.assertIn("locked", lock_errors[0].lower())

    def test_save_preserves_inbox_metadata(self) -> None:
        item = self.import_item(incomplete_draft())
        snapshot = item.snapshot_sha256
        created = item.created_at
        original = item.original_draft
        fields = self.service.load(item.id)
        fields.title = "补标题"
        fields.author = "补作者"
        fields.tags = ["短篇"]
        saved = self.service.save(item.id, fields)
        self.assertEqual(saved.snapshot_sha256, snapshot)
        self.assertEqual(saved.created_at, created)
        self.assertEqual(saved.original_draft, original)
        self.assertEqual(saved.status, "incomplete")
        self.assertEqual(saved.updated_at, T1)

    def test_content_sha256_and_duplicates_sync_with_body(self) -> None:
        a = self.import_item(
            parse_submission("[收录]\n[标题] 甲\n[作者] a\n[标签] x\n\n正文相同。\n")
        )
        b = self.import_item(
            parse_submission("[收录]\n[标题] 乙\n[作者] b\n[标签] y\n\n正文相同。\n")
        )
        self.assertEqual(
            [i.id for i in self.repo.find_exact_body_duplicates(a.id)], [b.id]
        )
        sha_before = a.content_sha256
        fields = self.service.load(a.id)
        fields.body_text = "全新正文。"
        saved = self.service.save(a.id, fields)
        self.assertIsNotNone(saved.content_sha256)
        self.assertNotEqual(saved.content_sha256, sha_before)
        self.assertEqual(self.repo.find_exact_body_duplicates(a.id), [])
        self.assertEqual(self.repo.find_exact_body_duplicates(b.id), [])
        fields_b = self.service.load(b.id)
        fields_b.body_text = "全新正文。"
        self.service.save(b.id, fields_b)
        self.assertEqual(
            [i.id for i in self.repo.find_exact_body_duplicates(a.id)], [b.id]
        )

    def test_save_does_not_change_status_or_history(self) -> None:
        item = self.import_item(incomplete_draft())
        fields = self.service.load(item.id)
        fields.title = "补标题"
        fields.author = "补作者"
        fields.tags = ["短篇"]
        saved = self.service.save(item.id, fields)
        self.assertEqual(saved.status, "incomplete")
        self.assertEqual(len(self.repo.list_history(item.id)), 1)

    def test_identical_save_is_idempotent_without_clock(self) -> None:
        item = self.import_item(incomplete_draft())
        self.assertEqual(self.clock.calls, 1)
        fields = self.service.load(item.id)
        fields.title = "补标题"
        fields.author = "补作者"
        fields.tags = ["短篇"]
        saved1 = self.service.save(item.id, fields)
        self.assertEqual(saved1.updated_at, T1)
        self.assertEqual(self.clock.calls, 2)
        saved2 = self.service.save(item.id, fields)
        self.assertEqual(saved2.updated_at, T1)
        self.assertEqual(self.clock.calls, 2)
        unchanged = self.service.load(item.id)
        saved3 = self.service.save(item.id, unchanged)
        self.assertEqual(saved3.updated_at, T1)
        self.assertEqual(self.clock.calls, 2)

    def test_editable_statuses(self) -> None:
        candidate = self.import_item(full_draft())
        incomplete = self.import_item(incomplete_draft())
        suspected = self.repo.transition(
            self.import_item(parse_submission("[收录]\n\n另一条只有正文。\n")).id,
            "suspected_duplicate",
            "人工标记",
        )
        for item in (candidate, incomplete, suspected):
            with self.subTest(status=item.status):
                fields = self.service.load(item.id)
                fields.author = "补充作者"
                saved = self.service.save(item.id, fields)
                self.assertEqual(saved.status, item.status)
                self.assertEqual(saved.working_draft["metadata"]["author"], "补充作者")

    def test_locked_statuses_rejected(self) -> None:
        candidate = self.import_item(full_draft())
        approved = self.repo.transition(candidate.id, "approved", "确认")
        exported = self.repo.transition(approved.id, "exported", "已导出")
        ignored = self.import_item(ignored_draft())
        for item in (approved, exported, ignored):
            with self.subTest(status=item.status):
                fields = self.service.load(item.id)
                fields.title = "试图修改"
                with self.assertRaises(InboxItemLockedError):
                    self.service.save(item.id, fields)
                with self.assertRaises(InboxItemLockedError):
                    self.repo.replace_working_draft(
                        item.id, copy.deepcopy(item.working_draft)
                    )

    def test_save_missing_item_raises(self) -> None:
        with self.assertRaises(InboxItemNotFoundError):
            self.service.save(999, ReviewFields(title="x", body_text="正文"))

    def test_sql_error_in_update_rolls_back(self) -> None:
        item = self.import_item(incomplete_draft())
        raw = sqlite3.connect(str(self.path))
        try:
            raw.execute(
                "CREATE TRIGGER fail_edit BEFORE UPDATE OF working_draft_json "
                "ON inbox_item BEGIN SELECT RAISE(ABORT, 'simulated'); END"
            )
            raw.commit()
        finally:
            raw.close()
        fields = self.service.load(item.id)
        fields.title = "新标题"
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.save(item.id, fields)
        raw = sqlite3.connect(str(self.path))
        try:
            raw.execute("DROP TRIGGER fail_edit")
            raw.commit()
        finally:
            raw.close()
        reloaded = self.repo.get(item.id)
        self.assertIsNone(reloaded.working_draft["metadata"]["title"])
        self.assertEqual(reloaded.updated_at, T0)
        self.assertEqual(reloaded.status, "incomplete")
        self.assertEqual(len(self.repo.list_history(item.id)), 1)
        saved = self.service.save(item.id, fields)
        self.assertEqual(saved.working_draft["metadata"]["title"], "新标题")
        self.assertEqual(saved.updated_at, T2)

    def test_schema_and_user_version_unchanged(self) -> None:
        item = self.import_item(incomplete_draft())
        fields = self.service.load(item.id)
        fields.title = "补标题"
        self.service.save(item.id, fields)
        raw = sqlite3.connect(str(self.path))
        try:
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 1)
            tables = {
                row[0]
                for row in raw.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            self.assertEqual(tables, {"inbox_item", "inbox_status_history"})
        finally:
            raw.close()


if __name__ == "__main__":
    unittest.main()
