"""Tests for the local review web UI (TASK-004)."""

import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from umalib_ingest.inbox import InboxRepository
from umalib_ingest.web import create_server

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "src" / "umalib_ingest" / "web" / "static"


class FixedClock:
    def __init__(self, *timestamps: str) -> None:
        self._timestamps = list(timestamps)
        self._index = 0

    def __call__(self) -> str:
        value = self._timestamps[min(self._index, len(self._timestamps) - 1)]
        self._index += 1
        return value


def full_submission():
    return (
        "[收录]\n"
        "[标题] 星光下的测试稿\n"
        "[作者] 测试作者\n"
        "[译者] 测试译者\n"
        "[系列] 测试系列\n"
        "[标签] 角色, 短篇\n"
        "[原作系列] os-url\n"
        "[原作] https://example.com/original?tid=1&to=1\n"
        "[译作系列] ts-url\n"
        "[译作] https://bbs.nga.cn/read.php?tid=1&pid=2&to=1\n"
        "\n"
        "正文第一行。\n"
        "正文中的[术语]保持。\n"
    )


class WebTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "inbox.db"
        self.repo = InboxRepository(self.db_path, clock=FixedClock(T0, T1))
        self.repo.initialize()
        self.server = create_server(self.repo, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method: str, path: str, body=None, headers=None):
        headers = dict(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
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

    def post_json(self, path: str, payload=None, headers=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        extra = {"Content-Type": "application/json"}
        extra.update(headers or {})
        status, _, data = self.request("POST", path, body=body, headers=extra)
        return status, json.loads(data.decode("utf-8")) if data else {}

    def put_json(self, path: str, payload=None, headers=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        extra = {"Content-Type": "application/json"}
        extra.update(headers or {})
        status, _, data = self.request("PUT", path, body=body, headers=extra)
        return status, json.loads(data.decode("utf-8")) if data else {}

    def raw_request(self, data: bytes) -> tuple[int, bytes]:
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(data)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        response = b"".join(chunks)
        status_line = response.split(b"\r\n", 1)[0]
        status = int(status_line.split(b" ")[1])
        return status, response

    def import_full(self):
        status, body = self.post_json("/api/import", {"raw_text": full_submission()})
        self.assertEqual(status, 200, body)
        return body["data"]


class TestStaticFiles(WebTestCase):
    def test_static_pages_served(self) -> None:
        for path in ("/", "/index.html", "/app.js", "/styles.css"):
            with self.subTest(path=path):
                status, _, payload = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(payload)

    def test_unknown_and_traversal_paths_404(self) -> None:
        for path in (
            "/unknown.js",
            "/secret.txt",
            "/../app.js",
            "/..%2fapp.js",
            "/static/../app.js",
            "/%2e%2e/%2e%2e/etc/passwd",
            "/index.html/..",
        ):
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path)
                self.assertEqual(status, 404)


class TestSecurityHeaders(WebTestCase):
    def test_security_headers_and_no_cors(self) -> None:
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertIn("Content-Security-Policy", headers)
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        _, api_headers, _ = self.request("GET", "/api/items")
        self.assertEqual(api_headers.get("X-Content-Type-Options"), "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", api_headers)

    def test_html_has_no_inline_script_or_event_handlers(self) -> None:
        _, _, payload = self.request("GET", "/")
        html = payload.decode("utf-8")
        self.assertIn('script src="/app.js"', html)
        for forbidden in ("<script>", "onclick=", "onerror=", "onload=", "javascript:"):
            self.assertNotIn(forbidden, html)

    def test_invalid_host_rejected(self) -> None:
        for host in (
            "evil.example.com",
            "10.0.0.5",
            "127.0.0.1.evil.com",
            "[]",
            f"localhost:{self.port + 1}",
            f"127.0.0.1:{self.port + 1}",
            f"evil.example@localhost:{self.port}",
        ):
            with self.subTest(host=host):
                status, _ = self.raw_request(
                    f"GET / HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
                )
                self.assertEqual(status, 403)


class TestOriginChecks(WebTestCase):
    def post_with_origin(self, origin: str | None):
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        status, _, data = self.request(
            "POST", "/api/import", body=json.dumps({"raw_text": "普通讨论\n"}),
            headers=headers,
        )
        return status, json.loads(data.decode("utf-8"))

    def test_foreign_null_and_other_port_origins_rejected(self) -> None:
        for origin in (
            "http://evil.example.com",
            "null",
            f"http://127.0.0.1:{self.port + 1}",
            "https://127.0.0.1",
            "http://localhost:1",
            f"http://localhost:{self.port}",
        ):
            with self.subTest(origin=origin):
                status, body = self.post_with_origin(origin)
                self.assertEqual(status, 403)
                self.assertEqual(body["error"]["code"], "invalid_origin")

    def test_same_origin_and_no_origin_allowed(self) -> None:
        status, body = self.post_with_origin(f"http://127.0.0.1:{self.port}")
        self.assertEqual(status, 200, body)
        status, _ = self.post_with_origin(None)
        self.assertEqual(status, 200)
        body = b'{"raw_text":"x"}'
        status, response = self.raw_request(
            b"POST /api/import HTTP/1.0\r\n"
            b"Host: localhost:" + str(self.port).encode() + b"\r\n"
            b"Origin: http://localhost:" + str(self.port).encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        self.assertEqual(status, 200, response)


class TestRequestBodyLimits(WebTestCase):
    def test_missing_content_length(self) -> None:
        status, _ = self.raw_request(
            b"POST /api/import HTTP/1.0\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Connection: close\r\n\r\n"
        )
        self.assertEqual(status, 400)

    def test_invalid_content_length(self) -> None:
        for raw_length in (b"abc", b"-5", b"+1", b"1.5", b"1e3"):
            with self.subTest(raw_length=raw_length):
                status, _ = self.raw_request(
                    b"POST /api/import HTTP/1.0\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + raw_length + b"\r\n"
                    b"Connection: close\r\n\r\n"
                )
                self.assertEqual(status, 400)

    def test_oversize_payload_rejected(self) -> None:
        status, _ = self.raw_request(
            b"POST /api/import HTTP/1.0\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 3000000\r\n"
            b"Connection: close\r\n\r\n"
        )
        self.assertEqual(status, 413)

    def test_wrong_content_type_rejected(self) -> None:
        status, _, data = self.request(
            "POST",
            "/api/import",
            body=json.dumps({"raw_text": "x"}),
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(data)["error"]["code"], "unsupported_media_type")
        status, _, data = self.request("POST", "/api/import", body="x")
        self.assertEqual(status, 415)

    def test_invalid_json_rejected(self) -> None:
        status, _, data = self.request(
            "POST", "/api/import", body="{bad json", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"]["code"], "invalid_request")

    def test_non_object_json_rejected(self) -> None:
        for body in ('["a", 1]', '"字符串"', "42", "null"):
            with self.subTest(body=body):
                status, _, data = self.request(
                    "POST", "/api/import", body=body.encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(data)["error"]["code"], "invalid_request")


class TestItemsListApi(WebTestCase):
    def test_empty_list(self) -> None:
        status, body = self.get_json("/api/items")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["items"], [])

    def test_list_summary_and_filter(self) -> None:
        self.import_full()
        self.post_json("/api/import", {"raw_text": "[收录]\n\n只有正文。\n"})
        self.post_json("/api/import", {"raw_text": "普通讨论。\n"})
        status, body = self.get_json("/api/items")
        self.assertEqual(status, 200)
        items = body["data"]["items"]
        self.assertEqual([item["id"] for item in items], [1, 2, 3])
        self.assertEqual(items[0]["status"], "candidate")
        self.assertEqual(items[1]["status"], "incomplete")
        self.assertEqual(items[2]["status"], "ignored")
        first = items[0]
        for key in ("id", "status", "title", "author", "missing_fields", "updated_at"):
            self.assertIn(key, first)
        self.assertNotIn("raw_text", first)
        self.assertNotIn("body_text", first)
        for status_value in ("candidate", "incomplete", "suspected_duplicate", "approved", "exported", "ignored"):
            with self.subTest(status=status_value):
                _, filtered = self.get_json(f"/api/items?status={status_value}")
                self.assertEqual(
                    [i["id"] for i in filtered["data"]["items"]],
                    [1] if status_value == "candidate" else
                    ([2] if status_value == "incomplete" else
                     ([3] if status_value == "ignored" else [])),
                )

    def test_invalid_filter_status(self) -> None:
        status, body = self.get_json("/api/items?status=not_a_status")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_status")
        status, body = self.get_json("/api/items?status=")
        self.assertEqual(status, 200)


class TestImportApi(WebTestCase):
    def test_import_full_submission(self) -> None:
        detail = self.import_full()
        self.assertEqual(detail["status"], "candidate")
        self.assertEqual(detail["review_fields"]["title"], "星光下的测试稿")
        self.assertEqual(detail["review_fields"]["tags"], ["角色", "短篇"])
        self.assertEqual(detail["missing_fields"], [])
        self.assertIn("正文第一行。", detail["raw_text"])

    def test_import_incomplete_and_ignored(self) -> None:
        status, body = self.post_json("/api/import", {"raw_text": "[收录]\n\n只有正文。\n"})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "incomplete")
        self.assertEqual(body["data"]["missing_fields"], ["title", "author", "tags"])
        status, body = self.post_json("/api/import", {"raw_text": "普通讨论。\n"})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "ignored")

    def test_import_idempotent_for_same_snapshot(self) -> None:
        first = self.import_full()
        second = self.import_full()
        self.assertEqual(second["id"], first["id"])
        status, body = self.get_json("/api/items")
        self.assertEqual(len(body["data"]["items"]), 1)

    def test_import_validation(self) -> None:
        status, body = self.post_json("/api/import", {"raw_text": 123})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")
        status, body = self.post_json("/api/import", {"raw_text": "x", "canonical_url": 5})
        self.assertEqual(status, 400)
        status, body = self.post_json("/api/import", {"raw_text": "x", "unknown_key": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

    def test_import_with_canonical_url(self) -> None:
        status, body = self.post_json(
            "/api/import",
            {"raw_text": "[收录]\n[标题] t\n\n正文\n", "canonical_url": "  https://bbs.nga.cn/read.php?tid=9&to=1  "},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            body["data"]["review_fields"]["title"], "t"
        )
        item = self.repo.get(body["data"]["id"])
        self.assertEqual(
            item.original_draft["origin"]["canonical_url"],
            "https://bbs.nga.cn/read.php?tid=9&to=1",
        )


class TestDetailApi(WebTestCase):
    def test_detail_contains_everything(self) -> None:
        self.post_json(
            "/api/import",
            {"raw_text": "[收录]\n[标题] 第一标题\n[标题] 第二标题\n[作者] 作者\n"
                         "[标签] 标签\n[未来字段] 未来值\n\n正文。\n"},
        )
        status, body = self.get_json("/api/items/1")
        self.assertEqual(status, 200)
        detail = body["data"]
        for key in ("id", "status", "created_at", "updated_at", "review_fields",
                    "raw_text", "missing_fields", "warnings", "unknown_fields",
                    "raw_fields", "history", "duplicates"):
            self.assertIn(key, detail)
        self.assertEqual(detail["review_fields"]["title"], "第一标题")
        self.assertIn("[未来字段] 未来值", detail["raw_text"])
        self.assertEqual(len(detail["warnings"]), 1)
        self.assertEqual(detail["warnings"][0]["code"], "duplicate_field")
        self.assertEqual(len(detail["unknown_fields"]), 1)
        self.assertEqual(detail["unknown_fields"][0]["name"], "未来字段")
        self.assertIn("未来字段", detail["raw_fields"])
        self.assertEqual(len(detail["history"]), 1)
        self.assertEqual(detail["history"][0]["reason"], "import")
        self.assertEqual(detail["duplicates"], [])

    def test_detail_missing_item_404(self) -> None:
        status, body = self.get_json("/api/items/999")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "item_not_found")


class TestReviewApi(WebTestCase):
    def review_payload(self):
        return {
            "title": " 修正标题 ",
            "author": " 修正作者 ",
            "translator": " 修正译者 ",
            "series": " 修正系列 ",
            "tags": [" 角色 ", "", "角色", "短篇"],
            "sources": {
                "nga": [" https://bbs.nga.cn/read.php?tid=1&to=1 ", "", " 同一条 ", " 同一条 "],
                "original_series": [" os-a "],
                "original": [" https://example.com/original?tid=1&to=1 "],
                "translation_series": [" ts-a ", " ts-a "],
                "translation": [" http://x.example/b/../c "],
            },
            "body_text": "  第一行  \n第二行[术语]保持\n\t结尾空白  ",
        }

    def test_review_save_normalizes_and_keeps_body_verbatim(self) -> None:
        item = self.import_full()
        status, body = self.put_json(f"/api/items/{item['id']}/review", self.review_payload())
        self.assertEqual(status, 200, body)
        detail = body["data"]
        fields = detail["review_fields"]
        self.assertEqual(fields["title"], "修正标题")
        self.assertEqual(fields["author"], "修正作者")
        self.assertEqual(fields["translator"], "修正译者")
        self.assertEqual(fields["series"], "修正系列")
        self.assertEqual(fields["tags"], ["角色", "短篇"])
        self.assertEqual(
            fields["sources"]["nga"],
            ["https://bbs.nga.cn/read.php?tid=1&to=1", "同一条", "同一条"],
        )
        self.assertEqual(fields["sources"]["original"], ["https://example.com/original?tid=1&to=1"])
        self.assertEqual(fields["sources"]["translation_series"], ["ts-a", "ts-a"])
        self.assertEqual(fields["sources"]["translation"], ["http://x.example/b/../c"])
        self.assertEqual(fields["body_text"], "  第一行  \n第二行[术语]保持\n\t结尾空白  ")
        self.assertEqual(detail["missing_fields"], [])

    def test_review_unknown_keys_rejected(self) -> None:
        item = self.import_full()
        payload = self.review_payload()
        payload["extra_key"] = 1
        status, body = self.put_json(f"/api/items/{item['id']}/review", payload)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

    def test_review_missing_keys_rejected_without_changing_item(self) -> None:
        item = self.import_full()
        original_fields = item["review_fields"]
        missing_title = self.review_payload()
        del missing_title["title"]
        missing_source = self.review_payload()
        del missing_source["sources"]["nga"]
        for index, payload in enumerate((missing_title, missing_source)):
            with self.subTest(variant=index):
                status, body = self.put_json(
                    f"/api/items/{item['id']}/review", payload
                )
                self.assertEqual(status, 400, body)
                self.assertEqual(body["error"]["code"], "invalid_request")
        status, detail = self.get_json(f"/api/items/{item['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["data"]["review_fields"], original_fields)
        payload = self.review_payload()
        payload["sources"]["extra_source"] = []
        status, body = self.put_json(f"/api/items/{item['id']}/review", payload)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

    def test_review_type_errors(self) -> None:
        item = self.import_full()
        base = self.review_payload()
        variants = [
            dict(base, tags="不是列表"),
            dict(base, tags=["好", 1]),
            dict(base, body_text=None),
            dict(base, sources=dict(base["sources"], nga="不是列表")),
            dict(base, sources="不是对象"),
            dict(base, title=123),
        ]
        for index, payload in enumerate(variants):
            with self.subTest(variant=index):
                status, body = self.put_json(f"/api/items/{item['id']}/review", payload)
                self.assertEqual(status, 400, body)
                self.assertIn(body["error"]["code"], ("invalid_request", "invalid_review_fields"))

    def test_review_locked_statuses(self) -> None:
        detail = self.import_full()
        self.post_json(f"/api/items/{detail['id']}/transition", {"to_status": "approved", "reason": "确认"})
        status, body = self.put_json(f"/api/items/{detail['id']}/review", self.review_payload())
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "item_locked")
        detail2 = self.post_json("/api/import", {"raw_text": "[收录]\n[标题] t\n[作者] a\n[标签] x\n\n正文\n"})[1]["data"]
        self.post_json(f"/api/items/{detail2['id']}/transition", {"to_status": "approved", "reason": "确认"})
        self.post_json(f"/api/items/{detail2['id']}/transition", {"to_status": "exported"})
        status, body = self.put_json(f"/api/items/{detail2['id']}/review", self.review_payload())
        self.assertEqual(status, 409)
        ignored = self.post_json("/api/import", {"raw_text": "普通讨论。\n"})[1]["data"]
        status, body = self.put_json(f"/api/items/{ignored['id']}/review", self.review_payload())
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "item_locked")


class TestTransitionApi(WebTestCase):
    def test_transition_approve_requires_complete_fields(self) -> None:
        self.post_json("/api/import", {"raw_text": "[收录]\n\n只有正文。\n"})
        status, body = self.post_json("/api/items/1/transition", {"to_status": "approved", "reason": "确认"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "review_incomplete")

    def test_transition_writes_reason_and_history(self) -> None:
        detail = self.import_full()
        status, body = self.post_json(
            f"/api/items/{detail['id']}/transition",
            {"to_status": "approved", "reason": "人工确认无误"},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["status"], "approved")
        history = body["data"]["history"]
        self.assertEqual(history[-1]["reason"], "人工确认无误")
        self.assertEqual(history[-1]["to_status"], "approved")

    def test_transition_forbids_exported(self) -> None:
        detail = self.import_full()
        self.post_json(f"/api/items/{detail['id']}/transition", {"to_status": "approved", "reason": "确认"})
        status, body = self.post_json(f"/api/items/{detail['id']}/transition", {"to_status": "exported", "reason": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

    def test_transition_invalid_transition(self) -> None:
        detail = self.import_full()
        status, body = self.post_json(f"/api/items/{detail['id']}/transition", {"to_status": "candidate", "reason": "同状态"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "invalid_transition")
        status, body = self.post_json(f"/api/items/{detail['id']}/transition", {"to_status": "not_a_status", "reason": "x"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "invalid_transition")
        status, body = self.post_json(f"/api/items/{detail['id']}/transition", {"to_status": "approved", "reason": "   "})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "invalid_transition")
        status, body = self.post_json(f"/api/items/{detail['id']}/transition", {"to_status": 5, "reason": "x"})
        self.assertEqual(status, 400)

    def test_transition_missing_item(self) -> None:
        status, body = self.post_json("/api/items/999/transition", {"to_status": "approved", "reason": "x"})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "item_not_found")
        status, body = self.post_json("/api/items/abc/transition", {"to_status": "approved", "reason": "x"})
        self.assertEqual(status, 400)

    def test_page_has_no_exported_option_for_transition(self) -> None:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('value="exported"'), 1)


class TestXssSafety(WebTestCase):
    def test_hostile_text_roundtrips_as_plain_strings(self) -> None:
        hostile_title = '<script>alert(1)</script><img src=x onerror=alert(2)>&"引号"'
        hostile = hostile_title + "\n第二行"
        status, body = self.post_json(
            "/api/import",
            {"raw_text": "[收录]\n[标题] " + hostile + "\n[作者] a\n\n正文\n"},
        )
        self.assertEqual(status, 200, body)
        detail_id = body["data"]["id"]
        status, body = self.get_json(f"/api/items/{detail_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["review_fields"]["title"], hostile_title)
        self.assertIn(hostile, body["data"]["raw_text"])
        status, body = self.put_json(
            f"/api/items/{detail_id}/review",
            {
                "title": hostile_title,
                "author": None,
                "translator": None,
                "series": None,
                "tags": [hostile_title],
                "sources": {"nga": [], "original_series": [], "original": [hostile_title],
                            "translation_series": [], "translation": []},
                "body_text": hostile + "\n正文",
            },
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["review_fields"]["title"], hostile_title)
        self.assertEqual(body["data"]["review_fields"]["body_text"], hostile + "\n正文")

    def test_static_js_has_no_injection_apis(self) -> None:
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        for forbidden in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "onclick=",
            "onerror=",
        ):
            self.assertNotIn(forbidden, js)
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for forbidden in ("onclick=", "onerror=", "onload="):
            self.assertNotIn(forbidden, html)


class TestCli(WebTestCase):
    def run_cli(self, *args: str):
        env = dict(
            os.environ,
            PYTHONPATH=str(PROJECT_ROOT / "src"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        return subprocess.run(
            [sys.executable, "-B", "-m", "umalib_ingest.web", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def test_cli_missing_db(self) -> None:
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)

    def test_cli_nonexistent_path_without_init(self) -> None:
        missing = Path(self._tmp.name) / "missing.db"
        result = self.run_cli("--db", str(missing))
        self.assertNotEqual(result.returncode, 0)

    def test_cli_bad_schema(self) -> None:
        bad = Path(self._tmp.name) / "foreign.db"
        raw = sqlite3.connect(str(bad))
        try:
            raw.execute("CREATE TABLE article (id INTEGER PRIMARY KEY)")
            raw.commit()
        finally:
            raw.close()
        result = self.run_cli("--db", str(bad))
        self.assertNotEqual(result.returncode, 0)

    def test_cli_invalid_ports(self) -> None:
        existing = Path(self._tmp.name) / "existing.db"
        InboxRepository(existing, clock=FixedClock(T0)).initialize()
        for port in ("70000", "-1"):
            with self.subTest(port=port):
                result = self.run_cli("--db", str(existing), "--port", port)
                self.assertNotEqual(result.returncode, 0)
        result = self.run_cli("--db", str(existing), "--port", "abc")
        self.assertNotEqual(result.returncode, 0)

    def test_cli_init_creates_and_serves(self) -> None:
        db = str(Path(self._tmp.name) / "cli.db")
        env = dict(
            os.environ,
            PYTHONPATH=str(PROJECT_ROOT / "src"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "umalib_ingest.web", "--db", db, "--init", "--port", "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            line = proc.stdout.readline()
            self.assertIn("http://127.0.0.1:", line)
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


class TestHandoffApi(WebTestCase):
    def approve_item(self):
        detail = self.import_full()
        status, body = self.post_json(
            f"/api/items/{detail['id']}/transition",
            {"to_status": "approved", "reason": "确认"},
        )
        self.assertEqual(status, 200, body)
        return body["data"]

    def test_import_url_enters_origin_and_nga_sources(self) -> None:
        url = " https://bbs.nga.cn/read.php?tid=9&pid=8&to=1 "
        status, body = self.post_json(
            "/api/import", {"raw_text": full_submission(), "canonical_url": url}
        )
        self.assertEqual(status, 200, body)
        detail = body["data"]
        self.assertEqual(
            detail["review_fields"]["sources"]["nga"],
            ["https://bbs.nga.cn/read.php?tid=9&pid=8&to=1"],
        )
        item = self.repo.get(detail["id"])
        self.assertEqual(
            item.original_draft["origin"]["canonical_url"],
            "https://bbs.nga.cn/read.php?tid=9&pid=8&to=1",
        )
        self.assertEqual(
            item.original_draft["metadata"]["sources"]["nga"],
            ["https://bbs.nga.cn/read.php?tid=9&pid=8&to=1"],
        )

    def test_url_not_normalized_and_idempotent(self) -> None:
        url = "https://bbs.nga.cn/read.php?tid=9&to=1"
        first = self.post_json("/api/import", {"raw_text": full_submission(), "canonical_url": url})
        self.assertEqual(first[0], 200)
        second = self.post_json("/api/import", {"raw_text": full_submission(), "canonical_url": url})
        self.assertEqual(second[1]["data"]["id"], first[1]["data"]["id"])
        self.assertEqual(first[1]["data"]["review_fields"]["sources"]["nga"], [url])
        other = self.post_json(
            "/api/import",
            {"raw_text": full_submission(), "canonical_url": "https://bbs.nga.cn/read.php?tid=10&to=1"},
        )
        self.assertEqual(other[0], 200)
        self.assertNotEqual(other[1]["data"]["id"], first[1]["data"]["id"])
        self.assertEqual(
            self.repo.get(first[1]["data"]["id"]).original_draft["metadata"]["sources"]["nga"],
            [url],
        )

    def test_import_without_url_unchanged(self) -> None:
        detail = self.import_full()
        self.assertEqual(detail["review_fields"]["sources"]["nga"], [])

    def test_approved_package_api_success_not_ready_missing(self) -> None:
        detail = self.approve_item()
        status, body = self.get_json(f"/api/items/{detail['id']}/approved-package")
        self.assertEqual(status, 200, body)
        package = body["data"]
        self.assertEqual(
            set(package.keys()),
            {"schema_version", "inbox", "submission", "package_sha256"},
        )
        self.assertEqual(package["inbox"]["status"], "approved")
        self.assertEqual(package["inbox"]["item_id"], detail["id"])
        self.assertEqual(package["submission"]["metadata"]["title"], "星光下的测试稿")
        self.assertRegex(package["package_sha256"], r"^[0-9a-f]{64}$")
        incomplete = self.post_json("/api/import", {"raw_text": "[收录]\n\n只有正文。\n"})[1]["data"]
        status, body = self.get_json(f"/api/items/{incomplete['id']}/approved-package")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "handoff_not_ready")
        status, body = self.get_json("/api/items/999/approved-package")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "item_not_found")

    def test_detail_editable_and_package_available_flags(self) -> None:
        candidate = self.import_full()
        status, body = self.get_json(f"/api/items/{candidate['id']}")
        self.assertTrue(body["data"]["editable"])
        self.assertFalse(body["data"]["approved_package_available"])
        status, body = self.post_json(
            f"/api/items/{candidate['id']}/transition",
            {"to_status": "approved", "reason": "确认"},
        )
        self.assertEqual(status, 200, body)
        status, body = self.get_json(f"/api/items/{candidate['id']}")
        self.assertFalse(body["data"]["editable"])
        self.assertTrue(body["data"]["approved_package_available"])
        self.post_json("/api/import", {"raw_text": "普通讨论。\n"})
        status, body = self.get_json("/api/items/2")
        self.assertFalse(body["data"]["editable"])
        self.assertFalse(body["data"]["approved_package_available"])

    def test_package_api_does_not_change_inbox(self) -> None:
        detail = self.approve_item()
        before = self.repo.get(detail["id"])
        history_before = [
            (h.id, h.from_status, h.to_status, h.reason, h.changed_at)
            for h in self.repo.list_history(detail["id"])
        ]
        status, body = self.get_json(f"/api/items/{detail['id']}/approved-package")
        self.assertEqual(status, 200)
        after = self.repo.get(detail["id"])
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.updated_at, before.updated_at)
        self.assertEqual(after.working_draft, before.working_draft)
        history_after = [
            (h.id, h.from_status, h.to_status, h.reason, h.changed_at)
            for h in self.repo.list_history(detail["id"])
        ]
        self.assertEqual(history_after, history_before)
        self.assertEqual(
            set(pkg_key for pkg_key in body["data"]["inbox"]),
            {"item_id", "status", "snapshot_sha256", "content_sha256",
             "created_at", "approved_at"},
        )

    def test_csp_keeps_connect_self(self) -> None:
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])

    def test_approved_package_hostile_content_plain_text(self) -> None:
        hostile = '<script>alert(1)</script>"引号"'
        status, body = self.post_json(
            "/api/import",
            {"raw_text": "[收录]\n[标题] " + hostile + "\n[作者] a\n[标签] x\n\n正文" + hostile + "\n"},
        )
        self.assertEqual(status, 200, body)
        item_id = body["data"]["id"]
        status, body = self.post_json(
            f"/api/items/{item_id}/transition", {"to_status": "approved", "reason": "确认"}
        )
        self.assertEqual(status, 200, body)
        status, body = self.get_json(f"/api/items/{item_id}/approved-package")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["submission"]["metadata"]["title"], hostile)
        self.assertIn(hostile, body["data"]["submission"]["content"]["raw_text"])


class TestFrontendStructure(WebTestCase):
    def test_static_structure_for_locked_dirty_search_package(self) -> None:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        for marker in (
            "search-input",
            "dirty-message",
            "discard-btn",
            "readonly-notice",
            "pkg-status",
            "pkg-sha",
            "pkg-json",
            "放弃未保存修改",
        ):
            self.assertIn(marker, html)
        for marker in (
            "有未保存修改",
            "/approved-package",
            "discard-btn",
            ".disabled",
            "JSON.stringify",
            "toLowerCase",
            "indexOf",
            "detailRequestVersion",
            "detailRenderVersion",
            "requestVersion !== detailRequestVersion",
            "renderVersion !== detailRenderVersion",
        ):
            self.assertIn(marker, js)

    def test_frontend_has_no_forbidden_apis_or_storage(self) -> None:
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for forbidden in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "confirm(",
            "alert(",
            "localStorage",
            "sessionStorage",
            "onclick=",
            "onerror=",
            "onload=",
        ):
            self.assertNotIn(forbidden, js)
            self.assertNotIn(forbidden, html)

    def test_page_keeps_no_exported_transition_option(self) -> None:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('value="exported"'), 1)


if __name__ == "__main__":
    unittest.main()
