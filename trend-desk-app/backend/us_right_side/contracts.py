"""美股右侧资产页的稳定内部契约。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable


SCOPE = "trend_animals_us_right_side_v1"
RULES_VERSION = "us-right-side-v1"
MAX_SNAPSHOT_BATCH = 300
DEFAULT_PAGE_SIZE = 40
MAX_PAGE_SIZE = 100

FIELD_AVAILABLE = "available"
FIELD_NOT_REQUESTED = "not_requested"
FIELD_NOT_RETURNED = "not_returned"
FIELD_NOT_APPLICABLE = "not_applicable"
FIELD_STALE = "stale"
FIELD_STATES = {
    FIELD_AVAILABLE,
    FIELD_NOT_REQUESTED,
    FIELD_NOT_RETURNED,
    FIELD_NOT_APPLICABLE,
    FIELD_STALE,
}

SCREEN_FIELDS = ("isTrendRightSide",)
AGE_FIELDS = ("daysSinceTrendEntry",)
STRENGTH_FIELDS = ("trendStrengthLocalCurr",)
STANDARD_ASSET_FIELDS = (
    "industryTmId",
    "trendTemperatureCurr",
    "trendPhaseCurr",
)
INDUSTRY_FIELDS = (
    "isTrendRightSide",
    "trendTemperatureCurr",
    "trendStrengthLocalCurr",
    "trendPhaseCurr",
)
DEEP_FIELDS = (
    "gainSinceTrendEntry",
    "trendStrengthLocalChange",
    "stopwinFlagByDangerSignal",
    "stopwinFlagByBoilingTemperature",
    "stopwinFlagByPopChampagne",
    "tickerLabels",
    "heatScore7d",
    "return1m",
)

RUN_STATES = {
    "pending",
    "awaiting_screen_budget",
    "screening",
    "screen_partial",
    "screen_ready",
    "awaiting_age_budget",
    "age_enriching",
    "age_partial",
    "awaiting_strength_budget",
    "strength_enriching",
    "strength_partial",
    "awaiting_standard_budget",
    "standard_enriching",
    "standard_partial",
    "awaiting_industry_budget",
    "industry_enriching",
    "ready",
    "failed",
}


class UsRightSideError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409,
                 context: dict | None = None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.context = context or {}
        super().__init__(message)

    def as_payload(self) -> dict:
        return {"code": self.code, "message": self.message, "context": self.context}


def field_set_hash(fields: Iterable[str]) -> str:
    """字段集哈希与调用顺序无关，避免同一购买集合产生多份缓存。"""
    normalized = sorted({str(field).strip() for field in fields if str(field).strip()})
    return hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def batches(values: Iterable[int], size: int = MAX_SNAPSHOT_BATCH) -> list[list[int]]:
    if size < 1 or size > MAX_SNAPSHOT_BATCH:
        raise ValueError(f"batch size must be between 1 and {MAX_SNAPSHOT_BATCH}")
    unique = list(dict.fromkeys(int(value) for value in values))
    return [unique[index:index + size] for index in range(0, len(unique), size)]


@dataclass(frozen=True)
class SortSpec:
    value_key: str
    default_direction: str
    required_stage: str
    kind: str = "number"
    state_key: str | None = None


SORT_SPECS: dict[str, SortSpec] = {
    "ticker_name": SortSpec("ticker_name", "asc", "identity", "text"),
    "ticker_symbol": SortSpec("ticker_symbol", "asc", "identity", "text"),
    "price_index": SortSpec("price_index", "desc", "standard", state_key="price_index"),
    "temperature_curr": SortSpec(
        "temperature_curr", "desc", "standard", "temperature", "temperature_curr"),
    "phase_curr": SortSpec("phase_curr", "asc", "standard", "phase", "phase_curr"),
    "industry_name": SortSpec("industry_name", "asc", "industry", "text", "industry_name"),
    "industry_temperature_curr": SortSpec(
        "industry_temperature_curr", "desc", "industry", "temperature", "industry_temperature_curr"),
    "industry_strength_local_curr": SortSpec(
        "industry_strength_local_curr", "desc", "industry", state_key="industry_strength_local_curr"),
    "industry_phase_curr": SortSpec(
        "industry_phase_curr", "asc", "industry", "phase", "industry_phase_curr"),
    "days_since_trend_entry": SortSpec(
        "days_since_trend_entry", "asc", "age", state_key="days_since_trend_entry"),
    "gain_since_trend_entry": SortSpec(
        "gain_since_trend_entry", "desc", "deep", state_key="gain_since_trend_entry"),
    "market_cap": SortSpec("market_cap", "desc", "standard", state_key="market_cap"),
    "amount_1d": SortSpec("amount_1d", "desc", "standard", state_key="amount_1d"),
    "strength_local_curr": SortSpec(
        "strength_local_curr", "desc", "strength", state_key="strength_local_curr"),
    "risk_flag_count": SortSpec("risk_flag_count", "desc", "deep", state_key="risk_flag_count"),
}


def sort_capabilities(*, age_ready: bool, strength_ready: bool, standard_ready: bool, industry_ready: bool,
                      deep_ready: bool) -> dict[str, dict]:
    readiness = {
        "identity": True,
        "age": age_ready,
        "strength": strength_ready,
        "standard": standard_ready,
        "industry": industry_ready,
        "deep": deep_ready,
    }
    reason = {
        "age": "先补齐全部右侧资产的右侧天数",
        "strength": "先获取最近 30 天候选的本地强度",
        "standard": "先补齐强度 Top 100 的个股详情",
        "industry": "先补齐候选股所属去重行业的环境",
        "deep": "先为全部右侧资产补齐深度字段",
    }
    return {
        name: {
            "enabled": readiness[spec.required_stage],
            "default_direction": spec.default_direction,
            "blocked_reason": None if readiness[spec.required_stage] else reason[spec.required_stage],
        }
        for name, spec in SORT_SPECS.items()
    }
