"""TASK-013 M07 跨文件恢复矩阵测试（T49-T56）。

六个真实 killpoint 均由父进程终止专用 worker 子进程后，在新进程上下文中
检查状态并执行恢复；不做 mock 抛异常冒充。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _spawn_worker(work: Path, killpoint: str) -> subprocess.Popen:
    env = dict(
        os.environ,
        PYTHONPATH=f"{PROJECT_ROOT / 'src'};{PROJECT_ROOT}",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
    )
    proc = subprocess.Popen(
        [sys.executable, "-B", str(PROJECT_ROOT / "tools" / "demo_recovery_worker.py"),
         "--work-dir", str(work), "--killpoint", killpoint],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    return proc


def _kill_at_marker(proc: subprocess.Popen, work: Path) -> None:
    marker = work / "ready.marker"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not marker.exists():
        if proc.poll() is not None:
            raise AssertionError(f"worker 提前退出: {proc.returncode}")
        time.sleep(0.2)
    if not marker.exists():
        proc.terminate()
        raise AssertionError("worker 未到达 killpoint")
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


class RecoveryMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ws-recovery-")
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _open_service(self, work: Path):
        from umalib_ingest.inbox import InboxRepository
        from umalib_ingest.workspace.paths import WorkspacePaths
        from umalib_ingest.workspace.service import MaintenanceCoordinator, WorkspaceService
        from umalib_ingest.workspace.store import WorkspaceStore

        paths = WorkspacePaths(work)
        store = WorkspaceStore(paths)
        service = WorkspaceService(paths, store, None, MaintenanceCoordinator())
        manifest = next(iter([
            store.load(entry.name)
            for entry in (paths.workspaces_dir.iterdir() if paths.workspaces_dir.exists() else [])
            if entry.is_dir()
        ]))
        return paths, store, service, manifest, InboxRepository(paths.inbox_db(manifest.workspace_id))

    def test_k1_intent_without_pending_abandonable(self) -> None:
        """T49/T50：PREPARED 且无候选可明确放弃，不产生 INSERT."""
        proc = _spawn_worker(self.root, "k1")
        _kill_at_marker(proc, self.root)
        paths, store, service, manifest, repository = self._open_service(self.root)
        wsid = manifest.workspace_id
        findings = service.inspect_recovery(wsid)
        prepared = [f for f in findings if f["state"] == "ABANDONABLE"]
        self.assertEqual(len(prepared), 1)
        self.assertIsNone(manifest.pending_operation_id)
        op_id = prepared[0]["operation_id"]
        item = next(iter(repository.list_items()))
        self.assertEqual(item.status, "approved")
        result = service.recover(wsid, op_id, "abandon_unverified", True)
        self.assertEqual(result["status"], "ABANDONED")
        self.assertIsNone(store.load(wsid).pending_operation_id)

    def test_k2_pending_set_no_candidate_abandon(self) -> None:
        proc = _spawn_worker(self.root, "k2")
        _kill_at_marker(proc, self.root)
        _, store, service, manifest, _ = self._open_service(self.root)
        wsid = manifest.workspace_id
        self.assertIsNotNone(manifest.pending_operation_id)
        findings = service.inspect_recovery(wsid)
        op_id = next(f["operation_id"] for f in findings
                     if f["state"] == "ABANDONABLE")
        service.recover(wsid, op_id, "abandon_unverified", True)
        self.assertIsNone(store.load(wsid).pending_operation_id)

    def test_k3_orphan_candidate_abandon_keeps_file(self) -> None:
        """T51：有孤立候选无 result 不自动接 HEAD；放弃后文件仍在."""
        proc = _spawn_worker(self.root, "k3")
        _kill_at_marker(proc, self.root)
        paths, store, service, manifest, repository = self._open_service(self.root)
        wsid = manifest.workspace_id
        findings = service.inspect_recovery(wsid)
        orphan = [f for f in findings if f["state"] == "RECOVERY_REQUIRED"]
        self.assertEqual(len(orphan), 1)
        op_id = orphan[0]["operation_id"]
        candidate = paths.candidates_dir(wsid) / f"candidate-{op_id}.db"
        self.assertTrue(candidate.exists())
        service.recover(wsid, op_id, "abandon_unverified", True)
        self.assertTrue(candidate.exists())  # 文件保留
        self.assertIsNone(store.load(wsid).head)
        item = next(iter(repository.list_items()))
        self.assertEqual(item.status, "approved")

    def test_k4_verified_then_complete_no_duplicate_insert(self) -> None:
        """T52：VERIFIED 后补 exported/HEAD，不重复 INSERT."""
        proc = _spawn_worker(self.root, "k4")
        _kill_at_marker(proc, self.root)
        paths, store, service, manifest, repository = self._open_service(self.root)
        wsid = manifest.workspace_id
        findings = service.inspect_recovery(wsid)
        match = next(f for f in findings if f["state"] == "RECOVERY_REQUIRED")
        op_id = match["operation_id"]
        item = next(iter(repository.list_items()))
        self.assertEqual(item.status, "approved")
        repository.transition(item.id, "exported", f"恢复补状态 operation_id={op_id}")
        result = service.recover(wsid, op_id, "complete", True)
        self.assertEqual(result["status"], "COMMITTED")
        manifest_after = store.load(wsid)
        self.assertEqual(manifest_after.revision, 1)
        self.assertEqual(manifest_after.head["operation_id"], op_id)
        candidate = paths.candidates_dir(wsid) / f"candidate-{op_id}.db"
        conn = sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            count = conn.execute("SELECT COUNT(*) FROM article").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)  # 无重复 INSERT

    def test_k5_exported_then_head_recovery(self) -> None:
        """T53：exported 后补 HEAD，不重复状态历史."""
        proc = _spawn_worker(self.root, "k5")
        _kill_at_marker(proc, self.root)
        _, store, service, manifest, repository = self._open_service(self.root)
        wsid = manifest.workspace_id
        item = next(iter(repository.list_items()))
        self.assertEqual(item.status, "exported")
        self.assertIsNone(manifest.head)
        findings = service.inspect_recovery(wsid)
        match = next(f for f in findings
                     if f["state"] == "COMPLETE_PENDING_HEAD")
        result = service.recover(wsid, match["operation_id"], "complete", True)
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(store.load(wsid).head["operation_id"], match["operation_id"])
        self.assertEqual(store.load(wsid).revision, 1)
        history = repository.list_history(item.id)
        self.assertEqual(sum(1 for h in history if h.to_status == "exported"), 1)

    def test_k6_head_then_committed_log_recovery(self) -> None:
        """T54：HEAD 后补 COMMITTED/交付，revision 不二次增加."""
        proc = _spawn_worker(self.root, "k6")
        _kill_at_marker(proc, self.root)
        paths, store, service, manifest, _ = self._open_service(self.root)
        wsid = manifest.workspace_id
        self.assertIsNotNone(manifest.head)
        self.assertEqual(manifest.revision, 1)
        findings = service.inspect_recovery(wsid)
        match = next(f for f in findings
                     if f["state"] == "COMPLETE_PENDING_LOG")
        result = service.recover(wsid, match["operation_id"], "complete", True)
        self.assertEqual(result["status"], "COMMITTED")
        after = store.load(wsid)
        self.assertEqual(after.revision, 1)  # 不二次增加
        deliveries = service.list_deliveries(wsid)
        self.assertEqual(len(deliveries), 1)

    def test_tampered_candidate_blocks_recovery(self) -> None:
        """T55：候选被改后恢复阻断."""
        proc = _spawn_worker(self.root, "k4")
        _kill_at_marker(proc, self.root)
        paths, store, service, manifest, _ = self._open_service(self.root)
        wsid = manifest.workspace_id
        findings = service.inspect_recovery(wsid)
        match = next(f for f in findings if f["state"] == "RECOVERY_REQUIRED")
        candidate = paths.candidates_dir(wsid) / f"candidate-{match['operation_id']}.db"
        data = bytearray(candidate.read_bytes())
        data[0] ^= 0xFF
        candidate.write_bytes(bytes(data))
        with self.assertRaises(Exception):
            service.recover(wsid, match["operation_id"], "complete", True)
        self.assertIsNone(store.load(wsid).head)

    def test_pending_without_log_blocks_new_writes(self) -> None:
        """T56：不一致 pending/多意图不能随便选一个继续写."""
        proc = _spawn_worker(self.root, "k2")
        _kill_at_marker(proc, self.root)
        paths, store, service, manifest, _ = self._open_service(self.root)
        wsid = manifest.workspace_id
        pending = manifest.pending_operation_id
        # 删除操作日志，制造 pending 无日志状态
        log_path = paths.operations_dir(wsid) / f"{pending}.json"
        log_path.unlink()
        from umalib_ingest.workspace.errors import RecoveryRequiredError
        from umalib_ingest.workspace.operations import build_request_fingerprint

        with self.assertRaises(Exception):
            service.begin_operation(
                wsid, str(uuid.uuid4()), 1, build_request_fingerprint(
                    wsid, 1, "0" * 64, 0, None, {}, "0" * 64, "0" * 64, "x.db",
                ), {}, 0, None, "candidate-new.db", "0" * 64, "0" * 64,
            )


if __name__ == "__main__":
    unittest.main()
