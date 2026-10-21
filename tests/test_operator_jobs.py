"""Network job lifecycle tests required by TASK-014 W05."""

from __future__ import annotations

import threading
import time
import unittest

from umalib_ingest.operator.jobs import BusyError, NetworkJobService
from umalib_ingest.operator.models import RunRecord, utc_now


class OperatorJobTests(unittest.TestCase):
    def test_only_one_network_job_across_different_threads(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        service = NetworkJobService(lambda record: None)

        def executor(record, controller):
            entered.set()
            release.wait(2)
            controller.set_status("succeeded", result={})

        service.register_executor("scan", executor)
        service.start()
        try:
            first = service.submit("11111111-1111-4111-8111-111111111111",
                                   "100", "scan", {})
            self.assertTrue(entered.wait(1))
            with self.assertRaises(BusyError):
                service.submit("11111111-1111-4111-8111-111111111111",
                               "200", "scan", {})
            release.set()
            deadline = time.monotonic() + 2
            while service.get(first).status not in ("succeeded", "failed") \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(service.get(first).status, "succeeded")
        finally:
            release.set()
            service.shutdown()

    def test_restore_marks_running_job_interrupted_without_executing(self) -> None:
        persisted = []
        now = utc_now()
        record = RunRecord(
            schema_version="1", run_id="11111111-1111-4111-8111-111111111111",
            workspace_id="22222222-2222-4222-8222-222222222222", tid="30099686",
            kind="scan", status="running", request={},
            progress={"page": 2, "observed_last_page": 3, "navigations": 2},
            result=None, error=None, created_at=now, updated_at=now,
        )
        service = NetworkJobService(persisted.append)
        service.restore([record])
        restored = service.get(record.run_id)
        self.assertEqual(restored.status, "interrupted")
        self.assertEqual(restored.error["code"], "interrupted")
        self.assertEqual(len(persisted), 1)


if __name__ == "__main__":
    unittest.main()
