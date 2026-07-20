"""持仓出局检查（《我的纪律》v1.1，2026-07-12）。

唯一动作优先级：危险/转平及以下全清 → 当日止盈信号按同一基数 25%×N → 持有。
固定止损、强度下降、节气和主观判断不再生成独立交易动作。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, delete, select

from backend.db import ExitListItem, HoldingTemp, Position
from backend.discipline.exits import decide_exit
from backend.ocr.review import clean_approved_rows
from backend.pipeline.nodes.holding_temp import norm_code, norm_name
from backend.pipeline.state import NodeStatus, transition

# 兼容趋势事实层的只读分类；动作仍统一由 discipline.exits 决定。
EXIT_STATUSES = ("平", "凉", "寒", "冻")


@dataclass
class TempView:
    status: str | None = None
    tags: list = field(default_factory=list)
    sector: str | None = None
    jieqi: str | None = None
    right_side_days: int | None = None
    right_side_gain_pct: float | None = None
    raw_fields: dict = field(default_factory=dict)
    source: str | None = None


def _resolve_temp(p: Position, holdings_view: dict, rows_by_code: dict) -> TempView:
    h = holdings_view["by_code"].get(norm_code(p.code)) if p.code else None
    if h is None:
        h = holdings_view["by_name"].get(norm_name(p.name))
    if h is not None:
        return TempView(
            status=h.temperature_status, tags=(h.raw_fields or {}).get("tags") or [],
            sector=h.sector, jieqi=h.jieqi, right_side_days=h.right_side_days,
            right_side_gain_pct=h.right_side_gain_pct, raw_fields=h.raw_fields or {},
            source="trend_api" if h.data_source == "trend_api" else "holding_temp",
        )
    row = rows_by_code.get(norm_code(p.code)) if p.code else None
    if row is None:
        return TempView()
    rf = row.raw_fields or {}
    return TempView(
        status=row.temperature_status, tags=rf.get("tags") or [], sector=row.sector,
        jieqi=row.jieqi, right_side_days=row.right_side_days,
        right_side_gain_pct=row.right_side_gain_pct, raw_fields=rf, source="ocr_row",
    )


def _indexes(s: Session, batch_id: str) -> tuple[dict, dict]:
    rows = clean_approved_rows(s, batch_id=batch_id)
    rows_by_code = {}
    for row in rows:
        if row.code:
            rows_by_code.setdefault(norm_code(row.code), row)
    view = {"by_code": {}, "by_name": {}}
    for h in s.exec(select(HoldingTemp).where(HoldingTemp.batch_id == batch_id)).all():
        if h.code:
            view["by_code"].setdefault(norm_code(h.code), h)
        if h.name:
            view["by_name"].setdefault(norm_name(h.name), h)
    return rows_by_code, view


def _signal(tv: TempView) -> dict:
    rf = tv.raw_fields or {}
    flags = rf.get("api_flags") or {}
    return {
        "temperature_curr": tv.status,
        "danger": ("危险信号" in tv.tags or flags.get("danger") is True),
        "champagne": ("开香槟" in tv.tags or flags.get("champagne") is True),
        "boiling": (tv.status == "沸" or "沸" in tv.tags or flags.get("boiling") is True),
        # 波动率放大已融入「沸」，不再读取标签/API，避免重复计入止盈层数。
        "volatility_up": None,
    }


def _decision_for(p: Position, tv: TempView):
    return decide_exit(shares=p.shares, signal=_signal(tv))


def build_exit_overview(s: Session, *, batch_id: str) -> list[dict]:
    rows_by_code, holdings_view = _indexes(s, batch_id)
    overview = []
    for p in s.exec(select(Position).where(Position.batch_id == batch_id)).all():
        tv = _resolve_temp(p, holdings_view, rows_by_code)
        if tv.source is None:
            action, target, fraction = "数据不足·无法判断", 0, 0.0
            unavailable = ["temperature", "danger", "champagne", "boiling"]
        else:
            decision = _decision_for(p, tv)
            labels = {"sell_all": "全部清仓", "reduce": "分批止盈", "manual_review": "不足整手·人工确认", "hold": "继续持有"}
            action, target, fraction = labels[decision.action], decision.target_shares, decision.reduce_fraction
            # 过滤已下线的 volatility 未覆盖提示
            unavailable = [
                x for x in ((tv.raw_fields or {}).get("signal_unavailable") or [])
                if x not in {"volatility", "volatility_up"}
            ]
        overview.append({
            "position_id": p.position_id, "code": p.code, "name": p.name,
            "temperature_status": tv.status, "temp_source": tv.source,
            "right_side_days": tv.right_side_days, "right_side_gain_pct": tv.right_side_gain_pct,
            "jieqi": tv.jieqi, "pnl_pct": p.pnl_pct, "shares": p.shares, "tags": tv.tags,
            "signal_unavailable": unavailable, "suggest": action,
            "target_shares": target, "reduce_fraction": fraction,
        })
    return overview


def run_exit_check(s: Session, *, batch_id: str) -> dict:
    transition(s, batch_id=batch_id, node="exit_check", to=NodeStatus.RUNNING)
    s.exec(delete(ExitListItem).where(ExitListItem.batch_id == batch_id))
    rows_by_code, holdings_view = _indexes(s, batch_id)
    items = []

    def emit(p: Position, trigger: str, action: str, reason: str, detail: dict):
        s.add(ExitListItem(batch_id=batch_id, position_id=p.position_id,
                           trigger=trigger, action=action, reason=reason, detail=detail))
        items.append({"code": p.code, "name": p.name, "trigger": trigger,
                      "action": action, "reason": reason, "detail": detail})

    for p in s.exec(select(Position).where(Position.batch_id == batch_id)).all():
        tv = _resolve_temp(p, holdings_view, rows_by_code)
        base = {
            "name": p.name, "temperature_status": tv.status,
            "right_side_days": tv.right_side_days, "avg_cost": p.avg_cost,
            "current_price": p.current_price, "pnl_pct": p.pnl_pct,
            "sector": tv.sector, "temp_source": tv.source,
            "rules_version": "v1.1",
        }
        if tv.source is None:
            emit(p, "data_incomplete", "数据不足·无法判断",
                 f"{p.name} 缺少趋势温度与离场信号，不能把缺失解释为继续持有",
                 {**base, "signal_unavailable": ["temperature", "danger", "champagne", "boiling", "volatility"]})
            continue
        signal = _signal(tv)
        decision = decide_exit(shares=p.shares, signal=signal)
        detail = {**base, **decision.as_dict(), "signal": signal}
        if decision.action == "sell_all":
            reason = ("危险信号" if signal["danger"] else f"温度转{tv.status}") + " → 次日竞价全部清仓；全清短路止盈"
            emit(p, "full_exit", f"全部清仓({decision.target_shares}股)", reason, detail)
        elif decision.action in {"reduce", "manual_review"}:
            pct = int(decision.reduce_fraction * 100)
            action = (f"减仓{pct}%({decision.target_shares}股)" if decision.target_shares
                      else f"减仓{pct}%·不足整手人工确认")
            emit(p, "profit_take", action,
                 f"当日 {decision.profit_signal_count} 个有效止盈信号，按执行前同一基数减 {pct}%",
                 detail)
        # hold 只在 overview 显式展示，不制造提醒行。

    s.commit()
    overview = build_exit_overview(s, batch_id=batch_id)
    transition(s, batch_id=batch_id, node="exit_check", to=NodeStatus.DONE)
    return {"items": items, "overview": overview}
