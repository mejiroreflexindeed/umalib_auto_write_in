"""TASK-014 W02/W14：中文产品启动器（tools/launch_operator.py）。"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def default_root() -> Path:
    from umalib_ingest.local_paths import operator_root

    return operator_root()


def check_environment() -> dict[str, Any]:
    import platform

    report = {"python": platform.python_version(), "root": str(default_root())}
    try:
        import umalib_ingest.operator  # noqa: F401

        report["operator"] = "OK"
    except Exception as exc:
        report["operator"] = f"BLOCKED: {exc}"
    import importlib.util

    report["playwright"] = "OK" if importlib.util.find_spec("playwright") else "unavailable"
    report["edge"] = "OK" if _find_edge() else "unavailable（登录/扫描需要）"
    return report


def _find_edge() -> str | None:
    import shutil

    found = shutil.which("msedge")
    if found:
        return found
    for candidate in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -B tools/launch_operator.py",
        description="大书库录入助手（本地产品入口）",
    )
    parser.add_argument("--root", type=Path, default=None, help="产品数据根（默认 operator-user）")
    parser.add_argument("--port", type=int, default=0, help="监听端口（0=自动）")
    parser.add_argument("--no-browser", action="store_true", help="只启动服务不打开浏览器")
    parser.add_argument("--check", action="store_true", help="只检查环境并退出")
    args = parser.parse_args(argv)
    if args.check:
        for key, value in check_environment().items():
            print(f"  {key}: {value}")
        return 0

    import os

    root = args.root or default_root()
    os.environ["UMALIB_OPERATOR_ROOT"] = str(root)
    from umalib_ingest.operator.app_factory import create_operator_app

    try:
        context = create_operator_app(root=root, port=args.port)
    except Exception as exc:
        print(f"错误: 启动失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{context.server.server_address[1]}"
    print(f"大书库录入助手已启动: {url}", flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        context.serve_forever()
    except KeyboardInterrupt:
        print("已停止", flush=True)
    finally:
        context.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
