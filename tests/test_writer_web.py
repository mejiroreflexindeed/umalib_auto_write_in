"""Tests for the Writer UI HTTP integration (TASK-009).

All Inbox/source/candidate databases live in each test's own
TemporaryDirectory; the source is only ever read.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from umalib_ingest.inbox import InboxRepository
from umalib_ingest.web import create_server
from umalib_ingest.web.writer_ui import WriterUiConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "writer_web"
SCHEMA_SQL = (FIXTURES_DIR / "umalib-schema.sql").read_text(encoding="utf-8")
STATIC_DIR = PROJECT_ROOT / "src" / "umalib_ingest" / "web" / "static"
CONFIRMED_FINGERPRINT = (
    "d7b1966992e75819e37d41ae9c091ec21c2cdf9c5b6727af78f95398d0179aa8"
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def full_submission(tags=("角色甲",)):
    return (
        "[收录]\n"
        "[标题] 星光下的测试稿\n"
        "[作者] 测试作者\n"
        "[标签] " + ", ".join(tags) + "\n"
        "\n"
        "正文第一行。\n"
    )


class WriterWebTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="umalib_writer_web_")
        self.addCleanup(tmp.cleanup)
        self.tmpdir = Path(tmp.name)
        self.inbox_path = self.tmpdir / "inbox.db"
        self.repo = InboxRepository(self.inbox_path, clock=self._clock())
        self.repo.initialize()
        self.source_path = self.tmpdir / "umalib.db"
        conn = sqlite3.connect(str(self.source_path))
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        conn.close()
        self.candidate_dir = self.tmpdir / "out"
        self.candidate_dir.mkdir()
        self.writer_config = WriterUiConfig(
            source_db=self.source_path, candidate_dir=self.candidate_dir
        )
        self.server = create_server(self.repo, port=0, writer_config=self.writer_config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.addCleanup(self._stop_server)

    @staticmethod
    def _clock():
        values = ["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
                  "2026-01-03T00:00:00+00:00", "2026-01-04T00:00:00+00:00"]
        index = 0

        def clock():
            nonlocal index
            value = values[min(index, len(values) - 1)]
            index += 1
            return value

        return clock

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method: str, path: str, body=None, headers=None):
        headers = dict(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers)
            res = conn.getresponse()
            payload = res.read()
            return res.status, dict(res.getheaders()), payload
        finally:
            conn.close()

    def get_json(self, path: str):
        status, _, payload = self.request("GET", path)
        return status, json.loads(payload.decode("utf-8"))

    def post_json(self, path: str, payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _, data = self.request(
            "POST", path, body=body, headers={"Content-Type": "application/json"}
        )
        return status, json.loads(data.decode("utf-8")) if data else {}

    def put_json(self, path: str, payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _, data = self.request(
            "PUT", path, body=body, headers={"Content-Type": "application/json"}
        )
        return status, json.loads(data.decode("utf-8")) if data else {}

    def seed_tags(self, tags: list[tuple[str, int]]) -> list[int]:
        conn = sqlite3.connect(str(self.source_path))
        ids = []
        for name, tag_type in tags:
            cursor = conn.execute(
                'INSERT INTO "tag" (name, type) VALUES (?, ?)', (name, tag_type)
            )
            ids.append(cursor.lastrowid)
        conn.commit()
        conn.close()
        return ids

    def seed_article(self, name, author, content):
        conn = sqlite3.connect(str(self.source_path))
        conn.execute(
            'INSERT INTO "article" (name, author, content) VALUES (?, ?, ?)',
            (name, author, content),
        )
        conn.commit()
        conn.close()

    def import_and_approve(self, raw_text=None, tags=("角色甲",), series=None):
        status, body = self.post_json(
            "/api/import",
            {"raw_text": raw_text if raw_text is not None else full_submission(tags)},
        )
        self.assertEqual(status, 200, body)
        item_id = body["data"]["id"]
        detail = body["data"]
        fields = detail["review_fields"]
        review_payload = {
            "title": fields["title"],
            "author": fields["author"],
            "translator": fields["translator"],
            "series": series,
            "tags": fields["tags"],
            "sources": {
                "nga": fields["sources"]["nga"],
                "original_series": fields["sources"]["original_series"],
                "original": fields["sources"]["original"],
                "translation_series": fields["sources"]["translation_series"],
                "translation": fields["sources"]["translation"],
            },
            "body_text": fields["body_text"],
        }
        status, body = self.put_json(f"/api/items/{item_id}/review", review_payload)
        self.assertEqual(status, 200, body)
        status, body = self.post_json(
            f"/api/items/{item_id}/transition", {"to_status": "approved", "reason": "确认"}
        )
        self.assertEqual(status, 200, body)
        return item_id

    def base_payload(self, item_id, upload_time=1788062400, tag_choices=None, sources=()):
        return {
            "candidate_file_name": f"candidate-{item_id}.db",
            "choices": {
                "upload_time_seconds": upload_time,
                "note": "合成备注",
                "ordered_sources": list(sources),
                "tag_choices": tag_choices or [],
            },
        }

    def directory_names(self) -> set[str]:
        return {entry.name for entry in self.tmpdir.iterdir()}


class WriterConfigTests(WriterWebTestCase):
    def test_unconfigured_mode_keeps_review_and_rejects_writer(self) -> None:
        server = create_server(self.repo, port=0, writer_config=None)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/writer/config")
            res = conn.getresponse()
            body = json.loads(res.read().decode("utf-8"))
            self.assertEqual(body["data"], {"enabled": False})
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/writer/catalog")
            res = conn.getresponse()
            body = json.loads(res.read().decode("utf-8"))
            self.assertEqual(res.status, 409)
            self.assertEqual(body["error"]["code"], "writer_not_configured")
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/items")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_config_api_has_no_absolute_path(self) -> None:
        status, body = self.get_json("/api/writer/config")
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertTrue(data["enabled"])
        self.assertEqual(data["source_file_name"], "umalib.db")
        self.assertEqual(data["candidate_dir_name"], "out")
        raw = json.dumps(data, ensure_ascii=False)
        self.assertNotIn(str(self.tmpdir).replace("\\", "/"), raw)
        self.assertNotIn(str(self.tmpdir), raw)

    def test_cli_requires_both_or_neither(self) -> None:
        env = dict(
            os.environ,
            PYTHONPATH=str(PROJECT_ROOT / "src"),
            PYTHONDONTWRITEBYTECODE="1",
        )

        def run_cli(*args):
            return subprocess.run(
                [sys.executable, "-B", "-m", "umalib_ingest.web", *args],
                capture_output=True, text=True, env=env, timeout=60,
            )

        inbox = self.tmpdir / "cli-inbox.db"
        result = run_cli("--db", str(inbox), "--init", "--umalib-db", str(self.source_path))
        self.assertNotEqual(result.returncode, 0)
        result = run_cli("--db", str(inbox), "--init", "--candidate-dir", str(self.candidate_dir))
        self.assertNotEqual(result.returncode, 0)
        missing_source = self.tmpdir / "missing.db"
        result = run_cli(
            "--db", str(inbox), "--init",
            "--umalib-db", str(missing_source), "--candidate-dir", str(self.candidate_dir),
        )
        self.assertNotEqual(result.returncode, 0)
        same_as_inbox = run_cli(
            "--db", str(inbox), "--init",
            "--umalib-db", str(inbox), "--candidate-dir", str(self.candidate_dir),
        )
        self.assertNotEqual(result.returncode, 0)
        missing_dir = self.tmpdir / "no-such-dir"
        result = run_cli(
            "--db", str(inbox), "--init",
            "--umalib-db", str(self.source_path), "--candidate-dir", str(missing_dir),
        )
        self.assertNotEqual(result.returncode, 0)
        before = sha256_file(self.source_path)
        after = sha256_file(self.source_path)
        self.assertEqual(before, after)
        self.assertEqual(self.directory_names(), {"inbox.db", "umalib.db", "out", "cli-inbox.db"})

    def test_cli_valid_writer_start(self) -> None:
        env = dict(
            os.environ,
            PYTHONPATH=str(PROJECT_ROOT / "src"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        inbox = self.tmpdir / "cli-inbox2.db"
        proc = subprocess.Popen(
            [
                sys.executable, "-B", "-m", "umalib_ingest.web",
                "--db", str(inbox), "--init", "--port", "0",
                "--umalib-db", str(self.source_path),
                "--candidate-dir", str(self.candidate_dir),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        try:
            line = proc.stdout.readline()
            self.assertIn("http://127.0.0.1:", line)
            line = proc.stdout.readline()
            self.assertIn("Inbox 数据库:", line)
            line = proc.stdout.readline()
            self.assertIn("UmaLib 源数据库(只读):", line)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()


class WriterDtoTests(WriterWebTestCase):
    def _approved_item(self):
        self.seed_tags([("角色甲", 1)])
        return self.import_and_approve(tags=("角色甲",))

    def test_invalid_payloads_rejected(self) -> None:
        item_id = self._approved_item()
        base = self.base_payload(item_id)
        cases = [
            dict(base, extra_key=1),
            dict(base, choices={**base["choices"], "extra": 1}),
            dict(base, choices={**base["choices"], "upload_time_seconds": True}),
            dict(base, choices={**base["choices"], "upload_time_seconds": 2147483648}),
            dict(base, choices={**base["choices"], "upload_time_seconds": -1}),
            dict(base, choices={**base["choices"], "note": 1}),
            dict(base, choices={**base["choices"], "ordered_sources": "not-a-list"}),
            dict(base, choices={**base["choices"], "ordered_sources": [1]}),
            dict(base, choices={**base["choices"], "tag_choices": "not-a-list"}),
            dict(base, choices={**base["choices"], "tag_choices": [
                {"requested_kind": "bogus", "requested_value": "x", "existing_tag_id": None, "new_tag_type": None}
            ]}),
            dict(base, choices={**base["choices"], "tag_choices": [
                {"requested_kind": "tag", "requested_value": "", "existing_tag_id": None, "new_tag_type": None}
            ]}),
            dict(base, choices={**base["choices"], "tag_choices": [
                {"requested_kind": "tag", "requested_value": "x", "existing_tag_id": 1, "new_tag_type": 1}
            ]}),
            dict(base, choices={**base["choices"], "tag_choices": [
                {"requested_kind": "tag", "requested_value": "x", "existing_tag_id": -1, "new_tag_type": None}
            ]}),
            dict(base, choices={**base["choices"], "tag_choices": [
                {"requested_kind": "tag", "requested_value": "x", "existing_tag_id": None, "new_tag_type": 9}
            ]}),
            dict(base, choices={**base["choices"], "tag_choices": [
                {"requested_kind": "tag", "requested_value": "x", "existing_tag_id": True, "new_tag_type": None}
            ]}),
            dict(base, choices={**base["choices"], "tag_choices": [
                {"requested_kind": "tag", "requested_value": "x", "existing_tag_id": None, "new_tag_type": True}
            ]}),
        ]
        for index, payload in enumerate(cases):
            with self.subTest(case=index):
                status, body = self.post_json(
                    f"/api/items/{item_id}/writer/preview", payload
                )
                self.assertEqual(status, 400, body)
                self.assertEqual(body["error"]["code"], "writer_invalid_request")

    def test_candidate_file_name_rejected(self) -> None:
        item_id = self._approved_item()
        base = self.base_payload(item_id)
        for bad in ("C:/abs/path.db", "../evil.db", "sub/name.db", "a\\b.db", "x.txt",
                    ".", "..", "", "a\x00b.db", "x" * 300 + ".db"):
            with self.subTest(bad=bad):
                payload = dict(base, candidate_file_name=bad)
                status, body = self.post_json(
                    f"/api/items/{item_id}/writer/preview", payload
                )
                self.assertEqual(status, 400, body)
                self.assertEqual(body["error"]["code"], "writer_invalid_request")

    def test_candidate_exists_preserved(self) -> None:
        item_id = self._approved_item()
        candidate = self.candidate_dir / f"candidate-{item_id}.db"
        candidate.write_bytes(b"precious bytes")
        original_sha = sha256_file(candidate)
        original_mtime = candidate.stat().st_mtime_ns
        status, body = self.post_json(
            f"/api/items/{item_id}/writer/preview", self.base_payload(item_id)
        )
        self.assertEqual(status, 200)
        preview = body["data"]["preview"]
        self.assertFalse(preview["ready_to_write"])
        codes = [b["code"] for b in preview["diagnostics"]["blockers"]]
        self.assertIn("candidate_exists", codes)
        self.assertEqual(sha256_file(candidate), original_sha)
        self.assertEqual(candidate.stat().st_mtime_ns, original_mtime)


class WriterCatalogPlanPreviewTests(WriterWebTestCase):
    def test_catalog_sorted_with_duplicates(self) -> None:
        ids = self.seed_tags([("乙", 1), ("同名", 0), ("甲", 2), ("同名", 0)])
        status, body = self.get_json("/api/writer/catalog")
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertEqual([t["id"] for t in data["tags"]], sorted(ids))
        self.assertEqual(data["schema_fingerprint_sha256"], CONFIRMED_FINGERPRINT)
        same = [t for t in data["tags"] if t["name"] == "同名"]
        self.assertEqual(len(same), 2)
        self.assertNotEqual(same[0]["id"], same[1]["id"])
        self.assertNotIn("catalog_sha256", data)

    def test_status_gating(self) -> None:
        self.seed_tags([("角色甲", 1)])
        status, body = self.post_json(
            "/api/import", {"raw_text": "[收录]\n\n只有正文。\n"}
        )
        incomplete_id = body["data"]["id"]
        approved_id = self.import_and_approve(tags=("角色甲",))
        ignored_id = self.post_json("/api/import", {"raw_text": "普通讨论。\n"})[1]["data"]["id"]
        for item_id in (incomplete_id, ignored_id):
            with self.subTest(item_id=item_id):
                status, body = self.post_json(
                    f"/api/items/{item_id}/writer/preview", self.base_payload(item_id)
                )
                self.assertEqual(status, 409)
                self.assertEqual(body["error"]["code"], "writer_item_not_approved")
        status, body = self.post_json(
            f"/api/items/{approved_id}/writer/preview", self.base_payload(approved_id)
        )
        self.assertEqual(status, 200)

    def test_unique_reuse_ambiguous_and_new_type(self) -> None:
        ids = self.seed_tags([("角色甲", 1), ("重名", 0), ("重名", 0)])
        unique_id = ids[0]
        item_unique = self.import_and_approve(tags=("角色甲",))
        status, body = self.post_json(
            f"/api/items/{item_unique}/writer/preview", self.base_payload(item_unique)
        )
        self.assertEqual(status, 200)
        plan = body["data"]["plan"]
        self.assertTrue(plan["ready"])
        self.assertEqual(
            plan["tags"]["existing"], [{"id": unique_id, "name": "角色甲", "type": 1}]
        )
        item_ambiguous = self.import_and_approve(tags=("重名",))
        status, body = self.post_json(
            f"/api/items/{item_ambiguous}/writer/preview", self.base_payload(item_ambiguous)
        )
        self.assertEqual(status, 200)
        plan = body["data"]["plan"]
        self.assertFalse(plan["ready"])
        codes = [b["code"] for b in plan["diagnostics"]["blockers"]]
        self.assertIn("ambiguous_tag_name", codes)
        status, body = self.post_json(
            f"/api/items/{item_ambiguous}/writer/preview",
            self.base_payload(
                item_ambiguous,
                tag_choices=[
                    {"requested_kind": "tag", "requested_value": "重名",
                     "existing_tag_id": ids[1], "new_tag_type": None}
                ],
            ),
        )
        self.assertEqual(status, 200)
        plan = body["data"]["plan"]
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["tags"]["existing"][0]["id"], ids[1])
        item_new = self.import_and_approve(tags=("全新标签",))
        status, body = self.post_json(
            f"/api/items/{item_new}/writer/preview",
            self.base_payload(
                item_new,
                tag_choices=[
                    {"requested_kind": "tag", "requested_value": "全新标签",
                     "existing_tag_id": None, "new_tag_type": 2}
                ],
            ),
        )
        self.assertEqual(status, 200)
        plan = body["data"]["plan"]
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["tags"]["create"], [{"name": "全新标签", "type": 2}])

    def test_series_does_not_guess_type(self) -> None:
        self.seed_tags([("角色甲", 1)])
        item_id = self.import_and_approve(tags=("角色甲",), series="未知系列")
        status, body = self.post_json(
            f"/api/items/{item_id}/writer/preview", self.base_payload(item_id)
        )
        self.assertEqual(status, 200)
        plan = body["data"]["plan"]
        codes = [b["code"] for b in plan["diagnostics"]["blockers"]]
        self.assertIn("series_resolution_required", codes)
        self.assertEqual(plan["tags"]["create"], [])

    def test_source_required_and_ordered(self) -> None:
        self.seed_tags([("角色甲", 1)])
        raw = (
            "[收录]\n[标题] 有来源稿\n[作者] 作者\n[标签] 角色甲\n"
            "[原作] https://a.example/1\n[译作] https://b.example/2\n\n正文。\n"
        )
        item_id = self.import_and_approve(raw_text=raw, tags=("角色甲",))
        status, body = self.post_json(
            f"/api/items/{item_id}/writer/preview", self.base_payload(item_id)
        )
        self.assertEqual(status, 200)
        plan = body["data"]["plan"]
        codes = [b["code"] for b in plan["diagnostics"]["blockers"]]
        self.assertIn("source_selection_required", codes)
        status, body = self.post_json(
            f"/api/items/{item_id}/writer/preview",
            self.base_payload(
                item_id,
                sources=("https://b.example/2", "https://a.example/1"),
            ),
        )
        self.assertEqual(status, 200)
        plan = body["data"]["plan"]
        self.assertTrue(plan["ready"])
        self.assertEqual(
            plan["article"]["source"], "https://b.example/2 https://a.example/1"
        )

    def test_duplicates_blocker_and_warning(self) -> None:
        self.seed_tags([("角色甲", 1)])
        self.seed_article("重复标题", "重复作者", "<p>重复正文内容</p>")
        raw = "[收录]\n[标题] 重复标题\n[作者] 重复作者\n[标签] 角色甲\n\n重复正文内容\n"
        item_id = self.import_and_approve(raw_text=raw, tags=("角色甲",))
        status, body = self.post_json(
            f"/api/items/{item_id}/writer/preview", self.base_payload(item_id)
        )
        self.assertEqual(status, 200)
        preview = body["data"]["preview"]
        self.assertFalse(preview["ready_to_write"])
        codes = [b["code"] for b in preview["diagnostics"]["blockers"]]
        self.assertIn("exact_content_duplicate", codes)
        self.assertEqual(preview["duplicates"]["exact_content_count"], 1)

    def test_preview_stable_and_no_files_no_disclosure(self) -> None:
        self.seed_tags([("角色甲", 1)])
        self.seed_article("异文哨兵标题", "异文哨兵作者", "<p>异文哨兵正文</p>")
        item_id = self.import_and_approve(tags=("角色甲",))
        before = self.directory_names()
        payload = self.base_payload(item_id)
        first = self.post_json(f"/api/items/{item_id}/writer/preview", payload)[1]["data"]
        second = self.post_json(f"/api/items/{item_id}/writer/preview", payload)[1]["data"]
        self.assertEqual(
            json.dumps(first["plan"], sort_keys=True),
            json.dumps(second["plan"], sort_keys=True),
        )
        self.assertEqual(first["preview"]["preview_sha256"], second["preview"]["preview_sha256"])
        self.assertEqual(self.directory_names(), before)
        raw = json.dumps(first, ensure_ascii=False)
        self.assertNotIn(str(self.tmpdir).replace("\\", "/"), raw)
        self.assertNotIn("INSERT", raw)
        self.assertNotIn("异文哨兵正文", raw)


class WriterWriteTests(WriterWebTestCase):
    def _ready_write_payload(self, item_id, upload_time=1788062400):
        self.seed_tags([("角色甲", 1)])
        return self.base_payload(
            item_id,
            upload_time=upload_time,
            tag_choices=[
                {"requested_kind": "tag", "requested_value": "角色甲",
                 "existing_tag_id": 1, "new_tag_type": None},
                {"requested_kind": "tag", "requested_value": "合成新标签",
                 "existing_tag_id": None, "new_tag_type": 0},
            ],
        )

    def _preview(self, item_id, payload):
        status, body = self.post_json(f"/api/items/{item_id}/writer/preview", payload)
        self.assertEqual(status, 200, body)
        data = body["data"]
        self.assertTrue(data["plan"]["ready"], data["plan"]["diagnostics"])
        self.assertTrue(data["preview"]["ready_to_write"], data["preview"]["diagnostics"])
        return data

    def _write_payload(self, payload, data, source_sha, confirmed=True):
        return {
            **payload,
            "expected_plan_sha256": data["plan"]["plan_sha256"],
            "expected_preview_sha256": data["preview"]["preview_sha256"],
            "expected_source_database_sha256": source_sha,
            "app_closed_confirmed": confirmed,
        }

    def test_write_approval_mismatches(self) -> None:
        item_id = self.import_and_approve(tags=("角色甲", "合成新标签"))
        payload = self._ready_write_payload(item_id)
        data = self._preview(item_id, payload)
        source_sha = sha256_file(self.source_path)
        for key in ("expected_plan_sha256", "expected_preview_sha256", "expected_source_database_sha256"):
            with self.subTest(key=key):
                write_payload = self._write_payload(payload, data, source_sha)
                write_payload[key] = "f" * 64
                status, body = self.post_json(f"/api/items/{item_id}/writer/write", write_payload)
                self.assertEqual(status, 409, body)
                self.assertEqual(body["error"]["code"], "writer_approval_mismatch")
        for confirmed in (False, 1, "yes"):
            with self.subTest(confirmed=confirmed):
                write_payload = self._write_payload(payload, data, source_sha, confirmed=confirmed)
                status, body = self.post_json(f"/api/items/{item_id}/writer/write", write_payload)
                self.assertEqual(status, 400, body)
                self.assertEqual(body["error"]["code"], "writer_invalid_request")
        self.assertEqual(self.directory_names(), {"inbox.db", "umalib.db", "out"})
        self.assertEqual(list(self.candidate_dir.iterdir()), [])

    def test_stale_choice_rejected(self) -> None:
        item_id = self.import_and_approve(tags=("角色甲", "合成新标签"))
        payload = self._ready_write_payload(item_id)
        data = self._preview(item_id, payload)
        stale = self._write_payload(payload, data, sha256_file(self.source_path))
        stale["choices"]["upload_time_seconds"] = 1788062401
        status, body = self.post_json(f"/api/items/{item_id}/writer/write", stale)
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "writer_approval_mismatch")

    def test_write_success_exported_and_history(self) -> None:
        item_id = self.import_and_approve(tags=("角色甲", "合成新标签"))
        payload = self._ready_write_payload(item_id)
        source_before = (sha256_file(self.source_path), self.source_path.stat().st_mtime_ns)
        data = self._preview(item_id, payload)
        write_payload = self._write_payload(payload, data, sha256_file(self.source_path))
        status, body = self.post_json(f"/api/items/{item_id}/writer/write", write_payload)
        self.assertEqual(status, 200, body)
        result = body["data"]["result"]
        self.assertTrue(result["success"])
        self.assertEqual(body["data"]["exported"], True)
        self.assertEqual(result["counts_before"], {"article": 0, "tag": 1, "tagged": 0})
        self.assertEqual(result["counts_after"], {"article": 1, "tag": 2, "tagged": 2})
        self.assertEqual(result["source"]["unchanged"], True)
        item = body["data"]["item"]
        self.assertEqual(item["status"], "exported")
        history = item["history"]
        self.assertEqual(history[-1]["to_status"], "exported")
        self.assertIn("candidate-", history[-1]["reason"])
        self.assertIn(result["result_sha256"], history[-1]["reason"])
        candidate = self.candidate_dir / f"candidate-{item_id}.db"
        self.assertTrue(candidate.exists())
        self.assertEqual(sha256_file(self.source_path), source_before[0])
        self.assertEqual(self.source_path.stat().st_mtime_ns, source_before[1])
        self.assertEqual(
            sorted(p.name for p in self.candidate_dir.iterdir()),
            [f"candidate-{item_id}.db"],
        )
        status, body = self.post_json(
            f"/api/items/{item_id}/writer/write", write_payload
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "writer_item_not_approved")

    def test_writer_failure_keeps_approved(self) -> None:
        item_id = self.import_and_approve(tags=("角色甲", "合成新标签"))
        payload = self._ready_write_payload(item_id)
        data = self._preview(item_id, payload)
        write_payload = self._write_payload(payload, data, sha256_file(self.source_path))
        with mock.patch(
            "umalib_ingest.web.writer_ui.write_candidate_database",
            side_effect=Exception("boom"),
        ):
            status, body = self.post_json(f"/api/items/{item_id}/writer/write", write_payload)
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "internal_error")
        status, body = self.get_json(f"/api/items/{item_id}")
        self.assertEqual(body["data"]["status"], "approved")
        self.assertEqual([*self.candidate_dir.iterdir()], [])

    def test_post_write_transition_failure_preserves_candidate(self) -> None:
        item_id = self.import_and_approve(tags=("角色甲", "合成新标签"))
        payload = self._ready_write_payload(item_id)
        data = self._preview(item_id, payload)
        write_payload = self._write_payload(payload, data, sha256_file(self.source_path))
        source_before = (sha256_file(self.source_path), self.source_path.stat().st_mtime_ns)
        from umalib_ingest.inbox.errors import InvalidStatusTransitionError

        def failing_transition(self, item_id_arg, to_status, reason):
            raise InvalidStatusTransitionError("simulated")

        with mock.patch(
            "umalib_ingest.inbox.repository.InboxRepository.transition",
            failing_transition,
        ):
            status, body = self.post_json(f"/api/items/{item_id}/writer/write", write_payload)
        self.assertEqual(status, 500, body)
        self.assertEqual(body["error"]["code"], "writer_post_write_state_failed")
        candidate = self.candidate_dir / f"candidate-{item_id}.db"
        self.assertTrue(candidate.exists())
        status, body = self.get_json(f"/api/items/{item_id}")
        self.assertEqual(body["data"]["status"], "approved")
        self.assertEqual(sha256_file(self.source_path), source_before[0])
        self.assertEqual(self.source_path.stat().st_mtime_ns, source_before[1])

    def test_unexpected_post_write_transition_failure_uses_recovery_error(self) -> None:
        item_id = self.import_and_approve(tags=("角色甲", "合成新标签"))
        payload = self._ready_write_payload(item_id)
        data = self._preview(item_id, payload)
        write_payload = self._write_payload(payload, data, sha256_file(self.source_path))

        with mock.patch(
            "umalib_ingest.inbox.repository.InboxRepository.transition",
            side_effect=RuntimeError("simulated database failure"),
        ):
            status, body = self.post_json(
                f"/api/items/{item_id}/writer/write", write_payload
            )

        self.assertEqual(status, 500, body)
        self.assertEqual(body["error"]["code"], "writer_post_write_state_failed")
        self.assertNotIn("simulated database failure", json.dumps(body, ensure_ascii=False))
        self.assertTrue((self.candidate_dir / f"candidate-{item_id}.db").exists())
        status, body = self.get_json(f"/api/items/{item_id}")
        self.assertEqual(body["data"]["status"], "approved")

    def test_concurrent_write_only_one_succeeds(self) -> None:
        item_id = self.import_and_approve(tags=("角色甲", "合成新标签"))
        payload = self._ready_write_payload(item_id)
        data = self._preview(item_id, payload)
        write_payload = self._write_payload(payload, data, sha256_file(self.source_path))
        results = []

        def do_write():
            results.append(
                self.post_json(f"/api/items/{item_id}/writer/write", write_payload)[0]
            )

        threads = [threading.Thread(target=do_write) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(sorted(results), [200, 409])
        status, body = self.get_json(f"/api/items/{item_id}")
        self.assertEqual(body["data"]["status"], "exported")
        self.assertEqual(
            sorted(p.name for p in self.candidate_dir.iterdir()),
            [f"candidate-{item_id}.db"],
        )

    def test_transition_waits_for_write_lock_and_cannot_change_exported_item(self) -> None:
        item_id = self.import_and_approve(tags=("角色甲", "合成新标签"))
        payload = self._ready_write_payload(item_id)
        data = self._preview(item_id, payload)
        write_payload = self._write_payload(payload, data, sha256_file(self.source_path))
        import umalib_ingest.web.writer_ui as writer_ui_module

        real_write = writer_ui_module.write_candidate_database
        writer_entered = threading.Event()
        release_writer = threading.Event()
        transition_done = threading.Event()
        write_response = []
        transition_response = []

        def slow_write(*args, **kwargs):
            writer_entered.set()
            if not release_writer.wait(timeout=10):
                raise RuntimeError("test did not release Writer")
            return real_write(*args, **kwargs)

        def do_write():
            write_response.append(
                self.post_json(f"/api/items/{item_id}/writer/write", write_payload)
            )

        def do_transition():
            transition_response.append(
                self.post_json(
                    f"/api/items/{item_id}/transition",
                    {"to_status": "candidate", "reason": "并发退回"},
                )
            )
            transition_done.set()

        with mock.patch(
            "umalib_ingest.web.writer_ui.write_candidate_database",
            side_effect=slow_write,
        ):
            write_thread = threading.Thread(target=do_write)
            write_thread.start()
            self.assertTrue(writer_entered.wait(timeout=5))
            transition_thread = threading.Thread(target=do_transition)
            transition_thread.start()
            self.assertFalse(transition_done.wait(timeout=0.2))
            release_writer.set()
            write_thread.join(timeout=30)
            transition_thread.join(timeout=30)

        self.assertEqual(write_response[0][0], 200, write_response)
        self.assertEqual(transition_response[0][0], 409, transition_response)
        self.assertEqual(
            transition_response[0][1]["error"]["code"], "invalid_transition"
        )
        status, body = self.get_json(f"/api/items/{item_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "exported")


class WriterFrontendStructureTests(WriterWebTestCase):
    def test_writer_dom_and_texts(self) -> None:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for marker in (
            "w-candidate-file", "w-upload-time", "w-note", "w-catalog-btn",
            "w-preview-btn", "w-write-btn", "w-close-confirmed", "w-source-available",
            "w-source-add-btn", "w-source-selected", "w-tag-decisions", "w-result",
            "w-hashes", "w-operations", "w-plan-diagnostics", "w-preview-diagnostics",
            "status-bar", "status-writer", "status-dirty",
            "我已关闭大书库，确认只生成新的候选副本",
            "title-bar", "command-bar",
        ):
            self.assertIn(marker, html)
        self.assertEqual(html.count('value="exported"'), 1)

    def test_writer_js_state_machine(self) -> None:
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        for marker in (
            "localDateTimeToUnixSeconds",
            "getTime()",
            "invalidateWriter",
            "writerState",
            "/api/writer/config",
            "/api/writer/catalog",
            "/writer/preview",
            "/writer/write",
            "expected_plan_sha256",
            "expected_preview_sha256",
            "expected_source_database_sha256",
            "app_closed_confirmed",
            "w-close-confirmed",
            "updateWriteButton",
            "正式库未被替换",
        ):
            self.assertIn(marker, js)
        for forbidden in (
            "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
            "eval(", "alert(", "confirm(", "onclick=", "onerror=", "onload=",
            "localStorage", "sessionStorage",
        ):
            self.assertNotIn(forbidden, js)
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for forbidden in ("onclick=", "onerror=", "onload=", "<script>"):
            self.assertNotIn(forbidden, html)

        source_rows = js[js.index("function renderSourceRows"):js.index("function addSelectedSource")]
        self.assertIn("clear(list);", source_rows)
        self.assertIn("const itemId = state.selectedId;", js)
        self.assertIn("state.selectedId !== itemId", js)
        self.assertIn('$("w-close-confirmed").disabled = !previewReady', js)
        self.assertIn("#w-source-selected button, #w-tag-decisions select", js)

    def test_win7_style_structure(self) -> None:
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        for marker in (
            "Segoe UI",
            "#title-bar",
            "linear-gradient",
            "#command-bar",
            "panel",
            "button",
            "thead",
            "selected",
            ":focus",
            ":disabled",
            "severity-blocker",
            "severity-warning",
            "severity-success",
            "#status-bar",
            "@media (max-width: 1280px)",
            "@media (max-width: 1024px)",
        ):
            self.assertIn(marker, css)
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        css_text = css
        for forbidden in ("https://fonts", "http://fonts", "@import", "url(http"):
            self.assertNotIn(forbidden, css_text)
        self.assertNotIn('<link rel="stylesheet" href="http', html)
        self.assertNotIn('<script src="http', html)

    def test_old_protections_preserved(self) -> None:
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for marker in ("search-input", "dirty-message", "discard-btn", "pkg-json",
                       "readonly-notice", "放弃未保存修改"):
            self.assertIn(marker, html)
        for marker in (
            "有未保存修改", "guardDirty", "reviewSnapshot",
            "detailRequestVersion", "detailRenderVersion", "requestVersion",
            "JSON.stringify", "toLowerCase", "indexOf",
        ):
            self.assertIn(marker, js)

    def test_datetime_conversion_pure_function(self) -> None:
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("/ 1000", js)
        self.assertIn("Math.floor", js)


if __name__ == "__main__":
    unittest.main()
