"""美股右侧资产页的数据库与不可变文件归档。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from backend import config
from backend.db import (
    UsRightSideAssetSnapshot, UsRightSideIndustrySnapshot, UsRightSideRun,
)
from .contracts import FIELD_AVAILABLE, FIELD_NOT_RETURNED, UsRightSideError


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def payload_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str).encode()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return hashlib.sha256(encoded).hexdigest()


def archive_dir(as_of_date: str, run_id: str) -> Path:
    return config.DATA / "research" / "trend_animals" / "us_right_side" / as_of_date / run_id


def latest_universe_seed() -> tuple[Path, dict]:
    base = config.DATA / "research" / "trend_animals" / "us_coverage"
    candidates: list[tuple[str, Path, dict]] = []
    if base.exists():
        for seed_path in base.glob("*/current_universe_seed.json"):
            try:
                payload = json.loads(seed_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            date = str(payload.get("as_of_date") or seed_path.parent.name)
            if payload.get("instruments"):
                candidates.append((date, seed_path, payload))
    if not candidates:
        raise UsRightSideError(
            "universe_not_ready",
            "尚无获准归档的趋势动物美股成员范围，请先刷新覆盖范围",
            status_code=404,
        )
    _, path, payload = max(candidates, key=lambda item: item[0])
    return path, payload


def us_stock_universe(seed: dict) -> list[dict]:
    rows: list[dict] = []
    seen: set[int] = set()
    for raw in seed.get("instruments") or []:
        if not isinstance(raw, dict) or raw.get("root_asset") != "美股" or raw.get("asset") != "美股":
            continue
        try:
            tm_id = int(raw.get("tmId"))
        except (TypeError, ValueError):
            continue
        symbol = str(raw.get("tickerSymbol") or "").strip().upper()
        if tm_id <= 0 or not symbol:
            continue
        if tm_id in seen:
            raise UsRightSideError("universe_invalid", f"成员范围含重复 tmId={tm_id}")
        seen.add(tm_id)
        rows.append({
            "tmId": tm_id,
            "tickerSymbol": symbol,
            "tickerName": raw.get("tickerName"),
            "asset": raw.get("asset"),
            "currencyDefault": raw.get("currencyDefault"),
            "asOfDate": raw.get("asOfDate"),
        })
    if not rows:
        raise UsRightSideError("universe_not_ready", "美股成员范围为空", status_code=404)
    return sorted(rows, key=lambda item: (item["tickerSymbol"], item["tmId"]))


def get_run(session: Session, run_id: str) -> UsRightSideRun:
    run = session.get(UsRightSideRun, run_id)
    if run is None:
        raise UsRightSideError("run_not_found", "未找到美股右侧运行", status_code=404)
    return run


def latest_run(session: Session) -> UsRightSideRun | None:
    return session.exec(select(UsRightSideRun).order_by(UsRightSideRun.created_at.desc())).first()


def assets_for_run(session: Session, run_id: str) -> list[UsRightSideAssetSnapshot]:
    return list(session.exec(
        select(UsRightSideAssetSnapshot).where(UsRightSideAssetSnapshot.run_id == run_id)
    ).all())


def industries_for_run(session: Session, run_id: str) -> list[UsRightSideIndustrySnapshot]:
    return list(session.exec(
        select(UsRightSideIndustrySnapshot).where(UsRightSideIndustrySnapshot.run_id == run_id)
    ).all())


def decimal_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def datetime_text(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def run_payload(run: UsRightSideRun) -> dict:
    total_actual = sum((value or Decimal("0")) for value in (
        run.actual_screen_cost_cny, run.actual_age_cost_cny, run.actual_strength_cost_cny,
        run.actual_standard_cost_cny,
        run.actual_industry_cost_cny, run.actual_deep_cost_cny,
    ))
    return {
        "run_id": run.run_id,
        "as_of_date": run.as_of_date,
        "membership_as_of_date": run.membership_as_of_date,
        "upstream_update_dt": run.upstream_update_dt,
        "scope": run.scope,
        "rules_version": run.rules_version,
        "status": run.status,
        "counts": {
            "universe": run.universe_count,
            "scanned": run.scanned_count,
            "right_side": run.right_side_count,
            "unknown": run.unknown_count,
            "age_covered": run.age_covered_count,
            "strength_covered": run.strength_covered_count,
            "strength_target": run.strength_target_count,
            "strength_max_days": run.strength_max_days,
            "standard_covered": run.standard_covered_count,
            "standard_target": run.standard_target_count,
            "standard_max_days": run.standard_max_days,
            "standard_top_n": run.standard_top_n,
            "industries": run.industry_count,
            "industry_covered": run.industry_covered_count,
            "deep_covered": run.deep_covered_count,
        },
        "readiness": {
            "age": run.age_ready,
            "strength": run.strength_ready,
            "standard": run.standard_ready,
            "industry": run.industry_ready,
            "deep": run.deep_ready,
        },
        "cost": {
            "estimated_screen_cny": decimal_text(run.estimated_screen_cost_cny),
            "approved_screen_cny": decimal_text(run.approved_screen_budget_cny),
            "actual_screen_cny": decimal_text(run.actual_screen_cost_cny),
            "estimated_age_cny": decimal_text(run.estimated_age_cost_cny),
            "approved_age_cny": decimal_text(run.approved_age_budget_cny),
            "actual_age_cny": decimal_text(run.actual_age_cost_cny),
            "estimated_strength_cny": decimal_text(run.estimated_strength_cost_cny),
            "approved_strength_cny": decimal_text(run.approved_strength_budget_cny),
            "actual_strength_cny": decimal_text(run.actual_strength_cost_cny),
            "estimated_standard_cny": decimal_text(run.estimated_standard_cost_cny),
            "approved_standard_cny": decimal_text(run.approved_standard_budget_cny),
            "actual_standard_cny": decimal_text(run.actual_standard_cost_cny),
            "estimated_industry_cny": decimal_text(run.estimated_industry_cost_cny),
            "approved_industry_cny": decimal_text(run.approved_industry_budget_cny),
            "actual_industry_cny": decimal_text(run.actual_industry_cost_cny),
            "estimated_deep_cny": decimal_text(run.estimated_deep_cost_cny),
            "approved_deep_cny": decimal_text(run.approved_deep_budget_cny),
            "actual_deep_cny": decimal_text(run.actual_deep_cost_cny),
            "actual_total_cny": decimal_text(total_actual),
            "breakdown": run.cost_breakdown_json,
        },
        "progress": run.progress_json,
        "cache_hit": run.cache_hit,
        "audit": {
            "cache_key": {
                "as_of_date": run.as_of_date,
                "scope": run.scope,
                "universe_sha256": run.universe_sha256,
                "screen_fields_hash": run.screen_fields_hash,
            },
            "field_sets": {
                "screen": {"fields": run.screen_fields, "sha256": run.screen_fields_hash},
                "age": {"fields": run.age_fields or [], "sha256": run.age_fields_hash or ""},
                "strength": {"fields": run.strength_fields or [], "sha256": run.strength_fields_hash or ""},
                "standard": {"fields": run.standard_fields, "sha256": run.standard_fields_hash},
                "industry": {"fields": run.industry_fields, "sha256": run.industry_fields_hash},
                "deep": {"fields": run.deep_fields, "sha256": run.deep_fields_hash},
            },
            "manifest": {
                "available": bool(run.manifest_path and run.manifest_sha256),
                "sha256": run.manifest_sha256,
            },
            "lease_active": bool(run.lease_owner and run.lease_expires_at),
        },
        "error": ({"code": run.error_code, "message": run.error_message}
                  if run.error_code else None),
        "created_at": datetime_text(run.created_at),
        "updated_at": datetime_text(run.updated_at),
        "completed_at": datetime_text(run.completed_at),
    }


def asset_payload(asset: UsRightSideAssetSnapshot,
                  industry: UsRightSideIndustrySnapshot | None = None) -> dict:
    fields = dict(asset.field_states_json or {})
    payload = {
        "tm_id": asset.tm_id,
        "ticker_symbol": asset.ticker_symbol,
        "ticker_name": asset.ticker_name,
        "asset": asset.asset,
        "currency_default": asset.currency_default,
        "as_of_date": asset.as_of_date,
        "is_right_side": asset.is_right_side,
        "tradable_flag": asset.tradable_flag,
        "price_index": decimal_text(asset.price_index),
        "market_cap": decimal_text(asset.market_cap),
        "amount_1d": decimal_text(asset.amount_1d),
        "temperature_prev": asset.temperature_prev,
        "temperature_curr": asset.temperature_curr,
        "days_since_trend_entry": asset.days_since_trend_entry,
        "gain_since_trend_entry": decimal_text(asset.gain_since_trend_entry),
        "phase_curr": asset.phase_curr,
        "strength_local_curr": decimal_text(asset.strength_local_curr),
        "strength_local_change": asset.strength_local_change,
        "industry_tm_id": asset.industry_tm_id,
        "industry_name": asset.industry_name or (industry.industry_name if industry else None),
        "danger_flag": asset.danger_flag,
        "boiling_flag": asset.boiling_flag,
        "champagne_flag": asset.champagne_flag,
        "risk_flag_count": asset.risk_flag_count,
        "ticker_labels": asset.ticker_labels,
        "heat_score_7d": decimal_text(asset.heat_score_7d),
        "return_1m": decimal_text(asset.return_1m),
        "field_states": fields,
    }
    payload.update({
        "industry_temperature_curr": industry.temperature_curr if industry else None,
        "industry_strength_local_curr": decimal_text(industry.strength_local_curr) if industry else None,
        "industry_phase_curr": industry.phase_curr if industry else None,
        "industry_is_right_side": industry.is_right_side if industry else None,
        "industry_as_of_date": industry.as_of_date if industry else None,
        "industry_field_states": dict(industry.field_states_json or {}) if industry else {},
        "industry_source_method": industry.source_method if industry else None,
    })
    if industry and not asset.industry_name:
        fields["industry_name"] = FIELD_AVAILABLE if industry.industry_name else FIELD_NOT_RETURNED
    # 排序器读取统一 field_states；行业状态合并为独立键。
    for key, state in (industry.field_states_json or {}).items() if industry else []:
        fields[f"industry_{key}"] = state
    return payload
