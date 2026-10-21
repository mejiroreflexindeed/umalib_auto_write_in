"""Inbox errors (TASK-002)."""

from __future__ import annotations


class InboxError(Exception):
    """Base class for all Inbox errors."""


class InboxItemNotFoundError(InboxError):
    """Raised when an inbox item does not exist."""


class InboxItemLockedError(InboxError):
    """Raised when an item's status forbids editing its working draft."""


class InvalidStatusTransitionError(InboxError):
    """Raised for illegal status values, illegal transitions or empty reasons."""


class UnsupportedInboxSchemaError(InboxError):
    """Raised when the SQLite file is not a supported Inbox database."""
