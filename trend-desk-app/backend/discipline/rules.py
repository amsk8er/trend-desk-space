"""纪律 v1.5 的单一机器可执行规则快照。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

RULES_VERSION = "v1.5"
EFFECTIVE_FROM = "2026-07-16"
SOURCE_PATH = Path(
    os.getenv("DISCIPLINE_RULES_SOURCE_PATH", "discipline-v1.5.md")
).expanduser()

RULES = {
    "version": RULES_VERSION,
    "effective_from": EFFECTIVE_FROM,
    "selection": {
        "phase_order": [
            "立春", "雨水", "惊蛰", "春分", "清明", "谷雨",
            "立夏", "小满", "芒种", "夏至", "小暑", "大暑",
            "立秋", "处暑", "白露", "秋分", "寒露", "霜降",
            "立冬", "小雪", "大雪", "冬至", "小寒", "大寒",
        ],
        "max_entry_phase_exclusive": "大暑",
        "stock": {
            "min_float_market_cap_yi": 300.0,
            "min_amount_yi": 5.0,
            "max_right_side_days": 10,
            "min_sector_temperature": "温",
            "requires_warm_to_hot": True,
            "exclude_warm_to_boiling": True,
        },
        "etf": {
            "min_aum_yi": 25.0,
            "min_amount_yi": 2.0,
            "min_strength": 80.0,
            "requires_warm_to_hot": True,
            "exclude_warm_to_boiling": True,
            "deduplicate_by_benchmark": True,
            "benchmark_tiebreakers": ["strength", "amount_yi", "aum_yi", "code"],
        },
    },
    "observation": {
        "strength_change": {
            "field": "trendStrengthLocalChange",
            "applies_to": ["eligible_candidates", "holdings"],
            "decision_effect": "none",
            "documented_values": {"↑↑": "significant", "↑": "moderate", "": "none"},
            "unknown_value_policy": "display_raw_only",
        },
    },
    "capacity": {
        "base_new_position_pct": 0.05,
        "environment_factors": {"沸": 1.0, "热": 1.0, "温": 1.0, "平": 0.5, "凉": 0.25, "寒": 0.0, "冻": 0.0},
        "normal": {"max_new_tools": 2, "max_added_weight": 0.10},
        "resonance": {"max_new_tools": 5, "max_added_weight": 0.25},
        "max_total_weight": 1.0,
        "max_tools": 20,
    },
    "exit": {
        "full_exit_temperatures": ["平", "凉", "寒", "冻"],
        # Nick 2026-07-12：波动率放大已融入「沸」，无需单独处理。
        "profit_signals": ["champagne", "boiling"],
        "fraction_per_signal": 0.25,
        "round_lot": 100,
        "full_exit_priority": 1,
        "reduce_priority": 2,
        "hold_priority": 4,
    },
}


def canonical_rules_json() -> str:
    return json.dumps(RULES, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


RULES_HASH = hashlib.sha256(canonical_rules_json().encode("utf-8")).hexdigest()


def source_hash() -> str | None:
    """规则原文审计哈希；源文件不可读时不伪造。"""
    try:
        return hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest()
    except OSError:
        return None
