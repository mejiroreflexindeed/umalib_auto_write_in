"""Strictly read-only UmaLib database inspector (TASK-006)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from umalib_ingest.umalib_adapter.errors import UmaLibDatabaseError, UmaLibPathError
from umalib_ingest.umalib_adapter.report import (
    REPORT_SCHEMA_VERSION,
    InspectionIssue,
    UmaLibInspectionReport,
    sort_issues,
)

_SQLITE_MAGIC = b"SQLite format 3\x00"

_CORE_TABLES = ("article", "tag", "tagged")
_COUNT_TABLES = ("article", "tag", "tagged", "creator", "rec", "dict")

_CORE_COLUMNS: dict[str, tuple[str, ...]] = {
    "article": ("id", "name", "note", "content", "author", "translator", "uploadTime", "source"),
    "tag": ("id", "name", "type", "cover", "description"),
    "tagged": ("id", "artId", "tagId"),
}

_CORE_COLUMN_TYPES: dict[str, dict[str, str]] = {
    "article": {
        "id": "INTEGER",
        "name": "TEXT",
        "note": "TEXT",
        "content": "TEXT",
        "author": "TEXT",
        "translator": "TEXT",
        "uploadTime": "INTEGER",
        "source": "TEXT",
    },
    "tag": {
        "id": "INTEGER",
        "name": "TEXT",
        "type": "INTEGER",
        "cover": "TEXT",
        "description": "TEXT",
    },
    "tagged": {"id": "INTEGER", "artId": "INTEGER", "tagId": "INTEGER"},
}

_KNOWN_TABLES = ("article", "tag", "tagged", "creator", "rec", "dict")

_KNOWN_COLUMNS: dict[str, tuple[str, ...]] = {
    "article": ("id", "name", "note", "content", "author", "translator", "uploadTime", "source"),
    "tag": ("id", "name", "type", "cover", "description"),
    "tagged": ("id", "artId", "tagId"),
    "creator": ("id", "names"),
    "rec": ("id", "refId", "title", "others", "type", "r", "name", "reason"),
    "dict": ("id", "class", "desc", "key", "refId", "related", "relatedId"),
}

_TAGGED_NOT_NULL = ("artId", "tagId")
_TAGGED_FOREIGN_KEYS = (
    ("artId", "article", "id"),
    ("tagId", "tag", "id"),
)
_ALLOWED_TAG_TYPES = (0, 1, 2, 3, 4)

_INLINE_COLOR_RE = re.compile(r"(?<![a-zA-Z-])color\s*:", re.IGNORECASE)
_BACKGROUND_COLOR_RE = re.compile(r"background-color", re.IGNORECASE)
_QL_CLASS_RE = re.compile(r"\bql-[a-zA-Z]", re.IGNORECASE)
_IMG_RE = re.compile(r"<img", re.IGNORECASE)
_ANNOTATION_RE = re.compile(r"<p[^>]*>\[[^\]]+\]", re.IGNORECASE)


@dataclass(frozen=True)
class _InspectionFileState:
    """File and directory evidence captured around one inspection."""

    sha256: str
    size_bytes: int
    mtime_ns: int
    directory_names: tuple[str, ...]
    sidecars: tuple[tuple[str, int, int, str], ...]


def _sqlite_affinity(declared_type: object) -> str:
    """Return SQLite's deterministic type affinity for a declared type."""
    value = str(declared_type or "").upper()
    if "INT" in value:
        return "INTEGER"
    if any(token in value for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if value == "" or "BLOB" in value:
        return "BLOB"
    if any(token in value for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def inspect_umalib_database(path: str | Path) -> UmaLibInspectionReport:
    """Inspect a UmaLib database copy strictly read-only and build a report.

    Never writes to or copies the database; path/open/query problems raise
    UmaLibPathError or UmaLibDatabaseError without leaking SQL or content.
    """
    target = _validate_target(path)
    before = _snapshot_target(target)
    with _open_readonly(target) as conn:
        try:
            database_info = _collect_database_info(conn, target, before)
            schema_payload, schema_tables = _collect_schema(conn)
            schema_fingerprint = _sha256_hex(_stable_schema_json(schema_payload))
            issues: list[InspectionIssue] = []
            core_match = _check_core_contract(schema_tables, issues)
            _check_known_schema(schema_tables, schema_payload["user_version"], issues)
            counts = _collect_counts(conn, schema_tables, issues)
            tag_audit = _collect_tag_audit(conn, schema_tables, issues)
            article_features = _collect_article_features(conn, schema_tables)
            integrity = _collect_integrity(conn, schema_tables, issues)
        except sqlite3.Error:
            raise UmaLibDatabaseError("数据库打开或查询失败") from None

    after = _snapshot_target(target)
    if after != before:
        raise UmaLibDatabaseError("检查期间数据库或同目录文件发生变化，请重试")

    report = UmaLibInspectionReport(
        report_schema_version=REPORT_SCHEMA_VERSION,
        database=database_info,
        schema={
            "schema_fingerprint_sha256": schema_fingerprint,
            "user_tables": sorted(schema_tables.keys()),
            "core_contract_match": core_match,
        },
        counts=counts,
        tag_audit=tag_audit,
        article_features=article_features,
        integrity=integrity,
        issues=tuple(sort_issues(issues)),
    )
    return report


def _validate_target(path: str | Path) -> Path:
    """Validate the target exists as a regular SQLite file."""
    target = Path(path)
    file_name = target.name or str(target)
    if not target.exists():
        raise UmaLibPathError(f"路径不存在: {file_name}")
    if target.is_dir():
        raise UmaLibPathError(f"目标是目录而不是数据库文件: {file_name}")
    if not target.is_file():
        raise UmaLibPathError(f"目标不是普通文件: {file_name}")
    try:
        with open(target, "rb") as handle:
            magic = handle.read(16)
    except OSError:
        raise UmaLibPathError("无法读取目标文件") from None
    if magic != _SQLITE_MAGIC:
        raise UmaLibDatabaseError("目标不是 SQLite 数据库文件")
    return target


def _sha256_file(path: Path) -> str:
    """Return the hex SHA-256 of a file, streamed in fixed-size chunks."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        raise UmaLibPathError("无法读取目标文件") from None
    return digest.hexdigest()


def _sha256_hex(text: str) -> str:
    """Return the lowercase hex SHA-256 of a UTF-8 encoded string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshot_target(target: Path) -> _InspectionFileState:
    """Capture target and sidecar evidence for post-inspection comparison."""
    try:
        stat = target.stat()
        directory_names = tuple(sorted(entry.name for entry in target.parent.iterdir()))
        sidecars = []
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = target.with_name(target.name + suffix)
            if not sidecar.exists():
                continue
            sidecar_stat = sidecar.stat()
            sidecars.append(
                (
                    sidecar.name,
                    sidecar_stat.st_size,
                    sidecar_stat.st_mtime_ns,
                    _sha256_file(sidecar),
                )
            )
        return _InspectionFileState(
            sha256=_sha256_file(target),
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            directory_names=directory_names,
            sidecars=tuple(sidecars),
        )
    except OSError:
        raise UmaLibPathError("无法读取目标文件状态") from None


def _quoted(value: str) -> str:
    """Quote a value for use as a PRAGMA function-call argument."""
    return "'" + value.replace("'", "''") + "'"


def _readonly_uri(target: Path) -> str:
    """Build a properly escaped SQLite read-only URI for the target path."""
    return target.resolve().as_uri() + "?mode=ro"


@contextmanager
def _open_readonly(target: Path) -> Iterator[sqlite3.Connection]:
    """Open a strictly read-only, query-only connection; always closes."""
    try:
        conn = sqlite3.connect(_readonly_uri(target), uri=True)
    except sqlite3.Error:
        raise UmaLibDatabaseError("无法以只读方式打开数据库") from None
    conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute("PRAGMA query_only = ON")
            if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise UmaLibDatabaseError("无法启用 query_only 只读模式")
            conn.execute("BEGIN")
        except sqlite3.Error:
            raise UmaLibDatabaseError("无法启用 query_only 只读模式") from None
        yield conn
    finally:
        conn.close()


def _collect_database_info(
    conn: sqlite3.Connection, target: Path, state: _InspectionFileState
) -> dict[str, object]:
    """Collect file-level identification without absolute paths."""
    return {
        "file_name": target.name,
        "size_bytes": state.size_bytes,
        "sha256": state.sha256,
        "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "page_size": conn.execute("PRAGMA page_size").fetchone()[0],
        "page_count": conn.execute("PRAGMA page_count").fetchone()[0],
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    """Return PRAGMA table_info rows for one table as plain dicts."""
    rows = conn.execute(f"PRAGMA table_info({_quoted(table)})").fetchall()
    return [
        {
            "name": row["name"],
            "type": row["type"],
            "notnull": bool(row["notnull"]),
            "pk": row["pk"],
        }
        for row in rows
    ]


def _table_foreign_keys(conn: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    """Return PRAGMA foreign_key_list rows for one table as plain dicts."""
    rows = conn.execute(f"PRAGMA foreign_key_list({_quoted(table)})").fetchall()
    result = [
        {
            "from": row["from"],
            "table": row["table"],
            "to": row["to"],
            "on_delete": row["on_delete"],
            "on_update": row["on_update"],
        }
        for row in rows
    ]
    result.sort(
        key=lambda fk: (fk["from"] or "", fk["table"] or "", str(fk["to"] or ""))
    )
    return result


def _table_indexes(conn: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    """Return indexes for one table with ordered index columns."""
    rows = conn.execute(f"PRAGMA index_list({_quoted(table)})").fetchall()
    result = []
    for row in rows:
        columns = [
            info["name"]
            for info in conn.execute(
                f"PRAGMA index_info({_quoted(row['name'])})"
            ).fetchall()
        ]
        result.append(
            {
                "name": row["name"],
                "unique": bool(row["unique"]),
                "origin": row["origin"],
                "columns": columns,
            }
        )
    result.sort(key=lambda idx: idx["name"])
    return result


def _collect_schema(
    conn: sqlite3.Connection,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    """Collect the structured schema payload and per-table structure map."""
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    table_names = sorted(
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    )
    tables_payload = []
    tables_map: dict[str, dict[str, object]] = {}
    for name in table_names:
        columns = _table_columns(conn, name)
        foreign_keys = _table_foreign_keys(conn, name)
        indexes = _table_indexes(conn, name)
        primary_key = [
            column["name"] for column in sorted(columns, key=lambda c: c["pk"]) if column["pk"] > 0
        ]
        tables_payload.append(
            {
                "name": name,
                "columns": sorted(
                    columns,
                    key=lambda c: c["name"],
                ),
                "primary_key": primary_key,
                "foreign_keys": foreign_keys,
                "indexes": indexes,
            }
        )
        tables_map[name] = {
            "columns": columns,
            "primary_key": primary_key,
            "foreign_keys": foreign_keys,
            "indexes": indexes,
        }
    return {"user_version": user_version, "tables": tables_payload}, tables_map


def _stable_schema_json(payload: dict[str, object]) -> str:
    """Serialize the schema payload with stable ordering for fingerprinting."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _check_core_contract(
    schema_tables: dict[str, dict[str, object]], issues: list[InspectionIssue]
) -> bool:
    """Validate the core article/tag/tagged contract; append blockers on failure."""
    match = True
    for table in _CORE_TABLES:
        if table not in schema_tables:
            issues.append(
                InspectionIssue("blocker", "missing_table", f"缺少核心表: {table}")
            )
            match = False
    if not match:
        return False
    for table, required_columns in _CORE_COLUMNS.items():
        columns = {c["name"]: c for c in schema_tables[table]["columns"]}
        for column in required_columns:
            if column not in columns:
                issues.append(
                    InspectionIssue(
                        "blocker", "missing_column", f"{table} 缺少必需列: {column}"
                    )
                )
                match = False
                continue
            expected_type = _CORE_COLUMN_TYPES[table][column]
            declared_type = str(columns[column]["type"] or "").upper()
            actual_type = _sqlite_affinity(declared_type)
            exact_integer_primary_key = column == "id" and declared_type != "INTEGER"
            if actual_type != expected_type or exact_integer_primary_key:
                expectation = (
                    "精确的 INTEGER 主键声明"
                    if exact_integer_primary_key
                    else f"{expected_type} affinity"
                )
                issues.append(
                    InspectionIssue(
                        "blocker",
                        "column_type_mismatch",
                        f"{table}.{column} 声明类型为 {declared_type or '未声明'}，"
                        f"affinity 为 {actual_type}，预期 {expectation}",
                    )
                )
                match = False
        if schema_tables[table]["primary_key"] != ["id"]:
            issues.append(
                InspectionIssue(
                    "blocker", "primary_key_mismatch", f"{table}.id 不是单列主键"
                )
            )
            match = False
    tagged_columns = {c["name"]: c for c in schema_tables["tagged"]["columns"]}
    for column in _TAGGED_NOT_NULL:
        if column in tagged_columns and not tagged_columns[column]["notnull"]:
            issues.append(
                InspectionIssue(
                    "blocker", "not_null_mismatch", f"tagged.{column} 不是非空列"
                )
            )
            match = False
    tagged_fks = {
        (fk["from"], fk["table"], fk["to"]): fk
        for fk in schema_tables["tagged"]["foreign_keys"]
    }
    for from_col, ref_table, ref_col in _TAGGED_FOREIGN_KEYS:
        fk = tagged_fks.get((from_col, ref_table, ref_col))
        if fk is None:
            issues.append(
                InspectionIssue(
                    "blocker",
                    "missing_foreign_key",
                    f"tagged.{from_col} 缺少外键 -> {ref_table}({ref_col})",
                )
            )
            match = False
            continue
        actions = (fk["on_delete"], fk["on_update"])
        if any(action != "NO ACTION" for action in actions):
            issues.append(
                InspectionIssue(
                    "blocker",
                    "foreign_key_action_mismatch",
                    f"tagged.{from_col} 外键动作与契约不一致",
                )
            )
            match = False
    return match


def _check_known_schema(
    schema_tables: dict[str, dict[str, object]],
    user_version: int,
    issues: list[InspectionIssue],
) -> None:
    """Report extra tables/columns and unknown user_version as info-level differences."""
    if user_version != 0:
        issues.append(
            InspectionIssue(
                "info",
                "unknown_user_version",
                f"user_version={user_version}（未知，仅作可审查差异）",
            )
        )
    known_tables = set(_KNOWN_TABLES)
    for name in sorted(schema_tables.keys()):
        if name not in known_tables:
            issues.append(InspectionIssue("info", "extra_table", f"额外表: {name}"))
            continue
        known_columns = set(_KNOWN_COLUMNS[name])
        actual_columns = {column["name"] for column in schema_tables[name]["columns"]}
        for column in sorted(actual_columns - known_columns):
            issues.append(
                InspectionIssue("info", "extra_column", f"{name} 额外列: {column}")
            )


def _collect_counts(
    conn: sqlite3.Connection,
    schema_tables: dict[str, dict[str, object]],
    issues: list[InspectionIssue],
) -> dict[str, object]:
    """Collect per-table row counts; missing non-core tables become warnings."""
    counts: dict[str, object] = {}
    for table in _COUNT_TABLES:
        if table not in schema_tables:
            counts[table] = None
            if table in _CORE_TABLES:
                continue
            issues.append(
                InspectionIssue("warning", "missing_non_core_table", f"缺少非核心表: {table}")
            )
            continue
        counts[table] = conn.execute(
            f"SELECT COUNT(*) FROM {_quoted_table(table)}"
        ).fetchone()[0]
    return counts


def _quoted_table(table: str) -> str:
    """Quote a table name for use in a SELECT statement."""
    return '"' + table.replace('"', '""') + '"'


def _has_columns(
    schema_tables: dict[str, dict[str, object]],
    table: str,
    required: tuple[str, ...],
) -> bool:
    """Return whether a table exists with every named column."""
    if table not in schema_tables:
        return False
    actual = {column["name"] for column in schema_tables[table]["columns"]}
    return all(column in actual for column in required)


def _type_count_sort_key(key: str) -> tuple[int, int, str]:
    """Sort type-count keys: numeric ascending, non-numeric after, null last."""
    if key == "null":
        return (1, 0, key)
    try:
        return (0, int(key), key)
    except ValueError:
        return (0, 10**18, key)


def _collect_tag_audit(
    conn: sqlite3.Connection,
    schema_tables: dict[str, dict[str, object]],
    issues: list[InspectionIssue],
) -> dict[str, object]:
    """Collect tag statistics and audit findings; empty when tag is missing."""
    if not _has_columns(schema_tables, "tag", ("id", "name", "type")):
        return {
            "type_counts": {},
            "duplicate_names": [],
            "blank_name_ids": [],
            "unknown_type_ids": [],
        }
    type_counts: dict[str, int] = {}
    for row in conn.execute(
        'SELECT type, COUNT(*) AS n FROM "tag" GROUP BY type'
    ).fetchall():
        key = "null" if row["type"] is None else str(row["type"])
        type_counts[key] = row["n"]
    type_counts = dict(
        sorted(type_counts.items(), key=lambda kv: _type_count_sort_key(kv[0]))
    )

    duplicate_names = []
    tagged_available = "tagged" in schema_tables
    for row in conn.execute(
        'SELECT name FROM "tag" WHERE typeof(name) = \'text\' '
        "GROUP BY name HAVING COUNT(*) > 1"
    ).fetchall():
        name = row["name"]
        entries = []
        for tag_row in conn.execute(
            'SELECT id, type FROM "tag" WHERE name IS ? ORDER BY id ASC', (name,)
        ).fetchall():
            if tagged_available and _has_columns(
                schema_tables, "tagged", ("tagId",)
            ):
                tagged_count = conn.execute(
                    'SELECT COUNT(*) FROM "tagged" WHERE tagId = ?', (tag_row["id"],)
                ).fetchone()[0]
            else:
                tagged_count = None
            entries.append(
                {"id": tag_row["id"], "type": tag_row["type"], "tagged_count": tagged_count}
            )
        duplicate_names.append({"name": name, "entries": entries})
    duplicate_names.sort(key=lambda group: group["name"])
    if duplicate_names:
        issues.append(
            InspectionIssue(
                "warning",
                "duplicate_tag_name",
                f"发现 {len(duplicate_names)} 组重名标签",
            )
        )

    blank_name_ids = []
    invalid_name_type_count = 0
    for row in conn.execute('SELECT id, name FROM "tag" ORDER BY id ASC'):
        name = row["name"]
        if name is None or (isinstance(name, str) and name.strip() == ""):
            blank_name_ids.append(row["id"])
        elif not isinstance(name, str):
            invalid_name_type_count += 1
    if blank_name_ids:
        issues.append(
            InspectionIssue(
                "warning",
                "blank_tag_name",
                f"发现 {len(blank_name_ids)} 个空名标签",
            )
        )
    if invalid_name_type_count:
        issues.append(
            InspectionIssue(
                "warning",
                "invalid_tag_name_type",
                f"发现 {invalid_name_type_count} 个非文本标签名",
            )
        )

    unknown_type_rows = conn.execute(
        'SELECT id, type FROM "tag" WHERE type IS NOT NULL AND type NOT IN (0,1,2,3,4) '
        "ORDER BY id ASC"
    ).fetchall()
    unknown_type_ids = [{"id": row["id"], "type": row["type"]} for row in unknown_type_rows]
    if unknown_type_ids:
        issues.append(
            InspectionIssue(
                "warning",
                "unknown_tag_type",
                f"发现 {len(unknown_type_ids)} 个未知类型标签",
            )
        )

    return {
        "type_counts": type_counts,
        "duplicate_names": duplicate_names,
        "blank_name_ids": blank_name_ids,
        "unknown_type_ids": unknown_type_ids,
    }


def _collect_article_features(
    conn: sqlite3.Connection, schema_tables: dict[str, dict[str, object]]
) -> dict[str, int]:
    """Count article content feature occurrences without emitting content."""
    features = {
        "nonempty_content": 0,
        "inline_color": 0,
        "background_color": 0,
        "ql_class": 0,
        "img": 0,
        "annotation_definition": 0,
    }
    if not _has_columns(schema_tables, "article", ("content",)):
        return features
    for row in conn.execute('SELECT content FROM "article"'):
        content = row["content"]
        if not isinstance(content, str):
            continue
        if content.strip() != "":
            features["nonempty_content"] += 1
        if _INLINE_COLOR_RE.search(content):
            features["inline_color"] += 1
        if _BACKGROUND_COLOR_RE.search(content):
            features["background_color"] += 1
        if _QL_CLASS_RE.search(content):
            features["ql_class"] += 1
        if _IMG_RE.search(content):
            features["img"] += 1
        if _ANNOTATION_RE.search(content):
            features["annotation_definition"] += 1
    return features


def _collect_integrity(
    conn: sqlite3.Connection,
    schema_tables: dict[str, dict[str, object]],
    issues: list[InspectionIssue],
) -> dict[str, object]:
    """Run read-only integrity checks and append blockers on failures."""
    integrity_rows = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
    integrity_ok = integrity_rows == ["ok"]
    if not integrity_ok:
        issues.append(
            InspectionIssue(
                "blocker",
                "integrity_check_failed",
                f"integrity_check 未通过（{len(integrity_rows)} 行结果）",
            )
        )

    violations_by_table: dict[str, int] = {}
    violation_total: int | None
    try:
        for row in conn.execute("PRAGMA foreign_key_check"):
            violations_by_table[row["table"]] = (
                violations_by_table.get(row["table"], 0) + 1
            )
    except sqlite3.Error:
        violation_total = None
        issues.append(
            InspectionIssue(
                "blocker",
                "foreign_key_check_failed",
                "外键定义异常，无法完成 foreign_key_check",
            )
        )
    else:
        violations_by_table = dict(sorted(violations_by_table.items()))
        violation_total = sum(violations_by_table.values())
        if violation_total > 0:
            issues.append(
                InspectionIssue(
                    "blocker",
                    "foreign_key_violation",
                    f"发现 {violation_total} 条外键违规",
                )
            )

    duplicate_pair_count = 0
    if _has_columns(schema_tables, "tagged", ("artId", "tagId")):
        duplicate_pair_count = conn.execute(
            'SELECT COUNT(*) FROM ('
            'SELECT artId, tagId FROM "tagged" GROUP BY artId, tagId HAVING COUNT(*) > 1'
            ")"
        ).fetchone()[0]
    if duplicate_pair_count > 0:
        issues.append(
            InspectionIssue(
                "blocker",
                "duplicate_tagged_pair",
                f"发现 {duplicate_pair_count} 组重复 (artId, tagId) 关联",
            )
        )

    return {
        "integrity_check": integrity_ok,
        "foreign_key_violation_count": violation_total,
        "foreign_key_violations_by_table": violations_by_table,
        "duplicate_tagged_pair_count": duplicate_pair_count,
    }
