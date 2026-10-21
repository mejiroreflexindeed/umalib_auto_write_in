"""Regression tests for the operator-facing Demo workflow."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umalib_ingest.domain.models import SubmissionOrigin
from umalib_ingest.inbox import InboxRepository
from umalib_ingest.parser import parse_submission
from umalib_ingest.review import ReviewFields, ReviewService, ReviewSources
from umalib_ingest.web.demo_ui import DemoUiService
from umalib_ingest.web.writer_ui import WriterUiService
from umalib_ingest.workspace.paths import WorkspacePaths
from umalib_ingest.workspace.service import MaintenanceCoordinator, WorkspaceService
from umalib_ingest.workspace.store import WorkspaceStore


class DemoUiFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="demo-ui-flow-")
        self.root = Path(self.tmp.name)
        paths = WorkspacePaths(self.root)
        paths.ensure_root()
        store = WorkspaceStore(paths)
        self.workspace = WorkspaceService(paths, store, None, MaintenanceCoordinator())
        manifest = self.workspace.create("synthetic", "UI 回归")
        self.workspace_id = manifest.workspace_id
        self.repository = InboxRepository(paths.inbox_db(self.workspace_id))
        self.review = ReviewService(self.repository)
        self.writer = WriterUiService(
            self.repository, None, threading.RLock(), workspace_service=self.workspace
        )
        self.demo = DemoUiService(
            self.workspace, self.workspace_id, self.repository, self.review, self.writer
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _approved_item(self):
        draft = parse_submission(
            "[收录]\n[标题] 原标题\n[作者] 原作者\n[标签] 原标签\n\n原正文。",
            origin=SubmissionOrigin(platform="demo"),
        )
        item = self.repository.import_draft(draft)
        self.review.save(
            item.id,
            ReviewFields(
                title="人工标题",
                author="人工作者",
                translator=None,
                series=None,
                tags=["原标签"],
                sources=ReviewSources(
                    nga=[], original_series=[], original=[], translation_series=[], translation=[]
                ),
                body_text="人工正文。",
            ),
        )
        return item

    def test_source_operation_preserves_review_edits(self) -> None:
        item = self._approved_item()
        self.demo.apply_sources(item.id, ["https://example.com/source"])
        saved = self.repository.get(item.id)
        self.assertEqual(saved.working_draft["metadata"]["title"], "人工标题")
        self.assertEqual(saved.working_draft["metadata"]["author"], "人工作者")
        self.assertEqual(saved.working_draft["content"]["body_text"], "人工正文。")
        self.assertEqual(
            saved.working_draft["metadata"]["sources"]["original"],
            ["https://example.com/source"],
        )
        self.repository.transition(item.id, "approved", "回归批准")

    def test_choices_bind_current_working_draft(self) -> None:
        item = self._approved_item()
        self.demo.put_choices(
            item.id,
            {
                "note": None,
                "upload_time_seconds": 1,
                "ordered_sources": [],
                "tag_choices": [],
                "content_mode": "plain",
                "formatted_body": None,
                "plain_loss_confirmed": False,
            },
        )
        self.assertFalse(self.demo.get_choices(item.id)["choices"]["stale"])
        saved = self.repository.get(item.id)
        self.review.save(
            item.id,
            ReviewFields(
                title="人工标题",
                author="人工作者",
                translator=None,
                series=None,
                tags=["原标签"],
                sources=ReviewSources(
                    nga=[], original_series=[], original=[], translation_series=[], translation=[]
                ),
                body_text="改过的正文。",
            ),
        )
        self.assertNotEqual(saved.working_draft, self.repository.get(item.id).working_draft)
        choices = self.demo.get_choices(item.id)["choices"]
        self.assertTrue(choices["stale"])
