"""趋势动物快照校验与归一化。纯事实处理，不包含交易判断。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlmodel import Session, select

from backend.db import UsRightSideAssetSnapshot, UsRightSideIndustrySnapshot
from .contracts import (
    AGE_FIELDS, DEEP_FIELDS, FIELD_AVAILABLE, FIELD_NOT_APPLICABLE, FIELD_NOT_REQUESTED,
    FIELD_NOT_RETURNED, INDUSTRY_FIELDS, STANDARD_ASSET_FIELDS, STRENGTH_FIELDS,
    UsRightSideError,
)
from .repository import payload_sha256


STANDARD_MAP = {
    "tradableFlag": "tradable_flag",
    "industryTmId": "industry_tm_id",
    "industryName": "industry_name",
    "priceIndex": "price_index",
    "marketCap": "market_cap",
    "amount1d": "amount_1d",
    "trendTemperatureCurr": "temperature_curr",
    "trendTemperaturePrev": "temperature_prev",
    "daysSinceTrendEntry": "days_since_trend_entry",
    "trendPhaseCurr": "phase_curr",
    "trendStrengthLocalCurr": "strength_local_curr",
}
AGE_MAP = {"daysSinceTrendEntry": "days_since_trend_entry"}
STRENGTH_MAP = {"trendStrengthLocalCurr": "strength_local_curr"}
INDUSTRY_MAP = {
    "isTrendRightSide": "is_right_side",
    "trendTemperatureCurr": "temperature_curr",
    "trendStrengthLocalCurr": "strength_local_curr",
    "trendPhaseCurr": "phase_curr",
}
DEEP_MAP = {
    "gainSinceTrendEntry": "gain_since_trend_entry",
    "trendStrengthLocalChange": "strength_local_change",
    "stopwinFlagByDangerSignal": "danger_flag",
    "stopwinFlagByBoilingTemperature": "boiling_flag",
    "stopwinFlagByPopChampagne": "champagne_flag",
    "tickerLabels": "ticker_labels",
    "heatScore7d": "heat_score_7d",
    "return1m": "return_1m",
}
DECIMAL_ATTRS = {
    "price_index", "market_cap", "amount_1d", "strength_local_curr",
    "gain_since_trend_entry", "heat_score_7d", "return_1m",
}
BOOL_ATTRS = {"tradable_flag", "danger_flag", "boiling_flag", "champagne_flag", "is_right_side"}


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _labels(value: Any) -> list[str]:
    if isinstance(value, list):
        source = value
    elif isinstance(value, str):
        source = value.replace("；", ";").split(";")
    else:
        source = []
    return list(dict.fromkeys(str(item).strip() for item in source if str(item).strip()))


def _coerce(attr: str, value: Any) -> Any:
    if attr in DECIMAL_ATTRS:
        return _decimal(value)
    if attr in BOOL_ATTRS:
        return value if isinstance(value, bool) else None
    if attr in {"industry_tm_id", "days_since_trend_entry"}:
        return _integer(value)
    if attr == "ticker_labels":
        return _labels(value)
    return str(value).strip() if value is not None and str(value).strip() else None


def validated_rows(rows: Any, *, wanted_tm_ids: set[int], as_of_date: str,
                   context: str) -> dict[int, dict]:
    if not isinstance(rows, list):
        raise UsRightSideError("api_contract_error", f"{context} 返回不是数组")
    by_id: dict[int, dict] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise UsRightSideError("api_contract_error", f"{context} 包含非对象行")
        tm_id = _integer(raw.get("tmId"))
        if tm_id is None or tm_id not in wanted_tm_ids:
            raise UsRightSideError("api_contract_error", f"{context} 返回范围外 tmId")
        if tm_id in by_id:
            raise UsRightSideError("api_contract_error", f"{context} 返回重复 tmId={tm_id}")
        if raw.get("asOfDate") != as_of_date:
            raise UsRightSideError(
                "data_date_mismatch",
                f"{context} tmId={tm_id} 数据日 {raw.get('asOfDate')}，期望 {as_of_date}",
            )
        by_id[tm_id] = raw
    return by_id


def persist_screen_batch(session: Session, *, run_id: str, universe_by_tm: dict[int, dict],
                         tm_ids: list[int], rows: list[dict], as_of_date: str,
                         raw_archive_ref: str) -> dict:
    by_id = validated_rows(
        rows, wanted_tm_ids=set(tm_ids), as_of_date=as_of_date, context="右侧扫描")
    right_side = 0
    unknown = 0
    false_count = 0
    for tm_id in tm_ids:
        raw = by_id.get(tm_id)
        state = raw.get("isTrendRightSide") if raw else None
        if state is True:
            identity = universe_by_tm[tm_id]
            existing = session.exec(select(UsRightSideAssetSnapshot).where(
                UsRightSideAssetSnapshot.run_id == run_id,
                UsRightSideAssetSnapshot.tm_id == tm_id,
            )).first()
            asset = existing or UsRightSideAssetSnapshot(
                run_id=run_id,
                tm_id=tm_id,
                ticker_symbol=str(raw.get("tickerSymbol") or identity.get("tickerSymbol") or "").upper(),
                ticker_name=raw.get("tickerName") or identity.get("tickerName"),
                asset=str(raw.get("asset") or identity.get("asset") or "美股"),
                currency_default=raw.get("currencyDefault") or identity.get("currencyDefault"),
                as_of_date=as_of_date,
                is_right_side=True,
            )
            asset.field_states_json = {
                **{
                    attr: FIELD_NOT_REQUESTED
                    for attr in (*STANDARD_MAP.values(), *DEEP_MAP.values(), "risk_flag_count")
                },
                **(asset.field_states_json or {}),
                "is_right_side": FIELD_AVAILABLE,
            }
            asset.raw_archive_ref = raw_archive_ref
            asset.raw_sha256 = payload_sha256(raw)
            asset.updated_at = datetime.utcnow()
            session.add(asset)
            right_side += 1
        elif state is False:
            false_count += 1
        else:
            unknown += 1
    session.commit()
    return {
        "returned": len(by_id),
        "right_side": right_side,
        "not_right_side": false_count,
        "unknown": unknown,
        "missing_tm_ids": sorted(set(tm_ids) - set(by_id)),
    }


def _apply_fields(target: Any, raw: dict, mapping: dict[str, str],
                  fields: tuple[str, ...], *, phase_not_applicable: bool = False) -> None:
    states = dict(target.field_states_json or {})
    for external in fields:
        attr = mapping[external]
        value = raw.get(external)
        if phase_not_applicable and external == "trendPhaseCurr":
            setattr(target, attr, None)
            states[attr] = FIELD_NOT_APPLICABLE
        elif external in raw and value is not None:
            coerced = _coerce(attr, value)
            setattr(target, attr, coerced)
            states[attr] = FIELD_AVAILABLE if coerced is not None else FIELD_NOT_RETURNED
        else:
            setattr(target, attr, None if attr != "ticker_labels" else [])
            states[attr] = FIELD_NOT_RETURNED
    target.field_states_json = states


def persist_standard_batch(session: Session, *, run_id: str, tm_ids: list[int], rows: list[dict],
                           as_of_date: str, raw_archive_ref: str) -> dict:
    by_id = validated_rows(
        rows, wanted_tm_ids=set(tm_ids), as_of_date=as_of_date, context="标准字段增强")
    assets = {
        row.tm_id: row for row in session.exec(select(UsRightSideAssetSnapshot).where(
            UsRightSideAssetSnapshot.run_id == run_id,
            UsRightSideAssetSnapshot.tm_id.in_(tm_ids),
        )).all()
    }
    for tm_id, raw in by_id.items():
        asset = assets.get(tm_id)
        if asset is None:
            raise UsRightSideError("data_incomplete", f"标准增强缺少本地右侧资产 tmId={tm_id}")
        _apply_fields(asset, raw, STANDARD_MAP, STANDARD_ASSET_FIELDS)
        asset.raw_archive_ref = raw_archive_ref
        asset.raw_sha256 = payload_sha256(raw)
        asset.updated_at = datetime.utcnow()
        session.add(asset)
    session.commit()
    return {"returned": len(by_id), "missing_tm_ids": sorted(set(tm_ids) - set(by_id))}


def persist_age_batch(session: Session, *, run_id: str, tm_ids: list[int], rows: list[dict],
                      as_of_date: str, raw_archive_ref: str) -> dict:
    by_id = validated_rows(
        rows, wanted_tm_ids=set(tm_ids), as_of_date=as_of_date, context="右侧天数初筛")
    assets = {
        row.tm_id: row for row in session.exec(select(UsRightSideAssetSnapshot).where(
            UsRightSideAssetSnapshot.run_id == run_id,
            UsRightSideAssetSnapshot.tm_id.in_(tm_ids),
        )).all()
    }
    for tm_id, raw in by_id.items():
        asset = assets.get(tm_id)
        if asset is None:
            raise UsRightSideError("data_incomplete", f"右侧天数初筛缺少本地资产 tmId={tm_id}")
        _apply_fields(asset, raw, AGE_MAP, AGE_FIELDS)
        asset.raw_archive_ref = raw_archive_ref
        asset.raw_sha256 = payload_sha256(raw)
        asset.updated_at = datetime.utcnow()
        session.add(asset)
    session.commit()
    return {"returned": len(by_id), "missing_tm_ids": sorted(set(tm_ids) - set(by_id))}


def persist_strength_batch(session: Session, *, run_id: str, tm_ids: list[int], rows: list[dict],
                           as_of_date: str, raw_archive_ref: str) -> dict:
    by_id = validated_rows(
        rows, wanted_tm_ids=set(tm_ids), as_of_date=as_of_date, context="候选强度预筛")
    assets = {
        row.tm_id: row for row in session.exec(select(UsRightSideAssetSnapshot).where(
            UsRightSideAssetSnapshot.run_id == run_id,
            UsRightSideAssetSnapshot.tm_id.in_(tm_ids),
        )).all()
    }
    for tm_id, raw in by_id.items():
        asset = assets.get(tm_id)
        if asset is None:
            raise UsRightSideError("data_incomplete", f"强度预筛缺少本地资产 tmId={tm_id}")
        _apply_fields(asset, raw, STRENGTH_MAP, STRENGTH_FIELDS)
        asset.raw_archive_ref = raw_archive_ref
        asset.raw_sha256 = payload_sha256(raw)
        asset.updated_at = datetime.utcnow()
        session.add(asset)
    session.commit()
    return {"returned": len(by_id), "missing_tm_ids": sorted(set(tm_ids) - set(by_id))}


def persist_industry_batch(session: Session, *, run_id: str, tm_ids: list[int], rows: list[dict],
                           as_of_date: str, industry_names: dict[int, str],
                           raw_archive_ref: str) -> dict:
    by_id = validated_rows(
        rows, wanted_tm_ids=set(tm_ids), as_of_date=as_of_date, context="行业环境增强")
    for tm_id, raw in by_id.items():
        existing = session.exec(select(UsRightSideIndustrySnapshot).where(
            UsRightSideIndustrySnapshot.run_id == run_id,
            UsRightSideIndustrySnapshot.industry_tm_id == tm_id,
        )).first()
        industry = existing or UsRightSideIndustrySnapshot(
            run_id=run_id, industry_tm_id=tm_id,
            industry_name=industry_names.get(tm_id) or raw.get("tickerName"),
            as_of_date=as_of_date,
        )
        is_right_side = raw.get("isTrendRightSide")
        _apply_fields(
            industry, raw, INDUSTRY_MAP, INDUSTRY_FIELDS,
            phase_not_applicable=is_right_side is False,
        )
        industry.raw_archive_ref = raw_archive_ref
        industry.raw_sha256 = payload_sha256(raw)
        industry.updated_at = datetime.utcnow()
        session.add(industry)
    session.commit()
    return {"returned": len(by_id), "missing_tm_ids": sorted(set(tm_ids) - set(by_id))}


def persist_deep_batch(session: Session, *, run_id: str, tm_ids: list[int], rows: list[dict],
                       as_of_date: str, raw_archive_ref: str) -> dict:
    by_id = validated_rows(
        rows, wanted_tm_ids=set(tm_ids), as_of_date=as_of_date, context="深度字段增强")
    assets = {
        row.tm_id: row for row in session.exec(select(UsRightSideAssetSnapshot).where(
            UsRightSideAssetSnapshot.run_id == run_id,
            UsRightSideAssetSnapshot.tm_id.in_(tm_ids),
        )).all()
    }
    for tm_id, raw in by_id.items():
        asset = assets.get(tm_id)
        if asset is None:
            raise UsRightSideError("data_incomplete", f"深度增强缺少本地右侧资产 tmId={tm_id}")
        _apply_fields(asset, raw, DEEP_MAP, DEEP_FIELDS)
        flags = (asset.danger_flag, asset.boiling_flag, asset.champagne_flag)
        if all(flag is not None for flag in flags):
            asset.risk_flag_count = sum(bool(flag) for flag in flags)
            states = dict(asset.field_states_json or {})
            states["risk_flag_count"] = FIELD_AVAILABLE
            asset.field_states_json = states
        asset.raw_archive_ref = raw_archive_ref
        asset.raw_sha256 = payload_sha256(raw)
        asset.updated_at = datetime.utcnow()
        session.add(asset)
    session.commit()
    return {"returned": len(by_id), "missing_tm_ids": sorted(set(tm_ids) - set(by_id))}
