from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from umalib_ingest.local_paths import (
    PROJECT_ROOT,
    nga_profile_dir,
    nga_snapshot_dir,
    nga_state_file,
    operator_root,
    private_state_root,
    source_database_candidate,
)


class LocalPathDefaultsTests(unittest.TestCase):
    def test_defaults_use_project_siblings(self) -> None:
        with patch.dict(
            os.environ,
            {},
            clear=False,
        ):
            for name in (
                "UMALIB_PRIVATE_STATE_ROOT",
                "UMALIB_OPERATOR_ROOT",
                "UMALIB_SOURCE_DB",
            ):
                os.environ.pop(name, None)

            private_root = PROJECT_ROOT.parent / "umalib-private-state"
            self.assertEqual(private_state_root(), private_root)
            self.assertEqual(operator_root(), private_root / "operator-user")
            self.assertEqual(nga_profile_dir(), private_root / "nga-profile")
            self.assertEqual(nga_snapshot_dir(), private_root / "nga-snapshots")
            self.assertEqual(nga_state_file(), private_root / "nga-watcher-state.json")
            self.assertEqual(
                source_database_candidate(),
                PROJECT_ROOT.parent / "umalib-test-data" / "main-copy.db",
            )

    def test_environment_overrides_are_respected(self) -> None:
        private_root = Path(r"E:\UmaLibPrivate")
        operator_data = Path(r"E:\UmaLibOperator")
        source_db = Path(r"E:\UmaLibSource\main.db")
        with patch.dict(
            os.environ,
            {
                "UMALIB_PRIVATE_STATE_ROOT": str(private_root),
                "UMALIB_OPERATOR_ROOT": str(operator_data),
                "UMALIB_SOURCE_DB": str(source_db),
            },
            clear=False,
        ):
            self.assertEqual(private_state_root(), private_root)
            self.assertEqual(operator_root(), operator_data)
            self.assertEqual(source_database_candidate(), source_db)
            self.assertEqual(nga_profile_dir(), private_root / "nga-profile")


if __name__ == "__main__":
    unittest.main()
