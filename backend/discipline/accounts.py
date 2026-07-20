"""券商 OCR 持仓的人工确认闸门与正式连续持仓台账。"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from backend.db import (
    Batch, DailyDataset, PortfolioSnapshot, Position, PositionLot,
    TrendDailyMembership, TrendDailySnapshot,
)

READY_STATUSES = ("ready", "ready_degraded")


def _normal_name(value: str) -> str:
    return "".join(str(value or "").upper().split())


def resolve_ready_dataset(s: Session, trade_date: str) -> DailyDataset | None:
    """优先精确交易日；跨午夜后今日尚未采集时，回退到最近一个就绪数据集。

    盘后确认账户常发生在日历日已切到次日、但次日数据集仍 pending 的窗口。
    可执行计划必须以 ready 的信号数据集为锚，否则会静默跳过生成。
    """
    exact = s.exec(select(DailyDataset).where(
        DailyDataset.trade_date == trade_date,
        DailyDataset.status.in_(list(READY_STATUSES)),
    )).first()
    if exact is not None:
        return exact
    return s.exec(select(DailyDataset).where(
        DailyDataset.status.in_(list(READY_STATUSES)),
        DailyDataset.trade_date <= trade_date,
    ).order_by(DailyDataset.trade_date.desc())).first()


def confirm_ocr_positions(s: Session, *, batch_id: str,
                          position_ids: list[int] | None = None,
                          nav: float | None = None, cash: float | None = None) -> dict:
    batch = s.get(Batch, batch_id)
    if batch is None:
        raise KeyError(batch_id)
    positions = s.exec(select(Position).where(Position.batch_id == batch_id)).all()
    if position_ids is not None:
        wanted = set(position_ids)
        positions = [p for p in positions if p.position_id in wanted]
    if not positions:
        raise ValueError("no_positions_to_confirm")
    # Broker screenshots often have no instrument code.  The confirmed daily API
    # holding membership is the authority for matching names to codes.
    dataset = resolve_ready_dataset(s, batch.date)
    # 台账 / 计划锚定到就绪信号日（可能早于 batch.date）
    signal_date = dataset.trade_date if dataset is not None else batch.date
    if dataset is not None:
        holding_ids = set(s.exec(select(TrendDailyMembership.tm_id).where(
            TrendDailyMembership.dataset_id == dataset.dataset_id,
            TrendDailyMembership.membership_type == "holding")).all())
        holding_rows = s.exec(select(TrendDailySnapshot).where(
            TrendDailySnapshot.dataset_id == dataset.dataset_id)).all()
        code_by_name = {_normal_name(row.name): row.code for row in holding_rows
                        if row.tm_id in holding_ids and row.code}
        for position in positions:
            if not position.code and _normal_name(position.name) in code_by_name:
                position.code = code_by_name[_normal_name(position.name)]
                position.code_source = "trend_api"
                s.add(position)
    missing = [p.name for p in positions if not p.code]
    if missing:
        raise ValueError("unmapped_positions:" + ",".join(missing))
    market_value = sum(max(0, p.shares) * max(0.0, p.current_price) for p in positions)
    resolved_cash = float(cash or 0.0)
    resolved_nav = float(nav if nav is not None else market_value + resolved_cash)
    if nav is not None or cash is not None:
        if resolved_nav <= 0 or resolved_cash < 0 or resolved_cash > resolved_nav:
            raise ValueError("invalid_account_totals")
    now = datetime.utcnow()
    created = 0
    for p in positions:
        p.confirmed = True
        p.confirmed_at = now
        s.add(p)
        existing = s.exec(select(PositionLot).where(
            PositionLot.instrument_id == p.code,
            PositionLot.as_of_date == signal_date,
            PositionLot.source == "broker_ocr_confirmed",
        )).first()
        if existing is None:
            bare = str(p.code).split(".")[0]
            s.add(PositionLot(
                instrument_id=p.code, name=p.name,
                asset_type="etf" if bare.startswith(("15", "51", "56", "58")) else "stock",
                opened_on=p.entered_date or signal_date, initial_shares=p.shares,
                remaining_shares=p.shares, avg_cost=p.avg_cost,
                source="broker_ocr_confirmed", as_of_date=signal_date,
            ))
            created += 1
        else:
            existing.name = p.name
            existing.remaining_shares = p.shares
            existing.initial_shares = p.shares
            existing.avg_cost = p.avg_cost
            existing.synced_at = now
            s.add(existing)
    s.commit()
    out = {
        "batch_id": batch_id,
        "confirmed": len(positions),
        "position_lots_created": created,
        "confirmed_at": now.isoformat(),
        "batch_trade_date": batch.date,
        "signal_trade_date": signal_date,
        "plan": None,
        "message": None,
    }
    if nav is not None or cash is not None:
        snapshot = PortfolioSnapshot(
            trade_date=signal_date, nav=resolved_nav, cash=resolved_cash,
            market_value=market_value, source="broker_ocr", confirmed=True,
            as_of_date=signal_date,
        )
        s.add(snapshot); s.commit(); s.refresh(snapshot)
        out["portfolio_snapshot_id"] = snapshot.snapshot_id
        if dataset is not None:
            from backend.discipline.dataset_plan import ensure_executable_plan
            out["plan"] = ensure_executable_plan(
                s, dataset_id=dataset.dataset_id, portfolio_snapshot_id=snapshot.snapshot_id)
            if signal_date != batch.date:
                out["message"] = (
                    f"持仓已确认；今日({batch.date})数据集尚未就绪，"
                    f"已按最近就绪交易日 {signal_date} 生成可执行计划。"
                )
            else:
                out["message"] = f"持仓已确认，可执行计划已生成（{signal_date}）。"
        else:
            out["message"] = (
                f"持仓已确认，但没有就绪的每日数据集，无法生成可执行计划。"
                f"请先在「今日」页完成 {batch.date} 或更早交易日的数据采集。"
            )
    else:
        out["message"] = f"持仓已确认 {len(positions)} 项（未提交净值/现金，未生成账户计划）。"
    return out
