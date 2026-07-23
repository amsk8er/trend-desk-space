"""券商 OCR 持仓的人工确认闸门与正式连续持仓台账。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlmodel import Session, select

from backend.db import (
    Batch, DailyDataset, PortfolioSnapshot, Position, PositionLot,
    TrendDailyMembership, TrendDailySnapshot,
)

READY_STATUSES = ("ready", "ready_degraded")


def _normal_name(value: str) -> str:
    return "".join(str(value or "").upper().split())


def _bare_code(value: str | None) -> str:
    return str(value or "").strip().upper().split(".", 1)[0]


def _account_snapshot_hash(*, trade_date: str, nav: float, cash: float,
                           positions: list[dict]) -> str:
    material = {
        "trade_date": trade_date,
        "nav": round(nav, 4),
        "cash": round(cash, 4),
        "positions": sorted(positions, key=lambda row: row["code"]),
    }
    return hashlib.sha256(json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


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
    all_positions = s.exec(select(Position).where(Position.batch_id == batch_id)).all()
    positions = all_positions
    if position_ids is not None:
        wanted = set(position_ids)
        positions = [p for p in positions if p.position_id in wanted]
        if len(positions) != len(all_positions):
            raise ValueError("partial_account_snapshot_not_allowed")
    if not positions:
        raise ValueError("no_positions_to_confirm")
    try:
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
        if any(p.shares < 0 for p in positions):
            raise ValueError("invalid_position_shares")

        # A broker position screenshot is a full account snapshot, not a new lot
        # event.  Aggregate duplicate OCR rows by instrument before reconciling.
        account_rows: dict[str, dict] = {}
        market_value = 0.0
        for p in positions:
            key = _bare_code(p.code)
            row = account_rows.setdefault(key, {
                "code": str(p.code), "name": p.name, "shares": 0,
                "avg_cost_numerator": 0.0, "priced_shares": 0,
                "entered_on": p.entered_date,
            })
            row["shares"] += int(p.shares)
            if p.avg_cost > 0 and p.shares > 0:
                row["avg_cost_numerator"] += float(p.avg_cost) * int(p.shares)
                row["priced_shares"] += int(p.shares)
            market_value += int(p.shares) * max(0.0, float(p.current_price))

        resolved_cash = float(cash or 0.0)
        resolved_nav = float(nav if nav is not None else market_value + resolved_cash)
        if nav is not None or cash is not None:
            if resolved_nav <= 0 or resolved_cash < 0 or resolved_cash > resolved_nav:
                raise ValueError("invalid_account_totals")
        now = datetime.utcnow()

        all_lots = s.exec(select(PositionLot).order_by(
            PositionLot.opened_on, PositionLot.lot_id)).all()
        ocr_by_code: dict[str, list[PositionLot]] = {}
        formal_shares: dict[str, int] = {}
        preserved_formal = 0
        for lot in all_lots:
            key = _bare_code(lot.instrument_id)
            if lot.source == "broker_ocr_confirmed":
                ocr_by_code.setdefault(key, []).append(lot)
            elif lot.remaining_shares > 0:
                formal_shares[key] = formal_shares.get(key, 0) + lot.remaining_shares
                preserved_formal += 1

        conflicts = sorted(
            row["code"] for key, row in account_rows.items()
            if formal_shares.get(key, 0) > row["shares"]
        )
        if conflicts:
            raise ValueError("account_snapshot_below_formal_lots:" + ",".join(conflicts))

        created = 0
        closed = 0
        consolidated = 0
        for key, row in account_rows.items():
            residual = row["shares"] - formal_shares.get(key, 0)
            ocr_lots = ocr_by_code.get(key, [])
            canonical = ocr_lots[0] if ocr_lots else None
            if residual > 0 and canonical is None:
                bare = _bare_code(row["code"])
                canonical = PositionLot(
                    instrument_id=row["code"], name=row["name"],
                    asset_type="etf" if bare.startswith(("15", "16", "51", "55", "56", "58")) else "stock",
                    opened_on=row["entered_on"] or signal_date,
                    initial_shares=residual, remaining_shares=residual,
                    avg_cost=(row["avg_cost_numerator"] / row["priced_shares"]
                              if row["priced_shares"] else 0.0),
                    source="broker_ocr_confirmed", as_of_date=signal_date,
                )
                s.add(canonical)
                created += 1
            if canonical is not None:
                if residual == 0 and canonical.remaining_shares > 0:
                    closed += 1
                canonical.name = row["name"]
                canonical.initial_shares = residual
                canonical.remaining_shares = residual
                if row["priced_shares"]:
                    canonical.avg_cost = row["avg_cost_numerator"] / row["priced_shares"]
                canonical.as_of_date = signal_date
                canonical.synced_at = now
                s.add(canonical)
            for duplicate in ocr_lots[1:]:
                if duplicate.remaining_shares > 0:
                    closed += 1
                duplicate.remaining_shares = 0
                duplicate.as_of_date = signal_date
                duplicate.synced_at = now
                s.add(duplicate)
                consolidated += 1

        # Holdings absent from this full snapshot are closed only in the OCR
        # baseline.  Execution/import/adjustment lots remain untouched.
        for key, ocr_lots in ocr_by_code.items():
            if key in account_rows:
                continue
            for lot in ocr_lots:
                if lot.remaining_shares > 0:
                    closed += 1
                lot.remaining_shares = 0
                lot.as_of_date = signal_date
                lot.synced_at = now
                s.add(lot)

        for p in positions:
            p.confirmed = True
            p.confirmed_at = now
            s.add(p)
        s.flush()

        out = {
            "batch_id": batch_id,
            "confirmed": len(positions),
            "position_lots_created": created,
            "position_lots_closed": closed,
            "position_lots_consolidated": consolidated,
            "formal_lots_preserved": preserved_formal,
            "confirmed_at": now.isoformat(),
            "batch_trade_date": batch.date,
            "signal_trade_date": signal_date,
            "plan": None,
            "message": None,
        }
        if nav is not None or cash is not None:
            position_material = [
                {"code": row["code"], "shares": row["shares"]}
                for row in account_rows.values()
            ]
            snapshot_hash = _account_snapshot_hash(
                trade_date=signal_date, nav=resolved_nav, cash=resolved_cash,
                positions=position_material,
            )
            snapshots = s.exec(select(PortfolioSnapshot).where(
                PortfolioSnapshot.trade_date == signal_date,
                PortfolioSnapshot.source == "broker_ocr",
            ).order_by(PortfolioSnapshot.synced_at.desc())).all()
            snapshot = next((row for row in snapshots
                             if (row.derivation or {}).get("account_snapshot_hash") == snapshot_hash), None)
            if snapshot is None:
                # Safe retry for a pre-fix partial commit: the legacy row has no
                # hash, but its exact account totals identify the same snapshot.
                snapshot = next((row for row in snapshots
                                 if not (row.derivation or {}).get("account_snapshot_hash")
                                 and abs(row.nav - resolved_nav) <= 0.005
                                 and abs(row.cash - resolved_cash) <= 0.005
                                 and abs(row.market_value - market_value) <= 0.005), None)
            if snapshot is None:
                snapshot = PortfolioSnapshot(
                    trade_date=signal_date, nav=resolved_nav, cash=resolved_cash,
                    market_value=market_value, source="broker_ocr", confirmed=True,
                    as_of_date=signal_date,
                )
                s.add(snapshot)
                s.flush()
            snapshot.derivation = {
                **(snapshot.derivation or {}),
                "account_snapshot_hash": snapshot_hash,
                "batch_id": batch_id,
                "positions": sorted(position_material, key=lambda row: row["code"]),
            }
            s.add(snapshot)
            s.flush()
            out["portfolio_snapshot_id"] = snapshot.snapshot_id
            if dataset is not None:
                from backend.discipline.dataset_plan import ensure_executable_plan
                out["plan"] = ensure_executable_plan(
                    s, dataset_id=dataset.dataset_id,
                    portfolio_snapshot_id=snapshot.snapshot_id, commit=False)
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
        s.commit()
        return out
    except Exception:
        s.rollback()
        raise
