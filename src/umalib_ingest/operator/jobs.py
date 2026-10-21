"""TASK-014 W05：单 worker 网络任务队列、run 状态机、profile 独占（3.3.3/7）。"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from umalib_ingest.operator.models import (
    JsonValidationError,
    RunRecord,
    canonical_json,
    utc_now,
)

_TERMINAL = ("succeeded", "partial", "cancelled", "failed", "interrupted")


class RunController:
    """Injected into a task worker for status/cancel/login coordination."""

    def __init__(self, service: "NetworkJobService", run_id: str) -> None:
        self._service = service
        self._run_id = run_id
        self._login_event = threading.Event()

    @property
    def run_id(self) -> str:
        return self._run_id

    def set_status(self, status: str, *, result=None, error=None) -> None:
        self._service._set_status(self._run_id, status, result=result, error=error)

    def update_progress(self, **fields: Any) -> None:
        self._service._update_progress(self._run_id, **fields)

    def check_cancel(self) -> None:
        if self._service._is_cancelled(self._run_id):
            raise CancelledError()

    def wait_login(self, timeout_seconds: float) -> bool:
        """Block until login-confirm or cancel; returns True on confirm."""
        deadline = timeout_seconds
        while deadline > 0:
            if self._service._is_cancelled(self._run_id):
                raise CancelledError()
            if self._service._is_confirmed(self._run_id):
                return True
            self._login_event.wait(0.5)
            deadline -= 0.5
        return False

    def login_required(self) -> None:
        self._service._set_status(self._run_id, "awaiting_login")

    def notify_login(self) -> None:
        self._login_event.set()


class CancelledError(Exception):
    pass


class ProfileBusyError(Exception):
    """共享浏览器 profile 正被另一工作区/进程占用。"""


def _acquire_profile_file_lock(path):
    """跨进程非阻塞文件锁；失败抛 ProfileBusyError。进程退出由 OS 释放。"""
    import os
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ProfileBusyError(str(path)) from None
    return handle


def _release_profile_file_lock(handle) -> None:
    import os

    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


class NetworkJobService:
    """One network worker for the whole app; one profile across threads."""

    def __init__(self, persist: Callable[[RunRecord], None],
                 profile_lock_path=None) -> None:
        self._persist = persist
        self._profile_lock_path = profile_lock_path
        self._queue: "queue.Queue[str]" = __import__("queue").Queue()
        self._records: dict[str, RunRecord] = {}
        self._by_tid: dict[tuple[str, str], str] = {}
        self._confirmed: set[str] = set()
        self._cancelled: set[str] = set()
        self._executors: dict[str, Callable] = {}
        self._lock = threading.RLock()
        self._profile_lock = threading.Lock()
        self._accepting = True
        self._worker: threading.Thread | None = None
        self._active_run: str | None = None

    def register_executor(self, kind: str, fn: Callable) -> None:
        self._executors[kind] = fn

    def start(self) -> None:
        self._worker = threading.Thread(target=self._loop, daemon=True, name="operator-jobs")
        self._worker.start()

    def submit(self, workspace_id: str, tid: str, kind: str,
               request: dict[str, Any]) -> str:
        import queue

        with self._lock:
            if not self._accepting:
                raise RuntimeError("服务已关闭")
            existing = next(
                (record for record in self._records.values()
                 if record.status not in _TERMINAL), None)
            if existing is not None:
                raise BusyError(
                    f"已有网络任务正在运行（tid={existing.tid}，status={existing.status}）")
            run_id = str(uuid.uuid4())
            record = RunRecord(
                schema_version="1", run_id=run_id, workspace_id=workspace_id,
                tid=tid, kind=kind, status="queued", request=request,
                progress={"page": None, "observed_last_page": None, "navigations": 0},
                result=None, error=None, created_at=utc_now(), updated_at=utc_now(),
            )
            self._records[run_id] = record
            self._by_tid[(workspace_id, tid)] = run_id
            self._persist(record)
            self._queue.put(run_id)
            return run_id

    def restore(self, records: list[RunRecord]) -> None:
        """Restore persisted history; interrupted non-terminal work is not resumed."""
        with self._lock:
            for record in records:
                restored = record
                if record.status not in _TERMINAL:
                    restored = RunRecord(
                        schema_version=record.schema_version, run_id=record.run_id,
                        workspace_id=record.workspace_id, tid=record.tid,
                        kind=record.kind, status="interrupted", request=record.request,
                        progress=record.progress, result=record.result,
                        error={"code": "interrupted",
                               "message": "程序上次退出时任务尚未完成，请人工重试",
                               "retryable": True},
                        created_at=record.created_at, updated_at=utc_now(),
                    )
                    self._persist(restored)
                self._records[restored.run_id] = restored

    def _loop(self) -> None:
        import queue

        while True:
            try:
                run_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                if not self._accepting and self._queue.empty():
                    return
                continue
            with self._lock:
                record = self._records.get(run_id)
            if record is None or record.status in _TERMINAL:
                continue
            with self._profile_lock:
                profile_handle = None
                if self._profile_lock_path is not None:
                    try:
                        profile_handle = _acquire_profile_file_lock(self._profile_lock_path)
                    except ProfileBusyError:
                        self._set_status(run_id, "failed", error={
                            "code": "profile_busy",
                            "message": "共享浏览器 profile 正被另一个工作区或进程使用；"
                                       "请先关闭其他录入助手再试",
                            "retryable": True,
                        })
                        with self._lock:
                            self._by_tid.pop((record.workspace_id, record.tid), None)
                        continue
                with self._lock:
                    self._active_run = run_id
                    self._set_status_locked(run_id, "running")
                executor = self._executors.get(record.kind)
                try:
                    if executor is None:
                        self._set_status(run_id, "failed",
                                         error={"code": "unknown_kind",
                                                "message": "未知任务类型", "retryable": False})
                    else:
                        controller = RunController(self, run_id)
                        executor(record, controller)
                except CancelledError:
                    self._set_status(run_id, "cancelled",
                                     error={"code": "cancelled", "message": "已取消",
                                            "retryable": True})
                except Exception as exc:
                    self._set_status(run_id, "failed",
                                     error={"code": getattr(exc, "code", "job_error"),
                                            "message": str(exc)[:200], "retryable": True})
                finally:
                    if profile_handle is not None:
                        _release_profile_file_lock(profile_handle)
                    with self._lock:
                        self._active_run = None
                        self._by_tid.pop((record.workspace_id, record.tid), None)

    # ---- status helpers ----

    def _set_status(self, run_id: str, status: str, *, result=None, error=None) -> None:
        with self._lock:
            self._set_status_locked(run_id, status, result=result, error=error)

    def _set_status_locked(self, run_id: str, status: str, *, result=None,
                           error=None) -> None:
        record = self._records.get(run_id)
        if record is None:
            return
        if record.status in _TERMINAL:
            return  # 终止状态不可被迟到回调改回
        if record.status == "cancelling" and status not in _TERMINAL:
            return
        updated = RunRecord(
            schema_version=record.schema_version, run_id=record.run_id,
            workspace_id=record.workspace_id, tid=record.tid, kind=record.kind,
            status=status, request=record.request, progress=record.progress,
            result=result, error=error,
            created_at=record.created_at, updated_at=utc_now(),
        )
        self._records[run_id] = updated
        self._persist(updated)

    def _update_progress(self, run_id: str, **fields: Any) -> None:
        with self._lock:
            record = self._records.get(run_id)
            if record is None or record.status in _TERMINAL:
                return
            progress = {**record.progress, **fields}
            updated = RunRecord(
                schema_version=record.schema_version, run_id=record.run_id,
                workspace_id=record.workspace_id, tid=record.tid, kind=record.kind,
                status=record.status, request=record.request, progress=progress,
                result=record.result, error=record.error,
                created_at=record.created_at, updated_at=utc_now(),
            )
            self._records[run_id] = updated
            self._persist(updated)

    def _is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancelled

    def _is_confirmed(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._confirmed

    # ---- HTTP-facing ----

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._records.get(run_id)

    def cancel(self, run_id: str) -> RunRecord | None:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                return None
            if record.status in _TERMINAL:
                return record
            self._cancelled.add(run_id)
            if record.status == "awaiting_login":
                controller = getattr(self, "_controllers", {}).get(run_id)
                if controller is not None:
                    controller.notify_login()
            self._set_status_locked(run_id, "cancelling")
            return self._records.get(run_id)

    def login_confirm(self, run_id: str) -> RunRecord | None:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                return None
            if record.status != "awaiting_login":
                raise NotAwaitingError("当前没有等待登录确认的任务")
            self._confirmed.add(run_id)
            controller = getattr(self, "_controllers", {}).get(run_id)
            if controller is not None:
                controller.notify_login()
            return record

    def active_run(self) -> RunRecord | None:
        with self._lock:
            if self._active_run:
                return self._records.get(self._active_run)
            return None

    def shutdown(self) -> None:
        with self._lock:
            self._accepting = False
        for run_id in list(self._records):
            self._cancelled.add(run_id)
        if self._worker is not None:
            self._worker.join(timeout=30)


class BusyError(Exception):
    pass


class NotAwaitingError(Exception):
    pass
