"""Portable local path defaults for the standalone Windows application."""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def private_state_root() -> Path:
    """Return the private runtime-data root outside the source repository."""
    configured = os.environ.get("UMALIB_PRIVATE_STATE_ROOT")
    if configured:
        return Path(configured)
    return PROJECT_ROOT.parent / "umalib-private-state"


def operator_root() -> Path:
    """Return the default operator workspace root."""
    configured = os.environ.get("UMALIB_OPERATOR_ROOT")
    if configured:
        return Path(configured)
    return private_state_root() / "operator-user"


def source_database_candidate() -> Path:
    """Return the configured or adjacent read-only UmaLib source candidate."""
    configured = os.environ.get("UMALIB_SOURCE_DB")
    if configured:
        return Path(configured)
    return PROJECT_ROOT.parent / "umalib-test-data" / "main-copy.db"


def nga_profile_dir() -> Path:
    """Return the dedicated NGA browser profile directory."""
    return private_state_root() / "nga-profile"


def nga_snapshot_dir() -> Path:
    """Return the default NGA snapshot directory."""
    return private_state_root() / "nga-snapshots"


def nga_state_file() -> Path:
    """Return the default watcher state file."""
    return private_state_root() / "nga-watcher-state.json"
