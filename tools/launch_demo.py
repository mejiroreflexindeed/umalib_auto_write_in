"""TASK-013 启动器转发入口：tools/launch_demo.py（M11）。

转发 python -m umalib_ingest.demo 的参数；独立支持只读 --check。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def main(argv: list[str] | None = None) -> int:
    from umalib_ingest.demo.launcher import build_parser, check_environment, launch_demo

    args = build_parser().parse_args(argv)
    if args.check:
        for key, value in check_environment().items():
            print(f"  {key}: {value}")
        return 0
    return launch_demo(args)


if __name__ == "__main__":
    raise SystemExit(main())
