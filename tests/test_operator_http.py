"""TASK-014 W01 HTTP 接线测试。"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


class OperatorHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="op-http-")
        from umalib_ingest.operator.app_factory import create_operator_app

        self.ctx = create_operator_app(root=Path(self.tmp.name), new_workspace=True,
                                       nav_delay=0.0)
        self.thread = threading.Thread(target=self.ctx.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.ctx.server.server_address[1]

    def tearDown(self) -> None:
        self.ctx.shutdown()
        self.tmp.cleanup()

    def _req(self, method, path, body=None, extra_headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read().decode("utf-8")
        conn.close()
        return res.status, json.loads(data) if data else {}

    def test_status_and_threads_flow(self) -> None:
        status, body = self._req("GET", "/api/operator/status")
        self.assertEqual(status, 200, body)
        self.assertIn("data", body)
        self.assertIn("workspace_id", body["data"])

        status, body = self._req("POST", "/api/operator/threads",
                                 json.dumps({"url": "https://bbs.nga.cn/read.php?tid=30099686",
                                             "name": None, "expected_index_revision": 0}))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["entry"]["tid"], "30099686")

        status, body = self._req("GET", "/api/operator/threads")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]["entries"]), 1)
        self.assertEqual(body["data"]["revision"], 1)

    def test_unknown_route_404(self) -> None:
        status, body = self._req("GET", "/api/operator/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_write_security_rejects_foreign_origin_and_wrong_content_type(self) -> None:
        payload = json.dumps({"raw_text": "[收录]"})
        status, body = self._req(
            "POST", "/api/operator/import", payload,
            {"Origin": "http://evil.example"})
        self.assertEqual((status, body["error"]["code"]), (403, "invalid_origin"))
        status, body = self._req(
            "POST", "/api/operator/import", payload,
            {"Content-Type": "text/plain"})
        self.assertEqual(
            (status, body["error"]["code"]), (415, "unsupported_media_type"))

    def test_malformed_json_returns_one_structured_error(self) -> None:
        status, body = self._req("POST", "/api/operator/import", "{")
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_json"))

    def test_items_pagination_page_size_fixed(self) -> None:
        status, body = self._req("GET", "/api/operator/items?page_size=25")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "page_size_invalid")
        status, body = self._req("GET", "/api/operator/items?page=1&page_size=50")
        self.assertEqual(status, 200)
        self.assertIn("items", body["data"])

    def test_error_envelope_shape(self) -> None:
        status, body = self._req("POST", "/api/operator/threads",
                                 json.dumps({"url": "not-a-url",
                                             "name": None, "expected_index_revision": 0}))
        self.assertEqual(status, 422)
        self.assertEqual(set(body["error"]), {"code", "message", "retryable"})

    def test_review_transition_choices_and_writer_routes_are_reachable(self) -> None:
        raw = "[收录]\n[标题] HTTP 稿\n[作者] 作者\n[标签] 标签\n\n正文。"
        status, body = self._req(
            "POST", "/api/operator/import",
            json.dumps({"raw_text": raw}, ensure_ascii=False).encode("utf-8"),
        )
        self.assertEqual(status, 200, body)
        item_id = body["data"]["id"]
        status, detail = self._req("GET", f"/api/operator/items/{item_id}")
        self.assertEqual(status, 200, detail)
        fields = detail["data"]["review_fields"]
        status, saved = self._req(
            "PUT", f"/api/operator/items/{item_id}/review",
            json.dumps(fields, ensure_ascii=False).encode("utf-8"),
        )
        self.assertEqual(status, 200, saved)
        status, approved = self._req(
            "POST", f"/api/operator/items/{item_id}/transition",
            json.dumps({"to_status": "approved", "reason": "HTTP 验收"},
                       ensure_ascii=False).encode("utf-8"),
        )
        self.assertEqual(status, 200, approved)
        choices = {
            "note": None, "upload_time_seconds": 1788451200,
            "ordered_sources": [], "tag_choices": [],
            "content_mode": "plain", "formatted_body": None,
            "plain_loss_confirmed": False,
        }
        status, saved_choices = self._req(
            "PUT", f"/api/operator/items/{item_id}/choices",
            json.dumps(choices).encode("utf-8"),
        )
        self.assertEqual(status, 200, saved_choices)
        status, loaded_choices = self._req(
            "GET", f"/api/operator/items/{item_id}/choices")
        self.assertEqual(status, 200, loaded_choices)

    def test_detected_source_can_be_explicitly_bound(self) -> None:
        status, body = self._req("GET", "/api/operator/status")
        self.assertEqual(status, 200, body)
        source = body["data"]["source"]
        if not source.get("detected"):
            self.skipTest("本机没有可校验的 main-copy.db")
        status, bound = self._req(
            "POST", "/api/operator/workspaces",
            json.dumps({"source_id": source["detected"]["source_id"],
                        "name": "录入工作区", "confirmed": True}).encode("utf-8"),
        )
        self.assertEqual(status, 200, bound)
        status, after = self._req("GET", "/api/operator/status")
        self.assertTrue(after["data"]["source"]["bound"])


if __name__ == "__main__":
    unittest.main()
