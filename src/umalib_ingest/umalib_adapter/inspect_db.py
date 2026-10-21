"""CLI entry for the read-only UmaLib inspector (TASK-006)."""

from __future__ import annotations

import argparse
import sys

from umalib_ingest.umalib_adapter import (
    UmaLibInspectionReport,
    inspect_umalib_database,
)
from umalib_ingest.umalib_adapter.errors import UmaLibInspectionError


def _configure_stdio() -> None:
    """Force UTF-8 stdio so Windows consoles can render the summary."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _human_summary(report: UmaLibInspectionReport) -> str:
    """Build the concise human summary without article-level content."""
    data = report.to_dict()
    database = data["database"]
    schema = data["schema"]
    counts = data["counts"]
    tag_audit = data["tag_audit"]
    integrity = data["integrity"]
    issues = data["issues"]

    count_parts = [f"{name}={counts[name] if counts[name] is not None else '缺失'}" for name in ("article", "tag", "tagged", "creator", "rec", "dict")]
    type_counts = ", ".join(f"{key}: {value}" for key, value in tag_audit["type_counts"].items()) or "无"
    duplicate_groups = len(tag_audit["duplicate_names"])
    blank_names = len(tag_audit["blank_name_ids"])
    unknown_types = len(tag_audit["unknown_type_ids"])
    foreign_key_result = integrity["foreign_key_violation_count"]
    foreign_key_text = (
        "检查失败" if foreign_key_result is None else str(foreign_key_result)
    )
    blockers = [issue for issue in issues if issue["severity"] == "blocker"]
    warnings = [issue for issue in issues if issue["severity"] == "warning"]
    infos = [issue for issue in issues if issue["severity"] == "info"]

    lines = [
        "只读检查，不会写入数据库",
        f"文件: {database['file_name']} ({database['size_bytes']} 字节)",
        f"文件 SHA-256: {database['sha256']}",
        f"user_version: {database['user_version']}, "
        f"page_size: {database['page_size']}, page_count: {database['page_count']}",
        f"Schema 指纹: {schema['schema_fingerprint_sha256']}",
        f"核心契约 (core_contract_match): {'通过' if schema['core_contract_match'] else '不通过'}",
        f"行数: {' '.join(count_parts)}",
        f"标签类型计数: {type_counts}",
        f"标签审计: 重名 {duplicate_groups} 组, 空名 {blank_names} 个, 未知类型 {unknown_types} 个",
        f"完整性: integrity_check={'ok' if integrity['integrity_check'] else '失败'}, "
        f"外键违规 {foreign_key_text}, "
        f"重复关联 {integrity['duplicate_tagged_pair_count']}",
    ]
    if issues:
        lines.append("issues:")
        for issue in issues:
            lines.append(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")
    else:
        lines.append("issues: 无")
    lines.append(
        f"结论: {len(blockers)} 个 blocker, {len(warnings)} 个 warning, {len(infos)} 个 info"
    )
    lines.append("注意: core_contract_match 通过也不代表已允许 Writer 写入。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the read-only inspection CLI and return the process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m umalib_ingest.umalib_adapter.inspect_db",
        description="只读大书库数据库检查器（不会写入或复制数据库）",
    )
    parser.add_argument("--db", required=True, help="要检查的数据库副本路径（必填）")
    parser.add_argument("--json", action="store_true", help="只向 stdout 输出一个稳定 JSON 对象")
    args = parser.parse_args(argv)

    _configure_stdio()
    try:
        report = inspect_umalib_database(args.db)
    except UmaLibInspectionError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("错误: 无法完成检查", file=sys.stderr)
        return 2

    blockers = [issue for issue in report.issues if issue.severity == "blocker"]
    if args.json:
        print(report.to_json())
    else:
        print(_human_summary(report))
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
