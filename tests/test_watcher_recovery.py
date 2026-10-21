"""TASK-012B W4：故障与重启恢复矩阵测试（离线、确定注入、真实子进程终止）。

只终止本次登记的 Popen 子进程；不访问网络/profile/现有数据库。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tools.night_recovery import (  # noqa: E402
    check_cancel_after_diff_before_first_import,
    check_client_disconnect_server_survives,
    check_close_failure_not_masked,
    check_schema_mismatch_blocks_before_network,
    check_two_scans_lock_only_one_proceeds,
    _kill_matrix,
)


class RecoveryMatrixTests(unittest.TestCase):
    def test_close_failure_does_not_mask_original_error(self) -> None:
        record = check_close_failure_not_masked()
        self.assertEqual(record["verdict"], "PASS", record)

    def test_schema_mismatch_blocks_before_network(self) -> None:
        record = check_schema_mismatch_blocks_before_network()
        self.assertEqual(record["verdict"], "PASS", record)

    def test_two_scans_lock_only_one_proceeds(self) -> None:
        record = check_two_scans_lock_only_one_proceeds()
        self.assertEqual(record["verdict"], "PASS", record)

    def test_cancel_after_diff_before_first_import(self) -> None:
        record = check_cancel_after_diff_before_first_import()
        self.assertEqual(record["verdict"], "PASS", record)

    def test_kill_after_first_page_no_side_effects(self) -> None:
        record = _kill_matrix("point2")
        self.assertEqual(record["verdict"], "PASS", record)

    def test_kill_during_login_wait_no_side_effects(self) -> None:
        record = _kill_matrix("point3")
        self.assertEqual(record["verdict"], "PASS", record)

    def test_kill_after_imports_partial_then_restart_reuses_items(self) -> None:
        record = _kill_matrix("point9")
        self.assertEqual(record["verdict"], "PASS", record)

    def test_kill_after_all_imports_reuses_both_items(self) -> None:
        record = _kill_matrix("point10")
        self.assertEqual(record["verdict"], "PASS", record)

    def test_client_disconnect_server_survives(self) -> None:
        record = check_client_disconnect_server_survives()
        self.assertEqual(record["verdict"], "PASS", record)


if __name__ == "__main__":
    unittest.main()
