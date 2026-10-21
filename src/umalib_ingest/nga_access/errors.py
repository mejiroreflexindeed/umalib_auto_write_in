"""NGA authenticated-read PoC errors (TASK-010)."""

from __future__ import annotations


class NgaAccessError(Exception):
    """A stable, safe, machine-identifiable NGA PoC error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def safe_error(code: str, message: str) -> NgaAccessError:
    """Build an error with a safe Chinese message and the stable code."""
    return NgaAccessError(code, message)
