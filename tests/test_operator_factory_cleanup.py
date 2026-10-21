"""A failed operator launch must release its worker and workspace lock."""
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from umalib_ingest.operator.app_factory import create_operator_app
from umalib_ingest.operator.jobs import NetworkJobService
from umalib_ingest.workspace.store import InstanceLock


class FactoryCleanupTests(unittest.TestCase):
    def test_occupied_port_releases_worker_and_allows_reopen(self):
        started = []
        locks = []
        original_start = NetworkJobService.start
        original_acquire = InstanceLock.acquire

        def start(jobs):
            started.append(jobs)
            original_start(jobs)

        def acquire(lock):
            original_acquire(lock)
            locks.append(lock)

        with tempfile.TemporaryDirectory(prefix="op-factory-") as tmp, \
                socket.socket() as occupied, \
                patch("umalib_ingest.operator.app_factory.detect_main_copy", return_value=None), \
                patch.object(NetworkJobService, "start", start), \
                patch.object(InstanceLock, "acquire", acquire):
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            try:
                with self.assertRaises(OSError):
                    create_operator_app(root=Path(tmp), port=occupied.getsockname()[1])
                self.assertFalse(any(j._worker.is_alive() for j in started),
                                 "failed startup leaked its network worker")
                reopened = create_operator_app(root=Path(tmp))
                reopened.shutdown()
            finally:
                # Own resources only; also clean the deliberately reproduced red case.
                for jobs in started:
                    jobs.shutdown()
                for lock in reversed(locks):
                    lock.release()


if __name__ == "__main__":
    unittest.main()
