"""TASK-014 W03：帖子注册表（threads/index.json + config.json）加锁读写/CAS。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from umalib_ingest.operator.models import (
    JsonValidationError,
    ThreadConfig,
    ThreadEntry,
    ThreadIndex,
    ThreadStart,
    canonical_json,
    utc_now,
)
from umalib_ingest.operator.paths import OperatorPaths


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    import os
    import tempfile

    payload = canonical_json(data).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".op-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, str(path))
    except BaseException:
        try:
            Path(temp_name).unlink()
        except OSError:
            pass
        raise


class ThreadRegistry:
    """加锁读取/CAS 写入 threads/index.json 与各帖 config.json。"""

    def __init__(self, paths: OperatorPaths, workspace_id: str) -> None:
        self._paths = paths
        self._workspace_id = workspace_id
        self._lock = threading.RLock()

    def _index_path(self) -> Path:
        return self._paths.index_json(self._workspace_id)

    def _config_path(self, tid: str) -> Path:
        return self._paths.config_json(self._workspace_id, tid)

    def read_index(self) -> ThreadIndex:
        import json

        with self._lock:
            path = self._index_path()
            if not path.exists():
                return ThreadIndex(schema_version="1", revision=0, active_tid=None,
                                   entries=())
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise JsonValidationError("threads/index.json 损坏")
            return ThreadIndex.from_dict(data)

    def write_index(self, index: ThreadIndex) -> None:
        with self._lock:
            self._index_path().parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._index_path(), index.to_dict())

    def add_thread(self, url: str, name: str | None,
                   expected_revision: int) -> ThreadEntry:
        """Add a thread by URL; duplicate tid returns the existing entry (no reset)."""
        from umalib_ingest.nga_access.urls import parse_thread_url

        with self._lock:
            index = self.read_index()
            if index.revision != expected_revision:
                raise ConflictError("threads/index 已变化，请刷新")
            try:
                tid, _ = parse_thread_url(url)
            except Exception:
                raise ValueError("不是合法的 NGA 帖子 URL")
            tid_str = str(tid)
            for entry in index.entries:
                if entry.tid == tid_str:
                    return entry
            canonical = f"https://bbs.nga.cn/read.php?tid={tid_str}"
            now = utc_now()
            entry = ThreadEntry(
                tid=tid_str, name=(name or f"目标帖子 {tid_str}")[:80],
                enabled=True, canonical_url=canonical, config_revision=0,
                created_at=now, updated_at=now,
            )
            config = ThreadConfig(
                schema_version="1", revision=0, tid=tid_str, canonical_url=canonical,
                name=entry.name, enabled=True,
                start=ThreadStart(mode="now", page=None, date=None,
                                  timezone="UTC+08:00", lower_bound_utc=None),
                start_confirmed=False, initialized=False,
                created_at=now, updated_at=now,
            )
            self._config_path(tid_str).parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._config_path(tid_str), config.to_dict())
            new_index = ThreadIndex(
                schema_version="1", revision=index.revision + 1,
                active_tid=index.active_tid or tid_str, entries=(*index.entries, entry),
            )
            self.write_index(new_index)
            return entry

    def read_config(self, tid: str) -> ThreadConfig:
        import json

        with self._lock:
            path = self._config_path(tid)
            if not path.exists():
                raise KeyError(tid)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise JsonValidationError(f"threads/{tid}/config.json 损坏")
            return ThreadConfig.from_dict(data)

    def write_config(self, config: ThreadConfig) -> None:
        with self._lock:
            self._config_path(config.tid).parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._config_path(config.tid), config.to_dict())

    def cas_config(self, tid: str, expected_revision: int,
                   new_config: ThreadConfig) -> ThreadConfig:
        with self._lock:
            current = self.read_config(tid)
            if current.revision != expected_revision:
                raise ConflictError("config 已变化，请刷新")
            self.write_config(new_config)
            return new_config

    def patch_thread(self, tid: str, expected_revision: int, *, name: str | None = None,
                     enabled: bool | None = None, make_active: bool | None = None) -> ThreadIndex:
        with self._lock:
            index = self.read_index()
            if index.revision != expected_revision:
                raise ConflictError("threads/index 已变化，请刷新")
            entries = list(index.entries)
            active = index.active_tid
            changed = False
            for position, entry in enumerate(entries):
                if entry.tid != tid:
                    continue
                config = self.read_config(tid)
                new_name = name[:80] if name is not None else entry.name
                new_enabled = enabled if enabled is not None else entry.enabled
                config_changed = new_name != entry.name or new_enabled != entry.enabled
                if config_changed:
                    now = utc_now()
                    new_config = ThreadConfig(
                        schema_version=config.schema_version,
                        revision=config.revision + 1,
                        tid=config.tid, canonical_url=config.canonical_url,
                        name=new_name, enabled=new_enabled, start=config.start,
                        start_confirmed=config.start_confirmed,
                        initialized=config.initialized,
                        created_at=config.created_at, updated_at=now,
                    )
                    self.write_config(new_config)
                    entries[position] = ThreadEntry(
                        tid=entry.tid, name=new_name, enabled=new_enabled,
                        canonical_url=entry.canonical_url,
                        config_revision=new_config.revision,
                        created_at=entry.created_at, updated_at=now,
                    )
                    changed = True
                if make_active:
                    active = tid
                    changed = True
            if not changed:
                raise ValueError("没有可应用的变更")
            new_index = ThreadIndex(schema_version="1", revision=index.revision + 1,
                                    active_tid=active, entries=tuple(entries))
            self.write_index(new_index)
            return new_index


class ConflictError(Exception):
    pass
