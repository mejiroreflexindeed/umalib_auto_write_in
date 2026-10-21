"""Tests for the deterministic submission parser (TASK-001)."""

import json
import unittest
from pathlib import Path

from umalib_ingest.domain import SubmissionDraft, SubmissionOrigin, SubmissionTrigger
from umalib_ingest.parser import parse_submission

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "parser"


class FixtureTests(unittest.TestCase):
    """Compare full to_dict() output against the parser fixtures."""

    def test_fixtures(self) -> None:
        txt_files = sorted(FIXTURES_DIR.glob("*.txt"))
        self.assertTrue(txt_files, "no parser fixtures found")
        for txt_file in txt_files:
            with self.subTest(fixture=txt_file.name):
                expected_file = txt_file.with_suffix(".expected.json")
                self.assertTrue(expected_file.exists(), f"missing {expected_file.name}")
                raw_text = txt_file.read_text(encoding="utf-8")
                expected = json.loads(expected_file.read_text(encoding="utf-8"))
                self.assertEqual(parse_submission(raw_text).to_dict(), expected)


class ParseSubmissionUnitTests(unittest.TestCase):
    """Required coverage: the eight TASK-001 cases plus edge behaviour."""

    def test_empty_string_input(self) -> None:
        draft = parse_submission("")
        self.assertEqual(draft.parser_status, "ignored")
        self.assertFalse(draft.trigger.found)
        self.assertEqual(draft.content.raw_text, "")
        self.assertEqual(draft.content.body_text, "")
        self.assertEqual(draft.raw_fields, {})
        self.assertEqual(draft.diagnostics.missing_fields, [])
        self.assertEqual(draft.diagnostics.warnings, [])
        self.assertEqual(draft.diagnostics.unknown_fields, [])

    def test_no_trigger_ordinary_discussion(self) -> None:
        draft = parse_submission("大家好，这是一条普通讨论。\n没有任何投稿标记。\n")
        self.assertEqual(draft.parser_status, "ignored")
        self.assertFalse(draft.trigger.found)
        self.assertIsNone(draft.trigger.marker)
        self.assertIsNone(draft.metadata.title)
        self.assertEqual(draft.metadata.tags, [])
        self.assertEqual(draft.diagnostics.missing_fields, [])
        self.assertEqual(draft.content.body_text, "大家好，这是一条普通讨论。\n没有任何投稿标记。")

    def test_missing_title_author_tags(self) -> None:
        draft = parse_submission("[收录]\n\n只有正文。\n")
        self.assertEqual(draft.parser_status, "accepted")
        self.assertEqual(draft.diagnostics.missing_fields, ["title", "author", "tags"])
        self.assertEqual(draft.metadata.title, None)
        self.assertEqual(draft.metadata.author, None)
        self.assertEqual(draft.metadata.tags, [])

    def test_missing_body(self) -> None:
        draft = parse_submission("[收录]\n[标题] 只有标题\n[作者] 作者\n[标签] 标签\n")
        self.assertEqual(draft.diagnostics.missing_fields, ["body"])
        self.assertEqual(draft.content.body_text, "")

    def test_duplicate_singleton_keeps_first_non_empty(self) -> None:
        draft = parse_submission("[收录]\n[标题] 第一标题\n[标题]\n[标题] 第二标题\n\n正文\n")
        self.assertEqual(draft.metadata.title, "第一标题")
        self.assertEqual(draft.to_dict()["raw_fields"]["标题"], ["第一标题", "", "第二标题"])
        self.assertEqual(
            [(w.code, w.line) for w in draft.diagnostics.warnings],
            [("empty_field", 3), ("duplicate_field", 3), ("duplicate_field", 4)],
        )

    def test_duplicate_first_empty_then_value_kept(self) -> None:
        draft = parse_submission("[收录]\n[标题]\n[标题] 补上的标题\n\n正文\n")
        self.assertEqual(draft.metadata.title, "补上的标题")
        self.assertEqual(
            [(w.code, w.line) for w in draft.diagnostics.warnings],
            [("empty_field", 2), ("duplicate_field", 3)],
        )

    def test_tags_split_and_stable_dedup(self) -> None:
        draft = parse_submission(
            "[收录]\n[标签] 角色, 角色，短篇\n[标签] 短篇、日常\n\n正文\n"
        )
        self.assertEqual(draft.metadata.tags, ["角色", "短篇", "日常"])

    def test_unknown_field_preserved(self) -> None:
        draft = parse_submission("[收录]\n[未来字段] 暂未支持\n\n正文\n")
        self.assertEqual(draft.raw_fields["未来字段"], ["暂未支持"])
        unknown = draft.diagnostics.unknown_fields
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0].name, "未来字段")
        self.assertEqual(unknown[0].value, "暂未支持")
        self.assertEqual(unknown[0].line, 2)

    def test_author_slash_not_split(self) -> None:
        draft = parse_submission("[收录]\n[作者] 作者甲/作者乙\n\n正文\n")
        self.assertEqual(draft.metadata.author, "作者甲/作者乙")
        self.assertEqual(draft.to_dict()["raw_fields"]["作者"], ["作者甲/作者乙"])

    def test_source_not_url_normalized(self) -> None:
        draft = parse_submission(
            "[收录]\n[原作] https://example.com/a?tid=1&to=1 \n[译作] http://x.example/b/../c\n\n正文\n"
        )
        self.assertEqual(
            draft.metadata.sources.original,
            ["https://example.com/a?tid=1&to=1"],
        )
        self.assertEqual(
            draft.metadata.sources.translation,
            ["http://x.example/b/../c"],
        )

    def test_sources_keep_occurrence_order(self) -> None:
        draft = parse_submission(
            "[收录]\n[原作] 一\n[原作] 二\n[原作系列] 系列\n\n正文\n"
        )
        self.assertEqual(draft.metadata.sources.original, ["一", "二"])
        self.assertEqual(draft.metadata.sources.original_series, ["系列"])

    def test_trigger_must_be_standalone_line(self) -> None:
        draft = parse_submission("[收录] 这不是触发\n正文\n")
        self.assertEqual(draft.parser_status, "ignored")
        self.assertFalse(draft.trigger.found)

    def test_trigger_allows_surrounding_whitespace(self) -> None:
        draft = parse_submission("  [收录]  \n[标题] 标题\n\n正文\n")
        self.assertTrue(draft.trigger.found)
        self.assertEqual(draft.trigger.marker, "收录")
        self.assertEqual(draft.metadata.title, "标题")

    def test_body_annotation_kept_after_body_start(self) -> None:
        draft = parse_submission(
            "[收录]\n[标题] 标题\n\n正文中的[术语]保持。\n[术语]这是注释\n"
        )
        self.assertEqual(
            draft.content.body_text,
            "正文中的[术语]保持。\n[术语]这是注释",
        )

    def test_optional_body_separator_starts_body_without_becoming_unknown_field(self) -> None:
        draft = parse_submission(
            "[收录]\n[标题] 标题\n[作者] 作者\n[标签] 短篇\n"
            "[正文]\n第一段\n[术语]这是正文内注释\n"
        )
        self.assertEqual(draft.content.body_text, "第一段\n[术语]这是正文内注释")
        self.assertNotIn("正文", draft.raw_fields)
        self.assertEqual(draft.diagnostics.unknown_fields, [])

    def test_raw_text_preserved_exactly(self) -> None:
        raw = "[收录]\n[标题] 标题\n\n正文\n"
        self.assertEqual(parse_submission(raw).content.raw_text, raw)

    def test_default_origin_is_empty_nga(self) -> None:
        draft = parse_submission("正文\n")
        self.assertEqual(draft.origin.platform, "nga")
        self.assertIsNone(draft.origin.thread_id)
        self.assertIsNone(draft.origin.post_id)
        self.assertIsNone(draft.origin.floor)
        self.assertIsNone(draft.origin.canonical_url)

    def test_custom_origin_used(self) -> None:
        origin = SubmissionOrigin(
            thread_id="123",
            post_id="456",
            floor=7,
            canonical_url="https://bbs.nga.cn/read.php?tid=123&pid=456",
        )
        draft = parse_submission("正文\n", origin=origin)
        self.assertEqual(draft.origin.thread_id, "123")
        self.assertEqual(draft.origin.floor, 7)
        self.assertEqual(
            draft.to_dict()["origin"]["canonical_url"],
            "https://bbs.nga.cn/read.php?tid=123&pid=456",
        )

    def test_to_dict_shape_matches_schema(self) -> None:
        draft = parse_submission("[收录]\n[标题] 标题\n\n正文\n").to_dict()
        self.assertEqual(
            set(draft.keys()),
            {
                "schema_version",
                "parser_status",
                "origin",
                "trigger",
                "metadata",
                "content",
                "raw_fields",
                "diagnostics",
            },
        )
        self.assertEqual(
            set(draft["diagnostics"].keys()),
            {"missing_fields", "warnings", "unknown_fields"},
        )
        self.assertEqual(draft["schema_version"], "1.0")

    def test_line_numbers_start_at_one(self) -> None:
        draft = parse_submission("[收录]\n[未知] 值\n[标题]\n[标题] 一\n\n正文\n")
        self.assertEqual(draft.diagnostics.unknown_fields[0].line, 2)
        self.assertEqual(draft.diagnostics.warnings[0].line, 3)
        self.assertEqual(draft.diagnostics.warnings[1].line, 4)

    def test_accepted_without_any_field_keeps_all_required_missing(self) -> None:
        draft = parse_submission("[收录]")
        self.assertEqual(draft.parser_status, "accepted")
        self.assertEqual(
            draft.diagnostics.missing_fields,
            ["title", "author", "tags", "body"],
        )


class NewlineHandlingTests(unittest.TestCase):
    """Regression: LF, CRLF and CR line endings must not leave \\r residue."""

    def test_crlf_input(self) -> None:
        raw = "[收录]\r\n[标题] 标题\r\n[作者] 作者\r\n\r\n正文。\r\n"
        draft = parse_submission(raw)
        self.assertEqual(draft.parser_status, "accepted")
        self.assertEqual(draft.metadata.title, "标题")
        self.assertEqual(draft.metadata.author, "作者")
        self.assertEqual(draft.content.body_text, "正文。")
        self.assertEqual(draft.diagnostics.missing_fields, ["tags"])

    def test_cr_input(self) -> None:
        raw = "[收录]\r[标题] 标题\r\r正文。\r"
        draft = parse_submission(raw)
        self.assertEqual(draft.parser_status, "accepted")
        self.assertEqual(draft.metadata.title, "标题")
        self.assertEqual(draft.content.body_text, "正文。")
        self.assertEqual(draft.diagnostics.missing_fields, ["author", "tags"])

    def test_mixed_line_endings(self) -> None:
        raw = "[收录]\r\n[标题] 标题\r[标签] 短篇\n\r正文\n"
        draft = parse_submission(raw)
        self.assertEqual(draft.metadata.title, "标题")
        self.assertEqual(draft.metadata.tags, ["短篇"])
        self.assertEqual(draft.content.body_text, "正文")
        self.assertEqual(draft.diagnostics.missing_fields, ["author"])

    def test_crlf_blank_line_not_misjudged_as_body(self) -> None:
        raw = "[收录]\r\n[标题] 标题\r\n\r\n正文\r\n"
        draft = parse_submission(raw)
        self.assertEqual(draft.metadata.title, "标题")
        self.assertEqual(draft.content.body_text, "正文")

    def test_whitespace_only_lines_are_blank_in_field_block(self) -> None:
        raw = "[收录]\n[标题] 标题\n   \n\t\n[作者] 作者\n\n正文\n"
        draft = parse_submission(raw)
        self.assertEqual(draft.metadata.title, "标题")
        self.assertEqual(draft.metadata.author, "作者")
        self.assertEqual(draft.content.body_text, "正文")

    def test_whitespace_only_body_reports_missing_body(self) -> None:
        draft = parse_submission("[收录]\n[标题] 标题\n[作者] 作者\n[标签] 短篇\n\n   \n\t\n")
        self.assertIn("body", draft.diagnostics.missing_fields)
        self.assertEqual(draft.content.body_text.strip(), "")

    def test_whitespace_only_lines_do_not_start_body(self) -> None:
        draft = parse_submission("[收录]\n   \n\t\n\n[标题] 标题\n\n正文\n")
        self.assertEqual(draft.metadata.title, "标题")
        self.assertEqual(draft.content.body_text, "正文")

    def test_raw_text_preserved_with_crlf(self) -> None:
        raw = "[收录]\r\n[标题] 标题\r\n\r\n正文\r\n"
        self.assertEqual(parse_submission(raw).content.raw_text, raw)

    def test_raw_text_preserved_with_cr(self) -> None:
        raw = "[收录]\r[标题] 标题\r\r正文\r"
        self.assertEqual(parse_submission(raw).content.raw_text, raw)

    def test_ignored_case_with_crlf(self) -> None:
        raw = "普通讨论。\r\n没有标记。\r\n"
        draft = parse_submission(raw)
        self.assertEqual(draft.parser_status, "ignored")
        self.assertEqual(draft.content.body_text, "普通讨论。\n没有标记。")


class ModelDefaultsTests(unittest.TestCase):
    """Regression: default construction must not raise."""

    def test_submission_trigger_default(self) -> None:
        trigger = SubmissionTrigger()
        self.assertFalse(trigger.found)
        self.assertIsNone(trigger.marker)

    def test_submission_draft_default_construction(self) -> None:
        draft = SubmissionDraft()
        self.assertEqual(draft.parser_status, "ignored")
        self.assertFalse(draft.trigger.found)
        self.assertEqual(draft.origin.platform, "nga")
        self.assertEqual(draft.to_dict()["trigger"], {"found": False, "marker": None})


if __name__ == "__main__":
    unittest.main()
