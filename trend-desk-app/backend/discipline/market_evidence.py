"""市场证据摘要：只读既有事实，不生成第二套市场结论。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from backend import config
from backend.analysis.industry_heat import _headline as industry_headline
from backend.db import DailyDataset, IndustryHeatSnapshot, TradePlan
from backend.discipline.daily_data import china_trade_date, serialize_dataset
from backend.discipline.day_card import build_day_card
from backend.discipline.rules import RULES_VERSION


BUCKET_ORDER = {"support": 0, "challenge": 1, "watch": 2}
EFFECT_ORDER = {"active": 0, "gate": 1, "display_only": 2}
READY_DATASET_STATES = {"ready", "ready_degraded"}


def _item(
    item_id: str,
    *,
    bucket: str,
    label: str,
    value: Any,
    display_value: str,
    source: str,
    as_of: str | None,
    status: str,
    interpretation_rule: str,
    discipline_effect: str,
    detail: str,
) -> dict:
    return {
        "id": item_id,
        "bucket": bucket,
        "label": label,
        "value": value,
        "display_value": display_value,
        "source": source,
        "as_of": as_of,
        "status": status,
        "interpretation_rule": interpretation_rule,
        "discipline_effect": discipline_effect,
        "detail": detail,
    }


def _pick_dataset(session: Session, trade_date: str | None) -> DailyDataset | None:
    if trade_date:
        return session.exec(
            select(DailyDataset).where(DailyDataset.trade_date == trade_date)
        ).first()
    return session.exec(
        select(DailyDataset)
        .where(DailyDataset.status.in_(READY_DATASET_STATES))
        .order_by(DailyDataset.trade_date.desc())
    ).first()


def _dataset_evidence(session: Session, dataset: DailyDataset) -> tuple[dict, dict]:
    view = serialize_dataset(session, dataset, cached=True)
    trend_rows = int(view.get("trend_rows") or 0)
    market_rows = int(view.get("market_rows") or 0)
    value = {
        "dataset_id": dataset.dataset_id,
        "status": dataset.status,
        "source_mode": dataset.source_mode,
        "trend_rows": trend_rows,
        "market_rows": market_rows,
        "can_generate_plan": bool(view.get("can_generate_plan")),
        "error_code": dataset.error_code,
    }
    if dataset.status == "ready" and trend_rows > 0 and market_rows > 0:
        bucket, status = "support", "ready"
        detail = "每日事实包已就绪，趋势与市场数据均有落库记录。"
    elif dataset.status == "ready_degraded" or dataset.status == "ready":
        bucket, status = "watch", "partial"
        detail = "事实包可用但覆盖不完整；缺失不能解释为中性或通过。"
    else:
        bucket, status = "watch", "missing"
        detail = "每日事实包尚未就绪，不能据此补造环境证据。"
    display = f"{dataset.status} · 趋势 {trend_rows} 行 · 市场 {market_rows} 行"
    return _item(
        "dataset.health",
        bucket=bucket,
        label="每日事实包",
        value=value,
        display_value=display,
        source="daily_dataset",
        as_of=dataset.trade_date,
        status=status,
        interpretation_rule="dataset status and persisted row coverage",
        discipline_effect="gate",
        detail=detail,
    ), view


def _environment_evidence(card: dict, trade_date: str) -> dict:
    opening = (card.get("answers") or {}).get("Q1_opening")
    if not opening:
        return _item(
            "discipline.environment",
            bucket="watch",
            label="纪律环境",
            value=None,
            display_value="尚无计划环境快照",
            source="discipline_day_card",
            as_of=trade_date,
            status="missing",
            interpretation_rule="active discipline environment snapshot required",
            discipline_effect="active",
            detail="不得用行业热度或研究指标反推环境系数。",
        )
    temperature = opening.get("market_temperature") or "未知"
    factor = float(opening.get("environment_factor") or 0)
    per_position = float(opening.get("per_position_weight") or 0)
    allowed = bool(opening.get("opening_allowed"))
    return _item(
        "discipline.environment",
        bucket="support",
        label="纪律环境",
        value={
            "temperature": temperature,
            "environment_factor": factor,
            "per_position_weight": per_position,
            "opening_allowed": allowed,
        },
        display_value=f"{temperature} · 系数 {factor:.2f} · 单票 {per_position * 100:.2f}%",
        source="discipline_day_card",
        as_of=trade_date,
        status="ready",
        interpretation_rule=f"active discipline {RULES_VERSION} environment mapping",
        discipline_effect="active",
        detail="当前容量与开仓许可只以该纪律快照为准。",
    )


def _plan_health_evidence(session: Session, card: dict, trade_date: str) -> dict:
    plan_view = card.get("plan") or {}
    plan_id = plan_view.get("plan_id")
    plan = session.get(TradePlan, plan_id) if plan_id else None
    if plan is None:
        return _item(
            "plan.data_health",
            bucket="watch",
            label="计划数据闸门",
            value=None,
            display_value="尚无匹配计划",
            source="trade_plan.data_health",
            as_of=trade_date,
            status="missing",
            interpretation_rule="matching plan data-health required",
            discipline_effect="gate",
            detail="事实包存在不等于计划已具备锁定条件。",
        )
    health = dict(plan.data_health or {})
    errors = list(health.get("errors") or [])
    warnings = list(health.get("warnings") or [])
    lockable = bool(health.get("lockable"))
    value = {
        "plan_id": plan.plan_id,
        "lockable": lockable,
        "errors": errors,
        "warnings": warnings,
        "source_modes": health.get("source_modes") or {},
        "date_mismatches": health.get("date_mismatches") or [],
    }
    if lockable and not errors and not warnings:
        bucket, status = "support", "ready"
        display = "全部闸门通过"
        detail = "计划数据健康通过；是否行动仍由计划项和容量共同决定。"
    elif lockable and warnings and not errors:
        bucket, status = "watch", "partial"
        display = f"{len(warnings)} 项提醒"
        detail = "存在非阻断提醒，需保留原始代码并人工理解。"
    else:
        bucket, status = "watch", "missing"
        display = f"{len(errors)} 项阻断" if errors else "当前不可锁定"
        detail = "数据闸门未通过；错误不能被解释层覆盖。"
    return _item(
        "plan.data_health",
        bucket=bucket,
        label="计划数据闸门",
        value=value,
        display_value=display,
        source="trade_plan.data_health",
        as_of=plan.signal_date,
        status=status,
        interpretation_rule="lockable, errors and warnings",
        discipline_effect="gate",
        detail=detail,
    )


def _industry_rows(session: Session, dataset_id: str) -> list[IndustryHeatSnapshot]:
    return list(session.exec(
        select(IndustryHeatSnapshot)
        .where(IndustryHeatSnapshot.dataset_id == dataset_id)
        .order_by(IndustryHeatSnapshot.mainline_score.desc(), IndustryHeatSnapshot.trend_rank)
    ).all())


def _industry_evidence(
    rows: list[IndustryHeatSnapshot], *, trade_date: str, opening_allowed: bool | None,
) -> tuple[dict, dict]:
    headline = industry_headline(rows)
    coverage = {
        "industry_count": len(rows),
        "trend_complete": sum(
            row.temperature_curr is not None and row.strength_curr is not None for row in rows
        ),
        "wind_requested": sum(row.wind_status != "not_requested" for row in rows),
        "wind_verified": sum(row.wind_status == "verified" for row in rows),
        "wind_partial": sum(row.wind_status == "partial" for row in rows),
    }
    complete = bool(rows) and coverage["trend_complete"] == coverage["industry_count"]
    confidence = headline["confidence"]
    if not rows:
        bucket, status = "watch", "missing"
        detail = "尚无已落库行业热度；本接口不会在读取时计算或请求数据。"
    elif not complete:
        bucket, status = "watch", "partial"
        detail = "行业趋势覆盖不完整，不能把缺失行业视为中性。"
    elif confidence == "confirmed" and opening_allowed is not None:
        bucket = "support" if opening_allowed else "challenge"
        status = "ready"
        detail = (
            "行业主线证据与当前开仓环境同向。"
            if opening_allowed
            else "行业主线证据与当前禁止开仓环境分歧；该证据只有展示权。"
        )
    elif confidence == "weak" and opening_allowed:
        bucket, status = "challenge", "ready"
        detail = "行业领导力偏弱，与当前允许开仓环境形成反证，但不改写纪律。"
    else:
        bucket, status = "watch", "partial"
        detail = "行业主线仍是候选或当前环境不可比较，等待更多已落库证据。"
    value = {"headline": headline, "coverage": coverage}
    return _item(
        "industry.heat",
        bucket=bucket,
        label="行业领导力",
        value=value,
        display_value=headline["title"],
        source="industry_heat_snapshot",
        as_of=trade_date,
        status=status,
        interpretation_rule="explicit confidence/opening lookup with complete coverage",
        discipline_effect="display_only",
        detail=detail,
    ), coverage


def _wind_evidence(coverage: dict, *, trade_date: str) -> dict | None:
    if config.WIND_VALIDATION_MODE == "off":
        return None
    requested = int(coverage.get("wind_requested") or 0)
    verified = int(coverage.get("wind_verified") or 0)
    partial = int(coverage.get("wind_partial") or 0)
    if requested == 0:
        return None
    if verified >= requested:
        bucket, status = "support", "ready"
        detail = "已请求行业均有 Wind 验证记录；它仍然只有展示权。"
    elif verified > 0 or partial > 0:
        bucket, status = "watch", "partial"
        detail = "Wind 只覆盖部分已请求行业，未覆盖部分不能默认为通过。"
    else:
        bucket, status = "watch", "missing"
        detail = "已请求但没有可用 Wind 验证记录；不把缺失解释为看空。"
    return _item(
        "industry.wind_validation",
        bucket=bucket,
        label="Wind 行业验证",
        value={"requested": requested, "verified": verified, "partial": partial},
        display_value=f"{verified} 完整 · {partial} 部分 / {requested} 已请求",
        source="industry_heat_snapshot.wind_status",
        as_of=trade_date,
        status=status,
        interpretation_rule="verified coverage among requested industries",
        discipline_effect="display_only",
        detail=detail,
    )


def _freshness_evidence(dataset: DailyDataset) -> dict | None:
    dates = {
        str(source): str(value)
        for source, value in (dataset.source_dates or {}).items()
        if value not in (None, "")
    }
    mismatches = {source: value for source, value in dates.items() if value != dataset.trade_date}
    if not mismatches:
        return None
    return _item(
        "freshness.source_date_mismatch",
        bucket="watch",
        label="来源日期不一致",
        value={"expected": dataset.trade_date, "actual": dates, "mismatches": mismatches},
        display_value=f"{len(mismatches)} 个来源日期不一致",
        source="daily_dataset.source_dates",
        as_of=dataset.trade_date,
        status="stale",
        interpretation_rule="every populated source date equals dataset trade date",
        discipline_effect="gate",
        detail="显示各来源真实日期；请求时间不能替代事实时间。",
    )


def _summary(items: list[dict]) -> dict:
    return {
        key: sum(item[field] == key for item in items)
        for field, keys in (
            ("bucket", ("support", "challenge", "watch")),
            ("status", ("ready", "partial", "missing", "stale")),
        )
        for key in keys
    }


def build_market_evidence(session: Session, *, trade_date: str | None = None) -> dict:
    """Build a deterministic summary from persisted facts with zero network/LLM calls."""
    dataset = _pick_dataset(session, trade_date)
    requested_date = trade_date or (dataset.trade_date if dataset else china_trade_date())
    if dataset is None:
        items = [
            _item(
                "dataset.health",
                bucket="watch",
                label="每日事实包",
                value=None,
                display_value="尚无可用事实包",
                source="daily_dataset",
                as_of=requested_date,
                status="missing",
                interpretation_rule="persisted dataset required",
                discipline_effect="gate",
                detail="本接口不会触发采集、付费调用或补造事实。",
            ),
            _environment_evidence({}, requested_date),
            _plan_health_evidence(session, {}, requested_date),
        ]
        items.sort(key=lambda row: (
            BUCKET_ORDER[row["bucket"]], EFFECT_ORDER[row["discipline_effect"]], row["id"]
        ))
        return {
            "trade_date": requested_date,
            "dataset_id": None,
            "discipline_version": RULES_VERSION,
            "headline": None,
            "items": items,
            "summary": _summary(items),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "network_calls": 0,
            "llm_required": False,
        }

    card = build_day_card(session, trade_date=dataset.trade_date)
    opening = (card.get("answers") or {}).get("Q1_opening")
    items: list[dict] = []
    items.append(_environment_evidence(card, dataset.trade_date))
    dataset_item, _ = _dataset_evidence(session, dataset)
    items.append(dataset_item)
    items.append(_plan_health_evidence(session, card, dataset.trade_date))
    freshness = _freshness_evidence(dataset)
    if freshness:
        items.append(freshness)
    industry_item, coverage = _industry_evidence(
        _industry_rows(session, dataset.dataset_id),
        trade_date=dataset.trade_date,
        opening_allowed=bool(opening.get("opening_allowed")) if opening else None,
    )
    items.append(industry_item)
    wind_item = _wind_evidence(coverage, trade_date=dataset.trade_date)
    if wind_item:
        items.append(wind_item)
    items.sort(key=lambda row: (
        BUCKET_ORDER[row["bucket"]], EFFECT_ORDER[row["discipline_effect"]], row["id"]
    ))
    return {
        "trade_date": dataset.trade_date,
        "dataset_id": dataset.dataset_id,
        "discipline_version": (card.get("plan") or {}).get("discipline_version") or RULES_VERSION,
        "headline": opening,
        "items": items,
        "summary": _summary(items),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "network_calls": 0,
        "llm_required": False,
    }
