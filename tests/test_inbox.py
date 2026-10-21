"""Tests for the Inbox SQLite core (TASK-002)."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from umalib_ingest.inbox import (
    InboxItemNotFoundError,
    InboxRepository,
    InvalidStatusTransitionError,
    UnsupportedInboxSchemaError,
)
from umalib_ingest.inbox.repository import _SCHEMA_STATEMENTS
from umalib_ingest.parser import parse_submission

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"

SCHEMA_SQL_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "umalib_ingest"
    / "inbox"
    / "schema.sql"
)
SCHEMA_TEXT = SCHEMA_SQL_PATH.read_text(encoding="utf-8")


class BrokenSchemaRepository(InboxRepository):
    """Repository whose schema execution fails after the first statement."""

    def _execute_schema(self, conn):
        conn.execute(_SCHEMA_STATEMENTS[0])
        raise sqlite3.OperationalError("模拟 schema 中途失败")


class FixedClock:
    """Returns fixed timestamps in call order, then repeats the last one."""

    def __init__(self, *timestamps: str) -> None:
        self._timestamps = list(timestamps)
        self._index = 0

    def __call__(self) -> str:
        if self._index < len(self._timestamps):
            value = self._timestamps[self._index]
        else:
            value = self._timestamps[-1]
        self._index += 1
        return value


def candidate_draft():
    return parse_submission(
        "[收录]\n[标题] 星光下的测试稿\n[作者] 测试作者\n[标签] 角色, 短篇\n\n正文第一段。\n"
    )


def incomplete_draft():
    return parse_submission("[收录]\n\n只有正文。\n")


def ignored_draft():
    return parse_submission("普通讨论，没有投稿标记。\n")


def same_body_draft(title: str):
    return parse_submission(
        f"[收录]\n[标题] {title}\n[作者] 作者\n[标签] 标签\n\n正文相同。\n"
    )


class InboxRepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "inbox.db"
        self.repo = InboxRepository(self.path, clock=FixedClock(T0, T1))

    def make_initialized(self) -> InboxRepository:
        self.repo.initialize()
        return self.repo


class InboxSchemaTests(InboxRepositoryTestCase):
    def test_initialize_creates_schema_version_and_indexes(self) -> None:
        repo = self.make_initialized()
        repo.initialize()
        with repo._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        raw = sqlite3.connect(str(self.path))
        try:
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 1)
            tables = {
                row[0]
                for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertTrue({"inbox_item", "inbox_status_history"} <= tables)
            indexes = {
                row[0]
                for row in raw.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }
            self.assertIn("idx_inbox_item_status", indexes)
            self.assertIn("idx_inbox_item_content_sha256", indexes)
            fk_sql = raw.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='inbox_status_history'"
            ).fetchone()[0]
            self.assertIn("REFERENCES inbox_item", fk_sql)
        finally:
            raw.close()

    def test_reject_unsupported_user_version(self) -> None:
        raw = sqlite3.connect(str(self.path))
        try:
            raw.execute("PRAGMA user_version = 99")
            raw.commit()
        finally:
            raw.close()
        with self.assertRaises(UnsupportedInboxSchemaError):
            self.repo.initialize()

    def test_reject_user_version_zero_with_unknown_tables(self) -> None:
        raw = sqlite3.connect(str(self.path))
        try:
            raw.execute("CREATE TABLE article (id INTEGER PRIMARY KEY)")
            raw.commit()
        finally:
            raw.close()
        with self.assertRaises(UnsupportedInboxSchemaError):
            self.repo.initialize()

    def test_reject_any_existing_table_at_user_version_zero(self) -> None:
        raw = sqlite3.connect(str(self.path))
        try:
            raw.execute(
                "CREATE TABLE inbox_item (id INTEGER PRIMARY KEY, status TEXT)"
            )
            raw.commit()
        finally:
            raw.close()
        with self.assertRaises(UnsupportedInboxSchemaError):
            self.repo.initialize()

    def test_data_operations_require_initialized_schema(self) -> None:
        with self.assertRaises(UnsupportedInboxSchemaError):
            self.repo.list_items()
        with self.assertRaises(UnsupportedInboxSchemaError):
            self.repo.import_draft(candidate_draft())

    def test_data_operations_reject_malformed_v1_without_initialize(self) -> None:
        raw = sqlite3.connect(str(self.path))
        try:
            raw.execute("CREATE TABLE inbox_item (id INTEGER PRIMARY KEY)")
            raw.execute("CREATE TABLE inbox_status_history (id INTEGER PRIMARY KEY)")
            raw.execute("PRAGMA user_version = 1")
            raw.commit()
        finally:
            raw.close()
        with self.assertRaises(UnsupportedInboxSchemaError):
            self.repo.list_items()

    def _init_doctored_db(self, schema_text: str) -> None:
        raw = sqlite3.connect(str(self.path))
        try:
            raw.executescript(schema_text)
            raw.execute("PRAGMA user_version = 1")
            raw.commit()
        finally:
            raw.close()

    def test_reject_missing_required_column(self) -> None:
        schema = SCHEMA_TEXT.replace("    content_sha256 TEXT,\n", "")
        schema = schema.replace(
            "CREATE INDEX IF NOT EXISTS idx_inbox_item_content_sha256 "
            "ON inbox_item(content_sha256);",
            "",
        )
        self._init_doctored_db(schema)
        with self.assertRaises(UnsupportedInboxSchemaError):
            self.repo.initialize()

    def test_reject_missing_primary_key(self) -> None:
        schema = SCHEMA_TEXT.replace(
            "id INTEGER PRIMARY KEY AUTOINCREMENT,", "id INTEGER NOT NULL,"
        )
        self._init_doctored_db(schema)
        with self.assertRaises(UnsupportedInboxSchemaError):
            self.repo.initialize()

    def test_reject_missing_snapshot_unique(self) -> None:
        schema = SCHEMA_TEXT.replace(
            "snapshot_sha256 TEXT NOT NULL UNIQUE,",
            "snapshot_sha256 TEXT NOT NULL,",
        )
        self._init_doctored_db(schema)
        with self.assertRaises(UnsupportedInboxSchemaError):
            self.repo.initialize()

    def test_reject_wrong_index_columns(self) -> None:
        schema = SCHEMA_TEXT.replace(
            "CREATE INDEX IF NOT EXISTS idx_inbox_item_status ON inbox_item(status);",
            "CREATE INDEX IF NOT EXISTS idx_inbox_item_status "
            "ON inbox_item(content_sha256);",
        )
        self._init_doctored_db(schema)
        with self.assertRaises(UnsupportedInboxSchemaError):
            self.repo.initialize()

    def test_reject_unique_content_index(self) -> None:
        schema = SCHEMA_TEXT.replace(
            "CREATE INDEX IF NOT EXISTS idx_inbox_item_content_sha256 "
            "ON inbox_item(content_sha256);",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_item_content_sha256 "
            "ON inbox_item(content_sha256);",
        )
        self._init_doctored_db(schema)
        with self.assertRaises(UnsupportedInboxSchemaError):
            self.repo.initialize()

    def test_reject_missing_foreign_key(self) -> None:
        schema = SCHEMA_TEXT.replace(
            "item_id INTEGER NOT NULL REFERENCES inbox_item(id),",
            "item_id INTEGER NOT NULL,",
        )
        self._init_doctored_db(schema)
        with self.assertRaises(UnsupportedInboxSchemaError):
            self.repo.initialize()

    def test_reject_cascade_foreign_key(self) -> None:
        schema = SCHEMA_TEXT.replace(
            "item_id INTEGER NOT NULL REFERENCES inbox_item(id),",
            "item_id INTEGER NOT NULL REFERENCES inbox_item(id) ON DELETE CASCADE,",
        )
        self._init_doctored_db(schema)
        with self.assertRaises(UnsupportedInboxSchemaError):
            self.repo.initialize()

    def test_reject_missing_reason_check(self) -> None:
        schema = SCHEMA_TEXT.replace(
            "    reason TEXT NOT NULL CHECK (\n"
            "        length(trim(reason, char(9) || char(10) || char(13) || ' ')) > 0\n"
            "    ),",
            "    reason TEXT NOT NULL,",
        )
        self.assertNotEqual(schema, SCHEMA_TEXT)
        self._init_doctored_db(schema)
        with self.assertRaises(UnsupportedInboxSchemaError):
            self.repo.initialize()

    def test_initialize_failure_is_atomic(self) -> None:
        broken = BrokenSchemaRepository(self.path, clock=FixedClock(T0, T1))
        with self.assertRaises(sqlite3.OperationalError):
            broken.initialize()
        raw = sqlite3.connect(str(self.path))
        try:
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 0)
            tables = raw.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            self.assertEqual(tables, [])
        finally:
            raw.close()
        repo = self.make_initialized()
        self.assertEqual(repo.list_items(), [])


class InboxImportTests(InboxRepositoryTestCase):
    def test_initial_statuses_candidate_incomplete_ignored(self) -> None:
        repo = self.make_initialized()
        candidate = repo.import_draft(candidate_draft())
        incomplete = repo.import_draft(incomplete_draft())
        ignored = repo.import_draft(ignored_draft())
        self.assertEqual(candidate.status, "candidate")
        self.assertEqual(incomplete.status, "incomplete")
        self.assertEqual(ignored.status, "ignored")
        self.assertEqual(
            [item.id for item in repo.list_items()],
            [candidate.id, incomplete.id, ignored.id],
        )
        self.assertEqual([item.id for item in repo.list_items("incomplete")], [incomplete.id])
        with self.assertRaises(InvalidStatusTransitionError):
            repo.list_items("not_a_status")

    def test_sql_filtered_pagination_uses_working_title_and_thread(self) -> None:
        from umalib_ingest.domain import SubmissionOrigin
        from umalib_ingest.review import ReviewFields, ReviewService, ReviewSources

        repo = self.make_initialized()
        first = parse_submission(
            "[收录]\n[标题] 原标题\n[作者] 甲\n[标签] 标签\n\n正文\n",
            origin=SubmissionOrigin(thread_id="100", post_id="1"),
        )
        second = parse_submission(
            "[收录]\n[标题] 另一篇\n[作者] 乙\n[标签] 标签\n\n正文\n",
            origin=SubmissionOrigin(thread_id="200", post_id="2"),
        )
        first_item = repo.import_draft(first)
        repo.import_draft(second)
        ReviewService(repo).save(first_item.id, ReviewFields(
            title="人工改名", author="甲", translator=None, series=None,
            tags=["标签"], sources=ReviewSources(), body_text="正文"))

        items, total = repo.list_items_page(thread_id="100", query="人工改", page=1)
        self.assertEqual(total, 1)
        self.assertEqual([item.id for item in items], [first_item.id])
        items, total = repo.list_items_page(thread_id="100", query="原标题", page=1)
        self.assertEqual((items, total), ([], 0))

    def test_original_and_working_drafts_preserved(self) -> None:
        repo = self.make_initialized()
        draft = candidate_draft()
        item = repo.import_draft(draft)
        self.assertEqual(item.original_draft, draft.to_dict())
        self.assertEqual(item.working_draft, draft.to_dict())
        self.assertIn("raw_text", item.original_draft["content"])
        incomplete = repo.import_draft(incomplete_draft())
        self.assertEqual(
            incomplete.original_draft["diagnostics"]["missing_fields"],
            ["title", "author", "tags"],
        )

    def test_duplicate_import_is_idempotent(self) -> None:
        repo = self.make_initialized()
        draft = candidate_draft()
        first = repo.import_draft(draft)
        second = repo.import_draft(draft)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(repo.list_items()), 1)
        self.assertEqual(len(repo.list_history(first.id)), 1)

    def test_blank_body_has_no_content_hash(self) -> None:
        repo = self.make_initialized()
        blank = repo.import_draft(
            parse_submission("[收录]\n[标题] 甲\n[作者] a\n[标签] x\n\n   \n")
        )
        self.assertIsNone(blank.content_sha256)

    def test_import_writes_single_import_history(self) -> None:
        repo = self.make_initialized()
        item = repo.import_draft(candidate_draft())
        self.assertEqual(item.created_at, T0)
        self.assertEqual(item.updated_at, T0)
        history = repo.list_history(item.id)
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0].from_status)
        self.assertEqual(history[0].to_status, "candidate")
        self.assertEqual(history[0].reason, "import")
        self.assertEqual(history[0].changed_at, T0)


class InboxDuplicateTests(InboxRepositoryTestCase):
    def test_exact_body_duplicates_found(self) -> None:
        repo = self.make_initialized()
        a = repo.import_draft(same_body_draft("甲"))
        b = repo.import_draft(same_body_draft("乙"))
        c = repo.import_draft(
            parse_submission("[收录]\n[标题] 丙\n[作者] 作者\n[标签] 标签\n\n不同正文。\n")
        )
        self.assertEqual([item.id for item in repo.find_exact_body_duplicates(a.id)], [b.id])
        self.assertEqual([item.id for item in repo.find_exact_body_duplicates(b.id)], [a.id])
        self.assertEqual(repo.find_exact_body_duplicates(c.id), [])

    def test_blank_body_items_never_duplicates(self) -> None:
        repo = self.make_initialized()
        a = repo.import_draft(
            parse_submission("[收录]\n[标题] 甲\n[作者] a\n[标签] x\n\n   \n")
        )
        b = repo.import_draft(
            parse_submission("[收录]\n[标题] 乙\n[作者] b\n[标签] y\n\n\t\n")
        )
        self.assertIsNone(a.content_sha256)
        self.assertIsNone(b.content_sha256)
        self.assertEqual(repo.find_exact_body_duplicates(a.id), [])
        self.assertEqual(repo.find_exact_body_duplicates(b.id), [])


class InboxTransitionTests(InboxRepositoryTestCase):
    def test_reason_blank_rejected_at_database_level(self) -> None:
        repo = self.make_initialized()
        item = repo.import_draft(candidate_draft())
        raw = sqlite3.connect(str(self.path))
        try:
            for bad_reason in ("", "   ", "\t", "\n", "\r\n", " \t\r\n "):
                with self.assertRaises(sqlite3.IntegrityError):
                    raw.execute(
                        "INSERT INTO inbox_status_history (item_id, from_status, "
                        "to_status, reason, changed_at) VALUES "
                        "(?, 'candidate', 'approved', ?, '2026-01-01T00:00:00+00:00')",
                        (item.id, bad_reason),
                    )
        finally:
            raw.close()

    def test_valid_transition_writes_history_with_time_and_reason(self) -> None:
        repo = self.make_initialized()
        item = repo.import_draft(candidate_draft())
        updated = repo.transition(item.id, "approved", "  人工确认无误  ")
        self.assertEqual(updated.status, "approved")
        self.assertEqual(updated.updated_at, T1)
        self.assertEqual(updated.original_draft, item.original_draft)
        history = repo.list_history(item.id)
        self.assertEqual(len(history), 2)
        first, second = history
        self.assertIsNone(first.from_status)
        self.assertEqual(first.to_status, "candidate")
        self.assertEqual(first.reason, "import")
        self.assertEqual(first.changed_at, T0)
        self.assertEqual(second.from_status, "candidate")
        self.assertEqual(second.to_status, "approved")
        self.assertEqual(second.reason, "人工确认无误")
        self.assertEqual(second.changed_at, T1)

    def test_invalid_transitions_rejected(self) -> None:
        repo = self.make_initialized()
        item = repo.import_draft(candidate_draft())
        with self.assertRaises(InvalidStatusTransitionError):
            repo.transition(item.id, "candidate", "同状态")
        with self.assertRaises(InvalidStatusTransitionError):
            repo.transition(item.id, "approved", "   ")
        with self.assertRaises(InvalidStatusTransitionError):
            repo.transition(item.id, "not_a_status", "非法状态")
        with self.assertRaises(InvalidStatusTransitionError):
            repo.transition(item.id, "exported", "未审核直接导出")
        self.assertEqual(len(repo.list_history(item.id)), 1)

    def test_exported_is_terminal_and_only_from_approved(self) -> None:
        repo = self.make_initialized()
        item = repo.import_draft(candidate_draft())
        approved = repo.transition(item.id, "approved", "确认")
        self.assertEqual(approved.status, "approved")
        exported = repo.transition(approved.id, "exported", "已导出")
        self.assertEqual(exported.status, "exported")
        with self.assertRaises(InvalidStatusTransitionError):
            repo.transition(exported.id, "candidate", "终态回退")

    def test_manual_transitions_between_non_exported_states_allowed(self) -> None:
        repo = self.make_initialized()
        item = repo.import_draft(incomplete_draft())
        moved = repo.transition(item.id, "suspected_duplicate", "人工标记")
        self.assertEqual(moved.status, "suspected_duplicate")
        back = repo.transition(moved.id, "candidate", "复查后排除")
        self.assertEqual(back.status, "candidate")


class InboxLookupTests(InboxRepositoryTestCase):
    def test_missing_item_raises_clear_errors(self) -> None:
        self.make_initialized()
        self.assertIsNone(self.repo.get(999))
        with self.assertRaises(InboxItemNotFoundError):
            self.repo.transition(999, "approved", "x")
        with self.assertRaises(InboxItemNotFoundError):
            self.repo.list_history(999)
        with self.assertRaises(InboxItemNotFoundError):
            self.repo.find_exact_body_duplicates(999)


class InboxPersistenceTests(InboxRepositoryTestCase):
    def test_data_persists_after_reopen(self) -> None:
        repo1 = self.make_initialized()
        item = repo1.import_draft(candidate_draft())
        repo1.transition(item.id, "approved", "确认")
        repo2 = InboxRepository(self.path, clock=FixedClock(T0, T1))
        repo2.initialize()
        items = repo2.list_items()
        self.assertEqual([i.id for i in items], [item.id])
        self.assertEqual(items[0].status, "approved")
        again = repo2.import_draft(candidate_draft())
        self.assertEqual(again.id, item.id)
        self.assertEqual(len(repo2.list_history(item.id)), 2)


if __name__ == "__main__":
    unittest.main()
