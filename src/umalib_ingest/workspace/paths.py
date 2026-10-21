"""TASK-013 工作区路径解析与安全（M02）。

工作区根由启动器配置（环境变量或参数），不在 HTTP 层接受路径。所有子路径
从工作区 UUID 构造并校验：拒绝遍历、绝对路径注入与 junction 越界。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from umalib_ingest.workspace.errors import WorkspaceError, WorkspaceManifestError

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_MODE_ALIASES: dict[str, Any] = {
    "synthetic": {"source_kind": "synthetic"},
    "real_copy": {"source_kind": "main-copy"},
}


def validate_workspace_id(value: str) -> str:
    """Return the id when it is a canonical UUID, else raise."""
    if not isinstance(value, str) or _UUID_RE.fullmatch(value) is None:
        raise WorkspaceManifestError(f"非法工作区 ID: {value!r}")
    return value.lower()


def validate_mode(mode: str) -> str:
    """Return the mode when it is a known server-side alias, else raise."""
    if mode not in _MODE_ALIASES:
        raise WorkspaceError("invalid_mode", f"不支持的 mode: {mode!r}")
    return mode


def source_alias_for_mode(mode: str) -> str:
    """Map a validated mode to its base source alias."""
    return _MODE_ALIASES[mode]["source_kind"]


def mode_of_source_alias(alias: str) -> str:
    """Inverse map: source alias -> mode (server-side only)."""
    for mode, meta in _MODE_ALIASES.items():
        if meta["source_kind"] == alias:
            return mode
    raise WorkspaceError("invalid_source_alias", f"不支持的 source alias: {alias!r}")


class WorkspacePaths:
    """Path resolver for one demo root; every child path is verified."""

    def __init__(self, demo_root: Path) -> None:
        self.demo_root = demo_root.resolve()
        self.workspaces_dir = self.demo_root / "workspaces"
        self.launcher_json = self.demo_root / "launcher.json"

    def ensure_root(self) -> None:
        """Create the demo root structure (not per-workspace data)."""
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

    def workspace_dir(self, workspace_id: str) -> Path:
        validate_workspace_id(workspace_id)
        target = (self.workspaces_dir / workspace_id).resolve()
        self._guard(target, kind="工作区目录")
        return target

    def _guard(self, target: Path, kind: str) -> None:
        try:
            relative = target.relative_to(self.workspaces_dir)
        except ValueError:
            raise WorkspaceManifestError(f"{kind}越界: {target}") from None
        if relative.parts and relative.parts[0] == "..":
            raise WorkspaceManifestError(f"{kind}越界: {target}")

    def manifest_path(self, workspace_id: str) -> Path:
        return self.workspace_dir(workspace_id) / "workspace.json"

    def sub_path(self, workspace_id: str, *parts: str) -> Path:
        """Resolve a workspace-owned child path; rejects escapes."""
        base = self.workspace_dir(workspace_id)
        candidate = base.joinpath(*parts)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(base.resolve())
        except ValueError:
            raise WorkspaceManifestError(f"工作区子路径越界: {parts!r}") from None
        return resolved

    # 固定子路径
    def inbox_db(self, workspace_id: str) -> Path:
        return self.sub_path(workspace_id, "inbox.db")

    def base_db(self, workspace_id: str) -> Path:
        return self.sub_path(workspace_id, "base.db")

    def choices_dir(self, workspace_id: str) -> Path:
        return self.sub_path(workspace_id, "choices")

    def operations_dir(self, workspace_id: str) -> Path:
        return self.sub_path(workspace_id, "operations")

    def candidates_dir(self, workspace_id: str) -> Path:
        return self.sub_path(workspace_id, "candidates")

    def deliveries_dir(self, workspace_id: str) -> Path:
        return self.sub_path(workspace_id, "deliveries")

    def checkpoints_dir(self, workspace_id: str) -> Path:
        return self.sub_path(workspace_id, "checkpoints")

    def watcher_state(self, workspace_id: str) -> Path:
        return self.sub_path(workspace_id, "watcher", "state.json")

    def watcher_snapshots(self, workspace_id: str) -> Path:
        return self.sub_path(workspace_id, "watcher", "snapshots")

    def lock_file(self, workspace_id: str) -> Path:
        return self.sub_path(workspace_id, "instance.lock")

    def provenance_file(self, workspace_id: str) -> Path:
        return self.sub_path(workspace_id, "restore-provenance.json")


def default_demo_root() -> Path:
    """Return the configured demo root (env override for tests)."""
    env = os.environ.get("UMALIB_DEMO_ROOT")
    if env:
        return Path(env)
    from umalib_ingest.local_paths import private_state_root

    return private_state_root() / "demo-user"
