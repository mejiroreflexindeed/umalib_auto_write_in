"""Read-only UmaLib inspection errors (TASK-006)."""

from __future__ import annotations


class UmaLibInspectionError(Exception):
    """Base class for read-only UmaLib inspection errors."""


class UmaLibPathError(UmaLibInspectionError):
    """Raised when the target path is missing, a directory or not a regular file."""


class UmaLibDatabaseError(UmaLibInspectionError):
    """Raised when the target is not a readable, queryable SQLite database."""


class AdapterError(Exception):
    """Base class for pure change-plan adapter errors."""


class AdapterContentError(AdapterError):
    """Raised by the plain-text renderer for empty or disallowed content."""
