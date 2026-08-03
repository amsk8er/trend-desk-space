"""候选级补充字段归一与排序输入。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from backend.trend_animals.billing import billing_map
from backend.us_manual.contracts import (
    ENRICHMENT_FIELD_ALIASES,
    ENRICHMENT_FIELDS,
    UsManualError,
    as_int,
    sha256,
)


def enrichment_field_hash(fields: list[str] | tuple[str, ...] = ENRICHMENT_FIELDS) -> str:
    return sha256(list(fields))


def resolve_enrichment_fields(
    billing: list[dict], fields: list[str] | tuple[str, ...] = ENRICHMENT_FIELDS,
) -> list[str]:
    """Choose only billing-confirmed aliases for the candidate-quality request.

    The current API uses the ``*Curr`` industry fields.  Keeping the small alias table makes a
    documented vendor rename recoverable, but an absent field is intentionally
    left as the canonical name so ``estimate_snapshot_cost`` still blocks the
    request before it can become a paid, under-specified scan.
    """
    priced = billing_map(billing)
    resolved: list[str] = []
    for field in fields:
        aliases = ENRICHMENT_FIELD_ALIASES.get(field, (field,))
        resolved.append(next((name for name in aliases if name in priced), field))
    return resolved


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _first_nonempty(raw: dict, *names: str) -> Any:
    for name in names:
        value = raw.get(name)
        if value is not None and value != "":
            return value
    return None


def _labels(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    elif isinstance(value, str):
        values = value.replace("；", ";").split(";")
    else:
        values = []
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def apply_gate_fields(candidate: Any, raw: dict) -> None:
    """Persist fields paid for before the H6 stock/ETF gate."""
    temperature = _first_nonempty(
        raw,
        "industryTrendTemperatureCurr",
        "industryTrendTemperature",
        "industryTemperatureCurr",
    )
    candidate.industry_temperature_curr = str(temperature).strip() if temperature is not None else None
    candidate.raw_fields = {**(candidate.raw_fields or {}), "gate": raw}


def validate_snapshot_rows(rows: list[dict], *, wanted_tm_ids: set[int], as_of_date: str) -> dict[int, dict]:
    by_id: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise UsManualError("api_contract_error", "候选补充返回包含非对象行")
        tm_id = as_int(row.get("tmId"))
        if tm_id is None or tm_id not in wanted_tm_ids:
            raise UsManualError("api_contract_error", "候选补充返回了范围外 tmId")
        if row.get("asOfDate") != as_of_date:
            raise UsManualError("data_stale", f"tmId={tm_id} 的补充数据日期不匹配")
        if tm_id in by_id:
            raise UsManualError("api_contract_error", f"候选补充返回重复 tmId={tm_id}")
        by_id[tm_id] = row
    missing = sorted(wanted_tm_ids - set(by_id))
    if missing:
        raise UsManualError("data_incomplete", "候选补充返回不完整，已阻断计划生成", detail={"missing_tm_ids": missing})
    return by_id


def apply_enrichment(candidate: Any, raw: dict) -> None:
    """保留原始字段，同时把字段名归一为前端和规则稳定使用的列。"""
    candidate.amount_1d = _decimal_or_none(raw.get("amount1d"))
    candidate.market_cap = _decimal_or_none(raw.get("marketCap"))
    candidate.strength_local = _decimal_or_none(raw.get("trendStrengthLocalCurr"))
    candidate.industry_tm_id = as_int(raw.get("industryTmId"))
    candidate.industry_name = str(raw.get("industryName") or "").strip() or None
    temperature = _first_nonempty(
        raw,
        "industryTrendTemperatureCurr",
        "industryTrendTemperature",
        "industryTemperatureCurr",
    )
    if temperature is not None:
        candidate.industry_temperature_curr = str(temperature).strip() or None
    candidate.industry_strength_local = _decimal_or_none(_first_nonempty(
        raw,
        "industryTrendStrengthLocalCurr",
        "industryTrendStrengthLocal",
        "industryStrengthLocalCurr",
    ))
    candidate.industry_source_method = "ticker_snapshot_industry_fields"
    candidate.ticker_labels = _labels(raw.get("tickerLabels"))
    candidate.trend_phase_curr = str(raw.get("trendPhaseCurr") or "").strip() or None
    candidate.raw_fields = {**(candidate.raw_fields or {}), "enrichment": raw}
    if getattr(candidate, "asset_type", None) == "etf" and candidate.industry_tm_id is None:
        candidate.industry_source_method = "not_applicable_for_etf"
