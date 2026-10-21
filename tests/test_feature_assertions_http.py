"""TASK-015 自主 debug：HTTP 列表维护门闩与跨帖写入/交付断言（A071/A081/A087/A092）。"""

from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock
from uuid import uuid4

from umalib_ingest.demo.samples import create_synthetic_base
from umalib_ingest.domain.models import SubmissionOrigin
from umalib_ingest.operator.app_factory import create_operator_app
from umalib_ingest.parser import parse_submission


def _submission(title: str) -> str:
    return f"[收录]\n[标题] {title}\n[作者] 合成作者\n[标签] 标签\n\n合成正文"


def _start(tmp, new_workspace=True):
    ctx = create_operator_app(root=Path(tmp), new_workspace=new_workspace,
                              nav_delay=0.0)
    thread = threading.Thread(target=ctx.serve_forever, daemon=True)
    thread.start()
    return ctx, ctx.server.server_address[1]


def _call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path,
                 json.dumps(body).encode("utf-8") if body is not None else None,
                 headers=headers)
    res = conn.getresponse()
    raw = res.read().decode("utf-8")
    conn.close()
    return res.status, (json.loads(raw) if raw else {})


def _import(repo, title, author, tid, body="正文。", tag="标签"):
    text = f"[收录]\n[标题] {title}\n[作者] {author}\n[标签] {tag}\n\n{body}"
    return repo.import_draft(parse_submission(text, origin=SubmissionOrigin(
        platform="nga", thread_id=str(tid), post_id=str(uuid4().int % 10**9),
        floor=None, canonical_url=None)))


class ListFilterAssertions(unittest.TestCase):
    def test_author_filter_and_second_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx, port = _start(tmp)
            try:
                for index in range(51):
                    _import(ctx.repository, f"列表稿{index:02d}",
                            f"列表作者{index:02d}", "12345678")
                status, payload = _call(
                    port, "GET",
                    "/api/operator/items?q=" + urllib.parse.quote("列表作者07"))
                self.assertEqual(status, 200, payload)
                data = payload["data"]
                self.assertEqual(data["total"], 1)
                self.assertEqual(data["items"][0]["title"], "列表稿07")

                status, page1 = _call(port, "GET", "/api/operator/items?page=1")
                self.assertEqual(status, 200)
                status, page2 = _call(port, "GET", "/api/operator/items?page=2")
                self.assertEqual(status, 200)
                ids1 = {item["id"] for item in page1["data"]["items"]}
                ids2 = {item["id"] for item in page2["data"]["items"]}
                self.assertEqual(len(ids1), 50)
                self.assertEqual(len(ids2), 1)
                self.assertEqual(ids1 & ids2, set())
                self.assertEqual(page2["data"]["page"], 2)
            finally:
                ctx.shutdown()


class MaintenanceGateAssertions(unittest.TestCase):
    def test_all_mutation_endpoints_rejected_during_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx, port = _start(tmp)
            try:
                item = _import(ctx.repository, "维护稿", "维护作者", "12345678")
                ready = _import(ctx.repository, "维护就绪稿", "维护作者", "12345678")
                review_body = {
                    "title": "维护稿改", "author": "维护作者", "translator": None,
                    "series": None, "tags": ["标签"],
                    "sources": {"nga": [], "original_series": [],
                                "original": ["https://example.com/维护"],
                                "translation_series": [], "translation": []},
                    "body_text": "正文。",
                }
                choices_body = {
                    "note": None, "upload_time_seconds": 1788062400,
                    "ordered_sources": ["https://example.com/维护"],
                    "tag_choices": [{"requested_kind": "tag",
                                     "requested_value": "新标签",
                                     "existing_tag_id": None, "new_tag_type": 0}],
                    "content_mode": "plain", "formatted_body": None,
                    "plain_loss_confirmed": False,
                }
                ctx.service._workspace.save_choices(
                    ctx.service.workspace_id, ready.id, dict(choices_body))
                ctx.repository.transition(ready.id, "approved", "维护前准备")
                mutations = [
                    ("PUT", f"/api/operator/items/{item.id}/review", review_body),
                    ("POST", f"/api/operator/items/{item.id}/transition",
                     {"to_status": "approved", "reason": "维护期"}),
                    ("PUT", f"/api/operator/items/{item.id}/choices", choices_body),
                    ("POST", f"/api/operator/items/{ready.id}/writer/preview",
                     {"candidate_file_name": "candidate-x.db"}),
                    ("POST", "/api/operator/threads",
                     {"url": "https://bbs.nga.cn/read.php?tid=87654321",
                      "name": None, "expected_index_revision": 1}),
                    ("POST", "/api/operator/workspaces/new",
                     {"source_id": "sha256:0", "name": "维护新区",
                      "confirmed": True}),
                ]
                ctx.workspace_service.coordinator().enter_maintenance()
                try:
                    for method, path, body in mutations:
                        status, payload = _call(port, method, path, body)
                        self.assertGreaterEqual(status, 400, (path, status, payload))
                        code = (payload.get("error") or {}).get("code", "")
                        self.assertIn(code, ("maintenance_active", "workspace_busy",
                                             "source_token_stale"),
                                      (path, status, payload))
                finally:
                    ctx.workspace_service.coordinator().leave_maintenance()
                status, payload = _call(
                    port, "PUT", f"/api/operator/items/{item.id}/review", review_body)
                self.assertEqual(status, 200, payload)
            finally:
                ctx.shutdown()


class CrossThreadDeliveryAssertions(unittest.TestCase):
    def test_two_threads_accumulate_and_folder_actions_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "base.db"
            create_synthetic_base(source)
            ctx, port = _start(root / "prod")
            try:
                svc = ctx.service
                verified = svc.verify_source(source)
                svc.bind_source(verified["source_id"], "验收区", True)
                ws = svc.workspace_id
                head = ctx.workspace_service.resolve_head(ws)[1]
                conn = sqlite3.connect(head.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    base_count = conn.execute("SELECT COUNT(*) FROM article").fetchone()[0]
                finally:
                    conn.close()

                from umalib_ingest.review import ReviewFields, ReviewSources

                def deliver(title, tid, tag_choice):
                    item = _import(ctx.repository, title, "验收作者", tid)
                    ctx.review_service.save(item.id, ReviewFields(
                        title=title, author="验收作者", translator=None, series=None,
                        tags=["验收标签"],
                        sources=ReviewSources(
                            nga=[], original_series=[],
                            original=[f"https://example.com/{title}"],
                            translation_series=[], translation=[]),
                        body_text=f"{title}正文。"))
                    ctx.repository.transition(item.id, "approved", "验收")
                    svc._workspace.save_choices(svc.workspace_id, item.id, {
                        "note": None, "upload_time_seconds": 1788062400,
                        "ordered_sources": [f"https://example.com/{title}"],
                        "tag_choices": [tag_choice],
                        "content_mode": "plain", "formatted_body": None,
                        "plain_loss_confirmed": False,
                    })
                    preview = svc.writer_preview(item.id, "candidate-x.db")
                    return svc.writer_write(item.id, preview["preview_id"], True)

                first = deliver("跨帖稿一", "12345678", {
                    "requested_kind": "tag", "requested_value": "验收标签",
                    "existing_tag_id": None, "new_tag_type": 0})
                first_candidate = (ctx.workspace_service.paths.candidates_dir(
                    svc.workspace_id) / first["delivery"]["candidate_file_name"])
                conn = sqlite3.connect(first_candidate.resolve().as_uri() + "?mode=ro",
                                       uri=True)
                try:
                    tag_id = conn.execute(
                        "SELECT id FROM tag WHERE name = ?",
                        ("验收标签",)).fetchone()[0]
                finally:
                    conn.close()
                second = deliver("跨帖稿二", "87654321", {
                    "requested_kind": "tag", "requested_value": "验收标签",
                    "existing_tag_id": tag_id, "new_tag_type": None})
                self.assertEqual(second["delivery"]["count_after"], base_count + 2,
                                 second["delivery"])
                candidate = (ctx.workspace_service.paths.candidates_dir(
                    svc.workspace_id)
                    / second["delivery"]["candidate_file_name"])
                conn = sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    names = sorted(row[0] for row in conn.execute(
                        "SELECT name FROM article WHERE name IN (?,?)",
                        ("跨帖稿一", "跨帖稿二")))
                finally:
                    conn.close()
                self.assertEqual(names, ["跨帖稿一", "跨帖稿二"])

                with mock.patch("os.startfile", create=True) as fake_open:
                    opened = svc.open_delivery_folder(
                        second["delivery"]["operation_id"], True)
                fake_open.assert_called_once()
                self.assertTrue(Path(opened["directory"]).is_dir())
                self.assertEqual(Path(opened["directory"]).resolve(),
                                 candidate.parent.resolve())
                view = svc.delivery_view_copy(second["delivery"]["operation_id"], True)
                self.assertTrue(Path(view["view_copy_path"]).is_file())
                self.assertNotEqual(Path(view["view_copy_path"]),
                                    Path(view["candidate_path"]))
            finally:
                ctx.shutdown()


if __name__ == "__main__":
    unittest.main()
