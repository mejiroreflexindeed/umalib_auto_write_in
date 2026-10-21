"""TASK-013 演示样例与合成数据（M01）。

六份 UTF-8 样例全部为虚构内容；同时提供 synthetic 基础库构造（复用已确认
Schema DDL 的资产副本，不导入 tests/）。合成标签数据完全虚构。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

ASSETS_DIR = Path(__file__).parent / "assets"
SCHEMA_SQL_PATH = ASSETS_DIR / "umalib-schema.sql"


def sha256_text(text: str) -> str:
    """Return the lowercase hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SAMPLES: tuple[dict[str, Any], ...] = (
    {
        "sample_id": "demo-01-color",
        "title": "颜色与强调示例",
        "description": "完整稿：包含 [color]、[b]、[i] 与末尾 [关键词]注释",
        "raw_text": (
            "[收录]\n[标题] 彩色标题示例\n[作者] 演示作者甲\n[标签] 演示标签甲\n\n"
            "[color=#3366ff]这是蓝色文字[/color]，[b]加粗[/b]与[i]斜体[/i]。\n\n"
            "[关键词]演示注释"
        ),
        "source": "https://example.com/demo-source-1",
        "expected_status": "candidate",
        "next_step": "核对正文与格式建议后批准",
    },
    {
        "sample_id": "demo-02-incomplete",
        "title": "缺字段示例",
        "description": "缺作者字段，导入后为 incomplete，人工补全",
        "raw_text": (
            "[收录]\n[标题] 缺字段示例\n[标签] 演示标签乙\n\n正文内容。"
        ),
        "source": "https://example.com/demo-source-2",
        "expected_status": "incomplete",
        "next_step": "补作者后保存",
    },
    {
        "sample_id": "demo-03-discussion",
        "title": "普通讨论示例",
        "description": "无独立 [收录] 行，只作普通讨论，不入 Inbox",
        "raw_text": "大家觉得这个设定怎么样？",
        "source": "https://example.com/demo-source-3",
        "expected_status": "discussion",
        "next_step": "模拟扫描中验证只记 seen",
    },
    {
        "sample_id": "demo-04-duplicate-body",
        "title": "重复正文示例",
        "description": "与 demo-01 正文不同但与自身重复导入验证幂等",
        "raw_text": (
            "[收录]\n[标题] 重复正文示例\n[作者] 演示作者丙\n[标签] 演示标签丙\n\n"
            "同一正文用于验证重复导入不产生重复条目。"
        ),
        "source": "https://example.com/demo-source-4",
        "expected_status": "candidate",
        "next_step": "导入两次验证幂等",
    },
    {
        "sample_id": "demo-05-same-tag-multisource",
        "title": "同名标签与多来源示例",
        "description": "标签在目录中同名多个 ID，需人工选择；两个来源需人工排序",
        "raw_text": (
            "[收录]\n[标题] 多来源示例\n[作者] 演示作者丁\n[标签] 同名标签\n\n"
            "多来源正文内容。"
        ),
        "source": "https://example.com/demo-source-5a",
        "source_extra": "https://example.com/demo-source-5b",
        "expected_status": "candidate",
        "next_step": "选择同名标签 ID 与来源顺序",
    },
    {
        "sample_id": "demo-06-annotation",
        "title": "正文注释示例",
        "description": "正文行内 [术语]注释 与末尾连续注释保留",
        "raw_text": (
            "[收录]\n[标题] 注释示例\n[作者] 演示作者戊\n[标签] 演示标签戊\n\n"
            "正文[术语]注释说明。\n\n[关键词]尾注一\n[关键词]尾注二"
        ),
        "source": "https://example.com/demo-source-6",
        "expected_status": "candidate",
        "next_step": "核对注释保留与格式建议",
    },
)


def sample_by_id(sample_id: str) -> dict[str, Any]:
    """Return one sample by id or raise KeyError."""
    for sample in SAMPLES:
        if sample["sample_id"] == sample_id:
            return sample
    raise KeyError(sample_id)


def create_synthetic_base(db_path: Path) -> dict[str, Any]:
    """Create a synthetic UmaiLib base copy with fictional fixed tags.

    Reuses the confirmed schema DDL asset (byte-identical declaration types).
    Never reads the real database. Returns identity summary.
    """
    ddl = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    if db_path.exists():
        raise FileExistsError(f"synthetic base 已存在: {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(ddl)
        conn.executemany(
            "INSERT INTO tag(name, type, cover, description) VALUES (?, ?, NULL, NULL)",
            (
                ("唯一演示标签", 0),
                ("同名标签", 0),
                ("同名标签", 1),
                ("演示系列", 2),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "source": "synthetic",
        "sha256": hashlib.sha256(db_path.read_bytes()).hexdigest(),
        "tags": [
            {"id": 1, "name": "唯一演示标签", "type": 0},
            {"id": 2, "name": "同名标签", "type": 0},
            {"id": 3, "name": "同名标签", "type": 1},
            {"id": 4, "name": "演示系列", "type": 2},
        ],
    }


# 模拟扫描场景（M09 使用；字节级假 transport 由 simulated_scan.py 消费）
SCAN_SCENARIO_ONE = {
    "scenario_id": "sim-01-basics",
    "title": "场景一：普通讨论 + 投稿 + 缺字段",
    "floors": (
        {"pid": "910001001", "content": "普通讨论内容"},
        {
            "pid": "910001002",
            "content": "[收录]\n[标题] 模拟投稿一\n[作者] 模拟作者\n[标签] 模拟标签\n\n模拟正文。",
        },
        {
            "pid": "910001003",
            "content": "[收录]\n[标题] 缺字段投稿\n[标签] 模拟标签\n\n缺作者正文。",
        },
    ),
}

SCAN_SCENARIO_TWO = {
    "scenario_id": "sim-02-update",
    "title": "场景二：同楼更新产生 changed_after_import",
    "floors": (
        {
            "pid": "910001002",
            "content": "[收录]\n[标题] 模拟投稿一\n[作者] 模拟作者\n[标签] 模拟标签\n\n模拟正文（已修改）。",
        },
    ),
}
