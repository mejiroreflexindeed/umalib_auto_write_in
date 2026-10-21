"""Read-only UmaLib inspection and pure change-plan boundary (TASK-006/007)."""

from __future__ import annotations

from umalib_ingest.umalib_adapter.change_plan import (
    CONFIRMED_SCHEMA_FINGERPRINT_SHA256,
    AdapterChoices,
    AdapterDiagnostic,
    TagChoice,
    TargetTag,
    TargetTagCatalog,
    UmaLibChangePlan,
    build_umalib_change_plan,
    render_plain_text_quill_html,
)
from umalib_ingest.umalib_adapter.errors import (
    AdapterContentError,
    AdapterError,
    UmaLibDatabaseError,
    UmaLibInspectionError,
    UmaLibPathError,
)
from umalib_ingest.umalib_adapter.inspector import inspect_umalib_database
from umalib_ingest.umalib_adapter.report import (
    InspectionIssue,
    UmaLibInspectionReport,
)

__all__ = [
    "AdapterChoices",
    "AdapterContentError",
    "AdapterDiagnostic",
    "AdapterError",
    "CONFIRMED_SCHEMA_FINGERPRINT_SHA256",
    "InspectionIssue",
    "TagChoice",
    "TargetTag",
    "TargetTagCatalog",
    "UmaLibChangePlan",
    "UmaLibDatabaseError",
    "UmaLibInspectionError",
    "UmaLibInspectionReport",
    "UmaLibPathError",
    "build_umalib_change_plan",
    "inspect_umalib_database",
    "render_plain_text_quill_html",
]
