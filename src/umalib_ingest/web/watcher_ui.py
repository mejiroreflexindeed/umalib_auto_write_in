"""Watcher UI service for the local review console (TASK-011R+012)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from umalib_ingest.nga_access.errors import NgaAccessError
from umalib_ingest.watcher.scan import run_scan
from umalib_ingest.watcher.state import (
    WatcherConfig,
    load_state_snapshot,
    state_file_sha256,
)

DEFAULT_LOGIN_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_SCAN_PAGES = 12


@dataclass(frozen=True)
class WatcherUiConfig:
    """CLI-fixed Watcher paths; never accepted from HTTP requests."""

    state_file: Path | None
    profile_dir: Path | None
    snapshot_dir: Path | None
    inbox_db: Path | None
    login_timeout_seconds: float = DEFAULT_LOGIN_TIMEOUT_SECONDS
    max_scan_pages: int = DEFAULT_MAX_SCAN_PAGES

    @property
    def enabled(self) -> bool:
        return (
            self.state_file is not None
            and self.profile_dir is not None
            and self.snapshot_dir is not None
        )


class WatcherUiError(Exception):
    """A stable, safe error to map onto an HTTP response."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _invalid(message: str) -> WatcherUiError:
    return WatcherUiError(400, "watcher_invalid_request", message)


class WatcherUiService:
    """Orchestrates one-at-a-time Watcher scans for the web UI."""

    def __init__(
        self,
        config: WatcherUiConfig,
        *,
        runner: Callable[..., Any] = run_scan,
    ) -> None:
        self._config = config
        self._runner = runner
        self._scan_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._busy = False
        self._awaiting_login = False
        self._login_event = threading.Event()
        self._cancelled = False
        self._stopped = False
        self._last_result: dict[str, Any] | None = None
        self._last_error: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def config_dict(self) -> dict[str, Any]:
        """Minimal config view; no absolute paths."""
        if not self.enabled:
            return {"enabled": False}
        return {"enabled": True, "tid": 30099686}

    def status(self) -> dict[str, Any]:
        """Read-only status; never opens Edge/Inbox/network."""
        if not self.enabled:
            raise WatcherUiError(409, "watcher_not_configured", "启动时未配置 Watcher")
        try:
            state, sha = load_state_snapshot(self._config.state_file)
        except NgaAccessError as exc:
            raise WatcherUiError(409, exc.code, exc.message) from None
        floors = state.known_floors
        return {
            "enabled": True,
            "tid": state.tid,
            "last_page": state.last_page,
            "known_floor_count": len(floors),
            "triggered_count": sum(1 for f in floors.values() if f.trigger_found),
            "imported_count": sum(1 for f in floors.values() if f.inbox_item_id is not None),
            "changed_after_import_count": sum(
                1 for f in floors.values() if f.changed_after_import
            ),
            "baseline_created_at": state.baseline_created_at,
            "last_successful_scan_at": state.last_successful_scan_at,
            "state_sha256": sha,
            "busy": self._busy,
            "awaiting_login": self._awaiting_login,
            "last_result": self._last_result,
            "last_error": self._last_error,
        }

    def scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one scan; only one may run per process."""
        if self._stopped:
            raise WatcherUiError(
                409, "watcher_shutdown", "服务已关闭，请重启审核台后重新扫描"
            )
        if not self.enabled:
            raise WatcherUiError(409, "watcher_not_configured", "启动时未配置 Watcher")
        if not isinstance(payload, dict):
            raise _invalid("请求必须是 JSON 对象")
        unknown = set(payload) - {"dry_run", "confirm_import", "expected_state_sha256"}
        if unknown:
            raise _invalid(f"未知请求键: {sorted(unknown)}")
        dry_run = payload.get("dry_run", False)
        if type(dry_run) is not bool:
            raise _invalid("dry_run 必须是布尔值")
        confirm_import = payload.get("confirm_import", False)
        expected_sha = payload.get("expected_state_sha256")

        acquired = self._scan_lock.acquire(blocking=False)
        if not acquired:
            raise WatcherUiError(409, "watcher_scan_busy", "已有一个扫描正在运行")
        try:
            if not dry_run:
                if type(confirm_import) is not bool or confirm_import is not True:
                    raise WatcherUiError(
                        409, "watcher_confirm_required", "正式导入必须确认并携带当前 state SHA"
                    )
                if not isinstance(expected_sha, str) or len(expected_sha) != 64:
                    raise WatcherUiError(
                        409, "watcher_state_stale", "expected_state_sha256 缺失或格式非法"
                    )
                current_sha = state_file_sha256(self._config.state_file)
                if expected_sha != current_sha:
                    raise WatcherUiError(
                        409,
                        "watcher_state_stale",
                        "state 已变化，请刷新状态后重新确认",
                    )
            with self._lifecycle_lock:
                if self._stopped:
                    raise WatcherUiError(409, "watcher_shutdown", "服务已关闭，请重启审核台后重新扫描")
                self._busy = True
                self._awaiting_login = False
                self._login_event.clear()
                # Cancellation is monotonic for this service instance. Only a
                # fresh instance can run again after shutdown.
                self._last_result = None
                self._last_error = None
            config = WatcherConfig(
                state_file=self._config.state_file,
                profile_dir=self._config.profile_dir,
                snapshot_dir=self._config.snapshot_dir,
                inbox_db=self._config.inbox_db,
                navigation_delay_seconds=3.0,
                max_scan_pages=self._config.max_scan_pages,
            )
            try:
                result = self._runner(
                    config,
                    dry_run=dry_run,
                    confirm_login=self._confirm_callback,
                    login_wait_timeout_seconds=self._config.login_timeout_seconds,
                    expected_state_sha256=expected_sha if not dry_run else None,
                    should_cancel=lambda: self._cancelled,
                )
            except NgaAccessError as exc:
                self._last_error = {"code": exc.code, "message": exc.message}
                raise WatcherUiError(409, exc.code, exc.message) from None
            self._last_result = self._result_dto(result)
            return self._last_result
        except WatcherUiError as exc:
            self._last_error = {"code": exc.code, "message": exc.message}
            raise
        except Exception:
            self._last_error = {"code": "watcher_scan_failed", "message": "扫描未完成，请核对 Inbox 后重试"}
            raise WatcherUiError(500, "watcher_scan_failed", self._last_error["message"]) from None
        finally:
            self._awaiting_login = False
            self._login_event.clear()
            self._busy = False
            self._scan_lock.release()

    def _confirm_callback(self) -> None:
        """Called by the runner when manual login is required on the official page.

        A service shutdown also wakes this waiter, but then raises a stable
        cancel error instead of treating the wake-up as a login confirmation.
        """
        with self._lifecycle_lock:
            if self._cancelled:
                raise NgaAccessError("watcher_shutdown", "服务关闭，扫描已取消")
            self._awaiting_login = True
        confirmed = self._login_event.wait(timeout=self._config.login_timeout_seconds)
        self._awaiting_login = False
        if self._cancelled:
            raise NgaAccessError("watcher_shutdown", "服务关闭，扫描已取消（不等于登录确认）")
        if not confirmed:
            raise NgaAccessError(
                "login_not_confirmed",
                "等待用户在官方 NGA 页面完成登录超时；请确认后重试。",
            )

    def login_confirm(self) -> dict[str, Any]:
        """Set the one-shot manual login confirmation event."""
        with self._lifecycle_lock:
            return self._login_confirm_locked()

    def _login_confirm_locked(self) -> dict[str, Any]:
        if not self.enabled:
            raise WatcherUiError(409, "watcher_not_configured", "启动时未配置 Watcher")
        if not self._awaiting_login:
            raise WatcherUiError(
                409, "watcher_not_awaiting_login", "当前没有等待登录确认的扫描"
            )
        if self._cancelled:
            raise WatcherUiError(
                409, "watcher_shutdown", "服务已关闭，扫描已取消"
            )
        self._awaiting_login = False
        self._login_event.set()
        return {"ok": True}

    def shutdown(self) -> None:
        """Cancel any active scan and release the login waiter (idempotent).

        Never interpreted as a login confirmation: the waiting callback raises
        watcher_shutdown and the scan does not retry or advance state. After
        shutdown the service refuses new scans; a fresh service instance is
        required to scan again.
        """
        with self._lifecycle_lock:
            self._cancelled = True
            self._stopped = True
            self._login_event.set()

    @staticmethod
    def _result_dto(result: Any) -> dict[str, Any]:
        return {
            "mode": result.mode,
            "tid": result.tid,
            "previous_last_page": result.previous_last_page,
            "current_last_page": result.current_last_page,
            "scanned_pages": list(result.scanned_pages),
            "new_floor_count": result.new_floor_count,
            "unchanged_floor_count": result.unchanged_floor_count,
            "changed_floor_count": result.changed_floor_count,
            "discussion_count": result.discussion_count,
            "triggered_count": result.triggered_count,
            "imported_items": [
                {"item_id": item.item_id, "status": item.status}
                for item in result.imported_items
            ],
            "changed_after_import": list(result.changed_after_import),
            "state_before_sha256": result.state_before_sha256,
            "state_after_sha256": result.state_after_sha256,
            "dry_run": result.dry_run,
            "diagnostics": list(result.diagnostics),
        }
