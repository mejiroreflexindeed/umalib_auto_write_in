"""TASK-013 M06 累计候选库与操作日志测试（T41-T48）。

三篇稿件逐一批准写入：A/B/C 顺序派生、后篇复用前篇新标签、最终候选累计三篇、
幂等重试、冲突、preview 无副作用。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umalib_ingest.domain.models import SubmissionOrigin
from umalib_ingest.handoff import ApprovedPackageService
from umalib_ingest.inbox import InboxRepository
from umalib_ingest.parser import parse_submission
from umalib_ingest.review import ReviewFields, ReviewService, ReviewSources
from umalib_ingest.umalib_writer import load_target_tag_catalog
from umalib_ingest.web.writer_ui import WriterUiError, WriterUiService
from umalib_ingest.workspace.paths import WorkspacePaths
from umalib_ingest.workspace.service import MaintenanceCoordinator, WorkspaceService
from umalib_ingest.workspace.store import WorkspaceStore

T0_SOURCE = "https://example.com/demo-source"


def sample_text(title: str, tag: str) -> str:
    return f"[收录]\n[标题] {title}\n[作者] 演示作者\n[标签] {tag}\n\n正文内容：{title}。"


class WorkspaceWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ws-writer-")
        self.paths = WorkspacePaths(Path(self.tmp.name))
        self.paths.ensure_root()
        self.store = WorkspaceStore(self.paths)
        self.coordinator = MaintenanceCoordinator()
        self.service = WorkspaceService(
            self.paths, self.store, None, self.coordinator,
        )
        self.manifest = self.service.create("synthetic", "累计测试")
        self.wsid = self.manifest.workspace_id
        self.repository2 = InboxRepository(self.paths.inbox_db(self.wsid))
        self.review = ReviewService(self.repository2)
        self.writer = WriterUiService(
            self.repository2, None, threading.RLock(), workspace_service=self.service,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _import_and_approve(self, title: str, tag: str) -> int:
        text = sample_text(title, tag)
        origin = SubmissionOrigin(
            platform="nga", thread_id="30099686",
            post_id=str(uuid.uuid4().int % (10**9)),
            floor=None, canonical_url=None,
        )
        draft = parse_submission(text, origin=origin)
        item = self.repository2.import_draft(draft)
        fields = ReviewFields(
            title=title,
            author="演示作者",
            translator=None,
            series=None,
            tags=[tag],
            sources=ReviewSources(
                nga=[], original_series=[], original=[f"{T0_SOURCE}-{title}"],
                translation_series=[], translation=[],
            ),
            body_text=f"正文内容：{title}。",
        )
        self.review.save(item.id, fields)
        self.repository2.transition(item.id, "approved", "确认")
        return item.id

    def _dto(self, item_id: int, operation_id: str, tag: str, source: str) -> dict:
        return {
            "candidate_file_name": f"candidate-{operation_id}.db",
            "choices": {
                "upload_time_seconds": 1788062400,
                "note": "合成备注",
                "ordered_sources": [source],
                "tag_choices": [{"requested_kind": "tag", "requested_value": tag,
                                 "existing_tag_id": None, "new_tag_type": 0}],
                "content_mode": "plain",
                "formatted_body": None,
                "plain_loss_confirmed": False,
            },
            "operation_id": operation_id,
            "workspace_revision": 0,
        }

    def _preview(self, item_id: int, dto: dict) -> dict:
        payload = {"candidate_file_name": dto["candidate_file_name"],
                   "choices": dto["choices"]}
        return self.writer.preview(item_id, payload, self.wsid)

    def _write(self, item_id: int, dto: dict) -> tuple[dict, dict]:
        """Write once; returns (result, full_write_dto) for retry tests."""
        preview = self._preview(item_id, dto)
        write_dto = {
            "candidate_file_name": dto["candidate_file_name"],
            "choices": dto["choices"],
            "expected_plan_sha256": preview["plan"]["plan_sha256"],
            "expected_preview_sha256": preview["preview"]["preview_sha256"],
            "expected_source_database_sha256": preview["plan"]["input"]["target_database_sha256"],
            "app_closed_confirmed": True,
            "operation_id": dto["operation_id"],
            "workspace_revision": dto["workspace_revision"],
        }
        result = self.writer.write(item_id, write_dto, self.wsid)
        return result, write_dto

    def test_abc_cumulative_write(self) -> None:
        """T41/T42：A/B/C 逐次派生，最终累计三篇，复用前篇新标签."""
        item_a = self._import_and_approve("稿一", "新标签甲")
        item_b = self._import_and_approve("稿二", "新标签甲")
        item_c = self._import_and_approve("稿三", "同名标签")

        op_a = str(uuid.uuid4())
        dto_a = self._dto(item_a, op_a, "新标签甲", f"{T0_SOURCE}-稿一")
        result_a, _ = self._write(item_a, dto_a)
        self.assertEqual(result_a["delivery"]["count_after"], 1)
        candidate_a = self.paths.candidates_dir(self.wsid) / f"candidate-{op_a}.db"
        sha_a = candidate_a.read_bytes()

        manifest = self.store.load(self.wsid)
        self.assertEqual(manifest.revision, 1)

        op_b = str(uuid.uuid4())
        dto_b = self._dto(item_b, op_b, "新标签甲", f"{T0_SOURCE}-稿二")
        dto_b["workspace_revision"] = 1
        dto_b["choices"]["tag_choices"] = []  # 唯一匹配自动复用稿一新建的标签
        result_b, _ = self._write(item_b, dto_b)
        self.assertEqual(result_b["delivery"]["count_after"], 2)
        # 稿二复用稿一新建的标签：不重复新建同名标签
        catalog_b = load_target_tag_catalog(self.paths.candidates_dir(self.wsid) / f"candidate-{op_b}.db")
        new_tag_rows = [t for t in catalog_b.tags if t.name == "新标签甲"]
        self.assertEqual(len(new_tag_rows), 1)

        op_c = str(uuid.uuid4())
        dto_c = self._dto(item_c, op_c, "同名标签", f"{T0_SOURCE}-稿三")
        dto_c["workspace_revision"] = 2
        dto_c["choices"]["tag_choices"] = [
            {"requested_kind": "tag", "requested_value": "同名标签",
             "existing_tag_id": 2, "new_tag_type": None},
        ]
        result_c, _ = self._write(item_c, dto_c)
        self.assertEqual(result_c["delivery"]["count_after"], 3)

        # T48：A/B 候选未被修改
        self.assertEqual(candidate_a.read_bytes(), sha_a)

        # 最终 HEAD = C；deliveries 三条；revision=3
        manifest = self.store.load(self.wsid)
        self.assertEqual(manifest.head["operation_id"], op_c)
        self.assertEqual(manifest.revision, 3)
        deliveries = self.service.list_deliveries(self.wsid)
        self.assertEqual(len(deliveries), 3)
        self.assertTrue(all(d["count_after"] == i + 1 for i, d in enumerate(deliveries)))

        # 精确回读 C：三篇新增文章
        candidate_c = self.paths.candidates_dir(self.wsid) / f"candidate-{op_c}.db"
        import sqlite3

        conn = sqlite3.connect(candidate_c.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT name FROM article WHERE name IN ('稿一','稿二','稿三')"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual({row[0] for row in rows}, {"稿一", "稿二", "稿三"})

    def test_retry_same_operation_id_replays(self) -> None:
        """T44/T46：相同 operation_id 与请求重试只写入一次（不重新 preview）."""
        item_a = self._import_and_approve("稿一", "新标签甲")
        op = str(uuid.uuid4())
        dto = self._dto(item_a, op, "新标签甲", f"{T0_SOURCE}-稿一")
        _, write_dto = self._write(item_a, dto)
        candidate_path = self.paths.candidates_dir(self.wsid) / f"candidate-{op}.db"
        sha = candidate_path.read_bytes()
        second = self.writer.write(item_a, write_dto, self.wsid)
        self.assertTrue(second.get("replayed"))
        self.assertEqual(candidate_path.read_bytes(), sha)
        self.assertEqual(self.store.load(self.wsid).revision, 1)
        deliveries = self.service.list_deliveries(self.wsid)
        self.assertEqual(len(deliveries), 1)

    def test_same_operation_id_different_request_conflicts(self) -> None:
        """T45：相同 operation_id 换请求返回 conflict."""
        item_a = self._import_and_approve("稿一", "新标签甲")
        op = str(uuid.uuid4())
        dto = self._dto(item_a, op, "新标签甲", f"{T0_SOURCE}-稿一")
        _, write_dto = self._write(item_a, dto)
        write_dto2 = json.loads(json.dumps(write_dto))
        write_dto2["choices"]["upload_time_seconds"] = 999
        with self.assertRaises(WriterUiError) as caught:
            self.writer.write(item_a, write_dto2, self.wsid)
        self.assertEqual(caught.exception.code, "operation_conflict")

    def test_preview_creates_no_files_and_head_unchanged(self) -> None:
        """T47：preview 不创建 DB/操作日志，不改状态/HEAD."""
        item_a = self._import_and_approve("稿一", "新标签甲")
        op = str(uuid.uuid4())
        dto = self._dto(item_a, op, "新标签甲", f"{T0_SOURCE}-稿一")
        self._preview(item_a, dto)
        candidates_dir = self.paths.candidates_dir(self.wsid)
        self.assertEqual([e.name for e in candidates_dir.iterdir()], [])
        operations_dir = self.paths.operations_dir(self.wsid)
        self.assertEqual([e.name for e in operations_dir.iterdir()], [])
        self.assertEqual(self.store.load(self.wsid).revision, 0)

    def test_stale_revision_rejected(self) -> None:
        """T43：旧 source hash/revision 不可写入新 HEAD."""
        item_a = self._import_and_approve("稿一", "新标签甲")
        op = str(uuid.uuid4())
        dto = self._dto(item_a, op, "新标签甲", f"{T0_SOURCE}-稿一")
        self._write(item_a, dto)
        item_b = self._import_and_approve("稿二", "新标签甲")
        op2 = str(uuid.uuid4())
        dto2 = self._dto(item_b, op2, "新标签甲", f"{T0_SOURCE}-稿二")
        dto2["choices"]["tag_choices"] = []
        dto2["workspace_revision"] = 0  # 旧 revision
        with self.assertRaises(WriterUiError) as caught:
            self._write(item_b, dto2)
        self.assertEqual(caught.exception.code, "workspace_head_changed")


if __name__ == "__main__":
    unittest.main()
