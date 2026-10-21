"""TASK-013 M03 格式化引擎测试（T15-T26）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umalib_ingest.formatting.extract import extract_body_for_presentation
from umalib_ingest.formatting.quill import render_quill_html, validate_quill_subset
from umalib_ingest.formatting.ubb import parse_basic_ubb


def render(text: str) -> str:
    return render_quill_html(parse_basic_ubb(text).document)


def blockers(text: str) -> list[str]:
    return [d.code for d in parse_basic_ubb(text).diagnostics if d.severity == "blocker"]


class UbbProjectionTests(unittest.TestCase):
    """T15/T16/T17/T20：纯文本投影与基本格式的精确输出."""

    def test_plain_text_and_paragraphs(self) -> None:
        result = parse_basic_ubb("第一行\n\n第三行")
        self.assertEqual(result.plain_text, "第一行\n\n第三行")
        self.assertEqual(render_quill_html(result.document),
                         "<p>第一行</p><p><br></p><p>第三行</p>")

    def test_crlf_normalized(self) -> None:
        self.assertEqual(render("甲\r\n乙"), "<p>甲</p><p>乙</p>")

    def test_trailing_spaces_preserved(self) -> None:
        self.assertEqual(render("甲  "), "<p>甲  </p>")

    def test_bold_italic_underline_strike(self) -> None:
        self.assertEqual(render("[b]加粗[/b]"), "<p><strong>加粗</strong></p>")
        self.assertEqual(render("[i]斜[/i]"), "<p><em>斜</em></p>")
        self.assertEqual(render("[u]下[/u]"), "<p><u>下</u></p>")
        self.assertEqual(render("[s]删[/s]"), "<p><s>删</s></p>")

    def test_nested_exact_output(self) -> None:
        self.assertEqual(render("[b][i]嵌套[/i][/b]"),
                         "<p><strong><em>嵌套</em></strong></p>")
        self.assertEqual(render("[b]外[i]内[/i]外[/b]"),
                         "<p><strong>外</strong><strong><em>内</em></strong><strong>外</strong></p>")

    def test_adjacent_same_style_runs_merge(self) -> None:
        self.assertEqual(render("[b]甲[/b][b]乙[/b]"),
                         "<p><strong>甲乙</strong></p>")

    def test_unknown_keyword_text_kept(self) -> None:
        text = "正文[术语]注释说明"
        result = parse_basic_ubb(text)
        self.assertEqual(result.plain_text, text)
        self.assertIn("[术语]", render(text))


class UbbColorTests(unittest.TestCase):
    """T17/T18：颜色规范化与非法值阻断."""

    def test_named_and_hex_colors(self) -> None:
        self.assertEqual(render("[color=red]红[/color]"),
                         '<p><span style="color: #ff0000;">红</span></p>')
        self.assertEqual(render("[color=#f00]短[/color]"),
                         '<p><span style="color: #ff0000;">短</span></p>')
        self.assertEqual(render("[color=#3366ff]长[/color]"),
                         '<p><span style="color: #3366ff;">长</span></p>')

    def test_invalid_color_blocked_and_preserved(self) -> None:
        result = parse_basic_ubb("[color=url(x)]恶意[/color]")
        self.assertIn("invalid_color", [d.code for d in result.diagnostics])
        self.assertIn("[color=url(x)]", result.plain_text)


class UbbStructureTests(unittest.TestCase):
    """T19/T22/T23：结构错误与不支持格式."""

    def test_cross_close_blocked(self) -> None:
        self.assertIn("format_cross_close", blockers("[b]x[/i]y[/b]"))

    def test_unclosed_blocked(self) -> None:
        self.assertIn("format_unclosed", blockers("[b]没有闭合"))

    def test_depth_limit_blocked(self) -> None:
        deep = "[b]" * 20 + "x" + "[/b]" * 20
        self.assertIn("format_depth_exceeded", blockers(deep))

    def test_unsupported_format_preserved_with_warning(self) -> None:
        result = parse_basic_ubb("前[img]http://x/y.png[/img]后")
        codes = [d.code for d in result.diagnostics]
        self.assertIn("unsupported_format", codes)
        self.assertIn("[img]", result.plain_text)
        self.assertTrue(result.ready)

    def test_trailing_annotations_plain(self) -> None:
        text = "正文\n\n[关键词]注释一\n[关键词]注释二"
        result = parse_basic_ubb(text)
        self.assertTrue(all(p.annotation for p in result.document.paragraphs[2:]))
        self.assertEqual(render_quill_html(result.document),
                         "<p>正文</p><p><br></p><p>[关键词]注释一</p><p>[关键词]注释二</p>")

    def test_formatted_annotation_blocked(self) -> None:
        self.assertIn("annotation_format_unsupported", blockers("正文\n\n[b][关键词]注释[/b]"))

    def test_annotation_reference_in_body_unchanged(self) -> None:
        text = "正文引用了[关键词]这个词\n\n[关键词]注释"
        result = parse_basic_ubb(text)
        self.assertFalse(result.document.paragraphs[0].annotation)
        self.assertTrue(result.document.paragraphs[-1].annotation)
        self.assertIn("[关键词]这个词", result.document.paragraphs[0].plain_text)


class QuillSubsetTests(unittest.TestCase):
    """T31：安全子集独立验证；规范输出必须自洽."""

    def test_render_output_is_always_valid(self) -> None:
        cases = [
            "普通",
            "[b][i][color=blue]组合[/color][/i][/b]",
            "[s][u]划[/u][/s]",
            "正文\n\n[关键词]注释",
            "[color=#123abc]色[/color]",
        ]
        for case in cases:
            self.assertTrue(validate_quill_subset(render(case)), case)

    def test_rejects_script_event_svg_img(self) -> None:
        for bad in (
            "<p><script>alert(1)</script></p>",
            "<p onclick=\"x\">x</p>",
            "<svg></svg>",
            "<img src=\"x\">",
            '<p><span style="color: url(x);">x</span></p>',
            '<p><span style="background: red;">x</span></p>',
            "<a href=\"x\">x</a>",
            "<p>&nbsp;</p>",
        ):
            self.assertFalse(validate_quill_subset(bad), bad)


class BodyExtractionTests(unittest.TestCase):
    """T24/T25/T26：正文建议提取规则."""

    def test_color_first_line_is_body_not_metadata(self) -> None:
        extraction = extract_body_for_presentation("[color=red]红字[/color]。")
        self.assertEqual(extraction.mode, "auto")
        self.assertEqual(extraction.body, "[color=red]红字[/color]。")

    def test_field_block_removed_body_kept(self) -> None:
        raw = "[收录]\n[标题] 标题甲\n[作者] 作者甲\n[标签] 标签甲\n\n正文[术语]注释。"
        extraction = extract_body_for_presentation(raw)
        self.assertEqual(extraction.body, "正文[术语]注释。")

    def test_internal_blank_lines_preserved(self) -> None:
        raw = "[收录]\n[标题] 标题\n[作者] 作者\n[标签] 标签\n\n第一段\n\n第三段"
        extraction = extract_body_for_presentation(raw)
        self.assertEqual(extraction.body, "第一段\n\n第三段")

    def test_pretrigger_text_not_mangled(self) -> None:
        raw = "写在前面\n[收录]\n[标题] 标题\n[作者] 作者\n\n正文。"
        extraction = extract_body_for_presentation(raw)
        self.assertEqual(extraction.body, raw)

    def test_br_and_single_entity_decode(self) -> None:
        extraction = extract_body_for_presentation("甲<br>乙 &amp; 丙")
        self.assertEqual(extraction.body, "甲\n乙 & 丙")

    def test_unknown_html_blocks_auto(self) -> None:
        extraction = extract_body_for_presentation("<div>内容</div>")
        self.assertEqual(extraction.mode, "manual")
        self.assertIn("unsupported_html_extraction",
                      [d.code for d in extraction.diagnostics])


if __name__ == "__main__":
    unittest.main()
