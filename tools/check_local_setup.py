"""TASK-012B W6：只读本地环境检查工具（给录入人员的一条命令判断能否启动）。

- 只读：不创建文件、不初始化/迁移/备份数据库、不启动浏览器、不登录；
- 所有输入路径必须显式给出；无参数或 --help 只打印用法；
- SQLite 一律使用只读 URI 打开；--umalib-db 只调用既有只读 inspector；
- 输出 OK / WARN / BLOCKED + 下一步；JSON 与中文输出对齐；
- 退出码：0 可启动、2 阻断、1 内部错误。

用法：
  python -B tools/check_local_setup.py --inbox-db <inbox.db> \
      [--watcher-state <state.json> --watcher-profile <dir> --watcher-snapshot-dir <dir>] \
      [--umalib-db <umalib.db>] [--json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_BLOCKED = 2


def _item(name: str, status: str, message: str, next_step: str = "") -> dict[str, Any]:
    return {"name": name, "status": status, "message": message, "next_step": next_step}


def _readonly_uri(target: Path) -> str:
    return target.resolve().as_uri() + "?mode=ro&immutable=1"


def _database_evidence(target: Path) -> tuple[str, int, int, tuple[str, ...]]:
    """Reject WAL/sidecars before SQLite opens; inspect only closed snapshots."""
    if not target.is_file():
        raise ValueError("目标不是普通数据库文件")
    for suffix in ("-wal", "-shm", "-journal"):
        if target.with_name(target.name + suffix).exists():
            raise ValueError("发现数据库 sidecar；请关闭使用它的程序并提供干净副本，勿手动删除日志")
    with target.open("rb") as handle:
        header = handle.read(100)
        if len(header) >= 20 and (header[18] == 2 or header[19] == 2):
            raise ValueError("WAL 模式数据库不能在本工具中安全检查；请提供正常关闭并转为 DELETE 模式的副本")
        digest = hashlib.sha256(header)
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    stat = target.stat()
    return digest.hexdigest(), stat.st_size, stat.st_mtime_ns, tuple(sorted(p.name for p in target.parent.iterdir()))


def check_python_version() -> dict[str, Any]:
    version = sys.version_info
    ok = version >= (3, 10)
    return _item(
        "python-version",
        "OK" if ok else "BLOCKED",
        f"Python {version.major}.{version.minor}.{version.micro}",
        "" if ok else "需要 Python 3.10 或更高版本",
    )


def check_project_import() -> dict[str, Any]:
    try:
        import umalib_ingest  # noqa: F401

        return _item("umalib-import", "OK", "项目模块可导入", "")
    except Exception as exc:
        return _item("umalib-import", "BLOCKED", f"导入失败: {type(exc).__name__}", "检查项目目录与 PYTHONPATH")


def check_playwright() -> dict[str, Any]:
    import importlib.util

    if importlib.util.find_spec("playwright") is not None:
        return _item("playwright", "OK", "模块已安装（未启动浏览器，未验证在线访问或登录）", "")
    return _item(
        "playwright", "WARN", "未安装：审核台可用，Watcher 在线扫描不可用",
        "离线审核不受影响；需要扫描时再安装 playwright",
    )


def check_watcher_args(
    state: Path | None, profile: Path | None, snapshot: Path | None
) -> list[dict[str, Any]]:
    given = [bool(state), bool(profile), bool(snapshot)]
    if not any(given):
        return [_item(
            "watcher-args", "WARN", "未启用 Watcher（三个 watcher 参数都未给出）",
            "审核台可正常启动；如需 NGA 扫描请同时提供 --watcher-state/--watcher-profile/--watcher-snapshot-dir",
        )]
    if not all(given):
        missing = [
            name for name, present in zip(
                ("--watcher-state", "--watcher-profile", "--watcher-snapshot-dir"), given
            ) if not present
        ]
        return [_item(
            "watcher-args", "BLOCKED", "watcher 参数必须全有或全无，缺少: " + ", ".join(missing),
            "补齐缺失参数或全部去掉以停用 Watcher",
        )]
    return [_item("watcher-args", "OK", "三个 watcher 参数齐全", "")]


def check_watcher_paths(
    state: Path, profile: Path, snapshot: Path, inbox: Path
) -> list[dict[str, Any]]:
    from umalib_ingest.nga_access.errors import NgaAccessError
    from umalib_ingest.watcher.state import WatcherConfig

    try:
        WatcherConfig(
            state_file=state, profile_dir=profile, snapshot_dir=snapshot,
            inbox_db=inbox,
        )
        file_paths = (state.resolve(), inbox.resolve())
        dir_paths = (profile.resolve(), snapshot.resolve())
        if file_paths[0] == file_paths[1] or any(
            f == d or d in f.parents for f in file_paths for d in dir_paths
        ):
            return [_item("watcher-paths", "BLOCKED", "state/Inbox 不得重合或位于 profile/快照目录中", "分别设置路径")]
    except NgaAccessError as exc:
        return [_item("watcher-paths", "BLOCKED", exc.message, "修正路径后重试")]
    items = [
        _item("watcher-paths", "OK", "state/profile/snapshot 路径边界合法", ""),
    ]
    return items


def check_state_file(state: Path) -> dict[str, Any]:
    from umalib_ingest.nga_access.errors import NgaAccessError
    from umalib_ingest.watcher.state import load_state_snapshot

    if not state.exists():
        return _item(
            "watcher-state", "BLOCKED", f"state 文件不存在: {state}",
            "先运行 baseline-from-snapshot 建立基线，或提供正确的 --watcher-state",
        )
    try:
        loaded, sha = load_state_snapshot(state)
        return _item(
            "watcher-state", "OK",
            f"可读取；tid={loaded.tid} last_page={loaded.last_page} known_floors={len(loaded.known_floors)} sha={sha[:12]}…",
            "",
        )
    except NgaAccessError as exc:
        return _item("watcher-state", "BLOCKED", exc.message, "检查 state 文件内容或恢复备份")
    except OSError as exc:
        return _item("watcher-state", "BLOCKED", f"无法读取: {exc}", "检查文件权限")


def check_inbox(inbox: Path) -> dict[str, Any]:
    from umalib_ingest.inbox.repository import InboxRepository, SUPPORTED_SCHEMA_VERSION
    from umalib_ingest.inbox.errors import UnsupportedInboxSchemaError

    if not inbox.exists():
        return _item(
            "inbox-db", "BLOCKED", f"Inbox 不存在: {inbox}",
            "用审核台 --init 创建，或提供已有 Inbox 路径",
        )
    try:
        before = _database_evidence(inbox)
        conn = sqlite3.connect(_readonly_uri(inbox), uri=True)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if version == SUPPORTED_SCHEMA_VERSION:
                # Reuse the repository's pure schema validator on OUR read-only
                # connection. Never call initialize or its write-capable opener.
                InboxRepository(inbox)._ensure_initialized(conn)
        finally:
            conn.close()
        if _database_evidence(inbox) != before:
            raise ValueError("检查期间数据库或目录发生变化，请关闭其他使用者后重试")
    except (sqlite3.DatabaseError, UnsupportedInboxSchemaError, OSError, ValueError) as exc:
        return _item("inbox-db", "BLOCKED", f"Inbox 检查未通过: {exc}", "检查路径、Schema 和文件状态")
    if version == SUPPORTED_SCHEMA_VERSION:
        return _item("inbox-db", "OK", f"合法 Inbox（完整 Schema，user_version={version}）", "")
    if version == 0:
        if tables:
            return _item("inbox-db", "BLOCKED", "user_version=0 但含用户表，不是可初始化的空 Inbox", "换用正确的 Inbox")
        return _item(
            "inbox-db", "WARN", "Inbox 尚未初始化（user_version=0）",
            "审核台带 --init 启动会创建表结构",
        )
    return _item(
        "inbox-db", "BLOCKED", f"不支持的 Inbox 版本（user_version={version}）",
        "换用正确的 Inbox 数据库",
    )


def check_umalib_db(umalib: Path | None) -> list[dict[str, Any]]:
    if umalib is None:
        return []
    from umalib_ingest.umalib_adapter.inspector import inspect_umalib_database

    if not umalib.exists():
        return [_item("umalib-db", "BLOCKED", f"源库不存在: {umalib}", "提供正确的 --umalib-db")]
    try:
        before = _database_evidence(umalib)
        report = inspect_umalib_database(umalib)
        if _database_evidence(umalib) != before:
            raise ValueError("检查期间数据库发生变化")
    except Exception as exc:
        return [_item("umalib-db", "BLOCKED", f"只读检查失败: {type(exc).__name__}: {exc}", "检查文件")]
    counts = report.counts
    blockers = [i for i in report.issues if i.severity == "blocker"]
    status = "BLOCKED" if blockers else "OK"
    message = f"只读检查完成；counts={json.dumps(counts, ensure_ascii=False)}"
    next_step = "存在阻断问题，先处理 inspection 报告" if blockers else ""
    return [_item("umalib-db", status, message, next_step)]


def run_checks(
    inbox: Path,
    watcher_state: Path | None,
    watcher_profile: Path | None,
    watcher_snapshot: Path | None,
    umalib_db: Path | None,
) -> tuple[list[dict[str, Any]], str]:
    items: list[dict[str, Any]] = []
    items.append(check_python_version())
    items.append(check_project_import())
    items.append(check_playwright())
    if watcher_state and watcher_profile and watcher_snapshot and items[-1]["status"] != "OK":
        items[-1]["status"] = "BLOCKED"
    items.extend(check_watcher_args(watcher_state, watcher_profile, watcher_snapshot))
    if watcher_state and watcher_profile and watcher_snapshot:
        items.extend(check_watcher_paths(watcher_state, watcher_profile, watcher_snapshot, inbox))
        items.append(check_state_file(watcher_state))
    items.append(check_inbox(inbox))
    items.extend(check_umalib_db(umalib_db))
    items.append(_item("login", "INFO", "本次未验证登录（离线检查，不读取 profile）", ""))
    overall = "BLOCKED" if any(i["status"] == "BLOCKED" for i in items) else "OK"
    return items, overall


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -B tools/check_local_setup.py",
        description="只读检查本地审核台能否启动（不写任何文件）",
    )
    parser.add_argument("--inbox-db", type=Path, default=None, help="Inbox SQLite 路径")
    parser.add_argument("--watcher-state", type=Path, default=None, help="Watcher state 文件")
    parser.add_argument("--watcher-profile", type=Path, default=None, help="Watcher profile 目录")
    parser.add_argument("--watcher-snapshot-dir", type=Path, default=None, help="Watcher 快照目录")
    parser.add_argument("--umalib-db", type=Path, default=None, help="可选：UmaLib 源库只读摘要")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.inbox_db is None:
        build_parser().print_usage()
        print("错误: 必须显式提供 --inbox-db（其余参数可选）", file=sys.stderr)
        return EXIT_BLOCKED
    try:
        items, overall = run_checks(
            args.inbox_db, args.watcher_state, args.watcher_profile,
            args.watcher_snapshot_dir, args.umalib_db,
        )
    except Exception as exc:
        payload = {"overall": "INTERNAL", "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(payload, ensure_ascii=False) if args.json else f"内部错误: {exc}")
        return EXIT_INTERNAL
    report = {"overall": overall, "items": items}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in items:
            line = f"[{item['status']}] {item['name']}: {item['message']}"
            print(line)
            if item.get("next_step"):
                print(f"    → {item['next_step']}")
        print(f"结论: {'可以启动' if overall == 'OK' else '存在阻断项，不能启动'}")
    return EXIT_OK if overall == "OK" else EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
