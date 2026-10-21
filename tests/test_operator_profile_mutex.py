"""TASK-015 Q0 F003：共享 profile 的跨工作区/跨进程互斥与释放。"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umalib_ingest.operator.jobs import NetworkJobService  # noqa: E402

_TERMINAL = ("succeeded", "partial", "cancelled", "failed", "interrupted")


class ProfileMutexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="profile-mutex-")
        self.lock_path = Path(self.tmp.name) / "nga-profile.lock"
        self.services: list[NetworkJobService] = []

    def tearDown(self) -> None:
        for service in self.services:
            service.shutdown()
        self.tmp.cleanup()

    def _service(self, gate: threading.Event | None = None, fail: bool = False):
        records: list = []
        service = NetworkJobService(records.append, profile_lock_path=self.lock_path)
        self.services.append(service)

        def executor(record, controller):
            if fail:
                raise RuntimeError("boom")
            if gate is not None:
                gate.wait(timeout=30)
            controller.set_status("succeeded", result={})

        service.register_executor("scan", executor)
        service.start()
        return service

    def _wait(self, service, run_id, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = service.get(run_id)
            if record is not None and record.status in _TERMINAL:
                return record
            time.sleep(0.05)
        return service.get(run_id)

    def test_two_services_same_profile_mutex(self) -> None:
        gate_a = threading.Event()
        service_a = self._service(gate=gate_a)
        service_b = self._service()
        run_a = service_a.submit("11111111-1111-4111-8111-111111111111", "1", "scan", {})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if service_a.get(run_a).status == "running":
                break
            time.sleep(0.05)
        run_b = service_b.submit("22222222-2222-4222-8222-222222222222", "1", "scan", {})
        record_b = self._wait(service_b, run_b)
        self.assertEqual(record_b.status, "failed")
        self.assertEqual(record_b.error["code"], "profile_busy")
        gate_a.set()
        self.assertEqual(self._wait(service_a, run_a).status, "succeeded")
        # 释放后可再次使用
        run_b2 = service_b.submit("22222222-2222-4222-8222-222222222222", "1", "scan", {})
        self.assertEqual(self._wait(service_b, run_b2).status, "succeeded")

    def test_lock_released_after_failure(self) -> None:
        failing = self._service(fail=True)
        run = failing.submit("11111111-1111-4111-8111-111111111111", "1", "scan", {})
        self.assertEqual(self._wait(failing, run).status, "failed")
        healthy = self._service()
        run2 = healthy.submit("22222222-2222-4222-8222-222222222222", "1", "scan", {})
        self.assertEqual(self._wait(healthy, run2).status, "succeeded")

    def test_cross_process_lock(self) -> None:
        code = (
            "import sys, time; sys.path.insert(0, r'{src}'); "
            "from umalib_ingest.operator.jobs import _acquire_profile_file_lock; "
            "h = _acquire_profile_file_lock(r'{lock}'); print('HELD', flush=True); "
            "time.sleep(60)"
        ).format(src=str(PROJECT_ROOT / "src"), lock=str(self.lock_path))
        holder = subprocess.Popen([sys.executable, "-B", "-c", code],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, encoding="utf-8")
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                line = holder.stdout.readline()
                if "HELD" in line:
                    break
            service = self._service()
            run = service.submit("11111111-1111-4111-8111-111111111111", "1", "scan", {})
            record = self._wait(service, run)
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.error["code"], "profile_busy")
        finally:
            holder.terminate()
            holder.wait(timeout=15)
            if holder.stdout is not None:
                holder.stdout.close()
        # 等待系统实际释放锁（避免 terminate 后瞬时竞态）
        from umalib_ingest.operator.jobs import (
            _acquire_profile_file_lock,
            _release_profile_file_lock,
        )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                handle = _acquire_profile_file_lock(self.lock_path)
            except Exception:
                time.sleep(0.1)
                continue
            _release_profile_file_lock(handle)
            break
        # 进程退出由系统释放锁
        service = self._service()
        run2 = service.submit("11111111-1111-4111-8111-111111111111", "1", "scan", {})
        self.assertEqual(self._wait(service, run2).status, "succeeded")


if __name__ == "__main__":
    unittest.main()
