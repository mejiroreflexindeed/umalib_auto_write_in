"""Read-only UmaLib inspection tests (TASK-006).

Black-box tests for the public API fixed by the main agent:

    inspect_umalib_database
    UmaLibInspectionReport / InspectionIssue
    UmaLibInspectionError / UmaLibPathError / UmaLibDatabaseError

All fixture databases live in tempfile.TemporaryDirectory() and are removed
afterwards; nothing is created inside the project directory. Only stdlib
(unittest / sqlite3 / tempfile / hashlib / json / subprocess) is used.
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
from dataclasses import replace
from pathlib import Path
from unittest import mock

from umalib_ingest.umalib_adapter import (
    UmaLibDatabaseError,
    UmaLibInspectionError,
    UmaLibPathError,
    inspect_umalib_database,
)
import umalib_ingest.umalib_adapter.inspector as inspector_module

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"

_SQLITE_MAGIC = b"SQLite format 3\x00"

_ARTICLE_FULL = """CREATE TABLE article (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    note TEXT,
    content TEXT,
    author TEXT,
    translator TEXT,
    uploadTime INTEGER,
    source TEXT
);"""

_ARTICLE_NO_SOURCE = """CREATE TABLE article (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    note TEXT,
    content TEXT,
    author TEXT,
    translator TEXT,
    uploadTime INTEGER
);"""

_ARTICLE_NO_PK = """CREATE TABLE article (
    id INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL,
    note TEXT,
    content TEXT,
    author TEXT,
    translator TEXT,
    uploadTime INTEGER,
    source TEXT
);"""

_TAG_FULL = """CREATE TABLE tag (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    type INTEGER,
    cover TEXT,
    description TEXT
);"""

_TAGGED_FULL = """CREATE TABLE tagged (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artId INTEGER NOT NULL REFERENCES article(id),
    tagId INTEGER NOT NULL REFERENCES tag(id)
);"""

_TAGGED_NULLABLE_ARTID = """CREATE TABLE tagged (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artId INTEGER REFERENCES article(id),
    tagId INTEGER NOT NULL REFERENCES tag(id)
);"""

_TAGGED_NO_FK = """CREATE TABLE tagged (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artId INTEGER NOT NULL,
    tagId INTEGER NOT NULL
);"""

_TAGGED_CASCADE = """CREATE TABLE tagged (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artId INTEGER NOT NULL REFERENCES article(id) ON DELETE CASCADE,
    tagId INTEGER NOT NULL REFERENCES tag(id)
);"""

_NON_CORE_FULL = """CREATE TABLE creator (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    names TEXT
);
CREATE TABLE rec (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    refId INTEGER,
    title TEXT,
    "others" TEXT,
    type TEXT,
    r TEXT,
    name TEXT,
    reason TEXT
);
CREATE TABLE dict (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    "class" TEXT,
    "desc" TEXT,
    "key" TEXT,
    refId INTEGER,
    related TEXT,
    relatedId INTEGER
);"""

CLEAN_SCHEMA_SQL = "\n".join([_ARTICLE_FULL, _TAG_FULL, _TAGGED_FULL, _NON_CORE_FULL])
CORE_ONLY_SCHEMA_SQL = "\n".join([_ARTICLE_FULL, _TAG_FULL, _TAGGED_FULL])


def _schema(*parts: str) -> str:
    return "\n".join(parts)


def _seed_one_of_each(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO article (name, content) VALUES ('标题A', '<p>正文</p>')"
    )
    conn.execute("INSERT INTO tag (name, type) VALUES ('标签T', 0)")
    conn.execute("INSERT INTO tagged (artId, tagId) VALUES (1, 1)")


class _TempDbTestCase(unittest.TestCase):
    """Base class: every test gets a private TemporaryDirectory."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="umalib_inspect_")
        self.addCleanup(tmp.cleanup)
        self.tmpdir = Path(tmp.name)

    def make_db(self, name: str = "main.db", schema_sql: str = CLEAN_SCHEMA_SQL) -> Path:
        path = self.tmpdir / name
        conn = sqlite3.connect(str(path))
        conn.executescript(schema_sql)
        conn.commit()
        conn.close()
        return path

    def connect(self, path: Path) -> sqlite3.Connection:
        return sqlite3.connect(str(path))


class CoreContractTests(_TempDbTestCase):
    """Task items 1-3: core contract acceptance and blocker conditions."""

    def test_complete_core_schema_matches_contract(self) -> None:
        db = self.make_db()
        conn = self.connect(db)
        _seed_one_of_each(conn)
        conn.commit()
        conn.close()

        report = inspect_umalib_database(db)

        self.assertTrue(report.schema["core_contract_match"])
        self.assertEqual(report.issues, ())
        self.assertEqual(report.report_schema_version, "1.0")
        self.assertEqual(
            set(report.database),
            {"file_name", "size_bytes", "sha256", "user_version", "page_size", "page_count"},
        )
        self.assertEqual(
            set(report.schema),
            {"schema_fingerprint_sha256", "user_tables", "core_contract_match"},
        )
        self.assertEqual(
            set(report.counts), {"article", "tag", "tagged", "creator", "rec", "dict"}
        )
        self.assertEqual(
            set(report.tag_audit),
            {"type_counts", "duplicate_names", "blank_name_ids", "unknown_type_ids"},
        )
        self.assertEqual(
            set(report.article_features),
            {"nonempty_content", "inline_color", "background_color", "ql_class", "img", "annotation_definition"},
        )
        self.assertEqual(
            set(report.integrity),
            {
                "integrity_check",
                "foreign_key_violation_count",
                "foreign_key_violations_by_table",
                "duplicate_tagged_pair_count",
            },
        )
        self.assertEqual(report.schema["user_tables"], ["article", "creator", "dict", "rec", "tag", "tagged"])
        self.assertEqual(report.counts, {"article": 1, "tag": 1, "tagged": 1, "creator": 0, "rec": 0, "dict": 0})
        self.assertTrue(report.integrity["integrity_check"])
        self.assertEqual(report.database["file_name"], db.name)
        self.assertGreater(report.database["page_size"], 0)
        self.assertGreater(report.database["page_count"], 0)
        self.assertEqual(report.database["user_version"], 0)

    def test_missing_core_table_blockers(self) -> None:
        variants = {
            "article": _schema(_TAG_FULL, _TAGGED_NO_FK, _NON_CORE_FULL),
            "tag": _schema(_ARTICLE_FULL, _TAGGED_NO_FK, _NON_CORE_FULL),
            "tagged": _schema(_ARTICLE_FULL, _TAG_FULL, _NON_CORE_FULL),
        }
        for table, schema_sql in variants.items():
            with self.subTest(missing=table):
                db = self.make_db(f"no_{table}.db", schema_sql=schema_sql)
                report = inspect_umalib_database(db)
                self.assertFalse(report.schema["core_contract_match"])
                codes = [issue.code for issue in report.issues]
                self.assertEqual(codes, ["missing_table"])
                self.assertEqual(report.issues[0].severity, "blocker")
                self.assertIn(f"缺少核心表: {table}", report.issues[0].message)
                self.assertIsNone(report.counts[table])

    def test_missing_column_blocker(self) -> None:
        db = self.make_db(schema_sql=_schema(_ARTICLE_NO_SOURCE, _TAG_FULL, _TAGGED_FULL, _NON_CORE_FULL))
        report = inspect_umalib_database(db)
        self.assertFalse(report.schema["core_contract_match"])
        codes = [issue.code for issue in report.issues]
        self.assertEqual(codes, ["missing_column"])
        self.assertIn("source", report.issues[0].message)

    def test_primary_key_mismatch_blocker(self) -> None:
        db = self.make_db(schema_sql=_schema(_ARTICLE_NO_PK, _TAG_FULL, _TAGGED_FULL, _NON_CORE_FULL))
        report = inspect_umalib_database(db)
        self.assertFalse(report.schema["core_contract_match"])
        codes = [issue.code for issue in report.issues]
        self.assertEqual(codes, ["primary_key_mismatch"])
        self.assertIn("article", report.issues[0].message)

    def test_tagged_nullable_column_blocker(self) -> None:
        db = self.make_db(schema_sql=_schema(_ARTICLE_FULL, _TAG_FULL, _TAGGED_NULLABLE_ARTID, _NON_CORE_FULL))
        report = inspect_umalib_database(db)
        self.assertFalse(report.schema["core_contract_match"])
        codes = [issue.code for issue in report.issues]
        self.assertEqual(codes, ["not_null_mismatch"])
        self.assertIn("artId", report.issues[0].message)

    def test_missing_foreign_key_blocker(self) -> None:
        db = self.make_db(schema_sql=_schema(_ARTICLE_FULL, _TAG_FULL, _TAGGED_NO_FK, _NON_CORE_FULL))
        report = inspect_umalib_database(db)
        self.assertFalse(report.schema["core_contract_match"])
        codes = [issue.code for issue in report.issues]
        self.assertEqual(codes.count("missing_foreign_key"), 2)
        self.assertTrue(all(issue.severity == "blocker" for issue in report.issues))

    def test_malformed_foreign_key_definition_returns_blocker_report(self) -> None:
        malformed = """CREATE TABLE tagged (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artId INTEGER NOT NULL REFERENCES article(no_such_column),
    tagId INTEGER NOT NULL REFERENCES tag(id)
);"""
        db = self.make_db(
            schema_sql=_schema(_ARTICLE_FULL, _TAG_FULL, malformed, _NON_CORE_FULL)
        )
        report = inspect_umalib_database(db)
        self.assertFalse(report.schema["core_contract_match"])
        codes = [issue.code for issue in report.issues]
        self.assertIn("missing_foreign_key", codes)
        self.assertIn("foreign_key_check_failed", codes)
        self.assertIsNone(report.integrity["foreign_key_violation_count"])

    def test_foreign_key_action_mismatch_blocker(self) -> None:
        db = self.make_db(schema_sql=_schema(_ARTICLE_FULL, _TAG_FULL, _TAGGED_CASCADE, _NON_CORE_FULL))
        report = inspect_umalib_database(db)
        self.assertFalse(report.schema["core_contract_match"])
        codes = [issue.code for issue in report.issues]
        self.assertEqual(codes, ["foreign_key_action_mismatch"])

    def test_foreign_key_restrict_is_blocker(self) -> None:
        restrict_tagged = """CREATE TABLE tagged (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artId INTEGER NOT NULL REFERENCES article(id) ON DELETE RESTRICT,
    tagId INTEGER NOT NULL REFERENCES tag(id) ON DELETE RESTRICT
);"""
        db = self.make_db(
            schema_sql=_schema(_ARTICLE_FULL, _TAG_FULL, restrict_tagged, _NON_CORE_FULL)
        )
        report = inspect_umalib_database(db)
        self.assertFalse(report.schema["core_contract_match"])
        issues = report.issues
        self.assertEqual(
            [issue.code for issue in issues],
            ["foreign_key_action_mismatch", "foreign_key_action_mismatch"],
        )
        self.assertTrue(all(issue.severity == "blocker" for issue in issues))

    def test_each_audited_core_column_can_be_missing_without_query_crash(self) -> None:
        variants = {
            "tag_name": _schema(
                _ARTICLE_FULL,
                _TAG_FULL.replace("    name TEXT,\n", ""),
                _TAGGED_FULL,
                _NON_CORE_FULL,
            ),
            "article_content": _schema(
                _ARTICLE_FULL.replace("    content TEXT,\n", ""),
                _TAG_FULL,
                _TAGGED_FULL,
                _NON_CORE_FULL,
            ),
            "tagged_artId": _schema(
                _ARTICLE_FULL,
                _TAG_FULL,
                _TAGGED_FULL.replace(
                    "    artId INTEGER NOT NULL REFERENCES article(id),\n", ""
                ),
                _NON_CORE_FULL,
            ),
        }
        for label, schema_sql in variants.items():
            with self.subTest(label=label):
                db = self.make_db(label + ".db", schema_sql=schema_sql)
                report = inspect_umalib_database(db)
                self.assertFalse(report.schema["core_contract_match"])
                self.assertIn("missing_column", [issue.code for issue in report.issues])

    def test_wrong_core_column_types_are_blockers(self) -> None:
        wrong_article = _ARTICLE_FULL.replace(
            "    uploadTime INTEGER,", "    uploadTime TEXT,"
        )
        db = self.make_db(
            schema_sql=_schema(wrong_article, _TAG_FULL, _TAGGED_FULL, _NON_CORE_FULL)
        )
        report = inspect_umalib_database(db)
        self.assertFalse(report.schema["core_contract_match"])
        self.assertIn("column_type_mismatch", [issue.code for issue in report.issues])

    def test_varchar_text_affinity_is_compatible(self) -> None:
        article = (
            _ARTICLE_FULL.replace("name TEXT", "name VARCHAR(50)")
            .replace("note TEXT", "note VARCHAR(100)")
            .replace("source TEXT", "source VARCHAR(128)")
        )
        tag = _TAG_FULL.replace("name TEXT", "name VARCHAR (50)").replace(
            "description TEXT", "description VARCHAR (512)"
        )
        db = self.make_db(
            schema_sql=_schema(article, tag, _TAGGED_FULL, _NON_CORE_FULL)
        )
        report = inspect_umalib_database(db)
        self.assertTrue(report.schema["core_contract_match"])
        self.assertNotIn("column_type_mismatch", [issue.code for issue in report.issues])

    def test_id_requires_exact_integer_primary_key_declaration(self) -> None:
        article = _ARTICLE_FULL.replace(
            "id INTEGER PRIMARY KEY AUTOINCREMENT", "id INT PRIMARY KEY"
        )
        db = self.make_db(
            schema_sql=_schema(article, _TAG_FULL, _TAGGED_FULL, _NON_CORE_FULL)
        )
        report = inspect_umalib_database(db)
        self.assertFalse(report.schema["core_contract_match"])
        self.assertIn("column_type_mismatch", [issue.code for issue in report.issues])


class SchemaFingerprintTests(_TempDbTestCase):
    """Task items 4-7: schema fingerprint behaviour and report immutability."""

    def _fingerprint(self, schema_sql: str, name: str = "main.db") -> str:
        db = self.make_db(name, schema_sql=schema_sql)
        return inspect_umalib_database(db).schema["schema_fingerprint_sha256"]

    def test_extra_table_changes_fingerprint(self) -> None:
        base = self._fingerprint(CLEAN_SCHEMA_SQL, "base.db")
        extra = self._fingerprint(
            _schema(CLEAN_SCHEMA_SQL, "CREATE TABLE junk (id INTEGER PRIMARY KEY);"), "extra.db"
        )
        self.assertNotEqual(base, extra)
        report = inspect_umalib_database(self.tmpdir / "extra.db")
        self.assertIn("extra_table", [issue.code for issue in report.issues])

    def test_extra_column_changes_fingerprint(self) -> None:
        base = self._fingerprint(CLEAN_SCHEMA_SQL, "base.db")
        article_extra = _ARTICLE_FULL.replace("    source TEXT\n", "    source TEXT,\n    zz_extra TEXT\n")
        extra = self._fingerprint(_schema(article_extra, _TAG_FULL, _TAGGED_FULL, _NON_CORE_FULL), "extra.db")
        self.assertNotEqual(base, extra)
        report = inspect_umalib_database(self.tmpdir / "extra.db")
        self.assertIn("extra_column", [issue.code for issue in report.issues])

    def test_extra_index_changes_fingerprint(self) -> None:
        base = self._fingerprint(CLEAN_SCHEMA_SQL, "base.db")
        extra = self._fingerprint(
            _schema(CLEAN_SCHEMA_SQL, "CREATE INDEX idx_zz ON article(name);"), "extra.db"
        )
        self.assertNotEqual(base, extra)

    def test_user_version_change_changes_fingerprint(self) -> None:
        base = self._fingerprint(CLEAN_SCHEMA_SQL, "base.db")
        db = self.make_db("version.db")
        conn = self.connect(db)
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
        conn.close()
        report = inspect_umalib_database(db)
        self.assertNotEqual(base, report.schema["schema_fingerprint_sha256"])
        self.assertIn("unknown_user_version", [issue.code for issue in report.issues])

    def test_data_only_change_keeps_fingerprint(self) -> None:
        db1 = self.make_db("one.db")
        conn = self.connect(db1)
        _seed_one_of_each(conn)
        conn.commit()
        conn.close()
        db2 = self.make_db("two.db")
        conn = self.connect(db2)
        for index in range(10):
            conn.execute("INSERT INTO article (name, content) VALUES (?, ?)", (f"a{index}", "x"))
        conn.commit()
        conn.close()
        report1 = inspect_umalib_database(db1)
        report2 = inspect_umalib_database(db2)
        self.assertEqual(
            report1.schema["schema_fingerprint_sha256"],
            report2.schema["schema_fingerprint_sha256"],
        )
        self.assertNotEqual(report1.database["sha256"], report2.database["sha256"])

    def test_repeat_inspection_json_identical(self) -> None:
        db = self.make_db()
        conn = self.connect(db)
        _seed_one_of_each(conn)
        conn.commit()
        conn.close()
        report1 = inspect_umalib_database(db)
        report2 = inspect_umalib_database(db)
        self.assertEqual(report1.to_json(), report2.to_json())
        self.assertEqual(report1.to_dict(), report2.to_dict())
        self.assertEqual(json.loads(report1.to_json()), report1.to_dict())

    def test_to_dict_mutation_does_not_pollute(self) -> None:
        db = self.make_db()
        conn = self.connect(db)
        _seed_one_of_each(conn)
        conn.commit()
        conn.close()
        report = inspect_umalib_database(db)
        json_before = report.to_json()

        polluted = report.to_dict()
        polluted["database"]["file_name"] = "HACKED"
        polluted["counts"]["article"] = 999999
        polluted["schema"]["user_tables"].append("hacked_table")
        polluted["tag_audit"]["type_counts"]["0"] = 777
        polluted["issues"].append({"severity": "blocker", "code": "hacked", "message": "hacked"})
        polluted["integrity"]["integrity_check"] = False

        self.assertEqual(report.to_json(), json_before)
        fresh = report.to_dict()
        self.assertEqual(fresh["database"]["file_name"], db.name)
        self.assertNotIn("hacked_table", fresh["schema"]["user_tables"])
        self.assertEqual(report.to_dict()["counts"]["article"], 1)

        report2 = inspect_umalib_database(db)
        self.assertEqual(report2.to_json(), json_before)

        copy1 = report.to_dict()
        copy2 = report.to_dict()
        copy1["database"]["file_name"] = "ONLY_HERE"
        self.assertEqual(copy2["database"]["file_name"], db.name)

        direct_database = report.database
        direct_database["file_name"] = "DIRECT_POLLUTION"
        direct_schema = report.schema
        direct_schema["user_tables"].append("DIRECT_POLLUTION")
        self.assertEqual(report.database["file_name"], db.name)
        self.assertNotIn("DIRECT_POLLUTION", report.schema["user_tables"])
        self.assertEqual(report.to_json(), json_before)


class PathAndErrorTests(_TempDbTestCase):
    """Task items 8-9: explicit failure modes and special-character paths."""

    def test_nonexistent_path_raises_path_error(self) -> None:
        with self.assertRaises(UmaLibPathError):
            inspect_umalib_database(self.tmpdir / "nope.db")

    def test_directory_raises_path_error(self) -> None:
        with self.assertRaises(UmaLibPathError):
            inspect_umalib_database(self.tmpdir)

    def test_empty_file_raises_database_error(self) -> None:
        path = self.tmpdir / "empty.db"
        path.write_bytes(b"")
        with self.assertRaises(UmaLibDatabaseError):
            inspect_umalib_database(path)

    def test_non_sqlite_file_raises_database_error(self) -> None:
        path = self.tmpdir / "note.txt"
        path.write_bytes("hello world, not a database\n".encode("utf-8"))
        with self.assertRaises(UmaLibDatabaseError):
            inspect_umalib_database(path)

    def test_corrupted_sqlite_raises_database_error(self) -> None:
        path = self.tmpdir / "corrupt.db"
        path.write_bytes(_SQLITE_MAGIC + b"\x00" * 4096)
        with self.assertRaises(UmaLibDatabaseError):
            inspect_umalib_database(path)

    def test_errors_are_inspection_errors(self) -> None:
        for target in (
            self.tmpdir / "missing.db",
            self.tmpdir,
            self.tmpdir / "empty.db",
        ):
            with self.subTest(target=str(target)):
                with self.assertRaises(UmaLibInspectionError):
                    inspect_umalib_database(target)

    def test_special_characters_path_opens_readonly(self) -> None:
        sub = self.tmpdir / "目录 带空格#井号%百分号"
        sub.mkdir()
        db = sub / "主库 副本#v2%最终.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(CORE_ONLY_SCHEMA_SQL)
        _seed_one_of_each(conn)
        conn.commit()
        conn.close()
        report = inspect_umalib_database(db)
        self.assertTrue(report.schema["core_contract_match"])
        self.assertEqual(report.database["file_name"], db.name)
        self.assertTrue(report.integrity["integrity_check"])


class ReadOnlyInvarianceTests(_TempDbTestCase):
    """Task items 10-11: inspection must not touch the file or the directory."""

    def test_file_metadata_unchanged_after_inspection(self) -> None:
        db = self.make_db()
        conn = self.connect(db)
        _seed_one_of_each(conn)
        for index in range(20):
            conn.execute("INSERT INTO article (name, content) VALUES (?, ?)", (f"a{index}", "y" * 200))
        conn.commit()
        conn.close()

        def snapshot() -> tuple[bytes, int, int, list[str]]:
            return (
                db.read_bytes(),
                db.stat().st_size,
                db.stat().st_mtime_ns,
                sorted(entry.name for entry in self.tmpdir.iterdir()),
            )

        before = snapshot()
        report = inspect_umalib_database(db)
        after = snapshot()

        self.assertEqual(before, after)
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), report.database["sha256"])
        self.assertEqual(report.database["size_bytes"], db.stat().st_size)

    def test_no_sidecar_files_created(self) -> None:
        db = self.make_db()
        conn = self.connect(db)
        _seed_one_of_each(conn)
        conn.commit()
        conn.close()
        inspect_umalib_database(db)
        names = sorted(entry.name for entry in self.tmpdir.iterdir())
        self.assertEqual(names, [db.name])
        for name in names:
            self.assertFalse(name.endswith("-wal"))
            self.assertFalse(name.endswith("-shm"))
            self.assertFalse(name.endswith("-journal"))

    def test_changed_file_state_during_inspection_is_rejected(self) -> None:
        db = self.make_db()
        before = inspector_module._snapshot_target(db)
        changed = replace(before, mtime_ns=before.mtime_ns + 1)
        with mock.patch.object(
            inspector_module, "_snapshot_target", side_effect=[before, changed]
        ):
            with self.assertRaises(UmaLibDatabaseError):
                inspect_umalib_database(db)


class DataEvidenceTests(_TempDbTestCase):
    """Task items 12-15: counts, tag audit, integrity and article features."""

    def test_counts_and_tag_type_counts(self) -> None:
        db = self.make_db()
        conn = self.connect(db)
        for index in range(5):
            conn.execute("INSERT INTO article (name, content) VALUES (?, ?)", (f"a{index}", "<p>x</p>"))
        for name, type_value in [
            ("t0", 0), ("t1", 0), ("t2", 1), ("t3", 2), ("t4", None), ("t5", None), ("t6", 3),
        ]:
            conn.execute("INSERT INTO tag (name, type) VALUES (?, ?)", (name, type_value))
        for art_id, tag_id in [(1, 1), (1, 2), (2, 3), (3, 1)]:
            conn.execute("INSERT INTO tagged (artId, tagId) VALUES (?, ?)", (art_id, tag_id))
        for index in range(2):
            conn.execute("INSERT INTO creator (names) VALUES (?)", (f"c{index}",))
        for index in range(3):
            conn.execute("INSERT INTO rec (refId, title) VALUES (?, ?)", (index, f"r{index}"))
        conn.execute('INSERT INTO dict ("key") VALUES (?)', ("k0",))
        conn.commit()
        conn.close()

        report = inspect_umalib_database(db)
        self.assertEqual(
            report.counts, {"article": 5, "tag": 7, "tagged": 4, "creator": 2, "rec": 3, "dict": 1}
        )
        self.assertEqual(
            report.tag_audit["type_counts"],
            {"null": 2, "0": 2, "1": 1, "2": 1, "3": 1},
        )

    def test_tag_audit_duplicates_blanks_unknown_types_duplicate_pairs(self) -> None:
        db = self.make_db()
        conn = self.connect(db)
        conn.executemany(
            "INSERT INTO article (id, name) VALUES (?, ?)", [(1, "A1"), (2, "A2")]
        )
        conn.executemany(
            "INSERT INTO tag (id, name, type) VALUES (?, ?, ?)",
            [
                (1, "alpha", 0),
                (2, "Dup", 1),
                (3, None, 0),
                (4, "beta", None),
                (5, "Dup", 2),
                (6, "", 0),
                (7, "gamma", 3),
                (8, "delta", 4),
                (9, "   ", 0),
                (10, "eps", 5),
                (11, "zeta", -1),
            ],
        )
        conn.executemany(
            "INSERT INTO tagged (artId, tagId) VALUES (?, ?)",
            [(1, 1), (1, 1), (1, 2), (2, 2), (2, 3)],
        )
        conn.commit()
        conn.close()

        report = inspect_umalib_database(db)
        self.assertTrue(report.schema["core_contract_match"])
        self.assertTrue(report.integrity["integrity_check"])
        self.assertEqual(report.integrity["foreign_key_violation_count"], 0)
        self.assertEqual(report.integrity["duplicate_tagged_pair_count"], 1)
        self.assertEqual(
            report.tag_audit["type_counts"],
            {"null": 1, "0": 4, "1": 1, "2": 1, "3": 1, "4": 1, "5": 1, "-1": 1},
        )
        self.assertEqual(
            report.tag_audit["duplicate_names"],
            [
                {
                    "name": "Dup",
                    "entries": [
                        {"id": 2, "type": 1, "tagged_count": 2},
                        {"id": 5, "type": 2, "tagged_count": 0},
                    ],
                }
            ],
        )
        self.assertEqual(report.tag_audit["blank_name_ids"], [3, 6, 9])
        self.assertEqual(
            report.tag_audit["unknown_type_ids"],
            [{"id": 10, "type": 5}, {"id": 11, "type": -1}],
        )
        self.assertEqual(
            [issue.code for issue in report.issues],
            ["duplicate_tagged_pair", "blank_tag_name", "duplicate_tag_name", "unknown_tag_type"],
        )
        severities = {issue.code: issue.severity for issue in report.issues}
        self.assertEqual(severities["duplicate_tagged_pair"], "blocker")
        self.assertEqual(severities["blank_tag_name"], "warning")
        self.assertEqual(severities["duplicate_tag_name"], "warning")
        self.assertEqual(severities["unknown_tag_type"], "warning")

    def test_integrity_check_failure_blocker(self) -> None:
        db = self.make_db(schema_sql=CORE_ONLY_SCHEMA_SQL)
        conn = self.connect(db)
        _seed_one_of_each(conn)
        for index in range(1500):
            conn.execute("INSERT INTO article (name, content) VALUES (?, ?)", (f"a{index}", "z" * 500))
        conn.execute("DELETE FROM article WHERE id > 1")
        conn.commit()
        conn.close()

        data = bytearray(db.read_bytes())
        freelist_count = int.from_bytes(data[36:40], "big")
        self.assertGreater(freelist_count, 0)
        data[36:40] = (freelist_count + 1).to_bytes(4, "big")
        db.write_bytes(bytes(data))

        report = inspect_umalib_database(db)
        self.assertFalse(report.integrity["integrity_check"])
        self.assertIn("integrity_check_failed", [issue.code for issue in report.issues])
        self.assertEqual(report.counts["article"], 1)

    def test_foreign_key_violation_blocker(self) -> None:
        db = self.make_db(schema_sql=CORE_ONLY_SCHEMA_SQL)
        conn = self.connect(db)
        conn.execute("INSERT INTO article (name, content) VALUES ('A', 'x')")
        conn.execute("INSERT INTO tag (name, type) VALUES ('t', 0)")
        conn.execute("INSERT INTO tagged (artId, tagId) VALUES (1, 1)")
        conn.execute("INSERT INTO tagged (artId, tagId) VALUES (9999, 9999)")
        conn.commit()
        conn.close()

        report = inspect_umalib_database(db)
        self.assertTrue(report.integrity["integrity_check"])
        self.assertEqual(report.integrity["foreign_key_violation_count"], 2)
        self.assertEqual(report.integrity["foreign_key_violations_by_table"], {"tagged": 2})
        self.assertIn("foreign_key_violation", [issue.code for issue in report.issues])

    def test_article_features_counting(self) -> None:
        db = self.make_db(schema_sql=CORE_ONLY_SCHEMA_SQL)
        conn = self.connect(db)
        rows = [
            ("f1", '<p><span style="color: red">x</span></p>'),
            ("f2", '<p><span style="COLOR: blue">x</span></p>'),
            ("f3", '<p style="color : green">x</p>'),
            ("f4", '<p style="background-color: #fff">x</p>'),
            ("f5", '<p style="font-color: red">x</p>'),
            ("f6", '<p class="ql-align-center">x</p>'),
            ("f7", '<p class="sql-x">x</p>'),
            ("f8", '<p>a<img src="i.png"></p>'),
            ("f9", '<p>[关键词]注释文本</p>'),
            ("f10", '<p class="zz">[注]文本</p>'),
            ("f11", '<p>无括号</p>'),
            ("f12", ""),
            ("f13", "   "),
            ("f14", None),
        ]
        conn.executemany("INSERT INTO article (name, content) VALUES (?, ?)", rows)
        conn.commit()
        conn.close()

        report = inspect_umalib_database(db)
        self.assertEqual(
            report.article_features,
            {
                "nonempty_content": 11,
                "inline_color": 3,
                "background_color": 1,
                "ql_class": 1,
                "img": 1,
                "annotation_definition": 2,
            },
        )

    def test_non_integer_tag_type_does_not_crash(self) -> None:
        db = self.make_db()
        conn = self.connect(db)
        conn.executemany(
            "INSERT INTO tag (id, name, type) VALUES (?, ?, ?)",
            [(1, "整数", 1), (2, "文本", "x"), (3, "小数", 1.5), (4, "空类型", None)],
        )
        conn.commit()
        conn.close()

        report = inspect_umalib_database(db)
        self.assertEqual(
            report.tag_audit["type_counts"],
            {"1": 1, "1.5": 1, "x": 1, "null": 1},
        )
        self.assertEqual(
            report.tag_audit["unknown_type_ids"],
            [{"id": 2, "type": "x"}, {"id": 3, "type": 1.5}],
        )
        self.assertIn("unknown_tag_type", [issue.code for issue in report.issues])
        self.assertEqual(report.to_json(), inspect_umalib_database(db).to_json())

    def test_whitespace_only_tag_names_detected_as_blank(self) -> None:
        db = self.make_db()
        conn = self.connect(db)
        conn.executemany(
            "INSERT INTO tag (id, name, type) VALUES (?, ?, ?)",
            [
                (1, None, 0),
                (2, "", 0),
                (3, "   ", 0),
                (4, "\t", 0),
                (5, "\n", 0),
                (6, "\t \n", 0),
                (7, "正常名", 0),
            ],
        )
        conn.commit()
        conn.close()

        report = inspect_umalib_database(db)
        self.assertEqual(report.tag_audit["blank_name_ids"], [1, 2, 3, 4, 5, 6])
        self.assertIn("blank_tag_name", [issue.code for issue in report.issues])

    def test_non_text_tag_name_is_reported_without_crashing_or_disclosure(self) -> None:
        db = self.make_db()
        conn = self.connect(db)
        conn.execute(
            "INSERT INTO tag (id, name, type) VALUES (?, ?, ?)",
            (1, sqlite3.Binary(b"hidden-binary-name"), 0),
        )
        conn.commit()
        conn.close()

        report = inspect_umalib_database(db)
        self.assertIn("invalid_tag_name_type", [issue.code for issue in report.issues])
        self.assertNotIn("hidden-binary-name", report.to_json())


class DisclosureTests(_TempDbTestCase):
    """Task items 16-18: minimal disclosure in report and CLI output."""

    SENTINELS = {
        "name": "TITLESECRET9F3A",
        "note": "NOTESECRET9F3A",
        "content": "CONTENTSECRET9F3A",
        "author": "AUTHORSECRET9F3A",
        "translator": "TRANSLATORSECRET9F3A",
        "source": "SOURCESECRET9F3A",
    }

    def _cli_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_DIR)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUTF8"] = "1"
        return env

    def _run_cli(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-B", "-m", "umalib_ingest.umalib_adapter.inspect_db", *args],
            cwd=str(PROJECT_ROOT),
            env=self._cli_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_article_content_not_disclosed(self) -> None:
        db = self.make_db()
        conn = self.connect(db)
        conn.execute(
            "INSERT INTO article (name, note, content, author, translator, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            tuple(self.SENTINELS[key] for key in ("name", "note", "content", "author", "translator", "source")),
        )
        conn.commit()
        conn.close()

        report = inspect_umalib_database(db)
        payload = report.to_json()
        for sentinel in self.SENTINELS.values():
            self.assertNotIn(sentinel, payload)

        proc = self._run_cli(["--db", str(db)])
        self.assertEqual(proc.returncode, 0)
        human = proc.stdout.decode("utf-8")
        for sentinel in self.SENTINELS.values():
            self.assertNotIn(sentinel, human)

    def test_control_characters_escaped_in_json(self) -> None:
        name = "ctrl\x1b[31mNAME\x07BEL"
        db = self.make_db()
        conn = self.connect(db)
        conn.execute("INSERT INTO tag (name, type) VALUES (?, 0)", (name,))
        conn.execute("INSERT INTO tag (name, type) VALUES (?, 0)", (name,))
        conn.commit()
        conn.close()

        report = inspect_umalib_database(db)
        payload = report.to_json()
        self.assertNotIn("\x1b", payload)
        self.assertNotIn("\x07", payload)
        parsed = json.loads(payload)
        self.assertEqual(parsed["tag_audit"]["duplicate_names"][0]["name"], name)

        proc = self._run_cli(["--db", str(db)])
        human = proc.stdout.decode("utf-8")
        self.assertNotIn("\x1b", human)
        self.assertNotIn("\x07", human)
        self.assertNotIn("NAME", human)

    def test_report_contains_no_path_time_or_connection_info(self) -> None:
        db = self.make_db()
        conn = self.connect(db)
        _seed_one_of_each(conn)
        conn.commit()
        conn.close()

        report = inspect_umalib_database(db)
        payload = report.to_json()
        self.assertNotIn(str(self.tmpdir), payload)
        self.assertNotIn(str(self.tmpdir.resolve()), payload)
        self.assertEqual(report.database["file_name"], db.name)
        self.assertEqual(
            set(report.to_dict()),
            {
                "report_schema_version",
                "database",
                "schema",
                "counts",
                "tag_audit",
                "article_features",
                "integrity",
                "issues",
            },
        )
        for token in ("sqlite", "PRAGMA", "SELECT", "mode=ro", "connect"):
            self.assertNotIn(token, payload)


class CliTests(_TempDbTestCase):
    """Task items 19-20: CLI exit codes and stable single-object JSON output."""

    def _cli_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_DIR)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUTF8"] = "1"
        return env

    def _run_cli(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-B", "-m", "umalib_ingest.umalib_adapter.inspect_db", *args],
            cwd=str(PROJECT_ROOT),
            env=self._cli_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_help_exits_zero(self) -> None:
        proc = self._run_cli(["--help"])
        self.assertEqual(proc.returncode, 0)
        out = proc.stdout.decode("utf-8")
        self.assertIn("usage", out.lower())
        self.assertIn("--db", out)

    def test_human_summary_exit_zero_and_readonly_header(self) -> None:
        db = self.make_db("中文 主库#副本.db")
        conn = self.connect(db)
        _seed_one_of_each(conn)
        conn.commit()
        conn.close()
        proc = self._run_cli(["--db", str(db)])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        out = proc.stdout.decode("utf-8")
        self.assertTrue(out.startswith("只读检查，不会写入数据库"))
        self.assertIn("core_contract_match", out)

    def test_blocker_exit_code_one(self) -> None:
        db = self.make_db(schema_sql=_schema(_TAG_FULL, _TAGGED_NO_FK, _NON_CORE_FULL))
        proc = self._run_cli(["--db", str(db)])
        self.assertEqual(proc.returncode, 1)
        out = proc.stdout.decode("utf-8")
        self.assertTrue(out.startswith("只读检查，不会写入数据库"))
        self.assertIn("missing_table", out)

        proc_json = self._run_cli(["--db", str(db), "--json"])
        self.assertEqual(proc_json.returncode, 1)
        parsed = json.loads(proc_json.stdout.decode("utf-8"))
        self.assertIn("missing_table", [issue["code"] for issue in parsed["issues"]])

    def test_input_errors_exit_code_two(self) -> None:
        cases = {
            "missing_path": [str(self.tmpdir / "nope.db")],
            "directory": [str(self.tmpdir)],
            "empty_file": [str(self.tmpdir / "empty.db")],
        }
        (self.tmpdir / "empty.db").write_bytes(b"")
        for label, tail in cases.items():
            with self.subTest(label=label):
                proc = self._run_cli(["--db", *tail])
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, b"")
                stderr = proc.stderr.decode("utf-8")
                self.assertIn("错误", stderr)
                self.assertNotIn("Traceback", stderr)

        proc = self._run_cli([])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        stderr = proc.stderr.decode("utf-8")
        self.assertIn("usage", stderr.lower())
        self.assertIn("--db", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_json_output_single_object_and_repeatable(self) -> None:
        db = self.make_db()
        conn = self.connect(db)
        _seed_one_of_each(conn)
        conn.commit()
        conn.close()

        proc1 = self._run_cli(["--db", str(db), "--json"])
        proc2 = self._run_cli(["--db", str(db), "--json"])
        self.assertEqual(proc1.returncode, 0)
        self.assertEqual(proc2.returncode, 0)
        self.assertEqual(proc1.stderr, b"")
        self.assertEqual(proc1.stdout, proc2.stdout)
        parsed = json.loads(proc1.stdout.decode("utf-8"))
        self.assertEqual(parsed, inspect_umalib_database(db).to_dict())


if __name__ == "__main__":
    unittest.main()
