"""TASK-015 2026-09-21 收尾：A003/A004/A005/A006 入口与依赖提示断言。"""

from __future__ import annotations

import base64
import http.client
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

CMD_PATH = PROJECT_ROOT / "启动录入助手.cmd"


def _get(port: int, path: str) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("GET", path)
    response = conn.getresponse()
    body = response.read().decode("utf-8")
    conn.close()
    return response.status, body


class EntryCommandAssertions(unittest.TestCase):
    def test_crlf_utf8_and_failure_branches(self) -> None:
        raw = CMD_PATH.read_bytes()
        self.assertEqual(raw.count(b"\r\n"), 25, "CMD 必须整文件 CRLF")
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "CMD 不应带 BOM")
        raw.decode("utf-8")
        text = raw.decode("utf-8")
        self.assertIn("chcp 65001", text)
        self.assertIn("--check", text)
        self.assertIn("未找到内置 Python", text)
        self.assertIn("pause", text)

    def test_cmd_runs_check_from_foreign_cwd_without_pythonpath(self) -> None:
        env = {key: value for key, value in os.environ.items()
               if key.upper() != "PYTHONPATH"}
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(
            ["cmd.exe", "/d", "/c", str(CMD_PATH), "--check"],
            cwd=r"C:\Windows", capture_output=True, env=env, timeout=600)
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertIn("python:", out)
        self.assertIn("operator: OK", out)
        self.assertIn("playwright:", out)
        self.assertIn("edge:", out)

    def test_missing_python_keeps_window_and_explains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "启动录入助手.cmd"
            fake.write_bytes(CMD_PATH.read_bytes())
            proc = subprocess.run(
                ["cmd.exe", "/d", "/c", str(fake)], cwd=tmp,
                capture_output=True, stdin=subprocess.DEVNULL, timeout=180)
            out = proc.stdout.decode("utf-8", "replace")
            self.assertEqual(proc.returncode, 2, out)
            self.assertIn("未找到内置 Python", out)
            self.assertIn("launch_operator.py", out)


class ShortcutAssertions(unittest.TestCase):
    def test_shortcut_targets_real_entry_and_no_desktop_writes(self) -> None:
        link = PROJECT_ROOT / "启动录入助手.lnk"
        self.assertTrue(link.exists(), "缺少项目内快捷方式")
        script = (
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('"
            + str(link) + "');"
            "Write-Output ('TARGET=' + $s.TargetPath);"
            "Write-Output ('WORKDIR=' + $s.WorkingDirectory)")
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-EncodedCommand", encoded],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        target = re.search(r"TARGET=(.+)", proc.stdout).group(1).strip()
        workdir = re.search(r"WORKDIR=(.+)", proc.stdout).group(1).strip()
        self.assertTrue(target.lower().endswith("启动录入助手.cmd"), target)
        self.assertNotIn("demo", target.lower())
        self.assertEqual(Path(workdir).resolve(), PROJECT_ROOT)

        for base in ("src", "tools"):
            for path in (PROJECT_ROOT / base).rglob("*.py"):
                text = path.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn("Desktop", text, path)
                self.assertNotIn("SendKeys", text, path)


class SingleConsoleAssertions(unittest.TestCase):
    def test_single_console_without_legacy_dom(self) -> None:
        from umalib_ingest.operator.app_factory import create_operator_app

        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_operator_app(root=Path(tmp), new_workspace=True,
                                      nav_delay=0.0)
            thread = threading.Thread(target=ctx.serve_forever, daemon=True)
            thread.start()
            port = ctx.server.server_address[1]
            try:
                status, body = _get(port, "/operator.html")
                self.assertEqual(status, 200)
                self.assertEqual(body.count('<script src="/operator.js"></script>'), 1)
                self.assertEqual(body.count('id="main-nav"'), 1)
                for legacy in ("demo.js", "watcher.js", "legacy-console"):
                    self.assertNotIn(legacy, body)
                status, root_body = _get(port, "/")
                self.assertEqual(status, 200)
                self.assertIn("大书库录入助手", root_body)
            finally:
                ctx.shutdown()


class DependencyMessageAssertions(unittest.TestCase):
    def test_missing_dependencies_have_specific_messages(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "launch_operator_entry", PROJECT_ROOT / "tools" / "launch_operator.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with mock.patch("importlib.util.find_spec", return_value=None), \
                mock.patch.object(module, "_find_edge", return_value=None):
            report = module.check_environment()
        self.assertEqual(report["playwright"], "unavailable")
        self.assertIn("unavailable", report["edge"])
        self.assertIn("登录/扫描", report["edge"])
        self.assertEqual(report["operator"], "OK")
        self.assertNotIn("login_verified", json.dumps(report))

        healthy = module.check_environment()
        self.assertEqual(healthy["python"], sys.version.split()[0])
        self.assertEqual(healthy["operator"], "OK")


if __name__ == "__main__":
    unittest.main()
