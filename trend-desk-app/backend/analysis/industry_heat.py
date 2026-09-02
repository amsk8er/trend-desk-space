"""A-share industry heat, rotation and mainline evidence."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Callable

from sqlmodel import Session, delete, select

from backend import config
from backend.db import (
    DailyDataset, IndustryCatalogEntry, IndustryHeatSnapshot,
    TrendDailyMembership, TrendDailySnapshot,
)
from backend.analysis.wind_sector import (
    IndustryWindError, WindSectorClient, WindSkillAuthError, WindSkillCliClient,
    WindSkillMappingError, configured as wind_configured, default_client,
)


TEMP_RANK = {"冻": -1, "寒": 0, "凉": 1, "平": 2, "温": 3, "热": 4, "沸": 5}
TEMP_SCORE = {"冻": 0.0, "寒": 10.0, "凉": 25.0, "平": 45.0,
              "温": 65.0, "热": 85.0, "沸": 100.0}


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _slope(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    x_mean = (len(values) - 1) / 2
    y_mean = sum(values) / len(values)
    denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
    if not denominator:
        return 0.0
    return sum((index - x_mean) * (value - y_mean)
               for index, value in enumerate(values)) / denominator


def _warming_streak(history: list[TrendDailySnapshot]) -> int:
    streak = 0
    for left, right in zip(reversed(history[:-1]), reversed(history[1:])):
        lrank, rrank = TEMP_RANK.get(left.temperature_curr), TEMP_RANK.get(right.temperature_curr)
        if lrank is None or rrank is None or rrank <= lrank:
            break
        streak += 1
    return streak


def _hot_duration(history: list[TrendDailySnapshot]) -> int:
    duration = 0
    for row in reversed(history):
        if TEMP_RANK.get(row.temperature_curr, -99) < TEMP_RANK["温"]:
            break
        duration += 1
    return duration


def _trend_state(*, score: float, temperature: str | None,
                 slope: float | None) -> str:
    if temperature not in TEMP_RANK:
        return "insufficient"
    if score >= 75 and TEMP_RANK[temperature] >= TEMP_RANK["温"] and (slope or 0) >= 0:
        return "leading"
    if score >= 62:
        return "strengthening"
    if TEMP_RANK[temperature] >= TEMP_RANK["温"] and slope is not None and slope < 0:
        return "fading"
    if score < 40:
        return "lagging"
    return "rotating"


def _mainline_state(row: IndustryHeatSnapshot) -> str:
    if row.trend_state == "leading":
        if row.wind_status in {"verified", "partial"} and row.wind_score is not None:
            if row.wind_score >= 60 and row.wind_coverage >= 0.5:
                return "confirmed_mainline"
            if row.wind_score < 45:
                return "concentrated_not_diffused"
        return "candidate_unverified"
    if row.trend_state == "strengthening" and (row.wind_score or 0) >= 60:
        return "emerging"
    if row.trend_state == "fading":
        return "rotating_out"
    return row.trend_state


def _mainline_score(row: IndustryHeatSnapshot) -> float:
    if row.wind_score is None or row.wind_coverage <= 0:
        return round(row.trend_score, 2)
    wind_weight = min(0.30, 0.30 * row.wind_coverage)
    return round(row.trend_score * (1 - wind_weight) + row.wind_score * wind_weight, 2)


def calculate(session: Session, dataset_id: str) -> list[IndustryHeatSnapshot]:
    dataset = session.get(DailyDataset, dataset_id)
    if dataset is None:
        raise KeyError(dataset_id)
    joined = session.exec(
        select(TrendDailySnapshot, TrendDailyMembership)
        .join(TrendDailyMembership,
              (TrendDailyMembership.dataset_id == TrendDailySnapshot.dataset_id)
              & (TrendDailyMembership.tm_id == TrendDailySnapshot.tm_id))
        .where(TrendDailyMembership.membership_type == "sector")
        .where(TrendDailySnapshot.as_of_date <= dataset.trade_date)
        .order_by(TrendDailySnapshot.as_of_date, TrendDailySnapshot.tm_id)
    ).all()
    histories: dict[int, list[TrendDailySnapshot]] = defaultdict(list)
    current_ids: set[int] = set()
    for snapshot, membership in joined:
        histories[snapshot.tm_id].append(snapshot)
        if membership.dataset_id == dataset_id:
            current_ids.add(snapshot.tm_id)

    warm_counts: dict[int, int] = defaultdict(int)
    candidate_rows = session.exec(
        select(TrendDailySnapshot, TrendDailyMembership)
        .join(TrendDailyMembership,
              (TrendDailyMembership.dataset_id == TrendDailySnapshot.dataset_id)
              & (TrendDailyMembership.tm_id == TrendDailySnapshot.tm_id))
        .where(TrendDailyMembership.dataset_id == dataset_id)
        .where(TrendDailyMembership.membership_type == "warm_to_hot_stock")
    ).all()
    for snapshot, _ in candidate_rows:
        if snapshot.industry_tm_id is not None:
            warm_counts[snapshot.industry_tm_id] += 1
    max_warm = max(warm_counts.values(), default=0)

    out: list[IndustryHeatSnapshot] = []
    for tm_id in sorted(current_ids):
        history = histories[tm_id]
        current = history[-1]
        strengths = [float(row.strength) for row in history[-5:] if row.strength is not None]
        slope = _slope(strengths)
        warming = _warming_streak(history)
        duration = _hot_duration(history)
        participation = (100.0 * math.sqrt(warm_counts[tm_id] / max_warm)
                         if max_warm and warm_counts[tm_id] else 0.0)
        temp_component = TEMP_SCORE.get(current.temperature_curr, 0.0)
        strength_component = _clamp(float(current.strength or 0.0))
        slope_component = _clamp(50.0 + (slope or 0.0) * 7.5)
        warming_component = _clamp(warming * 25.0 + min(duration, 5) * 5.0)
        score = round(
            temp_component * 0.32 + strength_component * 0.33
            + slope_component * 0.15 + warming_component * 0.10
            + participation * 0.10,
            2,
        )
        out.append(IndustryHeatSnapshot(
            dataset_id=dataset_id, trade_date=dataset.trade_date,
            industry_tm_id=tm_id, industry_name=current.name,
            temperature_curr=current.temperature_curr,
            strength_curr=current.strength, strength_change=current.strength_change,
            phase_curr=current.phase, warming_streak=warming,
            hot_duration_days=duration,
            strength_slope_5d=round(slope, 3) if slope is not None else None,
            warm_to_hot_count=warm_counts[tm_id], trend_score=score,
            trend_state=_trend_state(score=score, temperature=current.temperature_curr, slope=slope),
            trend_evidence={
                "history_days": len(history), "temperature_score": temp_component,
                "strength_score": strength_component,
                "slope_score": round(slope_component, 2),
                "warming_score": round(warming_component, 2),
                "participation_score": round(participation, 2),
            },
            mainline_score=score,
        ))
    out.sort(key=lambda row: (-row.trend_score, -float(row.strength_curr or -1), row.industry_tm_id))
    for rank, row in enumerate(out, start=1):
        row.trend_rank = rank
        row.mainline_state = _mainline_state(row)
    return out


def persist(session: Session, rows: list[IndustryHeatSnapshot]) -> list[IndustryHeatSnapshot]:
    if not rows:
        return []
    dataset_id = rows[0].dataset_id
    session.exec(delete(IndustryHeatSnapshot).where(
        IndustryHeatSnapshot.dataset_id == dataset_id))
    for row in rows:
        session.add(row)
    session.flush()
    return rows


def apply_wind_validation(
    session: Session, rows: list[IndustryHeatSnapshot], *, top_n: int | None = None,
    client_factory: Callable[[], WindSectorClient | WindSkillCliClient] = default_client,
) -> dict:
    top_n = max(5, min(top_n or config.INDUSTRY_WIND_TOP_N, 10))
    selected = rows[:top_n]
    if not selected:
        return {"status": "no_rows", "requested": 0, "verified": 0}
    if not wind_configured():
        if config.WIND_VALIDATION_MODE == "off":
            message = "Wind行业验证已关闭"
        elif config.WIND_VALIDATION_MODE == "direct":
            message = "服务端未配置 WIND_API_KEY"
        else:
            message = "本地 Wind Skill CLI 不可用"
        for row in selected:
            row.wind_status = "not_configured"
            row.wind_error = message
            row.mainline_state = _mainline_state(row)
            session.add(row)
        session.flush()
        return {"status": "not_configured", "requested": len(selected), "verified": 0}
    client = client_factory()
    verified = 0
    auth_failed = False
    for index, row in enumerate(selected):
        catalog = session.get(IndustryCatalogEntry, row.industry_tm_id)
        cached_code = None
        if (catalog is not None and catalog.wind_mapping_status == "mapped"
                and catalog.wind_code):
            cached_code = catalog.wind_code
        try:
            if cached_code:
                result = client.validate(row.industry_name, windcode=cached_code)
            else:
                result = client.validate(row.industry_name)
            row.wind_status = result["status"]
            row.wind_score = result["score"]
            row.wind_coverage = result["coverage"]
            row.wind_metrics = {
                **result["metrics"], "components": result["components"],
            }
            row.wind_response_hash = result["response_hash"]
            row.wind_verified_at = result["verified_at"]
            row.wind_error = None
            mapped_code = result.get("wind_code")
            if catalog is not None and mapped_code:
                catalog.wind_code = str(mapped_code)
                catalog.wind_name = result.get("wind_name") or row.industry_name
                catalog.wind_mapping_status = "mapped"
                catalog.wind_mapping_error = None
                catalog.wind_mapping_at = datetime.utcnow()
                catalog.updated_at = datetime.utcnow()
                session.add(catalog)
            verified += 1
        except WindSkillAuthError as exc:
            # Authentication belongs to the user's local Skill installation; do
            # not turn it into a misleading per-industry "validation failed".
            row.wind_status = "not_configured"
            row.wind_error = str(exc)[:500]
            auth_failed = True
            for pending in selected[index + 1:]:
                pending.wind_status = "not_configured"
                pending.wind_error = str(exc)[:500]
                pending.mainline_score = _mainline_score(pending)
                pending.mainline_state = _mainline_state(pending)
                pending.updated_at = datetime.utcnow()
                session.add(pending)
        except WindSkillMappingError as exc:
            row.wind_status = "unavailable"
            row.wind_error = str(exc)[:500]
            if catalog is not None:
                catalog.wind_mapping_status = "unmapped"
                catalog.wind_mapping_error = str(exc)[:500]
                catalog.wind_mapping_at = datetime.utcnow()
                catalog.updated_at = datetime.utcnow()
                session.add(catalog)
        except (IndustryWindError, ValueError, TypeError) as exc:
            row.wind_status = "unavailable"
            row.wind_error = str(exc)[:500]
        row.mainline_score = _mainline_score(row)
        row.mainline_state = _mainline_state(row)
        row.updated_at = datetime.utcnow()
        session.add(row)
        if auth_failed:
            break
    session.flush()
    status = (
        "not_configured" if auth_failed and verified == 0
        else "ready" if verified == len(selected)
        else "partial" if verified else "unavailable"
    )
    return {"status": status, "requested": len(selected), "verified": verified}


def refresh(session: Session, dataset_id: str, *, use_wind: bool = True,
            top_n: int | None = None,
            client_factory: Callable[[], WindSectorClient | WindSkillCliClient] = default_client) -> dict:
    rows = persist(session, calculate(session, dataset_id))
    wind = (apply_wind_validation(session, rows, top_n=top_n, client_factory=client_factory)
            if use_wind else {"status": "not_requested", "requested": 0, "verified": 0})
    for row in rows:
        row.mainline_score = _mainline_score(row)
        row.mainline_state = _mainline_state(row)
        session.add(row)
    session.commit()
    return {"rows": len(rows), "wind": wind}


def _headline(rows: list[IndustryHeatSnapshot]) -> dict:
    confirmed = [row for row in rows if row.mainline_state == "confirmed_mainline"]
    candidates = [row for row in rows if row.trend_state == "leading"]
    primary = (confirmed or candidates or rows)[:3]
    if not rows:
        return {"title": "尚无行业数据", "summary": "等待趋势动物全行业快照。", "confidence": "none"}
    if confirmed:
        title = " · ".join(row.industry_name for row in primary)
        summary = "趋势动物热度领先，且 Wind 量价、广度或资金证据达到验证门槛。"
        confidence = "confirmed"
    elif candidates:
        title = "候选主线：" + " · ".join(row.industry_name for row in primary)
        summary = "原生趋势信号领先，但 Wind 验证尚不足，暂不视为全市场扩散。"
        confidence = "candidate"
    else:
        title = "仍未形成清晰主线"
        summary = "行业强弱尚未形成同时满足温度、强度与持续性的领先层。"
        confidence = "weak"
    return {"title": title, "summary": summary, "confidence": confidence}


def report(session: Session, *, trade_date: str | None = None, limit: int = 100) -> dict:
    query = select(DailyDataset).where(DailyDataset.status.in_(("ready", "ready_degraded")))
    if trade_date:
        query = query.where(DailyDataset.trade_date == trade_date)
    dataset = session.exec(query.order_by(DailyDataset.trade_date.desc())).first()
    if dataset is None:
        return {"trade_date": trade_date, "headline": _headline([]), "rows": [], "coverage": {}}
    rows = session.exec(select(IndustryHeatSnapshot).where(
        IndustryHeatSnapshot.dataset_id == dataset.dataset_id
    ).order_by(IndustryHeatSnapshot.mainline_score.desc(), IndustryHeatSnapshot.trend_rank).limit(
        min(max(limit, 1), 100))).all()
    if not rows:
        rows = calculate(session, dataset.dataset_id)[:limit]
    serialized = [row.model_dump() for row in rows]
    return {
        "trade_date": dataset.trade_date, "dataset_id": dataset.dataset_id,
        "headline": _headline(rows), "rows": serialized,
        "coverage": {
            "industry_count": len(rows),
            "trend_complete": sum(row.temperature_curr is not None and row.strength_curr is not None for row in rows),
            "wind_requested": sum(row.wind_status != "not_requested" for row in rows),
            "wind_verified": sum(row.wind_status in {"verified", "partial"} for row in rows),
        },
        "methodology": {
            "trend_score": "温度32% + 强度33% + 5日强度斜率15% + 连续升温/热度持续10% + 温转热个股数10%",
            "mainline_score": "Wind有证据时最多占30%；无Wind时保持趋势动物得分并标记待验证",
            "wind_source": (
                "本机 wind-mcp-skill CLI"
                if config.WIND_VALIDATION_MODE == "skill_cli"
                else "服务端 Wind MCP 直连"
                if config.WIND_VALIDATION_MODE == "direct"
                else "已关闭"
            ),
            "wind_fields": [
                "5日涨跌幅", "20日涨跌幅", "成交额", "量比", "上涨家数", "下跌家数",
                "平盘家数", "当日主力净流入额", "当日主力净流入占比",
            ],
        },
    }
