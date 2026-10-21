"""Profile and snapshot path safety validation (TASK-010, pure)."""

from __future__ import annotations

import os
from pathlib import Path

from umalib_ingest.nga_access.errors import NgaAccessError
from umalib_ingest.local_paths import private_state_root

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_EDGE_USER_DATA_HINTS = (
    "Microsoft\\Edge\\User Data",
    "Microsoft\\Edge Beta\\User Data",
    "Microsoft\\Edge Dev\\User Data",
)


def _reject(message: str) -> NgaAccessError:
    return NgaAccessError("unsafe_profile_path", message)


def _snapshot_reject(message: str) -> NgaAccessError:
    return NgaAccessError("unsafe_snapshot_path", message)


def _common_checks(path: Path) -> Path | None:
    """Return an error message when the path is structurally unsafe."""
    if not isinstance(path, Path):
        return "路径必须是 Path"
    if not path.is_absolute():
        return "路径必须是绝对路径，不接受隐式当前目录"
    text = str(path)
    if "~" in text:
        return "路径不得包含 ~（不展开用户主目录）"
    if "$" in text or "%" in text:
        return "路径不得包含环境变量展开形式"
    if path.is_symlink():
        return "路径不得是符号链接"
    try:
        if path.resolve() != path:
            return "路径组件包含符号链接"
    except OSError:
        return "路径无法解析"
    if path.parent == path:
        return "路径不得是盘符根目录"
    return None


def _in_private_state(path: Path) -> bool:
    """Return whether the path lives under the authorized private-state root."""
    return path.is_relative_to(private_state_root().resolve())


def validate_profile_dir(path: str | Path) -> Path:
    """Validate the independent persistent profile directory boundary."""
    profile = Path(path)
    problem = _common_checks(profile)
    if problem:
        raise _reject(problem)
    if profile.is_relative_to(PROJECT_ROOT):
        raise _reject("profile 不得位于项目目录内")
    home = Path.home()
    if profile.is_relative_to(home):
        raise _reject("profile 不得位于用户主目录（含日常 Edge 默认目录）")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data and profile.is_relative_to(Path(local_app_data)):
        raise _reject("profile 不得位于 LOCALAPPDATA（含日常 Edge User Data）")
    for hint in _EDGE_USER_DATA_HINTS:
        if hint.lower() in str(profile).lower():
            raise _reject("profile 不得指向日常 Edge User Data 目录")
    if not _in_private_state(profile):
        raise _reject(f"profile 必须位于独立数据目录 {private_state_root()} 下")
    return profile


def validate_snapshot_dir(path: str | Path) -> Path:
    """Validate the explicit snapshot output directory boundary."""
    snapshot = Path(path)
    problem = _common_checks(snapshot)
    if problem:
        raise _snapshot_reject(problem)
    if snapshot.is_relative_to(PROJECT_ROOT):
        raise _snapshot_reject("snapshot 目录不得位于项目目录内")
    home = Path.home()
    if snapshot.is_relative_to(home):
        raise _snapshot_reject("snapshot 目录不得位于用户主目录")
    if not _in_private_state(snapshot):
        raise _snapshot_reject(f"snapshot 目录必须位于独立数据目录 {private_state_root()} 下")
    return snapshot


def ensure_distinct(profile: Path, snapshot: Path) -> None:
    """Reject profile and snapshot pointing at the same directory."""
    if profile == snapshot:
        raise _reject("profile 目录不得与 snapshot 目录相同")
