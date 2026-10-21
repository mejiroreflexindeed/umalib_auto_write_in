"""Approved submission handoff errors (TASK-005)."""

from __future__ import annotations


class HandoffError(Exception):
    """Base class for approved-package handoff errors."""


class HandoffNotReadyError(HandoffError):
    """Raised when an item cannot yet produce an approved submission package."""
