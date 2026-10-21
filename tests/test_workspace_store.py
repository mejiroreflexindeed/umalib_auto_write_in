"""TASK-013 M02 工作区存储与独占测试（T07-T14）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umalib_ingest.workspace.errors import (
    WorkspaceConflictError,
    WorkspaceManifestError,
    WorkspaceNotFoundError,
)
from umalib_ingest.workspace.models import WorkspaceManifest, canonical_sha256
from umalib_ingest.workspace.paths import WorkspacePaths, validate_workspace_id
from umalib_ingest.workspace.store import InstanceLock, WorkspaceStore


class WorkspaceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ws-test-")
        self.paths = WorkspacePaths(Path(self.tmp.name))
        self.paths.ensure_root()
        self.store = WorkspaceStore(self.paths)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class ManifestTests(WorkspaceTestBase):
    """T07：manifest 首建/加载/版本/类型校验."""

    def test_create_and_load_roundtrip(self) -> None:
        manifest = self.store.create("synthetic", "测试工作区")
        loaded = self.store.load(manifest.workspace_id)
        self.assertEqual(loaded.to_dict(), manifest.to_dict())
        self.assertEqual(loaded.mode, "synthetic")
        self.assertEqual(loaded.revision, 0)
        self.assertIsNone(loaded.head)

    def test_invalid_schema_version_rejected(self) -> None:
        manifest = self.store.create("synthetic", "x")
        path = self.paths.manifest_path(manifest.workspace_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["schema_version"] = "9.9"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(WorkspaceManifestError):
            self.store.load(manifest.workspace_id)

    def test_unknown_keys_rejected(self) -> None:
        manifest = self.store.create("synthetic", "x")
        path = self.paths.manifest_path(manifest.workspace_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["evil"] = True
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(WorkspaceManifestError):
            self.store.load(manifest.workspace_id)

    def test_revision_must_be_int(self) -> None:
        manifest = self.store.create("synthetic", "x")
        path = self.paths.manifest_path(manifest.workspace_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["revision"] = "1"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(WorkspaceManifestError):
            self.store.load(manifest.workspace_id)

    def test_missing_workspace_not_found(self) -> None:
        with self.assertRaises(WorkspaceNotFoundError):
            self.store.load("00000000-0000-0000-0000-000000000000")


class PathSafetyTests(WorkspaceTestBase):
    """T08：路径穿越、绝对路径、junction 越界被拒绝."""

    def test_invalid_workspace_ids_rejected(self) -> None:
        for bad in ("../etc", "..", "abc", "", "0000-0000", "AAAAAAAA-0000-0000-0000-000000000000/../../x"):
            with self.assertRaises(WorkspaceManifestError):
                validate_workspace_id(bad)

    def test_sub_path_traversal_rejected(self) -> None:
        manifest = self.store.create("synthetic", "x")
        with self.assertRaises(WorkspaceManifestError):
            self.paths.sub_path(manifest.workspace_id, "..", "escape.db")

    def test_absolute_sub_path_rejected(self) -> None:
        manifest = self.store.create("synthetic", "x")
        with self.assertRaises(WorkspaceManifestError):
            self.paths.sub_path(manifest.workspace_id, "C:/Windows/x")


class LockTests(WorkspaceTestBase):
    """T09/T10：同区第二进程无法取锁；释放后可重启不删锁抢占."""

    def test_second_holder_cannot_acquire(self) -> None:
        from umalib_ingest.workspace.errors import WorkspaceLockedError

        manifest = self.store.create("synthetic", "x")
        lock_path = self.paths.lock_file(manifest.workspace_id)
        first = InstanceLock(lock_path)
        first.acquire()
        try:
            second = InstanceLock(lock_path)
            with self.assertRaises(WorkspaceLockedError):
                second.acquire()
        finally:
            first.release()
        third = InstanceLock(lock_path)
        third.acquire()
        third.release()

    def test_lock_file_present_after_holder_release(self) -> None:
        manifest = self.store.create("synthetic", "x")
        lock_path = self.paths.lock_file(manifest.workspace_id)
        with InstanceLock(lock_path):
            self.assertTrue(lock_path.exists())
        # 锁文件保留（系统锁基于句柄），后续可正常加锁，不需要删除
        with InstanceLock(lock_path):
            pass


class AtomicWriteTests(WorkspaceTestBase):
    """T11：原子写失败保留旧内容和明确错误."""

    def test_cas_failure_keeps_old_manifest(self) -> None:
        manifest = self.store.create("synthetic", "x")
        path = self.paths.manifest_path(manifest.workspace_id)
        before = path.read_bytes()
        new = WorkspaceManifest.from_dict(manifest.to_dict())
        new.name = "新名字"
        new.updated_at = "2099-01-01T00:00:00+00:00"
        with self.assertRaises(WorkspaceConflictError):
            self.store.compare_and_swap(
                manifest.workspace_id, 0, "f" * 64, new,
            )
        self.assertEqual(path.read_bytes(), before)

    def test_cas_stale_revision_rejected(self) -> None:
        manifest = self.store.create("synthetic", "x")
        new = WorkspaceManifest.from_dict(manifest.to_dict())
        new.name = "新名字"
        with self.assertRaises(WorkspaceConflictError):
            self.store.compare_and_swap(
                manifest.workspace_id, 7, manifest.manifest_sha256(), new,
            )

    def test_cas_success_with_correct_bytes(self) -> None:
        manifest = self.store.create("synthetic", "x")
        sha = manifest.manifest_sha256()
        new = WorkspaceManifest.from_dict(manifest.to_dict())
        new.name = "已更新"
        new.revision = 1
        new.updated_at = "2099-01-01T00:00:00+00:00"
        result = self.store.compare_and_swap(manifest.workspace_id, 0, sha, new)
        self.assertEqual(result.name, "已更新")
        self.assertEqual(self.store.load(manifest.workspace_id).revision, 1)


class LauncherTests(WorkspaceTestBase):
    """T14：mode/source alias 映射与 last_workspace_by_mode."""

    def test_last_workspace_per_mode_isolated(self) -> None:
        synthetic = self.store.create("synthetic", "s")
        real = self.store.create("real_copy", "r")
        self.store.remember_last("synthetic", synthetic.workspace_id)
        self.store.remember_last("real_copy", real.workspace_id)
        self.assertEqual(self.store.last_workspace("synthetic"), synthetic.workspace_id)
        self.assertEqual(self.store.last_workspace("real_copy"), real.workspace_id)

    def test_source_alias_mapping(self) -> None:
        from umalib_ingest.workspace.paths import (
            mode_of_source_alias,
            source_alias_for_mode,
        )

        self.assertEqual(source_alias_for_mode("synthetic"), "synthetic")
        self.assertEqual(source_alias_for_mode("real_copy"), "main-copy")
        self.assertEqual(mode_of_source_alias("main-copy"), "real_copy")


if __name__ == "__main__":
    unittest.main()
