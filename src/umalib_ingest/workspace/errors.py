"""TASK-013 工作区错误类型（M02）。"""

from __future__ import annotations


class WorkspaceError(Exception):
    """Base class for all workspace errors."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class WorkspaceNotFoundError(WorkspaceError):
    """The workspace id does not exist."""

    def __init__(self, workspace_id: str) -> None:
        super().__init__("workspace_not_found", f"工作区不存在: {workspace_id}")
        self.workspace_id = workspace_id


class WorkspaceLockedError(WorkspaceError):
    """Another process holds the workspace instance lock."""

    def __init__(self, workspace_id: str) -> None:
        super().__init__("workspace_locked", "工作区已被另一个进程打开")
        self.workspace_id = workspace_id


class WorkspaceManifestError(WorkspaceError):
    """The workspace manifest JSON is invalid or corrupted."""

    def __init__(self, message: str) -> None:
        super().__init__("workspace_manifest_invalid", message)


class WorkspaceConflictError(WorkspaceError):
    """A compare-and-swap revision or fingerprint mismatch."""

    def __init__(self, message: str = "工作区版本已变化，请刷新后重试") -> None:
        super().__init__("workspace_conflict", message)


class WorkspaceBusyError(WorkspaceError):
    """Maintenance or an active mutation blocks the request."""

    def __init__(self, message: str = "工作区正在维护或存在活动操作，请稍后重试") -> None:
        super().__init__("workspace_busy", message)


class OperationConflictError(WorkspaceError):
    """The operation id already exists with a different request fingerprint."""

    def __init__(self, operation_id: str) -> None:
        super().__init__("operation_conflict", "操作已存在但请求内容不同，不能重复执行")
        self.operation_id = operation_id


class RecoveryRequiredError(WorkspaceError):
    """A previous operation needs explicit recovery before new writes."""

    def __init__(self, operation_id: str) -> None:
        super().__init__("recovery_required", "存在需要先恢复的未完成操作")
        self.operation_id = operation_id


class CheckpointError(WorkspaceError):
    """Checkpoint create/restore failed."""

    def __init__(self, message: str) -> None:
        super().__init__("checkpoint_error", message)
