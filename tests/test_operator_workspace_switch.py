"""TASK-015 收尾：F012 新工作区 Inbox 初始化 与 F011 同楼版本精确查找。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umalib_ingest.demo.samples import create_synthetic_base  # noqa: E402
from umalib_ingest.domain.models import SubmissionOrigin  # noqa: E402
from umalib_ingest.operator.app_factory import create_operator_app  # noqa: E402
from umalib_ingest.parser import parse_submission  # noqa: E402


def _draft(text, tid, pid):
    return parse_submission(text, origin=SubmissionOrigin(
        platform="nga", thread_id=str(tid), post_id=str(pid), floor=None,
        canonical_url=None))


class WorkspaceSwitchTests(unittest.TestCase):
    """F012：换副本新建工作区后 Inbox 必须可用；重开/切回不损坏旧区与源副本。"""

    def test_new_workspace_inbox_initialized_and_switch_back(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="ws-switch-")
        root = Path(tmp.name)
        source_a = root / "source-a.db"
        source_b = root / "source-b.db"
        create_synthetic_base(source_a)
        create_synthetic_base(source_b)
        sha_a_before = source_a.read_bytes()
        sha_b_before = source_b.read_bytes()

        ctx = create_operator_app(root=root, new_workspace=True, nav_delay=0.0)
        server = threading.Thread(target=ctx.serve_forever, daemon=True)
        server.start()
        port = ctx.server.server_address[1]
        try:
            svc = ctx.service
            verified = svc.verify_source(source_a)
            svc.bind_source(verified["source_id"], "甲区", True)
            svc.add_thread("https://bbs.nga.cn/read.php?tid=12345678", "甲帖", 0)
            ctx.repository.import_draft(_draft(
                "[收录]\n[标题] 甲区稿\n[作者] 甲\n[标签] 标签\n\n甲正文。",
                12345678, 91006001))
            old_ws = svc.workspace_id

            verified_b = svc.verify_source(source_b)
            created = svc.create_workspace_from_source(
                verified_b["source_id"], "乙区", True)
            new_ws = created["workspace_id"]
            self.assertNotEqual(new_ws, old_ws)
            # F012：新建时即初始化 Inbox（user_version=1）
            import sqlite3

            inbox_b = svc.paths.inbox_db(new_ws)
            conn = sqlite3.connect(inbox_b.resolve().as_uri() + "?mode=ro", uri=True)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.close()
            self.assertEqual(version, 1)
        finally:
            ctx.shutdown()

        # 重开：进入新区并可导入/读取；随后切回旧区仍见原稿
        ctx2 = create_operator_app(root=root, nav_delay=0.0)
        try:
            self.assertEqual(ctx2.service.workspace_id, new_ws)
            ctx2.repository.import_draft(_draft(
                "[收录]\n[标题] 乙区稿\n[作者] 乙\n[标签] 标签\n\n乙正文。",
                12345678, 91006002))
            self.assertEqual(len(ctx2.repository.list_items_page()[0]), 1)
        finally:
            ctx2.shutdown()

        ctx3 = create_operator_app(root=root, nav_delay=0.0)
        try:
            ctx3.service.select_workspace(old_ws, True)
        finally:
            ctx3.shutdown()
        ctx4 = create_operator_app(root=root, nav_delay=0.0)
        try:
            self.assertEqual(ctx4.service.workspace_id, old_ws)
            items = ctx4.repository.list_items_page()[0]
            self.assertEqual(len(items), 1)
        finally:
            ctx4.shutdown()

        self.assertEqual(source_a.read_bytes(), sha_a_before)
        self.assertEqual(source_b.read_bytes(), sha_b_before)
        tmp.cleanup()


class SiblingPaginationTests(unittest.TestCase):
    """F011：同帖超过 50 稿仍能找到旧版本；不同 tid/pid 不串。"""

    def test_sibling_found_beyond_first_page(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="sib-page-")
        root = Path(tmp.name)
        create_synthetic_base(root / "source.db")
        ctx = create_operator_app(root=root, new_workspace=True, nav_delay=0.0)
        server = threading.Thread(target=ctx.serve_forever, daemon=True)
        server.start()
        port = ctx.server.server_address[1]
        try:
            repo = ctx.repository
            tid, pid_old = "12345678", 91007001
            old_item = repo.import_draft(_draft(
                "[收录]\n[标题] 旧版稿\n[作者] 甲\n[标签] 标签\n\n旧版正文。",
                tid, pid_old))
            for index in range(50):
                repo.import_draft(_draft(
                    f"[收录]\n[标题] 其他稿{index}\n[作者] 甲\n[标签] 标签\n\n其他正文{index}。",
                    tid, 91007100 + index))
            new_item = repo.import_draft(_draft(
                "[收录]\n[标题] 新版稿\n[作者] 甲\n[标签] 标签\n\n新版正文。",
                tid, pid_old))
            # 精确查询不受分页影响
            found = repo.find_items_by_origin(tid, pid_old)
            found_ids = [item.id for item in found]
            self.assertIn(old_item.id, found_ids)
            self.assertIn(new_item.id, found_ids)
            self.assertGreater(repo.list_items_page()[1], 50)
            # 隔离：其他 tid/pid 不返回
            self.assertEqual(repo.find_items_by_origin("87654321", pid_old), [])
            self.assertEqual(repo.find_items_by_origin(tid, 99999999), [])
            # 经真实 HTTP 详情：新区稿的 siblings 含 50+ 条之后的旧版
            import http.client

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            conn.request("GET", f"/api/operator/items/{new_item.id}")
            res = conn.getresponse()
            body = json.loads(res.read().decode("utf-8"))
            conn.close()
            sibling_ids = [s["id"] for s in body["data"]["siblings"]]
            self.assertIn(old_item.id, sibling_ids)
        finally:
            ctx.shutdown()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
