"""Review editing errors (TASK-003)."""

from __future__ import annotations


class ReviewError(Exception):
    """Base class for all review editing errors."""


class InvalidReviewFieldsError(ReviewError):
    """Raised for wrong field types or malformed working draft structure."""
