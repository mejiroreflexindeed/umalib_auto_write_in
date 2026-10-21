"""TASK-014：operator 路径解析与守卫（从 operator root/workspace/tid 计算）。"""

from __future__ import annotations

from pathlib import Path

from umalib_ingest.local_paths import operator_root
from umalib_ingest.operator.models import JsonValidationError, validate_tid
from umalib_ingest.workspace.paths import WorkspacePaths

DEFAULT_OPERATOR_ROOT = operator_root()


def default_root() -> Path:
    return operator_root()


class OperatorPaths:
    """Path resolver for the operator product data root."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.workspaces_dir = self.root / "workspaces"
        self.launcher_json = self.root / "launcher.json"

    def ensure_root(self) -> None:
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

    def workspace_paths(self, workspace_id: str) -> WorkspacePaths:
        # 与 TASK-013 布局一致：WorkspacePaths(root) 的 workspace 目录为
        # root/workspaces/<id>；threads/ 也挂在该目录下。
        return WorkspacePaths(self.root)

    def inbox_db(self, workspace_id: str) -> Path:
        return self._ws(workspace_id) / "inbox.db"

    def threads_dir(self, workspace_id: str) -> Path:
        return self._ws(workspace_id) / "threads"

    def thread_dir(self, workspace_id: str, tid: str) -> Path:
        validate_tid(tid)
        return self.threads_dir(workspace_id) / tid

    def source_binding(self, workspace_id: str) -> Path:
        return self._ws(workspace_id) / "source-binding.json"

    def index_json(self, workspace_id: str) -> Path:
        return self.threads_dir(workspace_id) / "index.json"

    def config_json(self, workspace_id: str, tid: str) -> Path:
        return self.thread_dir(workspace_id, tid) / "config.json"

    def state_json(self, workspace_id: str, tid: str) -> Path:
        return self.thread_dir(workspace_id, tid) / "state.json"

    def snapshots_dir(self, workspace_id: str, tid: str) -> Path:
        return self.thread_dir(workspace_id, tid) / "snapshots"

    def runs_dir(self, workspace_id: str, tid: str) -> Path:
        return self.thread_dir(workspace_id, tid) / "runs"

    def run_json(self, workspace_id: str, tid: str, run_id: str) -> Path:
        return self.runs_dir(workspace_id, tid) / f"{run_id}.json"

    def _ws(self, workspace_id: str) -> Path:
        # workspace_id 为规范 UUID，复用 WorkspacePaths 校验
        from umalib_ingest.workspace.paths import validate_workspace_id

        validate_workspace_id(workspace_id)
        return self.workspaces_dir / workspace_id
