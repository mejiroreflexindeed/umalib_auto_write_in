"""Read-only file-state helpers for the candidate Writer (TASK-008).

The Writer never imports private symbols from the inspector; this module is its
own small, tested read-only path/state layer.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from umalib_ingest.umalib_writer.errors import WriterDatabaseError, WriterPathError

_SQLITE_MAGIC = b"SQLite format 3\x00"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def stable_json(obj: object) -> str:
    """Serialize with stable key order for deterministic hashing."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    """Return the lowercase hex SHA-256 of a UTF-8 encoded string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 of a file, streamed in fixed-size chunks."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        raise WriterPathError("无法读取文件") from None
    return digest.hexdigest()


def path_sha256(path: str | Path) -> str:
    """Return the SHA-256 of the normalized absolute path string."""
    try:
        normalized = os.path.normcase(str(Path(path).resolve()))
    except (TypeError, ValueError, OSError, RuntimeError):
        raise WriterPathError("无法解析路径") from None
    return sha256_hex(normalized)


@dataclass(frozen=True)
class FileState:
    """File and sidecar evidence captured around a Writer operation."""

    sha256: str
    size_bytes: int
    mtime_ns: int
    dev: int
    ino: int
    sidecars: tuple[tuple[str, int, int, str], ...]
    directory_names: tuple[str, ...]


def snapshot_file(target: Path) -> FileState:
    """Capture the target's identity plus its sidecar evidence."""
    try:
        stat = target.stat()
        sidecars: list[tuple[str, int, int, str]] = []
        for suffix in _SIDECAR_SUFFIXES:
            sidecar = target.with_name(target.name + suffix)
            if not sidecar.exists():
                continue
            sidecar_stat = sidecar.stat()
            sidecars.append(
                (
                    sidecar.name,
                    sidecar_stat.st_size,
                    sidecar_stat.st_mtime_ns,
                    sha256_file(sidecar),
                )
            )
        return FileState(
            sha256=sha256_file(target),
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            dev=stat.st_dev,
            ino=stat.st_ino,
            sidecars=tuple(sidecars),
            directory_names=tuple(sorted(entry.name for entry in target.parent.iterdir())),
        )
    except OSError:
        raise WriterPathError("无法读取文件状态") from None


def assert_unchanged(target: Path, before: FileState) -> FileState:
    """Return current state, or raise if identity/content/sidecars changed.

    The directory listing is deliberately excluded: the Writer legitimately
    creates its own temporary file inside the same directory.
    """
    after = snapshot_file(target)
    relevant = lambda state: (
        state.sha256,
        state.size_bytes,
        state.mtime_ns,
        state.dev,
        state.ino,
        state.sidecars,
    )
    if relevant(after) != relevant(before):
        raise WriterDatabaseError("数据库文件或同目录文件在操作期间发生变化，已中止")
    return after


def has_sidecars(target: Path) -> bool:
    """Return whether any of the target's sidecar files exist."""
    return any(
        target.with_name(target.name + suffix).exists() for suffix in _SIDECAR_SUFFIXES
    )


@contextmanager
def read_only_connect(target: Path) -> Iterator[sqlite3.Connection]:
    """Open a strictly read-only, query-only connection; always closes."""
    try:
        conn = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        raise WriterDatabaseError("无法以只读方式打开数据库") from None
    conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute("PRAGMA query_only = ON")
            if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise WriterDatabaseError("无法启用 query_only 只读模式")
            conn.execute("BEGIN")
        except sqlite3.Error:
            raise WriterDatabaseError("无法启用 query_only 只读模式") from None
        yield conn
    finally:
        conn.close()


def validate_source_path(path: str | Path) -> Path:
    """Validate the source path without opening it for writing."""
    try:
        target = Path(path)
    except (TypeError, ValueError):
        raise WriterPathError("源数据库路径无效") from None
    file_name = target.name or "source.db"
    if not target.exists():
        raise WriterPathError(f"源数据库不存在: {file_name}")
    if target.is_symlink():
        raise WriterPathError(f"源数据库不能是符号链接: {file_name}")
    if target.is_dir() or not target.is_file():
        raise WriterPathError(f"源数据库不是普通文件: {file_name}")
    try:
        with open(target, "rb") as handle:
            magic = handle.read(16)
    except OSError:
        raise WriterPathError("无法读取源数据库") from None
    if magic != _SQLITE_MAGIC:
        raise WriterDatabaseError("源数据库不是 SQLite 文件")
    return target


def validate_candidate_path(source: str | Path, path: str | Path) -> Path:
    """Validate the candidate path; never creates anything."""
    try:
        source_path = Path(source)
        candidate = Path(path)
    except (TypeError, ValueError):
        raise WriterPathError("候选数据库路径无效") from None
    file_name = candidate.name or "candidate.db"
    if not file_name.endswith(".db"):
        raise WriterPathError(f"候选路径必须以 .db 结尾: {file_name}")
    parent = candidate.parent
    if not parent.exists() or not parent.is_dir() or parent.is_symlink():
        raise WriterPathError(f"候选父目录不存在或不是目录: {parent.name}")
    if candidate.is_symlink():
        raise WriterPathError("候选路径不能是符号链接")
    if candidate.exists():
        raise WriterPathError(f"候选文件已存在，禁止覆盖: {file_name}")
    if has_sidecars(candidate):
        raise WriterPathError(f"候选路径存在 sidecar 文件，禁止写入: {file_name}")
    if candidate.resolve() == source_path.resolve():
        raise WriterPathError("候选路径不得等于源数据库路径")
    return candidate
