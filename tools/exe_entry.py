"""体验版 EXE 入口（tools/exe_entry.py）。

与 tools/launch_operator.py 相同的启动流程，但数据根默认放在 EXE 同目录
“助手数据”下，便于拷给同僚直接使用；错误信息在控制台保留，不闪退。
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_root() -> Path:
    env = os.environ.get("UMALIB_OPERATOR_ROOT")
    if env:
        return Path(env)
    return _base_dir() / "助手数据"


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


def _pause_on_error() -> None:
    if getattr(sys, "frozen", False) and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("按回车键关闭窗口…")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="大书库录入助手", description="大书库录入助手（体验版）")
    parser.add_argument("--root", type=Path, default=None, help="产品数据根（默认 EXE 同目录 助手数据）")
    parser.add_argument("--port", type=int, default=0, help="监听端口（0=自动）")
    parser.add_argument("--no-browser", action="store_true", help="只启动服务不打开浏览器")
    parser.add_argument("--check", action="store_true", help="只检查环境并退出")
    args = parser.parse_args(argv)

    if args.check:
        import importlib.util
        import platform

        report = {"python": platform.python_version(), "root": str(default_root())}
        try:
            import umalib_ingest.operator  # noqa: F401

            report["operator"] = "OK"
        except Exception as exc:  # pragma: no cover - 诊断路径
            report["operator"] = f"BLOCKED: {exc}"
        report["playwright"] = "OK" if importlib.util.find_spec("playwright") else "unavailable"
        report["edge"] = "OK" if _find_edge() else "unavailable（登录/扫描需要）"
        for key, value in report.items():
            print(f"  {key}: {value}")
        return 0

    root = args.root or default_root()
    os.environ["UMALIB_OPERATOR_ROOT"] = str(root)
    from umalib_ingest.operator.app_factory import create_operator_app

    try:
        context = create_operator_app(root=root, port=args.port)
    except Exception as exc:
        print(f"错误: 启动失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        _pause_on_error()
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
