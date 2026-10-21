"""Tests for the candidate-database Writer (TASK-008).

All databases live in tempfile.TemporaryDirectory(); only synthetic metadata
fixtures are used. Source databases in failure-injection tests are test-owned
copies and are modified only to simulate concurrent changes.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from umalib_ingest.handoff.package import ApprovedSubmissionPackage
from umalib_ingest.umalib_adapter import (
    AdapterChoices,
    TagChoice,
    build_umalib_change_plan,
)
from umalib_ingest.umalib_writer import (
    CandidateWriteApproval,
    UmaLibWriterError,
    WriterApprovalError,
    WriterDatabaseError,
    WriterExecutionError,
    WriterNotReadyError,
    WriterPathError,
    load_target_tag_catalog,
    preview_candidate_write,
    write_candidate_database,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "writer"
SCHEMA_SQL = (FIXTURES_DIR / "umalib-schema.sql").read_text(encoding="utf-8")
CONFIRMED_FINGERPRINT = (
    "d7b1966992e75819e37d41ae9c091ec21c2cdf9c5b6727af78f95398d0179aa8"
)


def stable_json(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WriterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="umalib_writer_")
        self.addCleanup(tmp.cleanup)
        self.tmpdir = Path(tmp.name)

    def make_source_db(self, name: str = "source.db") -> Path:
        path = self.tmpdir / name
        conn = sqlite3.connect(str(path))
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        conn.close()
        return path

    def seed_tags(self, db: Path, tags: list[tuple[str, int]]) -> list[int]:
        conn = sqlite3.connect(str(db))
        ids = []
        for name, tag_type in tags:
            cursor = conn.execute(
                'INSERT INTO "tag" (name, type) VALUES (?, ?)', (name, tag_type)
            )
            ids.append(cursor.lastrowid)
        conn.commit()
        conn.close()
        return ids

    def seed_articles(self, db: Path, rows: list[dict]) -> None:
        conn = sqlite3.connect(str(db))
        for row in rows:
            conn.execute(
                'INSERT INTO "article" (name, note, content, author, translator, uploadTime, source) '
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row.get("name"),
                    row.get("note"),
                    row.get("content"),
                    row.get("author"),
                    row.get("translator"),
                    row.get("uploadTime", 1700000000),
                    row.get("source", ""),
                ),
            )
        conn.commit()
        conn.close()

    def seed_tagged(self, db: Path, pairs: list[tuple[int, int]]) -> None:
        conn = sqlite3.connect(str(db))
        for art_id, tag_id in pairs:
            conn.execute(
                'INSERT INTO "tagged" (artId, tagId) VALUES (?, ?)', (art_id, tag_id)
            )
        conn.commit()
        conn.close()

    def seed_other_tables(self, db: Path) -> None:
        conn = sqlite3.connect(str(db))
        conn.execute('INSERT INTO creator (id, names) VALUES (1, "合成创建者")')
        conn.execute(
            'INSERT INTO rec (id, refId, title, others, type, r, name, reason) '
            "VALUES (1, 1, '合成推荐', '', 0, 0, '合成名', '合成理由')"
        )
        conn.execute(
            'INSERT INTO dict (id, class, "desc", key, refId, related, relatedId) '
            "VALUES (1, 'c', '合成描述', 'k', NULL, NULL, NULL)"
        )
        conn.commit()
        conn.close()

    def make_package(
        self,
        *,
        title: str = "合成投稿标题",
        author: str = "合成作者",
        translator: str | None = None,
        series: str | None = None,
        tags: list[str] | None = None,
        body_text: str = "合成正文第一行。\n",
        note: str | None = None,
    ):
        submission = {
            "schema_version": "1.0",
            "parser_status": "accepted",
            "origin": {"platform": "nga", "thread_id": None, "post_id": None,
                       "floor": None, "canonical_url": None},
            "trigger": {"found": True, "marker": "收录"},
            "metadata": {
                "title": title,
                "author": author,
                "translator": translator,
                "series": series,
                "tags": tags if tags is not None else [],
                "sources": {"nga": [], "original_series": [], "original": [],
                            "translation_series": [], "translation": []},
            },
            "content": {"raw_text": "合成原始文本", "body_text": body_text},
            "raw_fields": {},
            "diagnostics": {"missing_fields": [], "warnings": [], "unknown_fields": []},
        }
        inbox = {
            "item_id": 1,
            "status": "approved",
            "snapshot_sha256": "a" * 64,
            "content_sha256": "b" * 64,
            "created_at": "2026-01-01T00:00:00+00:00",
            "approved_at": "2026-01-02T00:00:00+00:00",
        }
        package_hash = sha256_hex(
            stable_json({"schema_version": "1.0", "inbox": inbox, "submission": submission})
        )
        return ApprovedSubmissionPackage(
            schema_version="1.0",
            inbox=inbox,
            submission=submission,
            package_sha256=package_hash,
        )

    def make_ready_plan(
        self,
        source_db: Path,
        *,
        title: str = "合成投稿标题",
        author: str = "合成作者",
        translator: str | None = None,
        series: str | None = None,
        tags: list[str] | None = None,
        tag_choices: tuple[TagChoice, ...] = (),
        body_text: str = "合成正文第一行。\n",
        note: str | None = None,
        upload_time: int = 1700000000,
        ordered_sources: tuple[str, ...] = (),
    ):
        catalog = load_target_tag_catalog(source_db)
        package = self.make_package(
            title=title, author=author, translator=translator, series=series,
            tags=tags, body_text=body_text, note=note,
        )
        choices = AdapterChoices(
            upload_time_seconds=upload_time,
            note=note,
            ordered_sources=ordered_sources,
            tag_choices=tag_choices,
        )
        plan = build_umalib_change_plan(package, choices, catalog)
        if not plan.ready:
            raise AssertionError(f"测试计划未就绪: {plan.to_dict()['diagnostics']['blockers']}")
        return plan

    def make_approval(self, plan, preview, source_db: Path, confirmed: object = True):
        return CandidateWriteApproval(
            plan_sha256=plan.plan_sha256,
            preview_sha256=preview.preview_sha256,
            source_database_sha256=sha256_file(source_db),
            app_closed_confirmed=confirmed,
        )

    def rehash_plan(self, plan, **changes):
        """Return a deliberately forged but internally hash-consistent plan."""
        changed = dataclasses.replace(plan, **changes)
        payload = {
            key: value for key, value in changed.to_dict().items() if key != "plan_sha256"
        }
        return dataclasses.replace(changed, plan_sha256=sha256_hex(stable_json(payload)))

    def candidate_path(self, name: str = "candidate.db") -> Path:
        return self.tmpdir / name

    def directory_names(self) -> set[str]:
        return {entry.name for entry in self.tmpdir.iterdir()}


class CatalogLoaderTests(WriterTestCase):
    def test_load_normal_catalog_sorted_with_usage(self) -> None:
        db = self.make_source_db()
        self.seed_tags(db, [("乙", 1), ("甲", 0), ("乙", 2)])
        self.seed_articles(db, [{"name": "A", "author": "B", "content": "<p>x</p>"}])
        self.seed_tagged(db, [(1, 2), (1, 3)])
        before_sha = sha256_file(db)
        before_stat = db.stat()
        before_names = self.directory_names()
        catalog = load_target_tag_catalog(db)
        self.assertEqual([tag.id for tag in catalog.tags], [1, 2, 3])
        self.assertEqual(catalog.tags[0].name, "乙")
        self.assertEqual(catalog.tags[0].usage_count, 0)
        self.assertEqual(catalog.tags[1].name, "甲")
        self.assertEqual(catalog.tags[1].usage_count, 1)
        self.assertEqual(catalog.tags[2].name, "乙")
        self.assertEqual(catalog.tags[2].usage_count, 1)
        self.assertEqual(catalog.schema_fingerprint_sha256, CONFIRMED_FINGERPRINT)
        payload = [
            {"id": t.id, "name": t.name, "type": t.type} for t in catalog.tags
        ]
        expected_hash = sha256_hex(stable_json(payload))
        self.assertEqual(sha256_file(db), before_sha)
        self.assertEqual(db.stat().st_mtime_ns, before_stat.st_mtime_ns)
        self.assertEqual(self.directory_names(), before_names)
        self.assertEqual(sha256_hex(stable_json(payload)), expected_hash)

    def test_duplicate_names_keep_distinct_ids(self) -> None:
        db = self.make_source_db()
        self.seed_tags(db, [("同名", 0), ("同名", 0)])
        catalog = load_target_tag_catalog(db)
        same = [tag for tag in catalog.tags if tag.name == "同名"]
        self.assertEqual(len(same), 2)
        self.assertEqual(same[0].id + 1, same[1].id)

    def test_invalid_tag_rows_rejected(self) -> None:
        db = self.make_source_db()
        conn = sqlite3.connect(str(db))
        conn.execute('INSERT INTO "tag" (id, name, type) VALUES (10, NULL, 0)')
        conn.execute('INSERT INTO "tag" (id, name, type) VALUES (11, "  ", 0)')
        conn.execute('INSERT INTO "tag" (id, name, type) VALUES (12, "非法类型", 9)')
        conn.execute('INSERT INTO "tag" (id, name, type) VALUES (13, "文本类型", "x")')
        conn.commit()
        conn.close()
        with self.assertRaises(WriterDatabaseError):
            load_target_tag_catalog(db)

    def test_non_database_and_bad_schema_rejected(self) -> None:
        junk = self.tmpdir / "junk.db"
        junk.write_bytes(b"not a sqlite file at all")
        with self.assertRaises(UmaLibWriterError):
            load_target_tag_catalog(junk)
        missing_table = self.tmpdir / "missing.db"
        conn = sqlite3.connect(str(missing_table))
        conn.executescript("CREATE TABLE article (id INTEGER PRIMARY KEY);")
        conn.commit()
        conn.close()
        with self.assertRaises(WriterDatabaseError):
            load_target_tag_catalog(missing_table)
        drifted = self.tmpdir / "drifted.db"
        conn = sqlite3.connect(str(drifted))
        conn.executescript(SCHEMA_SQL)
        conn.execute('ALTER TABLE "article" ADD COLUMN zz_extra TEXT')
        conn.commit()
        conn.close()
        with self.assertRaises(WriterDatabaseError):
            load_target_tag_catalog(drifted)
        fk_bad = self.make_source_db("fk.db")
        self.seed_articles(fk_bad, [{"name": "A", "author": "B", "content": "<p>x</p>"}])
        self.seed_tagged(fk_bad, [(1, 9999)])
        with self.assertRaises(WriterDatabaseError):
            load_target_tag_catalog(fk_bad)

    def test_loader_never_touches_article_columns(self) -> None:
        from umalib_ingest.umalib_writer import catalog as catalog_module

        text = Path(catalog_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn('FROM "article"', text)
        self.assertNotIn("content", text)


class PreviewPlanValidationTests(WriterTestCase):
    def test_ready_plan_gives_stable_ready_preview(self) -> None:
        db = self.make_source_db()
        self.seed_tags(db, [("甲", 1)])
        plan = self.make_ready_plan(db, tags=["甲"])
        candidate = self.candidate_path()
        preview = preview_candidate_write(plan, db, candidate)
        data = preview.to_dict()
        self.assertTrue(data["ready_to_write"])
        self.assertEqual(data["plan_sha256"], plan.plan_sha256)
        self.assertEqual(data["operations"]["article_insert_count"], 1)
        self.assertEqual(data["operations"]["existing_tag_ids"], [1])
        self.assertEqual(data["operations"]["tagged_insert_count"], 1)
        self.assertEqual(data["candidate"]["exists"], False)
        payload = {k: v for k, v in data.items() if k != "preview_sha256"}
        self.assertEqual(sha256_hex(stable_json(payload)), data["preview_sha256"])
        second = preview_candidate_write(plan, db, candidate)
        self.assertEqual(stable_json(second.to_dict()), stable_json(data))
        self.assertEqual(second.preview_sha256, preview.preview_sha256)
        self.assertEqual(self.directory_names(), {"source.db"})

    def test_blocked_and_forged_plans_rejected(self) -> None:
        db = self.make_source_db()
        blocked_plan = build_umalib_change_plan(
            self.make_package(tags=["不存在的标签"]),
            AdapterChoices(1700000000, None, (), ()),
            load_target_tag_catalog(db),
        )
        preview = preview_candidate_write(blocked_plan, db, self.candidate_path())
        codes = [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("plan_not_ready", codes)
        self.assertIn("plan_has_blockers", codes)
        forged_ready = dataclasses.replace(blocked_plan, ready=True)
        preview = preview_candidate_write(forged_ready, db, self.candidate_path())
        codes = [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("plan_has_blockers", codes)

    def test_plan_hash_tamper_blocked(self) -> None:
        db = self.make_source_db()
        plan = self.make_ready_plan(db, tags=[])
        tampered = dataclasses.replace(plan, plan_sha256="f" * 64)
        preview = preview_candidate_write(tampered, db, self.candidate_path())
        codes = [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("plan_hash_mismatch", codes)

    def test_database_hash_schema_and_catalog_mismatch_blocked(self) -> None:
        db = self.make_source_db()
        other = self.make_source_db("other.db")
        self.seed_tags(other, [("不同标签", 1)])
        plan = self.make_ready_plan(other, tags=["不同标签"])
        preview = preview_candidate_write(plan, db, self.candidate_path())
        codes = [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("target_database_sha256_mismatch", codes)
        self.assertIn("target_catalog_hash_mismatch", codes)
        bad_input = dict(plan.input)
        bad_input["target_database_sha256"] = "e" * 64
        tampered = dataclasses.replace(plan, input=bad_input)
        preview = preview_candidate_write(tampered, other, self.candidate_path())
        codes = [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("target_database_sha256_mismatch", codes)
        bad_input = dict(plan.input)
        bad_input["target_schema_fingerprint_sha256"] = "e" * 64
        tampered = dataclasses.replace(plan, input=bad_input)
        preview = preview_candidate_write(tampered, other, self.candidate_path())
        codes = [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("target_schema_fingerprint_mismatch", codes)

    def test_existing_tag_mismatch_blocked(self) -> None:
        db = self.make_source_db()
        self.seed_tags(db, [("甲", 1)])
        plan = self.make_ready_plan(db, tags=["甲"])
        conn = sqlite3.connect(str(db))
        conn.execute('UPDATE "tag" SET name = "改名", type = 0 WHERE id = 1')
        conn.commit()
        conn.close()
        preview = preview_candidate_write(plan, db, self.candidate_path())
        codes = [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("existing_tag_mismatch", codes)

    def test_create_tag_conflicts_blocked(self) -> None:
        db = self.make_source_db()
        self.seed_tags(db, [("甲", 1)])
        plan = self.make_ready_plan(db, tags=["甲", "乙"], tag_choices=(TagChoice("tag", "乙", new_tag_type=2),))
        conn = sqlite3.connect(str(db))
        conn.execute('INSERT INTO "tag" (name, type) VALUES (?, ?)', ("乙", 2))
        conn.commit()
        conn.close()
        preview = preview_candidate_write(plan, db, self.candidate_path())
        codes = [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("new_tag_name_exists", codes)

    def test_article_field_bounds_blocked(self) -> None:
        db = self.make_source_db()
        plan = self.make_ready_plan(db, tags=[])
        cases = [
            ("name", "x" * 51, "article_name_too_long"),
            ("author", "x" * 51, "article_author_too_long"),
            ("note", "x" * 101, "article_note_invalid"),
            ("translator", "x" * 51, "article_translator_invalid"),
            ("upload_time", -1, "article_upload_time_invalid"),
            ("upload_time", 2147483648, "article_upload_time_invalid"),
            ("upload_time", 1.5, "article_upload_time_invalid"),
            ("upload_time", True, "article_upload_time_invalid"),
            ("source", "相对路径", "article_source_invalid"),
            ("source", "http://user:pw@h.example/1", "article_source_invalid"),
            ("source", "http://h.example:99999/1", "article_source_invalid"),
            ("source", "a  b", "article_source_invalid"),
            ("source", " http://h.example/1", "article_source_invalid"),
            ("source", "ftp://h.example/1", "article_source_invalid"),
            ("source", "<script>x</script>", "article_source_invalid"),
            ("source", "http://bad_host.example/1", "article_source_invalid"),
            ("source", "http://./1", "article_source_invalid"),
            ("source", "http://999.999.999.999/1", "article_source_invalid"),
        ]
        for field, value, expected_code in cases:
            with self.subTest(field=field, value=value):
                tampered = dataclasses.replace(
                    plan, article={**plan.article, field: value}
                )
                preview = preview_candidate_write(tampered, db, self.candidate_path())
                codes = [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]]
                self.assertIn(expected_code, codes, codes)

    def test_valid_source_forms_accepted(self) -> None:
        db = self.make_source_db()
        empty_source = self.make_ready_plan(db, tags=[])
        preview = preview_candidate_write(empty_source, db, self.candidate_path())
        self.assertTrue(preview.to_dict()["ready_to_write"])
        pkg = self.make_package()
        package2 = dataclasses.replace(pkg, submission={**pkg.submission})
        # build a plan whose package carries absolute http(s) sources
        submission = dict(package2.submission)
        metadata = dict(submission["metadata"])
        metadata["sources"] = {
            "nga": ["https://a.example/1"],
            "original_series": ["https://b.example/2"],
            "original": [], "translation_series": [], "translation": [],
        }
        submission["metadata"] = metadata
        package3 = dataclasses.replace(package2, submission=submission)
        package_hash = sha256_hex(
            stable_json({"schema_version": "1.0", "inbox": package3.inbox, "submission": package3.submission})
        )
        package4 = dataclasses.replace(package3, package_sha256=package_hash)
        plan = build_umalib_change_plan(
            package4,
            AdapterChoices(
                1700000000, None,
                ("https://a.example/1", "https://b.example/2"), (),
            ),
            load_target_tag_catalog(db),
        )
        self.assertTrue(plan.ready)
        preview = preview_candidate_write(plan, db, self.candidate_path())
        self.assertTrue(preview.to_dict()["ready_to_write"])
        self.assertEqual(
            preview.to_dict()["source"]["database_sha256"], sha256_file(db)
        )

    def test_content_structural_validation(self) -> None:
        db = self.make_source_db()
        plan = self.make_ready_plan(db, tags=[])
        bad_contents = [
            "",
            "<p>a<br>b</p>",
            '<p class="x">y</p>',
            "<p>&quot;</p>",
            "<p>a&b</p>",
            "<!--x--><p>y</p>",
            "<p><img src=x></p>",
            "<div><p>y</p></div>",
            "<p><script>x</script></p>",
            "<p><span style=\"color:red\">x</span></p>",
            "<p>y</p><script></script>",
            "<p></p>",
            "<p>\n</p>",
            "<p>\x00</p>",
            "<p><br></p>",
            "<p>   </p><p><br></p>",
        ]
        for content in bad_contents:
            with self.subTest(content=content):
                tampered = dataclasses.replace(
                    plan, article={**plan.article, "content": content}
                )
                preview = preview_candidate_write(tampered, db, self.candidate_path())
                codes = [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]]
                self.assertIn("article_content_invalid", codes)
        valid = dataclasses.replace(
            plan, article={**plan.article, "content": "<p>a&amp;b &lt;x&gt;</p><p><br></p><p>[关键词]注释</p>"}
        )
        preview = preview_candidate_write(valid, db, self.candidate_path())
        self.assertNotIn(
            "article_content_invalid",
            [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]],
        )

    def test_hash_consistent_unsafe_plan_shapes_are_blocked(self) -> None:
        db = self.make_source_db()
        self.seed_tags(db, [("甲", 0)])
        plan = self.make_ready_plan(db, tags=["甲"])

        with_article_id = self.rehash_plan(
            plan, article={**plan.article, "id": 9001}
        )
        preview = preview_candidate_write(with_article_id, db, self.candidate_path())
        self.assertIn(
            "article_shape_invalid",
            [item["code"] for item in preview.to_dict()["diagnostics"]["blockers"]],
        )

        with_tagged_ids = self.rehash_plan(
            plan, tags={**plan.tags, "tagged_ids": [1, 2]}
        )
        preview = preview_candidate_write(with_tagged_ids, db, self.candidate_path())
        self.assertIn(
            "tags_shape_invalid",
            [item["code"] for item in preview.to_dict()["diagnostics"]["blockers"]],
        )

        bool_type = self.rehash_plan(
            plan,
            tags={"existing": [{"id": 1, "name": "甲", "type": False}], "create": []},
        )
        preview = preview_candidate_write(bool_type, db, self.candidate_path())
        self.assertIn(
            "existing_tag_invalid",
            [item["code"] for item in preview.to_dict()["diagnostics"]["blockers"]],
        )

        duplicate_create = self.rehash_plan(
            plan,
            tags={
                "existing": [],
                "create": [{"name": "新标签", "type": 0}, {"name": "新标签", "type": 1}],
            },
        )
        preview = preview_candidate_write(duplicate_create, db, self.candidate_path())
        self.assertIn(
            "create_tag_duplicate",
            [item["code"] for item in preview.to_dict()["diagnostics"]["blockers"]],
        )

        malformed_targets = self.rehash_plan(
            plan,
            tags={
                "existing": [{"id": False, "name": "甲", "type": 0}],
                "create": [{"type": 0}],
            },
        )
        preview = preview_candidate_write(malformed_targets, db, self.candidate_path())
        data = preview.to_dict()
        self.assertFalse(data["ready_to_write"])
        self.assertEqual(data["operations"]["existing_tag_ids"], [])
        self.assertEqual(data["operations"]["create_tags"], [])

    def test_duplicate_evidence(self) -> None:
        db = self.make_source_db()
        sentinel_content = "<p>完全相同的正文</p>"
        title = "特有标题串"
        author = "特有作者串"
        self.seed_articles(db, [
            {"name": title, "author": author, "content": sentinel_content},
            {"name": title, "author": author, "content": "<p>不同正文</p>"},
        ])
        exact_plan = self.make_ready_plan(
            db, tags=[], title=title, author=author, body_text="完全相同的正文"
        )
        preview = preview_candidate_write(exact_plan, db, self.candidate_path())
        data = preview.to_dict()
        self.assertIn(
            "exact_content_duplicate",
            [b["code"] for b in data["diagnostics"]["blockers"]],
        )
        self.assertEqual(data["duplicates"]["exact_content_count"], 1)
        self.assertEqual(data["duplicates"]["exact_content_article_ids"], [1])
        self.assertFalse(data["ready_to_write"])
        similar_plan = self.make_ready_plan(
            db, tags=[], title=title, author=author, body_text="另一篇正文"
        )
        preview2 = preview_candidate_write(similar_plan, db, self.candidate_path())
        data2 = preview2.to_dict()
        self.assertIn(
            "same_title_author",
            [w["code"] for w in data2["diagnostics"]["warnings"]],
        )
        self.assertEqual(data2["duplicates"]["same_title_author_count"], 2)
        self.assertTrue(data2["ready_to_write"], data2["diagnostics"]["blockers"])
        raw = stable_json(data2)
        self.assertNotIn(title, raw)
        self.assertNotIn(sentinel_content, raw)

    def test_duplicate_ids_capped_and_sorted(self) -> None:
        db = self.make_source_db()
        self.seed_articles(
            db,
            [{"name": f"T{i}", "author": f"A{i}", "content": "<p>重复正文</p>"} for i in range(25)],
        )
        plan = self.make_ready_plan(db, tags=[])
        tampered = dataclasses.replace(
            plan, article={**plan.article, "content": "<p>重复正文</p>"}
        )
        preview = preview_candidate_write(tampered, db, self.candidate_path())
        data = preview.to_dict()
        self.assertEqual(data["duplicates"]["exact_content_count"], 25)
        ids = data["duplicates"]["exact_content_article_ids"]
        self.assertEqual(len(ids), 20)
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids, list(range(1, 21)))

    def test_source_change_at_end_of_preview_is_blocked(self) -> None:
        import umalib_ingest.umalib_writer.preview as preview_module

        db = self.make_source_db()
        plan = self.make_ready_plan(db, tags=[])
        real_collect = preview_module._collect_duplicates

        def collect_then_change(*args, **kwargs):
            evidence = real_collect(*args, **kwargs)
            conn = sqlite3.connect(str(db))
            conn.execute('INSERT INTO "tag" (name, type) VALUES (?, ?)', ("并发变更", 0))
            conn.commit()
            conn.close()
            return evidence

        with mock.patch.object(
            preview_module, "_collect_duplicates", side_effect=collect_then_change
        ):
            preview = preview_candidate_write(plan, db, self.candidate_path())
        self.assertIn(
            "source_changed_during_preview",
            [item["code"] for item in preview.to_dict()["diagnostics"]["blockers"]],
        )
        self.assertFalse(self.candidate_path().exists())

    def test_preview_no_paths_content_sql_and_immutable(self) -> None:
        db = self.make_source_db()
        self.seed_tags(db, [("甲", 1)])
        plan = self.make_ready_plan(db, tags=["甲"])
        preview = preview_candidate_write(plan, db, self.candidate_path())
        raw = stable_json(preview.to_dict())
        self.assertNotIn(str(self.tmpdir).replace("\\", "/"), raw)
        self.assertNotIn("INSERT", raw)
        self.assertNotIn("合成投稿标题", raw)
        first = preview.to_dict()
        first["operations"]["existing_tag_ids"].append(999)
        first["source"]["counts"]["article"] = -1
        again = preview.to_dict()
        self.assertEqual(again["operations"]["existing_tag_ids"], [1])
        self.assertEqual(again["source"]["counts"]["article"], 0)
        self.assertEqual(again["preview_sha256"], preview.preview_sha256)

    def test_invalid_source_preview_keeps_contract_safe_hashes(self) -> None:
        db = self.make_source_db()
        plan = self.make_ready_plan(db, tags=[])
        missing = self.tmpdir / "missing.db"
        preview = preview_candidate_write(plan, missing, self.candidate_path())
        data = preview.to_dict()
        self.assertFalse(data["ready_to_write"])
        self.assertEqual(data["source"]["database_sha256"], "0" * 64)
        self.assertEqual(data["source"]["schema_fingerprint_sha256"], "0" * 64)
        self.assertEqual(len(data["source"]["path_sha256"]), 64)
        self.assertNotIn(str(self.tmpdir), stable_json(data))


class PathAndApprovalTests(WriterTestCase):
    def test_source_equals_candidate_and_bad_suffix_and_missing_parent(self) -> None:
        db = self.make_source_db()
        plan = self.make_ready_plan(db, tags=[])
        same = preview_candidate_write(plan, db, db)
        codes = [b["code"] for b in same.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("candidate_exists", codes)
        bad_suffix = preview_candidate_write(plan, db, self.tmpdir / "candidate.txt")
        codes = [b["code"] for b in bad_suffix.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("candidate_path_invalid", codes)
        missing_parent = preview_candidate_write(
            plan, db, self.tmpdir / "missing-dir" / "candidate.db"
        )
        codes = [b["code"] for b in missing_parent.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("candidate_path_invalid", codes)
        with self.assertRaises(WriterNotReadyError):
            write_candidate_database(
                plan, db, db, self.make_approval(plan, same, db)
            )
        self.assertEqual(self.directory_names(), {"source.db"})

    def test_source_sidecar_and_candidate_sidecar_blocked(self) -> None:
        db = self.make_source_db()
        plan = self.make_ready_plan(db, tags=[])
        sidecar = self.tmpdir / "source.db-wal"
        sidecar.write_bytes(b"")
        preview = preview_candidate_write(plan, db, self.candidate_path())
        codes = [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("source_sidecar_present", codes)
        sidecar.unlink()
        candidate_sidecar = self.tmpdir / "candidate.db-shm"
        candidate_sidecar.write_bytes(b"")
        preview = preview_candidate_write(plan, db, self.candidate_path())
        codes = [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("candidate_sidecar_present", codes)
        candidate_sidecar.unlink()

    def test_source_symlink_rejected_with_portable_fallback(self) -> None:
        db = self.make_source_db()
        link = self.tmpdir / "link.db"
        plan = self.make_ready_plan(db, tags=[])
        try:
            os.symlink(str(db), str(link))
        except (OSError, NotImplementedError):
            link.write_bytes(db.read_bytes())
            original_is_symlink = Path.is_symlink

            def simulated_is_symlink(path: Path) -> bool:
                if path == link:
                    return True
                return original_is_symlink(path)

            with mock.patch.object(Path, "is_symlink", simulated_is_symlink):
                preview = preview_candidate_write(plan, link, self.candidate_path())
        else:
            preview = preview_candidate_write(plan, link, self.candidate_path())
        codes = [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("source_path_invalid", codes)

    def test_public_api_accepts_string_paths(self) -> None:
        db = self.make_source_db()
        plan = self.make_ready_plan(db, tags=[])
        candidate = self.candidate_path()
        preview = preview_candidate_write(plan, str(db), str(candidate))
        self.assertTrue(preview.ready_to_write)
        result = write_candidate_database(
            plan, str(db), str(candidate), self.make_approval(plan, preview, db)
        )
        self.assertTrue(result.success)

    def test_existing_candidate_preserved(self) -> None:
        db = self.make_source_db()
        plan = self.make_ready_plan(db, tags=[])
        candidate = self.candidate_path()
        candidate.write_bytes(b"precious bytes")
        original_sha = sha256_file(candidate)
        original_mtime = candidate.stat().st_mtime_ns
        preview = preview_candidate_write(plan, db, candidate)
        self.assertIn(
            "candidate_exists",
            [b["code"] for b in preview.to_dict()["diagnostics"]["blockers"]],
        )
        with self.assertRaises(WriterNotReadyError):
            write_candidate_database(plan, db, candidate, self.make_approval(plan, preview, db))
        self.assertEqual(sha256_file(candidate), original_sha)
        self.assertEqual(candidate.stat().st_mtime_ns, original_mtime)
        self.assertEqual(candidate.read_bytes(), b"precious bytes")

    def test_approval_binding_failures_create_no_files(self) -> None:
        db = self.make_source_db()
        plan = self.make_ready_plan(db, tags=[])
        candidate = self.candidate_path()
        preview = preview_candidate_write(plan, db, candidate)
        wrong_plan = dataclasses.replace(
            self.make_approval(plan, preview, db), plan_sha256="f" * 64
        )
        with self.assertRaises(WriterApprovalError):
            write_candidate_database(plan, db, candidate, wrong_plan)
        wrong_preview = dataclasses.replace(
            self.make_approval(plan, preview, db), preview_sha256="f" * 64
        )
        with self.assertRaises(WriterApprovalError):
            write_candidate_database(plan, db, candidate, wrong_preview)
        wrong_source = dataclasses.replace(
            self.make_approval(plan, preview, db), source_database_sha256="f" * 64
        )
        with self.assertRaises(WriterApprovalError):
            write_candidate_database(plan, db, candidate, wrong_source)
        for confirmed in (False, 1, 0, "yes", None):
            with self.subTest(confirmed=confirmed):
                approval = CandidateWriteApproval(
                    plan_sha256=plan.plan_sha256,
                    preview_sha256=preview.preview_sha256,
                    source_database_sha256=sha256_file(db),
                    app_closed_confirmed=confirmed,
                )
                with self.assertRaises(WriterApprovalError):
                    write_candidate_database(plan, db, candidate, approval)
        self.assertEqual(self.directory_names(), {"source.db"})

    def test_not_ready_plan_raises_with_preview(self) -> None:
        db = self.make_source_db()
        blocked = build_umalib_change_plan(
            self.make_package(tags=["不存在的标签"]),
            AdapterChoices(1700000000, None, (), ()),
            load_target_tag_catalog(db),
        )
        candidate = self.candidate_path()
        with self.assertRaises(WriterNotReadyError) as caught:
            write_candidate_database(
                blocked, db, candidate,
                CandidateWriteApproval("f" * 64, "f" * 64, "f" * 64, True),
            )
        self.assertFalse(caught.exception.preview.to_dict()["ready_to_write"])
        self.assertEqual(self.directory_names(), {"source.db"})


class WriteSuccessTests(WriterTestCase):
    def approve_and_write(self, plan, db, candidate):
        preview = preview_candidate_write(plan, db, candidate)
        self.assertTrue(preview.to_dict()["ready_to_write"], preview.to_dict()["diagnostics"])
        approval = self.make_approval(plan, preview, db)
        return write_candidate_database(plan, db, candidate, approval)

    def test_write_existing_tag_only(self) -> None:
        db = self.make_source_db()
        self.seed_tags(db, [("甲", 1)])
        self.seed_other_tables(db)
        plan = self.make_ready_plan(db, tags=["甲"])
        candidate = self.candidate_path()
        source_before = (sha256_file(db), db.stat().st_size, db.stat().st_mtime_ns)
        result = self.approve_and_write(plan, db, candidate)
        data = result.to_dict()
        self.assertTrue(data["success"])
        self.assertEqual(data["inserted"]["reused_tag_ids"], [1])
        self.assertEqual(data["inserted"]["created_tags"], [])
        self.assertEqual(data["inserted"]["tagged_count"], 1)
        self.assertEqual(data["counts_before"], {"article": 0, "tag": 1, "tagged": 0})
        self.assertEqual(data["counts_after"], {"article": 1, "tag": 1, "tagged": 1})
        self.assertTrue(data["source"]["unchanged"])
        self.assertEqual(
            (sha256_file(db), db.stat().st_size, db.stat().st_mtime_ns), source_before
        )
        self.assertEqual(self.directory_names(), {"source.db", "candidate.db"})
        conn = sqlite3.connect(str(candidate))
        conn.row_factory = sqlite3.Row
        article = conn.execute('SELECT * FROM "article" WHERE id = ?', (data["inserted"]["article_id"],)).fetchone()
        self.assertEqual(article["name"], "合成投稿标题")
        self.assertEqual(article["author"], "合成作者")
        self.assertEqual(article["content"], "<p>合成正文第一行。</p><p><br></p>")
        tagged = conn.execute(
            'SELECT * FROM "tagged" WHERE artId = ?', (data["inserted"]["article_id"],)
        ).fetchone()
        self.assertEqual(tagged["tagId"], 1)
        conn.close()
        self.assertEqual(data["checks"]["inspector_blocker_count"], 0)
        payload = {k: v for k, v in data.items() if k != "result_sha256"}
        self.assertEqual(sha256_hex(stable_json(payload)), data["result_sha256"])

    def test_write_existing_plus_created_tags(self) -> None:
        db = self.make_source_db()
        self.seed_tags(db, [("甲", 1)])
        plan = self.make_ready_plan(
            db, tags=["甲", "乙", "丙"],
            tag_choices=(TagChoice("tag", "乙", new_tag_type=2), TagChoice("tag", "丙", new_tag_type=2)),
        )
        result = self.approve_and_write(plan, db, self.candidate_path())
        data = result.to_dict()
        self.assertEqual(data["counts_after"], {"article": 1, "tag": 3, "tagged": 3})
        created = data["inserted"]["created_tags"]
        self.assertEqual([(t["name"], t["type"]) for t in created], [("丙", 2), ("乙", 2)])
        self.assertEqual(data["inserted"]["reused_tag_ids"], [1])
        conn = sqlite3.connect(str(self.candidate_path()))
        conn.row_factory = sqlite3.Row
        for tag in created:
            row = conn.execute(
                'SELECT name, type, cover, description FROM "tag" WHERE id = ?', (tag["id"],)
            ).fetchone()
            self.assertEqual(row["name"], tag["name"])
            self.assertEqual(row["type"], tag["type"])
            self.assertIsNone(row["cover"])
            self.assertIsNone(row["description"])
        conn.close()

    def test_write_no_tags(self) -> None:
        db = self.make_source_db()
        plan = self.make_ready_plan(db, tags=[])
        result = self.approve_and_write(plan, db, self.candidate_path())
        data = result.to_dict()
        self.assertEqual(data["inserted"]["tagged_count"], 0)
        self.assertEqual(data["counts_after"], {"article": 1, "tag": 0, "tagged": 0})

    def test_approved_same_title_author_warning_can_be_written(self) -> None:
        db = self.make_source_db()
        self.seed_articles(
            db,
            [{"name": "相同标题", "author": "相同作者", "content": "<p>旧正文</p>"}],
        )
        plan = self.make_ready_plan(
            db, tags=[], title="相同标题", author="相同作者", body_text="新正文"
        )
        preview = preview_candidate_write(plan, db, self.candidate_path())
        self.assertTrue(preview.ready_to_write)
        self.assertIn(
            "same_title_author",
            [item["code"] for item in preview.to_dict()["diagnostics"]["warnings"]],
        )
        result = write_candidate_database(
            plan, db, self.candidate_path(), self.make_approval(plan, preview, db)
        )
        self.assertEqual(result.to_dict()["counts_after"]["article"], 2)

    def test_article_fields_exact_readback(self) -> None:
        db = self.make_source_db()
        plan = self.make_ready_plan(
            db, tags=[], note="合成备注", translator="合成译者",
            body_text="第一行\n\n[关键词]注释\n", upload_time=1234567890,
        )
        result = self.approve_and_write(plan, db, self.candidate_path())
        data = result.to_dict()
        conn = sqlite3.connect(str(self.candidate_path()))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            'SELECT name, note, content, author, translator, uploadTime, source FROM "article" WHERE id = ?',
            (data["inserted"]["article_id"],),
        ).fetchone()
        conn.close()
        self.assertEqual(row["name"], "合成投稿标题")
        self.assertEqual(row["note"], "合成备注")
        self.assertEqual(row["content"], "<p>第一行</p><p><br></p><p>[关键词]注释</p><p><br></p>")
        self.assertEqual(row["author"], "合成作者")
        self.assertEqual(row["translator"], "合成译者")
        self.assertEqual(row["uploadTime"], 1234567890)
        self.assertEqual(row["source"], "")

    def test_autoincrement_ids_from_real_sequence(self) -> None:
        db = self.make_source_db()
        self.seed_tags(db, [("甲", 1)])
        self.seed_articles(db, [{"name": "旧文", "author": "旧作者", "content": "<p>旧</p>"}])
        plan = self.make_ready_plan(db, tags=["甲", "乙"], tag_choices=(TagChoice("tag", "乙", new_tag_type=1),))
        result = self.approve_and_write(plan, db, self.candidate_path())
        data = result.to_dict()
        self.assertEqual(data["inserted"]["article_id"], 2)
        self.assertEqual(data["inserted"]["created_tags"][0]["id"], 2)
        self.assertEqual(len(data["inserted"]["tagged_ids"]), 2)

    def test_other_tables_unchanged(self) -> None:
        db = self.make_source_db()
        self.seed_tags(db, [("甲", 1)])
        self.seed_other_tables(db)
        plan = self.make_ready_plan(db, tags=["甲"])
        self.approve_and_write(plan, db, self.candidate_path())
        for table in ("creator", "rec", "dict"):
            source_rows = self._all_rows(db, table)
            candidate_rows = self._all_rows(self.candidate_path(), table)
            self.assertEqual(source_rows, candidate_rows, table)

    def _all_rows(self, db: Path, table: str) -> list[tuple]:
        conn = sqlite3.connect(str(db))
        rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
        conn.close()
        return rows

    def test_result_immutable_and_hash_stable(self) -> None:
        db = self.make_source_db()
        self.seed_tags(db, [("甲", 1)])
        plan = self.make_ready_plan(db, tags=["甲"])
        result = self.approve_and_write(plan, db, self.candidate_path())
        first = result.to_dict()
        first["inserted"]["tagged_ids"].append(9999)
        first["counts_after"]["article"] = -1
        again = result.to_dict()
        self.assertEqual(again["inserted"]["tagged_count"], 1)
        self.assertEqual(again["counts_after"]["article"], 1)
        self.assertEqual(again["result_sha256"], result.result_sha256)
        keys = {
            "schema_version", "success", "plan_sha256", "preview_sha256", "source",
            "candidate", "inserted", "counts_before", "counts_after", "checks", "result_sha256",
        }
        self.assertEqual(set(again.keys()), keys)

    def test_no_temp_or_sidecar_files_after_write(self) -> None:
        db = self.make_source_db()
        plan = self.make_ready_plan(db, tags=[])
        self.approve_and_write(plan, db, self.candidate_path())
        names = self.directory_names()
        self.assertEqual(names, {"source.db", "candidate.db"})
        self.assertFalse((self.tmpdir / "candidate.db-wal").exists())
        self.assertFalse((self.tmpdir / "candidate.db-shm").exists())
        self.assertFalse((self.tmpdir / "candidate.db-journal").exists())
        self.assertFalse(
            any(name.startswith(".umalib-writer-") for name in names)
        )


class FailureRollbackTests(WriterTestCase):
    def build_ready(self, name: str = "source.db"):
        db = self.make_source_db(name)
        self.seed_tags(db, [("甲", 1)])
        plan = self.make_ready_plan(db, tags=["甲"])
        return db, plan

    def run_with_failure(self, patch_target: str, side_effect):
        db, plan = self.build_ready()
        candidate = self.candidate_path()
        preview = preview_candidate_write(plan, db, candidate)
        approval = self.make_approval(plan, preview, db)
        source_before = (sha256_file(db), db.stat().st_size, db.stat().st_mtime_ns)
        with mock.patch(patch_target, side_effect=side_effect):
            with self.assertRaises(UmaLibWriterError):
                write_candidate_database(plan, db, candidate, approval)
        self.assertEqual(
            (sha256_file(db), db.stat().st_size, db.stat().st_mtime_ns), source_before
        )
        self.assertEqual(self.directory_names(), {"source.db"})

    def test_article_insert_failure(self) -> None:
        self.run_with_failure(
            "umalib_ingest.umalib_writer.writer._insert_article",
            sqlite3.OperationalError("simulated"),
        )

    def test_create_tag_mid_failure(self) -> None:
        def fake_create_tags(conn, plan_dict):
            conn.execute(
                'INSERT INTO "tag" (name, type, cover, description) VALUES (?, ?, NULL, NULL)',
                ("半途标签", 0),
            )
            raise sqlite3.OperationalError("simulated")

        db, plan = self.build_ready()
        plan2 = self.make_ready_plan(
            db, tags=["甲", "乙"], tag_choices=(TagChoice("tag", "乙", new_tag_type=0),)
        )
        candidate = self.candidate_path()
        preview = preview_candidate_write(plan2, db, candidate)
        approval = self.make_approval(plan2, preview, db)
        source_before = (sha256_file(db), db.stat().st_size, db.stat().st_mtime_ns)
        with mock.patch(
            "umalib_ingest.umalib_writer.writer._insert_create_tags",
            side_effect=fake_create_tags,
        ):
            with self.assertRaises(UmaLibWriterError):
                write_candidate_database(plan2, db, candidate, approval)
        self.assertEqual(
            (sha256_file(db), db.stat().st_size, db.stat().st_mtime_ns), source_before
        )
        self.assertEqual(self.directory_names(), {"source.db"})

    def test_tagged_mid_failure(self) -> None:
        def fake_insert_tagged(conn, article_id, tag_ids):
            conn.execute(
                'INSERT INTO "tagged" (artId, tagId) VALUES (?, ?)', (article_id, tag_ids[0])
            )
            raise sqlite3.OperationalError("simulated")

        self.run_with_failure(
            "umalib_ingest.umalib_writer.writer._insert_tagged", fake_insert_tagged
        )

    def test_in_transaction_verify_failure(self) -> None:
        self.run_with_failure(
            "umalib_ingest.umalib_writer.writer._verify_in_transaction",
            sqlite3.OperationalError("simulated"),
        )

    def test_post_commit_verify_failure(self) -> None:
        self.run_with_failure(
            "umalib_ingest.umalib_writer.writer._post_commit_verify",
            WriterExecutionError("simulated"),
        )

    def test_source_changed_after_copy_aborts(self) -> None:
        import umalib_ingest.umalib_writer.writer as writer_module

        real_assert = writer_module.assert_unchanged

        def changing_assert(target, before):
            conn = sqlite3.connect(str(target))
            conn.execute('INSERT INTO "tag" (name, type) VALUES (?, ?)', ("并发标签", 0))
            conn.commit()
            conn.close()
            real_assert(target, before)

        db, plan = self.build_ready()
        candidate = self.candidate_path()
        preview = preview_candidate_write(plan, db, candidate)
        approval = self.make_approval(plan, preview, db)
        with mock.patch(
            "umalib_ingest.umalib_writer.writer.assert_unchanged",
            side_effect=changing_assert,
        ):
            with self.assertRaises(WriterDatabaseError):
                write_candidate_database(plan, db, candidate, approval)
        self.assertEqual(self.directory_names(), {"source.db"})
        conn = sqlite3.connect(str(db))
        count = conn.execute('SELECT COUNT(*) FROM "tag"').fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    def test_publish_link_failure_cleans_up(self) -> None:
        self.run_with_failure(
            "umalib_ingest.umalib_writer.writer.os.link",
            FileExistsError("race"),
        )

    def test_publish_race_preserves_external_candidate(self) -> None:
        db, plan = self.build_ready()
        candidate = self.candidate_path()
        preview = preview_candidate_write(plan, db, candidate)
        approval = self.make_approval(plan, preview, db)

        def occupying_link(_source, target):
            Path(target).write_bytes(b"external-race-winner")
            raise FileExistsError("simulated race")

        with mock.patch(
            "umalib_ingest.umalib_writer.writer.os.link", side_effect=occupying_link
        ):
            with self.assertRaises(WriterExecutionError):
                write_candidate_database(plan, db, candidate, approval)
        self.assertEqual(candidate.read_bytes(), b"external-race-winner")
        self.assertFalse(
            any(path.name.startswith(".umalib-writer-") for path in self.tmpdir.iterdir())
        )

    def test_publish_verification_failure_removes_owned_candidate(self) -> None:
        import umalib_ingest.umalib_writer.writer as writer_module

        db, plan = self.build_ready()
        candidate = self.candidate_path()
        preview = preview_candidate_write(plan, db, candidate)
        approval = self.make_approval(plan, preview, db)
        real_sha256_file = writer_module.sha256_file

        def mismatching_candidate_hash(path):
            if Path(path) == candidate:
                return "0" * 64
            return real_sha256_file(Path(path))

        with mock.patch.object(
            writer_module, "sha256_file", side_effect=mismatching_candidate_hash
        ):
            with self.assertRaises(WriterExecutionError):
                write_candidate_database(plan, db, candidate, approval)
        self.assertFalse(candidate.exists())
        self.assertEqual(self.directory_names(), {"source.db"})

    def test_cleanup_does_not_delete_replaced_temp_file(self) -> None:
        db, plan = self.build_ready()
        candidate = self.candidate_path()
        preview = preview_candidate_write(plan, db, candidate)
        approval = self.make_approval(plan, preview, db)
        replacement: list[Path] = []

        def replace_temp_then_fail(temp_path, *_args):
            temp_path.unlink()
            temp_path.write_bytes(b"foreign-temp-replacement")
            replacement.append(temp_path)
            raise WriterExecutionError("simulated replacement")

        with mock.patch(
            "umalib_ingest.umalib_writer.writer._post_commit_verify",
            side_effect=replace_temp_then_fail,
        ):
            with self.assertRaises(WriterExecutionError):
                write_candidate_database(plan, db, candidate, approval)
        self.assertFalse(candidate.exists())
        self.assertEqual(len(replacement), 1)
        self.assertEqual(replacement[0].read_bytes(), b"foreign-temp-replacement")

    def test_final_inspector_blocker_removes_owned_candidate(self) -> None:
        import umalib_ingest.umalib_writer.writer as writer_module

        db, plan = self.build_ready()
        candidate = self.candidate_path()
        preview = preview_candidate_write(plan, db, candidate)
        approval = self.make_approval(plan, preview, db)
        real_final = writer_module._final_candidate_inspection

        def corrupt_then_inspect(path, *args):
            conn = sqlite3.connect(str(path))
            conn.execute(
                'INSERT INTO "tagged" (artId, tagId) VALUES (?, ?)', (999999, 1)
            )
            conn.commit()
            conn.close()
            return real_final(path, *args)

        with mock.patch.object(
            writer_module, "_final_candidate_inspection", side_effect=corrupt_then_inspect
        ):
            with self.assertRaises(WriterExecutionError):
                write_candidate_database(plan, db, candidate, approval)
        self.assertFalse(candidate.exists())
        self.assertEqual(self.directory_names(), {"source.db"})


class SqlBoundaryTests(WriterTestCase):
    def test_sql_allowlist_and_parameterization(self) -> None:
        import umalib_ingest.umalib_writer.writer as writer_module
        import umalib_ingest.umalib_writer.catalog as catalog_module
        import umalib_ingest.umalib_writer.preview as preview_module

        writer_text = Path(writer_module.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "executescript",
            "ATTACH",
            "DETACH",
            "VACUUM",
            "writable_schema",
            "journal_mode",
            "os.replace",
            "shutil.move",
            "CREATE TABLE",
            "DROP",
            "ALTER TABLE",
            "OR REPLACE",
            "UPDATE ",
            "DELETE ",
            '"creator"',
            '"rec"',
            '"dict"',
            "sqlite_sequence",
        ):
            self.assertNotIn(forbidden, writer_text, forbidden)
        for statement in ('INSERT INTO "article"', 'INSERT INTO "tag"', 'INSERT INTO "tagged"'):
            self.assertIn(statement, writer_text)
            self.assertIn("?", writer_text)
        for insert in re_find_all(writer_text, r'INSERT INTO "[a-z]+" [^(]*\([^)]*\)\s*VALUES[^"]*'):
            self.assertIn("?", insert)
        self.assertNotIn("glob", writer_text.lower().replace("module", ""))

    def test_other_tables_rows_identical_after_write(self) -> None:
        db = self.make_source_db()
        self.seed_tags(db, [("甲", 1)])
        self.seed_other_tables(db)
        plan = self.make_ready_plan(db, tags=["甲"])
        preview = preview_candidate_write(plan, db, self.candidate_path())
        approval = self.make_approval(plan, preview, db)
        write_candidate_database(plan, db, self.candidate_path(), approval)
        conn_source = sqlite3.connect(str(db))
        conn_candidate = sqlite3.connect(str(self.candidate_path()))
        for table in ("creator", "rec", "dict"):
            source_rows = conn_source.execute(f'SELECT * FROM "{table}"').fetchall()
            candidate_rows = conn_candidate.execute(f'SELECT * FROM "{table}"').fetchall()
            self.assertEqual(source_rows, candidate_rows, table)
        conn_source.close()
        conn_candidate.close()


def re_find_all(text: str, pattern: str) -> list[str]:
    import re

    return re.findall(pattern, text)


if __name__ == "__main__":
    unittest.main()
