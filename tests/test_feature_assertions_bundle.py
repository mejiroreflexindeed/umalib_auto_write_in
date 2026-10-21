"""TASK-015 自主 debug：源码包解包 smoke（A096）。

断言：包内无真实数据库/虚拟环境数据；解到含中文与空格目录后入口能实际
启动并提供产品页面（非 --check）。
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class BundleSmokeAssertions(unittest.TestCase):
    def test_bundle_excludes_real_data_and_starts_from_chinese_path(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="bundle-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            zip_path = root / "bundle.zip"
            built = subprocess.run(
                [sys.executable, "-B",
                 str(PROJECT_ROOT / "tools" / "build_demo_bundle.py"),
                 "--output", str(zip_path)],
                cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=300)
            self.assertEqual(built.returncode, 0, built.stderr[-800:])

            with zipfile.ZipFile(zip_path) as archive:
                names = archive.namelist()
                bad_data = [name for name in names
                            if name.lower().endswith((".db", ".sqlite", ".wal",
                                                      ".shm", ".db-wal", ".db-shm"))]
                self.assertEqual(bad_data, [], "包内不得含真实数据库数据")
                self.assertFalse(
                    any(name.startswith(".venv/") or "/.venv/" in name
                        for name in names), "包内不得含虚拟环境")
                self.assertIn("启动录入助手.cmd", names)
                self.assertTrue(any(name.endswith("tools/launch_operator.py")
                                    for name in names))
                self.assertTrue(any(name.endswith("bundle-manifest.json")
                                    for name in names))

                unpack = root / "解包 中文 目录"
                archive.extractall(unpack)

            env = {key: value for key, value in os.environ.items()
                   if key.upper() != "PYTHONPATH"}
            env["PYTHONUTF8"] = "1"
            env["UMALIB_OPERATOR_ROOT"] = str(root / "bundle-start-root")
            proc = subprocess.Popen(
                [sys.executable, "-B", "tools/launch_operator.py",
                 "--no-browser", "--port", "0"],
                cwd=str(unpack), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace")
            try:
                port = None
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    match = re.search(r"http://127\.0\.0\.1:(\d+)", line)
                    if match:
                        port = int(match.group(1))
                        break
                self.assertIsNotNone(port, "解包后入口未能启动到页面")
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
                conn.request("GET", "/operator.html")
                response = conn.getresponse()
                html = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("大书库录入助手", html)
                self.assertIn("operator.js", html)
                # 优雅退出：调用产品自身的 shutdown 接口，等待进程自然结束
                conn.request("POST", "/api/operator/shutdown",
                             json.dumps({"confirmed": True}).encode("utf-8"),
                             {"Content-Type": "application/json"})
                shutdown_response = conn.getresponse()
                shutdown_response.read()
                conn.close()
                self.assertEqual(shutdown_response.status, 200)
            finally:
                graceful = False
                try:
                    proc.wait(timeout=60)
                    graceful = proc.poll() is not None
                except subprocess.TimeoutExpired:
                    graceful = False
                cleanup_record = {"pid": proc.pid, "graceful": graceful}
                if not graceful:
                    # 沙箱权限不足时保留确切 PID/返回码，分层记录，不静默忽略。
                    kill = subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True, text=True, timeout=30)
                    cleanup_record["taskkill_returncode"] = kill.returncode
                    cleanup_record["taskkill_stdout"] = (kill.stdout or "").strip()[:200]
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        cleanup_record["still_alive"] = True
                if proc.stdout is not None:
                    try:
                        proc.stdout.close()
                    except Exception:
                        pass
                if not graceful:
                    self.fail("产品未优雅退出，清理记录：" + json.dumps(
                        cleanup_record, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
