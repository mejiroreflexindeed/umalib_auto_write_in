"""TASK-013 M10 检查点与恢复测试（T71-T76）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umalib_ingest.domain.models import SubmissionOrigin
from umalib_ingest.inbox import InboxRepository
from umalib_ingest.parser import parse_submission
from umalib_ingest.review import ReviewFields, ReviewService, ReviewSources
from umalib_ingest.workspace.checkpoints import CheckpointService
from umalib_ingest.workspace.errors import CheckpointError, WorkspaceBusyError
from umalib_ingest.workspace.paths import WorkspacePaths
from umalib_ingest.workspace.service import MaintenanceCoordinator, WorkspaceService
from umalib_ingest.workspace.store import WorkspaceStore


class CheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ws-checkpoint-")
        self.paths = WorkspacePaths(Path(self.tmp.name))
        self.paths.ensure_root()
        self.store = WorkspaceStore(self.paths)
        self.coordinator = MaintenanceCoordinator()
        self.service = WorkspaceService(self.paths, self.store, None, self.coordinator)
        self.checkpoints = CheckpointService(
            self.paths, self.store, self.coordinator, None,
        )
        self.manifest = self.service.create("synthetic", "检查点测试")
        self.wsid = self.manifest.workspace_id
        self.repository = InboxRepository(self.paths.inbox_db(self.wsid))
        self.review = ReviewService(self.repository)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _import_one(self, title: str) -> int:
        item = self.repository.import_draft(parse_submission(
            f"[收录]\n[标题] {title}\n[作者] 作者\n[标签] 标签甲\n\n正文。",
            origin=SubmissionOrigin(platform="nga", thread_id="30099686",
                                    post_id=str(uuid.uuid4().int % (10**9)),
                                    floor=None, canonical_url=None),
        ))
        self.review.save(item.id, ReviewFields(
            title=title, author="作者", translator=None, series=None, tags=["标签甲"],
            sources=ReviewSources(nga=[], original_series=[], original=[f"https://example.com/{title}"],
                                  translation_series=[], translation=[]),
            body_text="正文。",
        ))
        return item.id

    def test_create_roundtrip_and_restore(self) -> None:
        """T71/T74：一致检查点 + 恢复到新工作区后可继续写入."""
        item_id = self._import_one("检查点稿")
        summary = self.checkpoints.create(self.wsid, True)
        checkpoint_id = summary["checkpoint_id"]
        checkpoint_dir = self.paths.checkpoints_dir(self.wsid) / checkpoint_id
        self.assertTrue(checkpoint_dir.is_dir())
        # 清单不含 snapshots/检查点递归
        payload = json.loads((checkpoint_dir / "checkpoint.json").read_text(encoding="utf-8"))
        files = list(payload["files"].keys())
        self.assertNotIn("watcher/snapshots", files)
        self.assertFalse(any(name.startswith("checkpoints/") for name in files))
        # 恢复到新工作区
        restored = self.checkpoints.restore(self.wsid, checkpoint_id, True)
        new_id = restored["new_workspace_id"]
        self.assertNotEqual(new_id, self.wsid)
        new_manifest = self.store.load(new_id)
        self.assertEqual(new_manifest.revision, self.store.load(self.wsid).revision)
        self.assertIsNotNone(new_manifest.restored_from)
        # 原工作区未被覆盖
        self.assertEqual(self.store.load(self.wsid).workspace_id, self.wsid)
        # 恢复后可打开 Inbox 且能继续操作
        new_repo = InboxRepository(self.paths.inbox_db(new_id))
        items = new_repo.list_items()
        self.assertEqual(len(items), 1)
        # 恢复后再写一篇由 M06 测试链证明（此处验证可继续导入）
        item2 = new_repo.import_draft(parse_submission(
            "[收录]\n[标题] 恢复后稿\n[作者] 作者\n[标签] 标签甲\n\n正文。",
            origin=SubmissionOrigin(platform="nga", thread_id="30099686",
                                    post_id="910009999", floor=None, canonical_url=None),
        ))
        self.assertGreater(item2.id, 0)
        # provenance 校验
        provenance = json.loads(
            self.paths.provenance_file(new_id).read_text(encoding="utf-8")
        )
        self.assertIn(self.wsid, provenance["history_workspace_ids"])

    def test_operator_thread_and_source_state_roundtrip(self) -> None:
        """Operator config/state survive restore; browser snapshots stay excluded."""
        workspace = self.paths.workspace_dir(self.wsid)
        source_binding = workspace / "source-binding.json"
        source_binding.write_text('{"test":"binding"}', encoding="utf-8")
        thread_dir = workspace / "threads" / "30099686"
        (thread_dir / "runs").mkdir(parents=True)
        (thread_dir / "snapshots").mkdir(parents=True)
        (workspace / "threads" / "index.json").write_text('{"index":1}', encoding="utf-8")
        (thread_dir / "config.json").write_text('{"config":1}', encoding="utf-8")
        (thread_dir / "state.json").write_text('{"state":1}', encoding="utf-8")
        (thread_dir / "runs" / "run.json").write_text('{"run":1}', encoding="utf-8")
        (thread_dir / "snapshots" / "page.html").write_text("large", encoding="utf-8")

        summary = self.checkpoints.create(self.wsid, True)
        restored = self.checkpoints.restore(self.wsid, summary["checkpoint_id"], True)
        target = self.paths.workspace_dir(restored["new_workspace_id"])
        self.assertEqual((target / "source-binding.json").read_bytes(), source_binding.read_bytes())
        self.assertTrue((target / "threads" / "30099686" / "state.json").exists())
        self.assertTrue((target / "threads" / "30099686" / "runs" / "run.json").exists())
        self.assertFalse((target / "threads" / "30099686" / "snapshots").exists())

    def test_busy_during_active_mutation(self) -> None:
        """T72：活跃写入时检查点 busy，无死锁."""
        with self.coordinator.mutation():
            with self.assertRaises(WorkspaceBusyError):
                self.checkpoints.create(self.wsid, True)

    def test_checkpoint_requires_confirmation(self) -> None:
        with self.assertRaises(CheckpointError):
            self.checkpoints.create(self.wsid, False)

    def test_restore_verifies_hash(self) -> None:
        """T73/T75：清单校验失败保护原工作区."""
        item_id = self._import_one("稿")
        summary = self.checkpoints.create(self.wsid, True)
        checkpoint_dir = self.paths.checkpoints_dir(self.wsid) / summary["checkpoint_id"]
        # 篡改一个被清单记录的文件
        target = checkpoint_dir / "inbox.db"
        data = bytearray(target.read_bytes())
        data[0] ^= 0xFF
        target.write_bytes(bytes(data))
        with self.assertRaises(CheckpointError):
            self.checkpoints.restore(self.wsid, summary["checkpoint_id"], True)
        # 原工作区仍可用
        self.assertEqual(len(self.repository.list_items()), 1)

    def test_missing_candidate_blocks_checkpoint(self) -> None:
        """T76：复制失败/缺失只留原工作区可用."""
        # 制造 manifest HEAD 指向不存在的候选
        manifest = self.store.load(self.wsid)
        data = manifest.to_dict()
        data["head"] = {"operation_id": str(uuid.uuid4()), "candidate_file_name": "candidate-ghost.db",
                        "sha256": "f" * 64}
        from umalib_ingest.workspace.store import atomic_write_json

        atomic_write_json(self.paths.manifest_path(self.wsid), data)
        with self.assertRaises(CheckpointError):
            self.checkpoints.create(self.wsid, True)
        # 工作区文件仍在
        self.assertTrue(self.paths.inbox_db(self.wsid).exists())


if __name__ == "__main__":
    unittest.main()
