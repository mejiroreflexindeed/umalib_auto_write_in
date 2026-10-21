"""SQLite-backed Inbox repository core (TASK-002)."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from umalib_ingest.domain.models import SubmissionDraft
from umalib_ingest.inbox.errors import (
    InboxError,
    InboxItemLockedError,
    InboxItemNotFoundError,
    InvalidStatusTransitionError,
    UnsupportedInboxSchemaError,
)
from umalib_ingest.inbox.models import InboxItem, InboxStatusHistory, VALID_STATUSES

SUPPORTED_SCHEMA_VERSION = 1
INBOX_TABLES = ("inbox_item", "inbox_status_history")
EDITABLE_STATUSES = ("candidate", "incomplete", "suspected_duplicate")

_EDITABLE_METADATA_KEYS = ("title", "author", "translator", "series", "tags")
_EDITABLE_SOURCE_KEYS = (
    "nga",
    "original_series",
    "original",
    "translation_series",
    "translation",
)
_VALID_MISSING_FIELDS = ("title", "author", "tags", "body")

_SCHEMA_SQL_PATH = Path(__file__).with_name("schema.sql")

_SCHEMA_STATEMENTS: tuple[str, ...] = tuple(
    statement.strip()
    for statement in _SCHEMA_SQL_PATH.read_text(encoding="utf-8").split(";")
    if statement.strip()
)

_EXPECTED_TABLE_COLUMNS: dict[str, tuple[tuple[str, str, bool, bool], ...]] = {
    "inbox_item": (
        ("id", "INTEGER", False, True),
        ("status", "TEXT", True, False),
        ("original_draft_json", "TEXT", True, False),
        ("working_draft_json", "TEXT", True, False),
        ("snapshot_sha256", "TEXT", True, False),
        ("content_sha256", "TEXT", False, False),
        ("created_at", "TEXT", True, False),
        ("updated_at", "TEXT", True, False),
    ),
    "inbox_status_history": (
        ("id", "INTEGER", False, True),
        ("item_id", "INTEGER", True, False),
        ("from_status", "TEXT", False, False),
        ("to_status", "TEXT", True, False),
        ("reason", "TEXT", True, False),
        ("changed_at", "TEXT", True, False),
    ),
}

_REQUIRED_TABLE_SQL_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "inbox_item": (
        "CHECK (status IN ('candidate','incomplete','suspected_duplicate',"
        "'approved','exported','ignored'))",
    ),
    "inbox_status_history": (
        "CHECK (from_status IS NULL OR from_status IN "
        "('candidate','incomplete','suspected_duplicate','approved','exported','ignored'))",
        "CHECK (to_status IN "
        "('candidate','incomplete','suspected_duplicate','approved','exported','ignored'))",
        "CHECK (length(trim(reason, char(9) || char(10) || char(13) || ' ')) > 0)",
    ),
}

_Clock = Callable[[], str]


def _default_clock() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _stable_json(obj: dict[str, object]) -> str:
    """Serialize a dict with stable key order for deterministic storage and hashing."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _working_draft_nodes(
    working_draft: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    """Validate editable draft paths and return their containing dictionaries."""
    metadata = working_draft.get("metadata")
    content = working_draft.get("content")
    diagnostics = working_draft.get("diagnostics")
    if not isinstance(metadata, dict):
        raise InboxError("working_draft 缺少 metadata 字典")
    if not isinstance(content, dict):
        raise InboxError("working_draft 缺少 content 字典")
    if not isinstance(diagnostics, dict):
        raise InboxError("working_draft 缺少 diagnostics 字典")
    sources = metadata.get("sources")
    if not isinstance(sources, dict):
        raise InboxError("working_draft 缺少 metadata.sources 字典")

    for key in ("title", "author", "translator", "series"):
        value = metadata.get(key)
        if value is not None and not isinstance(value, str):
            raise InboxError(f"working_draft 的 metadata.{key} 必须是字符串或 None")
    tags = metadata.get("tags")
    if not isinstance(tags, list) or not all(isinstance(value, str) for value in tags):
        raise InboxError("working_draft 的 metadata.tags 必须是字符串列表")
    for key in _EDITABLE_SOURCE_KEYS:
        value = sources.get(key)
        if not isinstance(value, list) or not all(
            isinstance(entry, str) for entry in value
        ):
            raise InboxError(f"working_draft 的 metadata.sources.{key} 必须是字符串列表")
    if not isinstance(content.get("body_text"), str):
        raise InboxError("working_draft 缺少 content.body_text 字符串")
    missing_fields = diagnostics.get("missing_fields")
    if (
        not isinstance(missing_fields, list)
        or not all(isinstance(value, str) for value in missing_fields)
        or len(set(missing_fields)) != len(missing_fields)
        or any(value not in _VALID_MISSING_FIELDS for value in missing_fields)
    ):
        raise InboxError("working_draft 的 diagnostics.missing_fields 非法")
    return metadata, content, sources, diagnostics


def _protected_working_draft(working_draft: dict[str, object]) -> dict[str, object]:
    """Return a deep copy with only human-editable paths removed."""
    protected = copy.deepcopy(working_draft)
    metadata, content, sources, diagnostics = _working_draft_nodes(protected)
    for key in _EDITABLE_METADATA_KEYS:
        metadata.pop(key, None)
    for key in _EDITABLE_SOURCE_KEYS:
        sources.pop(key, None)
    content.pop("body_text", None)
    diagnostics.pop("missing_fields", None)
    return protected


def _compact_sql(sql: str) -> str:
    """Remove insignificant SQL whitespace while preserving string literals."""
    compact: list[str] = []
    in_literal = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'":
            compact.append(char)
            if in_literal and index + 1 < len(sql) and sql[index + 1] == "'":
                compact.append("'")
                index += 2
                continue
            in_literal = not in_literal
        elif not in_literal and char.isspace():
            index += 1
            continue
        else:
            compact.append(char if in_literal else char.lower())
        index += 1
    return "".join(compact)


def _sha256_hex(text: str) -> str:
    """Return the hex SHA-256 of a UTF-8 encoded string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_status(status: str) -> None:
    """Reject status values outside the allowed set."""
    if status not in VALID_STATUSES:
        raise InvalidStatusTransitionError(
            f"非法状态: {status!r}，允许值: {', '.join(VALID_STATUSES)}"
        )


def _initial_status(draft: SubmissionDraft) -> str:
    """Return the initial status for an imported draft."""
    if draft.parser_status == "ignored":
        return "ignored"
    if draft.diagnostics.missing_fields:
        return "incomplete"
    return "candidate"


def _row_to_item(row: sqlite3.Row) -> InboxItem:
    """Convert a row to an InboxItem with plain-dict drafts."""
    return InboxItem(
        id=row["id"],
        status=row["status"],
        original_draft=json.loads(row["original_draft_json"]),
        working_draft=json.loads(row["working_draft_json"]),
        snapshot_sha256=row["snapshot_sha256"],
        content_sha256=row["content_sha256"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_history(row: sqlite3.Row) -> InboxStatusHistory:
    """Convert a row to an InboxStatusHistory."""
    return InboxStatusHistory(
        id=row["id"],
        item_id=row["item_id"],
        from_status=row["from_status"],
        to_status=row["to_status"],
        reason=row["reason"],
        changed_at=row["changed_at"],
    )


class InboxRepository:
    """File-backed Inbox database handling SubmissionDraft persistence."""

    def __init__(self, db_path: str | Path, clock: _Clock | None = None) -> None:
        """Bind the repository to an explicit database path with an optional clock."""
        self._db_path = str(db_path)
        self._clock = clock if clock is not None else _default_clock

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection with foreign keys enabled."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def _verify_tables(self, conn: sqlite3.Connection) -> None:
        """Raise when the Inbox tables are missing."""
        existing = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = [name for name in INBOX_TABLES if name not in existing]
        if missing:
            raise UnsupportedInboxSchemaError(f"Inbox 表缺失: {', '.join(missing)}")

    @staticmethod
    def _pragma_arg(value: str) -> str:
        """Quote a value for use as a PRAGMA function-call argument."""
        return "'" + value.replace("'", "''") + "'"

    def _index_columns(self, conn: sqlite3.Connection, index_name: str) -> list[str]:
        """Return the ordered column names of an index."""
        rows = conn.execute(
            f"PRAGMA index_info({self._pragma_arg(index_name)})"
        ).fetchall()
        return [row["name"] for row in rows]

    def _verify_table_columns(
        self,
        conn: sqlite3.Connection,
        table: str,
        expected: tuple[tuple[str, str, bool, bool], ...],
    ) -> None:
        """Raise when a table's required columns, types, NOT NULL or primary key mismatch."""
        info = {
            row["name"]: row
            for row in conn.execute(f"PRAGMA table_info({self._pragma_arg(table)})")
        }
        for name, type_, notnull, is_pk in expected:
            row = info.get(name)
            if row is None:
                raise UnsupportedInboxSchemaError(f"{table} 缺少必需列: {name}")
            if row["type"].upper() != type_:
                raise UnsupportedInboxSchemaError(
                    f"{table}.{name} 列类型不符: 期望 {type_}, 实际 {row['type']}"
                )
            if bool(row["notnull"]) != notnull:
                raise UnsupportedInboxSchemaError(
                    f"{table}.{name} NOT NULL 约束不符"
                )
        pk_columns = [row["name"] for row in info.values() if row["pk"] > 0]
        expected_pk = [name for name, _, _, is_pk in expected if is_pk]
        if pk_columns != expected_pk:
            raise UnsupportedInboxSchemaError(
                f"{table} 主键不符: 期望 {expected_pk}, 实际 {pk_columns}"
            )

    def _verify_unique_constraint(
        self, conn: sqlite3.Connection, table: str, column: str
    ) -> None:
        """Raise when the table lacks a unique index on the given column."""
        rows = conn.execute(f"PRAGMA index_list({self._pragma_arg(table)})").fetchall()
        for row in rows:
            if not row["unique"]:
                continue
            if row["origin"] not in ("u", "c"):
                continue
            if self._index_columns(conn, row["name"]) == [column]:
                return
        raise UnsupportedInboxSchemaError(f"{table} 缺少 {column} 唯一约束")

    def _verify_index(
        self,
        conn: sqlite3.Connection,
        table: str,
        index_name: str,
        column: str,
        *,
        unique: bool,
    ) -> None:
        """Raise when a named index has the wrong column or uniqueness."""
        rows = conn.execute(
            f"PRAGMA index_list({self._pragma_arg(table)})"
        ).fetchall()
        row = next((candidate for candidate in rows if candidate["name"] == index_name), None)
        if row is None:
            raise UnsupportedInboxSchemaError(f"缺少索引: {index_name} (表 {table})")
        columns = self._index_columns(conn, index_name)
        if columns != [column]:
            raise UnsupportedInboxSchemaError(
                f"索引 {index_name} 列不符: 期望 [{column}], 实际 {columns}"
            )
        if bool(row["unique"]) != unique:
            expected = "唯一" if unique else "非唯一"
            raise UnsupportedInboxSchemaError(
                f"索引 {index_name} 唯一性不符: 期望{expected}"
            )
        if bool(row["partial"]):
            raise UnsupportedInboxSchemaError(f"索引 {index_name} 不得为部分索引")

    def _verify_table_constraints(
        self, conn: sqlite3.Connection, table: str, required: tuple[str, ...]
    ) -> None:
        """Raise when required table-level CHECK constraints are absent."""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if row is None or row["sql"] is None:
            raise UnsupportedInboxSchemaError(f"Inbox 表缺失或无定义: {table}")
        actual = _compact_sql(row["sql"])
        for fragment in required:
            if _compact_sql(fragment) not in actual:
                raise UnsupportedInboxSchemaError(
                    f"{table} 缺少必需 CHECK 约束: {fragment}"
                )

    def _verify_foreign_key(
        self,
        conn: sqlite3.Connection,
        table: str,
        from_col: str,
        ref_table: str,
        to_col: str,
    ) -> None:
        """Raise when the table lacks the expected foreign key."""
        rows = conn.execute(
            f"PRAGMA foreign_key_list({self._pragma_arg(table)})"
        ).fetchall()
        for row in rows:
            if (
                row["from"] == from_col
                and row["table"] == ref_table
                and row["to"] == to_col
            ):
                if row["on_delete"].upper() not in ("NO ACTION", "RESTRICT"):
                    raise UnsupportedInboxSchemaError(
                        f"{table}.{from_col} 外键删除策略不安全: {row['on_delete']}"
                    )
                return
        raise UnsupportedInboxSchemaError(
            f"{table}.{from_col} 缺少外键 -> {ref_table}({to_col})"
        )

    def _verify_schema(self, conn: sqlite3.Connection) -> None:
        """Verify the full required Inbox schema structure of an existing database."""
        self._verify_tables(conn)
        for table, expected in _EXPECTED_TABLE_COLUMNS.items():
            self._verify_table_columns(conn, table, expected)
        for table, fragments in _REQUIRED_TABLE_SQL_FRAGMENTS.items():
            self._verify_table_constraints(conn, table, fragments)
        self._verify_unique_constraint(conn, "inbox_item", "snapshot_sha256")
        self._verify_index(
            conn,
            "inbox_item",
            "idx_inbox_item_status",
            "status",
            unique=False,
        )
        self._verify_index(
            conn,
            "inbox_item",
            "idx_inbox_item_content_sha256",
            "content_sha256",
            unique=False,
        )
        self._verify_foreign_key(
            conn, "inbox_status_history", "item_id", "inbox_item", "id"
        )

    def _execute_schema(self, conn: sqlite3.Connection) -> None:
        """Execute every schema statement on the connection."""
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)

    def _ensure_initialized(self, conn: sqlite3.Connection) -> None:
        """Raise unless the connection points to a supported Inbox database."""
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SUPPORTED_SCHEMA_VERSION:
            raise UnsupportedInboxSchemaError(
                f"数据库未初始化或不支持 (user_version={version})"
            )
        self._verify_schema(conn)

    def initialize(self) -> None:
        """Create or verify the Inbox schema; reject unsupported SQLite files.

        Schema creation is atomic: a mid-script failure leaves no user tables
        and keeps user_version at 0.
        """
        with self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != 0:
                if version != SUPPORTED_SCHEMA_VERSION:
                    raise UnsupportedInboxSchemaError(
                        f"不支持的 user_version: {version}"
                    )
                self._verify_schema(conn)
                return
            existing = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            if existing:
                raise UnsupportedInboxSchemaError(
                    f"user_version=0 但数据库已包含用户表: {', '.join(sorted(existing))}"
                )
            conn.execute("BEGIN")
            try:
                self._execute_schema(conn)
                conn.execute("PRAGMA user_version = 1")
                self._verify_schema(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def import_draft(self, draft: SubmissionDraft) -> InboxItem:
        """Persist a draft and its first status history in one transaction.

        Re-importing an identical snapshot returns the existing item without
        duplicating the item or its first history record.
        """
        snapshot = _stable_json(draft.to_dict())
        snapshot_sha256 = _sha256_hex(snapshot)
        status = _initial_status(draft)
        body_text = draft.content.body_text if draft.content is not None else ""
        content_sha256 = _sha256_hex(body_text) if body_text.strip() else None
        with self._connect() as conn:
            self._ensure_initialized(conn)
            with conn:
                existing = conn.execute(
                    "SELECT * FROM inbox_item WHERE snapshot_sha256 = ?",
                    (snapshot_sha256,),
                ).fetchone()
                if existing is not None:
                    return _row_to_item(existing)
                now = self._clock()
                cursor = conn.execute(
                    "INSERT INTO inbox_item (status, original_draft_json, "
                    "working_draft_json, snapshot_sha256, content_sha256, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (status, snapshot, snapshot, snapshot_sha256, content_sha256, now, now),
                )
                conn.execute(
                    "INSERT INTO inbox_status_history (item_id, from_status, "
                    "to_status, reason, changed_at) VALUES (?, NULL, ?, 'import', ?)",
                    (cursor.lastrowid, status, now),
                )
                row = conn.execute(
                    "SELECT * FROM inbox_item WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
                return _row_to_item(row)

    def get(self, item_id: int) -> InboxItem | None:
        """Return one item by id, or None when it does not exist."""
        with self._connect() as conn:
            self._ensure_initialized(conn)
            row = conn.execute(
                "SELECT * FROM inbox_item WHERE id = ?", (item_id,)
            ).fetchone()
            return _row_to_item(row) if row is not None else None

    def list_items(self, status: str | None = None) -> list[InboxItem]:
        """List items ordered by id, optionally filtered by a validated status."""
        if status is not None:
            _validate_status(status)
        with self._connect() as conn:
            self._ensure_initialized(conn)
            if status is None:
                rows = conn.execute("SELECT * FROM inbox_item ORDER BY id ASC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM inbox_item WHERE status = ? ORDER BY id ASC",
                    (status,),
                ).fetchall()
            return [_row_to_item(row) for row in rows]

    def list_items_page(
        self,
        *,
        status: str | None = None,
        thread_id: str | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[InboxItem], int]:
        """Filter and paginate Inbox rows inside SQLite.

        The operator UI can contain years of scan history, so it must not load
        and deserialize every draft merely to render one page.
        """
        if status is not None:
            _validate_status(status)
        if type(page) is not int or page < 1:
            raise ValueError("page 必须为正整数")
        if type(page_size) is not int or page_size < 1 or page_size > 200:
            raise ValueError("page_size 必须在 1..200")
        where: list[str] = []
        params: list[object] = []
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if thread_id is not None:
            where.append("CAST(json_extract(original_draft_json, '$.origin.thread_id') AS TEXT) = ?")
            params.append(thread_id)
        if query is not None and query.strip():
            escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            where.append(
                "(COALESCE(json_extract(working_draft_json, '$.metadata.title'), '') "
                "LIKE ? ESCAPE '\\' OR "
                "COALESCE(json_extract(working_draft_json, '$.metadata.author'), '') "
                "LIKE ? ESCAPE '\\')"
            )
            params.extend((pattern, pattern))
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._connect() as conn:
            self._ensure_initialized(conn)
            total = int(conn.execute(
                "SELECT COUNT(*) FROM inbox_item" + clause, params).fetchone()[0])
            rows = conn.execute(
                "SELECT * FROM inbox_item" + clause
                + " ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
            return [_row_to_item(row) for row in rows], total

    def list_history(self, item_id: int) -> list[InboxStatusHistory]:
        """List status history for an item ordered by id; missing item raises."""
        with self._connect() as conn:
            self._ensure_initialized(conn)
            exists = conn.execute(
                "SELECT id FROM inbox_item WHERE id = ?", (item_id,)
            ).fetchone()
            if exists is None:
                raise InboxItemNotFoundError(f"item {item_id} 不存在")
            rows = conn.execute(
                "SELECT * FROM inbox_status_history WHERE item_id = ? ORDER BY id ASC",
                (item_id,),
            ).fetchall()
            return [_row_to_history(row) for row in rows]

    def transition(self, item_id: int, to_status: str, reason: str) -> InboxItem:
        """Apply a status change and its audit history in one transaction."""
        _validate_status(to_status)
        reason = reason.strip()
        if not reason:
            raise InvalidStatusTransitionError("reason 去除首尾空白后不能为空")
        with self._connect() as conn:
            self._ensure_initialized(conn)
            with conn:
                row = conn.execute(
                    "SELECT * FROM inbox_item WHERE id = ?", (item_id,)
                ).fetchone()
                if row is None:
                    raise InboxItemNotFoundError(f"item {item_id} 不存在")
                from_status = row["status"]
                if from_status == to_status:
                    raise InvalidStatusTransitionError(
                        f"同状态转换被拒绝: {from_status} -> {to_status}"
                    )
                if from_status == "exported":
                    raise InvalidStatusTransitionError("exported 是终态，不能再转换")
                if to_status == "exported" and from_status != "approved":
                    raise InvalidStatusTransitionError("只有 approved 可以转换为 exported")
                now = self._clock()
                conn.execute(
                    "UPDATE inbox_item SET status = ?, updated_at = ? WHERE id = ?",
                    (to_status, now, item_id),
                )
                conn.execute(
                    "INSERT INTO inbox_status_history (item_id, from_status, "
                    "to_status, reason, changed_at) VALUES (?, ?, ?, ?, ?)",
                    (item_id, from_status, to_status, reason, now),
                )
                updated = conn.execute(
                    "SELECT * FROM inbox_item WHERE id = ?", (item_id,)
                ).fetchone()
                return _row_to_item(updated)

    def find_exact_body_duplicates(self, item_id: int) -> list[InboxItem]:
        """Return other items with an identical non-blank body hash, ordered by id."""
        with self._connect() as conn:
            self._ensure_initialized(conn)
            row = conn.execute(
                "SELECT content_sha256 FROM inbox_item WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise InboxItemNotFoundError(f"item {item_id} 不存在")
            if row["content_sha256"] is None:
                return []
            rows = conn.execute(
                "SELECT * FROM inbox_item WHERE content_sha256 = ? AND id != ? "
                "ORDER BY id ASC",
                (row["content_sha256"], item_id),
            ).fetchall()
            return [_row_to_item(row) for row in rows]

    def find_items_by_origin(self, thread_id: str, post_id: str) -> list[InboxItem]:
        """Return all items of one original floor (tid+pid), newest id last.

        Accurate database-side filter, independent of list pagination, so an
        older version is found even when the thread has thousands of items.
        """
        with self._connect() as conn:
            self._ensure_initialized(conn)
            rows = conn.execute(
                "SELECT * FROM inbox_item "
                "WHERE CAST(json_extract(original_draft_json, '$.origin.thread_id') AS TEXT) = ? "
                "AND CAST(json_extract(original_draft_json, '$.origin.post_id') AS TEXT) = ? "
                "ORDER BY id ASC",
                (str(thread_id), str(post_id)),
            ).fetchall()
            return [_row_to_item(row) for row in rows]

    def replace_working_draft(
        self, item_id: int, working_draft: dict[str, object]
    ) -> InboxItem:
        """Atomically replace the working draft of an item in an editable status.

        Status locking and the update share one transaction. An identical
        working draft is an idempotent no-op: clock is not called and
        updated_at is not changed.
        """
        if not isinstance(working_draft, dict):
            raise InboxError("working_draft 必须是字典")
        _, content, _, _ = _working_draft_nodes(working_draft)
        new_json = _stable_json(working_draft)
        body_text = content["body_text"]
        content_sha256 = _sha256_hex(body_text) if body_text.strip() else None
        with self._connect() as conn:
            self._ensure_initialized(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM inbox_item WHERE id = ?", (item_id,)
                ).fetchone()
                if row is None:
                    raise InboxItemNotFoundError(f"item {item_id} 不存在")
                if row["status"] not in EDITABLE_STATUSES:
                    raise InboxItemLockedError(f"状态 {row['status']} 不可编辑")
                current_working_draft = json.loads(row["working_draft_json"])
                if not isinstance(current_working_draft, dict):
                    raise InboxError("数据库中的 working_draft 必须是字典")
                if _protected_working_draft(
                    current_working_draft
                ) != _protected_working_draft(working_draft):
                    raise InboxError("working_draft 包含不允许修改的受保护字段")
                if row["working_draft_json"] == new_json:
                    result = _row_to_item(row)
                else:
                    now = self._clock()
                    conn.execute(
                        "UPDATE inbox_item SET working_draft_json = ?, "
                        "content_sha256 = ?, updated_at = ? WHERE id = ?",
                        (new_json, content_sha256, now, item_id),
                    )
                    updated = conn.execute(
                        "SELECT * FROM inbox_item WHERE id = ?", (item_id,)
                    ).fetchone()
                    result = _row_to_item(updated)
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
            else:
                conn.commit()
                return result
