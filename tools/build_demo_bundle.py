"""TASK-013 M11：源码 Demo 交付包构建（当前环境源码运行包，非独立 EXE）。

只打包 allowlist；不含 .venv、真实 DB、profile、真实 snapshots、私有操作日志、
Cookie、旧证据和工作区数据。新目录解包后可用现有解释器验证 import/静态资源/
--help/--check。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_ALLOWED_FILES = (
    "pyproject.toml",
    "README.md",
    "AGENTS.md",
    "Start-Demo.cmd",
    "Start-Real-Copy.cmd",
    "启动录入助手.cmd",
)
_ALLOWED_DIRS = (
    "src",
    "tools",
    "demo_samples",
    "docs",
    "contracts",
)
_EXCLUDED_NAMES = {
    ".venv", "__pycache__", ".git", ".pytest_cache",
}


def _bundle_manifest() -> dict:
    manifest = {"files": {}}
    for name in _ALLOWED_FILES:
        path = PROJECT_ROOT / name
        if path.exists():
            manifest["files"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    for directory in _ALLOWED_DIRS:
        base = PROJECT_ROOT / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _EXCLUDED_NAMES for part in path.parts):
                continue
            if path.suffix in (".db", ".sqlite", ".pyc") or path.suffix in ("-wal", "-shm"):
                continue
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            manifest["files"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def build_bundle(output: Path) -> dict:
    """Create the zip bundle at a NEW path; refuses to overwrite."""
    if output.exists():
        raise FileExistsError(f"输出已存在，不覆盖: {output}")
    manifest = _bundle_manifest()
    with zipfile.ZipFile(str(output), "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in sorted(manifest["files"]):
            source = PROJECT_ROOT / relative
            archive.write(source, arcname=relative)
        archive.writestr(
            "bundle-manifest.json",
            json.dumps({"files": manifest["files"], "kind": "source-runnable-bundle",
                        "note": "当前环境源码运行包；需要本机已有 Python 3.10+ 与 .venv"},
                       ensure_ascii=False, indent=2),
        )
    return {
        "output": str(output),
        "size_bytes": output.stat().st_size,
        "file_count": len(manifest["files"]),
        "bundle_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -B tools/build_demo_bundle.py",
        description="生成当前环境源码 Demo 运行包（不是独立 EXE）",
    )
    parser.add_argument("--output", required=True, type=Path,
                        help="新 zip 路径（已存在则拒绝）")
    args = parser.parse_args(argv)
    try:
        summary = build_bundle(args.output)
    except FileExistsError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
