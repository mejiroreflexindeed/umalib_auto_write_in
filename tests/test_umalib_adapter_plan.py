"""Tests for the pure UmaLib change-plan core (TASK-007).

All fixtures are synthetic metadata only; no real article content is copied.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from umalib_ingest.handoff.package import ApprovedSubmissionPackage
from umalib_ingest.umalib_adapter import (
    AdapterChoices,
    AdapterContentError,
    AdapterDiagnostic,
    AdapterError,
    TagChoice,
    TargetTag,
    TargetTagCatalog,
    UmaLibChangePlan,
    build_umalib_change_plan,
    render_plain_text_quill_html,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "adapter"
CONFIRMED_FINGERPRINT = (
    "d7b1966992e75819e37d41ae9c091ec21c2cdf9c5b6727af78f95398d0179aa8"
)
SYNTHETIC_DB_SHA = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def stable_json(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def make_package(spec: dict | None = None, *, tamper_hash: bool = False):
    data = load_json_fixture("synthetic_package.json") if spec is None else spec
    inbox = copy.deepcopy(data["inbox"])
    submission = copy.deepcopy(data["submission"])
    package_hash = sha256_hex(
        stable_json(
            {
                "schema_version": data["schema_version"],
                "inbox": inbox,
                "submission": submission,
            }
        )
    )
    if tamper_hash:
        package_hash = "0" * 64
    return ApprovedSubmissionPackage(
        schema_version=data["schema_version"],
        inbox=inbox,
        submission=submission,
        package_sha256=package_hash,
    )


def make_catalog(spec: dict | None = None) -> TargetTagCatalog:
    data = load_json_fixture("synthetic_catalog.json") if spec is None else spec
    return TargetTagCatalog(
        database_sha256=data["database_sha256"],
        schema_fingerprint_sha256=data["schema_fingerprint_sha256"],
        tags=tuple(
            TargetTag(
                id=item["id"],
                name=item["name"],
                type=item["type"],
                usage_count=item.get("usage_count"),
            )
            for item in data["tags"]
        ),
    )


def make_choices(
    *,
    upload_time_seconds: int = 1700000000,
    note: str | None = None,
    ordered_sources: tuple[str, ...] = (),
    tag_choices: tuple[TagChoice, ...] = (),
) -> AdapterChoices:
    return AdapterChoices(
        upload_time_seconds=upload_time_seconds,
        note=note,
        ordered_sources=ordered_sources,
        tag_choices=tag_choices,
    )


def build_with_choices(package=None, choices=None, catalog=None):
    package = make_package() if package is None else package
    choices = make_choices() if choices is None else choices
    catalog = make_catalog() if catalog is None else catalog
    return build_umalib_change_plan(package, choices, catalog)


def spec_from_objects(package, choices, catalog) -> dict:
    return {
        "package": {
            "schema_version": package.schema_version,
            "inbox": package.inbox,
            "submission": package.submission,
            "package_sha256": package.package_sha256,
        },
        "choices": {
            "upload_time_seconds": choices.upload_time_seconds,
            "note": choices.note,
            "ordered_sources": list(choices.ordered_sources),
            "tag_choices": [
                {
                    "requested_kind": c.requested_kind,
                    "requested_value": c.requested_value,
                    "existing_tag_id": c.existing_tag_id,
                    "new_tag_type": c.new_tag_type,
                }
                for c in choices.tag_choices
            ],
        },
        "catalog": {
            "database_sha256": catalog.database_sha256,
            "schema_fingerprint_sha256": catalog.schema_fingerprint_sha256,
            "tags": [
                {"id": t.id, "name": t.name, "type": t.type, "usage_count": t.usage_count}
                for t in catalog.tags
            ],
        },
    }


def spec_to_plan(spec: dict):
    """Rebuild a plan from a JSON-able spec; used by the cross-process test."""
    from umalib_ingest.handoff.package import ApprovedSubmissionPackage
    from umalib_ingest.umalib_adapter import (
        AdapterChoices,
        TagChoice,
        TargetTag,
        TargetTagCatalog,
        build_umalib_change_plan,
    )

    package = ApprovedSubmissionPackage(
        schema_version=spec["package"]["schema_version"],
        inbox=spec["package"]["inbox"],
        submission=spec["package"]["submission"],
        package_sha256=spec["package"]["package_sha256"],
    )
    choices = AdapterChoices(
        upload_time_seconds=spec["choices"]["upload_time_seconds"],
        note=spec["choices"]["note"],
        ordered_sources=tuple(spec["choices"]["ordered_sources"]),
        tag_choices=tuple(
            TagChoice(
                requested_kind=c["requested_kind"],
                requested_value=c["requested_value"],
                existing_tag_id=c["existing_tag_id"],
                new_tag_type=c["new_tag_type"],
            )
            for c in spec["choices"]["tag_choices"]
        ),
    )
    catalog = TargetTagCatalog(
        database_sha256=spec["catalog"]["database_sha256"],
        schema_fingerprint_sha256=spec["catalog"]["schema_fingerprint_sha256"],
        tags=tuple(TargetTag(**item) for item in spec["catalog"]["tags"]),
    )
    return build_umalib_change_plan(package, choices, catalog)


def ready_choices() -> AdapterChoices:
    """Choices that fully resolve the synthetic fixture package."""
    return AdapterChoices(
        upload_time_seconds=1700000000,
        note="合成备注",
        ordered_sources=(
            "https://example.com/original-1",
            "https://example.com/translation-1",
            "https://example.com/os-1",
            "https://example.com/ts-1",
        ),
        tag_choices=(
            TagChoice("tag", "合成角色甲", existing_tag_id=1),
            TagChoice("tag", "合成重复标签", existing_tag_id=3),
            TagChoice("series", "合成系列乙", existing_tag_id=2),
        ),
    )


class RendererTests(unittest.TestCase):
    def test_example_rendering(self) -> None:
        self.assertEqual(
            render_plain_text_quill_html("第一行\n\n[关键词]注释\n"),
            "<p>第一行</p><p><br></p><p>[关键词]注释</p><p><br></p>",
        )

    def test_line_endings_normalized(self) -> None:
        expected = "<p>甲</p><p>乙</p>"
        self.assertEqual(render_plain_text_quill_html("甲\n乙"), expected)
        self.assertEqual(render_plain_text_quill_html("甲\r\n乙"), expected)
        self.assertEqual(render_plain_text_quill_html("甲\r乙"), expected)

    def test_consecutive_empty_lines_and_trailing(self) -> None:
        self.assertEqual(
            render_plain_text_quill_html("a\n\n\nb\n"),
            "<p>a</p><p><br></p><p><br></p><p>b</p><p><br></p>",
        )
        self.assertEqual(
            render_plain_text_quill_html("a\n\n"),
            "<p>a</p><p><br></p><p><br></p>",
        )
        self.assertEqual(
            render_plain_text_quill_html("a\n"),
            "<p>a</p><p><br></p>",
        )
        self.assertEqual(render_plain_text_quill_html("a"), "<p>a</p>")

    def test_html_escaped_not_interpreted(self) -> None:
        self.assertEqual(
            render_plain_text_quill_html('<script>alert("x")</script>&'),
            '<p>&lt;script&gt;alert("x")&lt;/script&gt;&amp;</p>',
        )

    def test_annotation_kept_as_text(self) -> None:
        self.assertEqual(
            render_plain_text_quill_html("[关键词]注释"),
            "<p>[关键词]注释</p>",
        )

    def test_tab_and_unicode_preserved(self) -> None:
        self.assertEqual(
            render_plain_text_quill_html("\t制表 中文\u2028行分隔"),
            "<p>\t制表 中文\u2028行分隔</p>",
        )

    def test_empty_and_disallowed_controls(self) -> None:
        for bad in ("", "\n", "   ", "\t\n", "\x00", "a\x1bb", "a\x07"):
            with self.subTest(bad=bad):
                with self.assertRaises(AdapterContentError):
                    render_plain_text_quill_html(bad)

    def test_output_only_minimal_structure(self) -> None:
        out = render_plain_text_quill_html("x\n\ny\n[z]注")
        self.assertIsNotNone(
            re.fullmatch(r"(?:<p><br></p>|<p>[^<>]*</p>)+", out)
        )
        for forbidden in ("<img", "<a ", "<span", "style=", "class=", "script", "src="):
            self.assertNotIn(forbidden, out)


class PlanHashTests(unittest.TestCase):
    def test_minimal_complete_plan_ready(self) -> None:
        plan = build_with_choices(choices=ready_choices())
        data = plan.to_dict()
        self.assertTrue(data["ready"])
        self.assertEqual(data["article"]["name"], "合成投稿标题")
        self.assertEqual(data["article"]["note"], "合成备注")
        self.assertEqual(data["article"]["upload_time"], 1700000000)
        self.assertEqual(
            data["article"]["source"],
            "https://example.com/original-1 https://example.com/translation-1 "
            "https://example.com/os-1 https://example.com/ts-1",
        )
        self.assertEqual(
            data["tags"]["existing"],
            [
                {"id": 1, "name": "合成角色甲", "type": 1},
                {"id": 2, "name": "合成系列乙", "type": 2},
                {"id": 3, "name": "合成重复标签", "type": 0},
            ],
        )
        self.assertEqual(data["tags"]["create"], [])
        self.assertEqual(data["diagnostics"]["blockers"], [])
        self.assertRegex(data["plan_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            data["input"]["package_sha256"], make_package().package_sha256
        )

    def test_repeat_build_identical(self) -> None:
        first = build_with_choices(choices=ready_choices())
        second = build_with_choices(choices=ready_choices())
        self.assertEqual(stable_json(first.to_dict()), stable_json(second.to_dict()))
        self.assertEqual(first.plan_sha256, second.plan_sha256)

    def test_cross_process_build_identical(self) -> None:
        package = make_package()
        choices = ready_choices()
        catalog = make_catalog()
        plan = build_umalib_change_plan(package, choices, catalog)
        expected = stable_json(plan.to_dict()).encode("utf-8")

        spec = spec_from_objects(package, choices, catalog)
        env = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
        script = (
            "import sys, json; sys.path.insert(0, 'tests'); "
            "from test_umalib_adapter_plan import spec_to_plan, stable_json; "
            "spec = json.loads(sys.stdin.buffer.read().decode('utf-8')); "
            "sys.stdout.buffer.write(stable_json(spec_to_plan(spec).to_dict()).encode('utf-8'))"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            input=json.dumps(spec, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            cwd=str(PROJECT_ROOT),
            env=env,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stdout, expected)

    def test_field_change_changes_plan_hash(self) -> None:
        base = build_with_choices(choices=ready_choices())
        variant_upload = build_with_choices(
            choices=make_choices(
                upload_time_seconds=1700000001,
                ordered_sources=ready_choices().ordered_sources,
                tag_choices=ready_choices().tag_choices,
            )
        )
        self.assertNotEqual(base.plan_sha256, variant_upload.plan_sha256)
        variant_note = build_with_choices(
            choices=make_choices(
                note="另一备注",
                ordered_sources=ready_choices().ordered_sources,
                tag_choices=ready_choices().tag_choices,
            )
        )
        self.assertNotEqual(base.plan_sha256, variant_note.plan_sha256)

    def test_hash_excludes_itself_and_algorithm(self) -> None:
        plan = build_with_choices(choices=ready_choices())
        data = plan.to_dict()
        payload = {key: value for key, value in data.items() if key != "plan_sha256"}
        self.assertEqual(sha256_hex(stable_json(payload)), data["plan_sha256"])

    def test_package_hash_tamper_blocks(self) -> None:
        tampered = make_package(tamper_hash=True)
        plan = build_with_choices(package=tampered, choices=ready_choices())
        data = plan.to_dict()
        self.assertFalse(data["ready"])
        codes = [b["code"] for b in data["diagnostics"]["blockers"]]
        self.assertIn("approved_package_hash_mismatch", codes)

    def test_target_database_sha_malformed_blocks(self) -> None:
        spec = load_json_fixture("synthetic_catalog.json")
        spec["database_sha256"] = "not-a-hash"
        plan = build_with_choices(choices=ready_choices(), catalog=make_catalog(spec))
        data = plan.to_dict()
        self.assertFalse(data["ready"])
        self.assertIn(
            "target_database_sha256_malformed",
            [b["code"] for b in data["diagnostics"]["blockers"]],
        )
        self.assertEqual(data["input"]["target_database_sha256"], "0" * 64)

    def test_schema_fingerprint_mismatch_blocks(self) -> None:
        spec = load_json_fixture("synthetic_catalog.json")
        spec["schema_fingerprint_sha256"] = "e" * 64
        plan = build_with_choices(choices=ready_choices(), catalog=make_catalog(spec))
        data = plan.to_dict()
        self.assertFalse(data["ready"])
        self.assertIn(
            "target_schema_fingerprint_mismatch",
            [b["code"] for b in data["diagnostics"]["blockers"]],
        )
        spec["schema_fingerprint_sha256"] = "xyz"
        plan = build_with_choices(choices=ready_choices(), catalog=make_catalog(spec))
        data = plan.to_dict()
        self.assertIn(
            "target_schema_fingerprint_malformed",
            [b["code"] for b in data["diagnostics"]["blockers"]],
        )
        self.assertEqual(
            data["input"]["target_schema_fingerprint_sha256"], "0" * 64
        )

    def test_catalog_hash_stable_and_binds_plan(self) -> None:
        plan = build_with_choices(choices=ready_choices())
        expected = sha256_hex(
            stable_json(
                [{"id": t.id, "name": t.name, "type": t.type} for t in sorted(make_catalog().tags, key=lambda x: x.id)]
            )
        )
        self.assertEqual(plan.to_dict()["input"]["target_tag_catalog_sha256"], expected)
        spec = load_json_fixture("synthetic_catalog.json")
        spec["tags"][3]["usage_count"] = 999
        plan2 = build_with_choices(choices=ready_choices(), catalog=make_catalog(spec))
        self.assertEqual(
            plan2.to_dict()["input"]["target_tag_catalog_sha256"], expected
        )
        self.assertEqual(plan2.plan_sha256, plan.plan_sha256)
        spec["tags"][3]["type"] = 1
        plan3 = build_with_choices(choices=ready_choices(), catalog=make_catalog(spec))
        self.assertNotEqual(
            plan3.to_dict()["input"]["target_tag_catalog_sha256"], expected
        )
        self.assertNotEqual(plan3.plan_sha256, plan.plan_sha256)

    def test_inputs_not_modified(self) -> None:
        package = make_package()
        choices = ready_choices()
        catalog = make_catalog()
        package_snapshot = copy.deepcopy(package)
        choices_snapshot = copy.deepcopy(choices)
        catalog_snapshot = copy.deepcopy(catalog)
        build_umalib_change_plan(package, choices, catalog)
        self.assertEqual(package, package_snapshot)
        self.assertEqual(choices, choices_snapshot)
        self.assertEqual(catalog, catalog_snapshot)

    def test_plan_hash_covered_state_is_deeply_immutable(self) -> None:
        plan = build_with_choices(choices=ready_choices())
        original = plan.to_dict()
        with self.assertRaises(TypeError):
            plan.article["name"] = "篡改"
        with self.assertRaises(TypeError):
            plan.input["package_sha256"] = "f" * 64
        with self.assertRaises(AttributeError):
            plan.tags["existing"].append({"id": 999, "name": "篡改", "type": 0})
        exported = plan.to_dict()
        exported["article"]["name"] = "只改副本"
        exported["tags"]["existing"].append({"id": 999, "name": "只改副本", "type": 0})
        self.assertEqual(plan.to_dict(), original)

    def test_package_version_status_and_malformed_hash_block(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        spec["schema_version"] = "2.0"
        spec["submission"]["schema_version"] = "2.0"
        spec["inbox"]["status"] = "candidate"
        package = make_package(spec=spec)
        package.package_sha256 = "bad"
        data = build_with_choices(package=package, choices=ready_choices()).to_dict()
        codes = {item["code"] for item in data["diagnostics"]["blockers"]}
        self.assertIn("approved_package_schema_mismatch", codes)
        self.assertIn("submission_schema_mismatch", codes)
        self.assertIn("approved_package_status_mismatch", codes)
        self.assertIn("approved_package_sha256_malformed", codes)
        self.assertEqual(data["input"]["package_sha256"], "0" * 64)

    def test_structurally_wrong_package_and_choice_containers_raise(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        del spec["submission"]["metadata"]["sources"]["nga"]
        with self.assertRaises(AdapterError):
            build_with_choices(package=make_package(spec=spec), choices=ready_choices())
        bad_choices = AdapterChoices(
            upload_time_seconds=1700000000,
            note=None,
            ordered_sources=[],
            tag_choices=(),
        )
        with self.assertRaises(AdapterError):
            build_with_choices(choices=bad_choices)
        bool_time = AdapterChoices(
            upload_time_seconds=True,
            note=None,
            ordered_sources=(),
            tag_choices=(),
        )
        with self.assertRaises(AdapterError):
            build_with_choices(choices=bool_time)

    def test_output_has_no_paths_sql_ids_or_clock(self) -> None:
        plan = build_with_choices(choices=ready_choices())
        raw = stable_json(plan.to_dict())
        self.assertNotIn(str(PROJECT_ROOT).replace("\\", "/"), raw)
        self.assertNotIn(str(PROJECT_ROOT), raw)
        for forbidden in ("INSERT INTO", "SELECT ", "CREATE TABLE", "UPDATE "):
            self.assertNotIn(forbidden, raw)
        data = plan.to_dict()
        self.assertNotIn("id", data["article"])


class SourceTests(unittest.TestCase):
    def test_order_preserved_and_flattened(self) -> None:
        choices = make_choices(
            ordered_sources=(
                "https://example.com/translation-1",
                "https://example.com/original-1",
            ),
            tag_choices=ready_choices().tag_choices,
        )
        plan = build_with_choices(choices=choices)
        self.assertEqual(
            plan.to_dict()["article"]["source"],
            "https://example.com/translation-1 https://example.com/original-1",
        )

    def test_selection_required_when_available(self) -> None:
        plan = build_with_choices(choices=make_choices(tag_choices=ready_choices().tag_choices))
        data = plan.to_dict()
        self.assertFalse(data["ready"])
        self.assertIn(
            "source_selection_required",
            [b["code"] for b in data["diagnostics"]["blockers"]],
        )
        self.assertEqual(data["article"]["source"], "")

    def test_not_available_and_invalid_tokens(self) -> None:
        choices = make_choices(
            ordered_sources=("https://example.com/not-in-package", "含 空白", 42),
            tag_choices=ready_choices().tag_choices,
        )
        plan = build_with_choices(choices=choices)
        codes = [b["code"] for b in plan.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("source_not_available", codes)
        self.assertIn("source_invalid_token", codes)

    def test_duplicate_selection_dedup_with_warning(self) -> None:
        url = "https://example.com/original-1"
        choices = make_choices(
            ordered_sources=(url, url, "https://example.com/ts-1"),
            tag_choices=ready_choices().tag_choices,
        )
        plan = build_with_choices(choices=choices)
        data = plan.to_dict()
        self.assertEqual(data["article"]["source"], f"{url} https://example.com/ts-1")
        self.assertIn(
            "duplicate_source_selection",
            [w["code"] for w in data["diagnostics"]["warnings"]],
        )

    def test_no_sources_allows_empty(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        spec["submission"]["metadata"]["sources"] = {
            "nga": [], "original_series": [], "original": [],
            "translation_series": [], "translation": [],
        }
        package = make_package(spec=spec)
        choices = make_choices(tag_choices=ready_choices().tag_choices)
        plan = build_with_choices(package=package, choices=choices)
        data = plan.to_dict()
        self.assertEqual(data["article"]["source"], "")
        self.assertTrue(data["ready"])

    def test_no_implicit_priority(self) -> None:
        choices = make_choices(
            ordered_sources=("https://example.com/ts-1",),
            tag_choices=ready_choices().tag_choices,
        )
        plan = build_with_choices(choices=choices)
        self.assertEqual(plan.to_dict()["article"]["source"], "https://example.com/ts-1")

    def test_selected_source_requires_absolute_http_or_https_url(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        spec["submission"]["metadata"]["sources"]["nga"] = [
            "javascript:alert(1)",
            "relative/path",
            "https://[invalid",
            "https://@",
            "https://example.com:bad",
            "https://user:pass@example.com/private",
        ]
        package = make_package(spec=spec)
        for token in spec["submission"]["metadata"]["sources"]["nga"]:
            with self.subTest(token=token):
                choices = make_choices(
                    ordered_sources=(token,),
                    tag_choices=ready_choices().tag_choices,
                )
                data = build_with_choices(package=package, choices=choices).to_dict()
                self.assertIn(
                    "source_invalid_url",
                    [item["code"] for item in data["diagnostics"]["blockers"]],
                )
                self.assertEqual(data["article"]["source"], "")


class TagResolutionTests(unittest.TestCase):
    def test_unique_exact_match_auto_reuse(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        spec["submission"]["metadata"]["tags"] = ["合成角色甲", "合成长篇丙"]
        package = make_package(spec=spec)
        choices = make_choices(ordered_sources=ready_choices().ordered_sources)
        plan = build_with_choices(package=package, choices=choices)
        data = plan.to_dict()
        self.assertTrue(data["ready"])
        existing = {t["name"]: t["id"] for t in data["tags"]["existing"]}
        self.assertEqual(existing["合成角色甲"], 1)
        self.assertEqual(existing["合成系列乙"], 2)
        self.assertEqual(existing["合成长篇丙"], 5)

    def test_unmatched_tag_and_series_block(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        spec["submission"]["metadata"]["tags"] = ["不存在的标签"]
        spec["submission"]["metadata"]["series"] = "不存在的系列"
        package = make_package(spec=spec)
        plan = build_with_choices(package=package, choices=ready_choices())
        codes = [b["code"] for b in plan.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("tag_resolution_required", codes)
        self.assertIn("series_resolution_required", codes)
        self.assertFalse(plan.to_dict()["ready"])

    def test_explicit_new_tag(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        spec["submission"]["metadata"]["tags"] = ["全新标签"]
        spec["submission"]["metadata"]["series"] = None
        package = make_package(spec=spec)
        choices = make_choices(
            ordered_sources=ready_choices().ordered_sources,
            tag_choices=(TagChoice("tag", "全新标签", new_tag_type=2),),
        )
        plan = build_with_choices(package=package, choices=choices)
        data = plan.to_dict()
        self.assertTrue(data["ready"])
        self.assertEqual(data["tags"]["create"], [{"name": "全新标签", "type": 2}])

    def test_new_tag_name_exists_blocks(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        spec["submission"]["metadata"]["tags"] = ["合成重复标签"]
        spec["submission"]["metadata"]["series"] = None
        package = make_package(spec=spec)
        choices = make_choices(
            tag_choices=(TagChoice("tag", "合成重复标签", new_tag_type=0),),
        )
        plan = build_with_choices(package=package, choices=choices)
        self.assertIn(
            "new_tag_name_exists",
            [b["code"] for b in plan.to_dict()["diagnostics"]["blockers"]],
        )

    def test_duplicate_name_blocks_and_explicit_id_resolves(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        spec["submission"]["metadata"]["series"] = None
        package = make_package(spec=spec)
        choices = make_choices(
            ordered_sources=ready_choices().ordered_sources,
            tag_choices=(TagChoice("tag", "合成角色甲", existing_tag_id=1),),
        )
        plan = build_with_choices(package=package, choices=choices)
        codes = [b["code"] for b in plan.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("ambiguous_tag_name", codes)
        blockers = plan.to_dict()["diagnostics"]["blockers"]
        message = [b["message"] for b in blockers if b["code"] == "ambiguous_tag_name"][0]
        self.assertIn("id=3", message)
        self.assertIn("id=4", message)
        choices = make_choices(
            ordered_sources=ready_choices().ordered_sources,
            tag_choices=(
                TagChoice("tag", "合成角色甲", existing_tag_id=1),
                TagChoice("tag", "合成重复标签", existing_tag_id=4),
            ),
        )
        plan = build_with_choices(package=package, choices=choices)
        data = plan.to_dict()
        self.assertTrue(data["ready"])
        existing = {t["name"]: t["id"] for t in data["tags"]["existing"]}
        self.assertEqual(existing["合成重复标签"], 4)

    def test_existing_id_not_found_blocks(self) -> None:
        choices = make_choices(
            ordered_sources=ready_choices().ordered_sources,
            tag_choices=(
                TagChoice("tag", "合成角色甲", existing_tag_id=1),
                TagChoice("tag", "合成重复标签", existing_tag_id=999),
                TagChoice("series", "合成系列乙", existing_tag_id=2),
            ),
        )
        plan = build_with_choices(choices=choices)
        self.assertIn(
            "tag_choice_invalid_existing_id",
            [b["code"] for b in plan.to_dict()["diagnostics"]["blockers"]],
        )

    def test_alias_mapping_warning(self) -> None:
        choices = make_choices(
            ordered_sources=ready_choices().ordered_sources,
            tag_choices=(
                TagChoice("tag", "合成角色甲", existing_tag_id=2),
                TagChoice("tag", "合成重复标签", existing_tag_id=3),
                TagChoice("series", "合成系列乙", existing_tag_id=2),
            ),
        )
        plan = build_with_choices(choices=choices)
        warnings = [w["code"] for w in plan.to_dict()["diagnostics"]["warnings"]]
        self.assertIn("tag_alias_mapping", warnings)
        self.assertIn("duplicate_target_resolution", warnings)

    def test_series_does_not_guess_type(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        spec["submission"]["metadata"]["tags"] = []
        spec["submission"]["metadata"]["series"] = "未知系列"
        package = make_package(spec=spec)
        plan = build_with_choices(package=package, choices=ready_choices())
        codes = [b["code"] for b in plan.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("series_resolution_required", codes)
        self.assertEqual(plan.to_dict()["tags"]["create"], [])

    def test_choice_conflict_and_incomplete(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        spec["submission"]["metadata"]["tags"] = ["甲"]
        spec["submission"]["metadata"]["series"] = None
        package = make_package(spec=spec)
        conflict = make_choices(
            tag_choices=(TagChoice("tag", "甲", existing_tag_id=1, new_tag_type=1),),
        )
        plan = build_with_choices(package=package, choices=conflict)
        self.assertIn(
            "tag_choice_conflict",
            [b["code"] for b in plan.to_dict()["diagnostics"]["blockers"]],
        )
        incomplete = make_choices(tag_choices=(TagChoice("tag", "甲"),))
        plan = build_with_choices(package=package, choices=incomplete)
        self.assertIn(
            "tag_choice_incomplete",
            [b["code"] for b in plan.to_dict()["diagnostics"]["blockers"]],
        )

    def test_boolean_choice_values_are_not_integer_ids_or_types(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        spec["submission"]["metadata"]["tags"] = ["合成角色甲", "全新标签"]
        spec["submission"]["metadata"]["series"] = None
        package = make_package(spec=spec)
        choices = make_choices(
            ordered_sources=ready_choices().ordered_sources,
            tag_choices=(
                TagChoice("tag", "合成角色甲", existing_tag_id=True),
                TagChoice("tag", "全新标签", new_tag_type=True),
            ),
        )
        data = build_with_choices(package=package, choices=choices).to_dict()
        codes = [item["code"] for item in data["diagnostics"]["blockers"]]
        self.assertIn("tag_choice_invalid_existing_id", codes)
        self.assertIn("invalid_new_tag_type", codes)
        self.assertEqual(data["tags"]["existing"], [])
        self.assertEqual(data["tags"]["create"], [])

    def test_duplicate_and_orphan_choice(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        spec["submission"]["metadata"]["tags"] = ["合成角色甲"]
        spec["submission"]["metadata"]["series"] = None
        package = make_package(spec=spec)
        choices = make_choices(
            tag_choices=(
                TagChoice("tag", "合成角色甲", existing_tag_id=1),
                TagChoice("tag", "合成角色甲", existing_tag_id=1),
                TagChoice("tag", "不存在项", existing_tag_id=1),
            ),
        )
        plan = build_with_choices(package=package, choices=choices)
        codes = [b["code"] for b in plan.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("duplicate_choice", codes)
        self.assertIn("orphan_choice", codes)

    def test_catalog_validation_blockers(self) -> None:
        spec = load_json_fixture("synthetic_catalog.json")
        spec["tags"].append({"id": 3, "name": "重复ID", "type": 1, "usage_count": 0})
        spec["tags"].append({"id": 99, "name": "", "type": 1, "usage_count": 0})
        spec["tags"].append({"id": 100, "name": "坏类型", "type": 9, "usage_count": 0})
        spec["tags"].append({"id": -1, "name": "负ID", "type": 0, "usage_count": 0})
        spec["tags"].append({"id": 0, "name": "零ID", "type": 0, "usage_count": 0})
        spec["tags"].append({"id": True, "name": "布尔ID", "type": True, "usage_count": 0})
        spec["tags"].append({"id": 101, "name": "负计数", "type": 0, "usage_count": -1})
        plan = build_with_choices(choices=ready_choices(), catalog=make_catalog(spec))
        codes = [b["code"] for b in plan.to_dict()["diagnostics"]["blockers"]]
        self.assertIn("catalog_duplicate_id", codes)
        self.assertIn("catalog_invalid_tag", codes)
        self.assertFalse(plan.to_dict()["ready"])

    def test_catalog_field_shape_errors_raise_clear_adapter_error(self) -> None:
        catalog = TargetTagCatalog(
            database_sha256=SYNTHETIC_DB_SHA,
            schema_fingerprint_sha256=CONFIRMED_FINGERPRINT,
            tags=(TargetTag(id="1", name="坏ID类型", type=0),),
        )
        with self.assertRaises(AdapterError):
            build_with_choices(choices=ready_choices(), catalog=catalog)

    def test_dedup_same_target_with_warning(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        spec["submission"]["metadata"]["tags"] = ["全新甲", "全新甲"]
        spec["submission"]["metadata"]["series"] = None
        package = make_package(spec=spec)
        choices = make_choices(
            ordered_sources=ready_choices().ordered_sources,
            tag_choices=(TagChoice("tag", "全新甲", new_tag_type=1),),
        )
        plan = build_with_choices(package=package, choices=choices)
        data = plan.to_dict()
        self.assertEqual(data["tags"]["create"], [{"name": "全新甲", "type": 1}])
        self.assertIn(
            "duplicate_target_resolution",
            [w["code"] for w in data["diagnostics"]["warnings"]],
        )
        self.assertTrue(data["ready"])


class ContractShapeTests(unittest.TestCase):
    def test_to_dict_matches_schema_shape(self) -> None:
        plan = build_with_choices(choices=ready_choices())
        data = plan.to_dict()
        self.assertEqual(
            set(data.keys()),
            {"schema_version", "input", "ready", "article", "tags", "diagnostics", "plan_sha256"},
        )
        self.assertEqual(data["schema_version"], "1.0")
        self.assertEqual(
            set(data["input"].keys()),
            {
                "item_id", "package_sha256", "target_database_sha256",
                "target_schema_fingerprint_sha256", "target_tag_catalog_sha256",
            },
        )
        self.assertEqual(
            set(data["article"].keys()),
            {"name", "note", "content", "author", "translator", "upload_time", "source"},
        )
        self.assertEqual(set(data["tags"].keys()), {"existing", "create"})
        self.assertEqual(set(data["diagnostics"].keys()), {"blockers", "warnings"})
        for item in data["tags"]["existing"]:
            self.assertEqual(set(item.keys()), {"id", "name", "type"})
        for item in data["tags"]["create"]:
            self.assertEqual(set(item.keys()), {"name", "type"})
        for diag in data["diagnostics"]["blockers"] + data["diagnostics"]["warnings"]:
            self.assertEqual(set(diag.keys()), {"code", "field", "message"})
        for key in (
            "package_sha256", "target_database_sha256", "target_schema_fingerprint_sha256",
            "target_tag_catalog_sha256", "plan_sha256",
        ):
            self.assertRegex(data["input"].get(key) or data.get(key) or "", r"^[0-9a-f]{64}$")

    def test_ready_equals_no_blockers(self) -> None:
        ready_plan = build_with_choices(choices=ready_choices())
        self.assertEqual(ready_plan.ready, not ready_plan.to_dict()["diagnostics"]["blockers"])
        blocked_plan = build_with_choices(choices=make_choices(tag_choices=ready_choices().tag_choices))
        self.assertFalse(blocked_plan.ready)
        self.assertTrue(blocked_plan.to_dict()["diagnostics"]["blockers"])

    def test_blocked_preview_keeps_contract_safe_scalar_values(self) -> None:
        catalog_spec = load_json_fixture("synthetic_catalog.json")
        catalog_spec["database_sha256"] = "bad"
        catalog_spec["schema_fingerprint_sha256"] = "bad"
        choices = AdapterChoices(
            upload_time_seconds=-1,
            note=None,
            ordered_sources=ready_choices().ordered_sources,
            tag_choices=ready_choices().tag_choices,
        )
        data = build_with_choices(
            choices=choices,
            catalog=make_catalog(catalog_spec),
        ).to_dict()
        self.assertFalse(data["ready"])
        self.assertEqual(data["article"]["upload_time"], 0)
        for key in (
            "package_sha256",
            "target_database_sha256",
            "target_schema_fingerprint_sha256",
            "target_tag_catalog_sha256",
        ):
            self.assertRegex(data["input"][key], r"^[0-9a-f]{64}$")
        self.assertRegex(data["plan_sha256"], r"^[0-9a-f]{64}$")

    def test_output_ordering_stable(self) -> None:
        spec = load_json_fixture("synthetic_package.json")
        spec["submission"]["metadata"]["tags"] = ["x1", "x2", "x3"]
        spec["submission"]["metadata"]["series"] = "x4"
        spec["submission"]["metadata"]["sources"] = {
            "nga": [], "original_series": ["https://a.example/1"],
            "original": [], "translation_series": [], "translation": [],
        }
        package = make_package(spec=spec)
        choices = make_choices(
            ordered_sources=("https://a.example/1", "https://a.example/1"),
            tag_choices=(
                TagChoice("tag", "x1", new_tag_type=1),
                TagChoice("tag", "x2", existing_tag_id=999),
                TagChoice("series", "x4", existing_tag_id=1),
            ),
        )
        plan = build_umalib_change_plan(package, choices, make_catalog())
        data = plan.to_dict()
        blockers = data["diagnostics"]["blockers"]
        self.assertEqual(
            blockers,
            sorted(blockers, key=lambda d: (d["code"], d["field"])),
        )
        self.assertEqual(
            data["tags"]["existing"],
            sorted(data["tags"]["existing"], key=lambda t: t["id"]),
        )
        self.assertEqual(
            data["tags"]["create"],
            sorted(data["tags"]["create"], key=lambda t: (t["name"], t["type"])),
        )


if __name__ == "__main__":
    unittest.main()
