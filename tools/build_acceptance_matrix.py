"""TASK-015 Q2：生成 acceptance-matrix.json（A001—A112 + R01—R08 + F 闭环）。

规则（替代按编号区间直接发 PASS）：
- PASS 只来自 `tools/acceptance-matrix-map.json` 的显式映射；
- 每条证据必须：文件存在、断言/检查实际通过（unittest 日志含具体
  `... ok` 行；JSON 含指定 check 且 ok=true）；
- 任一子证据缺失 → BLOCKED；任一断言失败 → FAIL；未映射项 → PARTIAL 待抽验。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DEFAULT_MAP = Path(__file__).resolve().parent / "acceptance-matrix-map.json"


def _validate_evidence(root: Path, entries: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Return (problems, missing) — problems include failed assertions."""
    problems: list[str] = []
    missing: list[str] = []
    for entry in entries:
        path = root / entry["file"]
        if not path.exists():
            missing.append(f"证据文件缺失: {entry['file']}")
            continue
        kind = entry.get("kind")
        if kind == "unittest_log":
            raw = path.read_bytes()
            if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
                text = raw.decode("utf-16")
            else:
                text = raw.decode("utf-8", errors="replace")
            if "FAILED" in text or "ERROR:" in text:
                problems.append(f"{entry['file']} 含 FAIL/ERROR 记录")
            if "Ran " not in text or "OK" not in text:
                problems.append(f"{entry['file']} 缺少完整运行摘要（Ran/OK）")
            for needle in entry.get("must_contain", []):
                if needle not in text:
                    problems.append(f"{entry['file']} 缺少测试用例: {needle}")
        elif kind == "phase_check":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                problems.append(f"{entry['file']} 不是合法 JSON")
                continue
            found: dict[str, bool] = {}

            def walk(node: Any) -> None:
                if isinstance(node, dict):
                    if "check" in node and "ok" in node:
                        found[str(node["check"])] = bool(node["ok"])
                    for value in node.values():
                        walk(value)
                elif isinstance(node, list):
                    for value in node:
                        walk(value)

            walk(data)
            for name in entry.get("checks", []):
                if name not in found:
                    missing.append(f"{entry['file']} 缺少 check: {name}")
                elif not found[name]:
                    problems.append(f"{entry['file']} check 未通过: {name}")
        elif kind == "json_field":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                problems.append(f"{entry['file']} 不是合法 JSON")
                continue
            for probe in entry.get("fields", []):
                current: Any = data
                ok = True
                for part in probe["path"].split("."):
                    if isinstance(current, dict) and part in current:
                        current = current[part]
                    else:
                        ok = False
                        break
                if not ok or current != probe["expected"]:
                    problems.append(
                        f"{entry['file']} {probe['path']} 期望 {probe['expected']!r} 实际 {current!r}")
        else:
            problems.append(f"未知证据类型: {kind!r}")
    return problems, missing


def build(feature_map_path: Path, evidence_root: Path, matrix_map_path: Path) -> dict[str, Any]:
    fmap = json.loads(feature_map_path.read_text(encoding="utf-8"))
    matrix_map = json.loads(matrix_map_path.read_text(encoding="utf-8"))
    groups: list[dict[str, Any]] = matrix_map.get("groups", [])
    by_id: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item_id in group.get("ids", []):
            by_id[item_id] = group

    items: list[dict[str, Any]] = []
    for feature in fmap["features"]:
        items.append({"id": feature["id"], "requirement": feature["requirement"],
                      "dev_package": feature["dev_package"]})
    for rid in ("R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08",
                "F003", "F008", "F009"):
        items.append({"id": rid, "requirement": f"缺陷闭环 {rid}",
                      "dev_package": "Q0"})

    results: list[dict[str, Any]] = []
    for item in items:
        group = by_id.get(item["id"])
        if group is None:
            results.append({
                **item, "status": "PARTIAL",
                "test": "无逐条映射", "command": "",
                "evidence": "acceptance-all/*/summary.json; logs/targeted-tests.log",
                "reason": "未映射到具体断言；由覆盖组与 Astra 抽验决定，不能按存在推定",
            })
            continue
        problems, missing = _validate_evidence(evidence_root, group.get("evidence", []))
        status = group.get("status", "PARTIAL")
        reason = ""
        if problems:
            status = "FAIL"
            reason = "；".join(problems)
        elif missing:
            status = "BLOCKED"
            reason = "；".join(missing)
        results.append({
            **item, "status": status, "test": group.get("test", ""),
            "command": group.get("command", ""),
            "evidence": "；".join(e["file"] for e in group.get("evidence", [])),
            "reason": reason,
        })

    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "schema_version": "1",
        "generated_by": "tools/build_acceptance_matrix.py",
        "evidence_root": str(evidence_root),
        "note": ("PASS 必须满足：显式映射 + 证据文件存在 + 断言/检查实际通过。"
                 "PARTIAL/BLOCKED/FAIL 为如实保留项；本矩阵不代表 Astra 验收。"),
        "counts": counts,
        "items": results,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -B tools/build_acceptance_matrix.py")
    parser.add_argument("--feature-map", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    matrix = build(args.feature_map, args.evidence_root, args.map)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(matrix, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(json.dumps(matrix["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
