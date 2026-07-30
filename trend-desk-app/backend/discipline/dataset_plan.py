"""每日事实包 → 信号草稿 → 券商确认后的可执行草稿。"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from uuid import uuid4

from sqlmodel import Session, select

from backend.db import (
    DailyDataset, DailyExitSignal, FeeSchedule, PortfolioSnapshot, PositionLot, SignalSnapshot,
    TradePlan, TradePlanItem, TrendDailyMembership, TrendDailySnapshot,
    TushareDailyFact, VolatilitySupplement,
)
from backend.discipline.exits import decide_exit
from backend.discipline.data_sources import normalize_etf_benchmark
from backend.discipline.plan import ensure_active_version, generate_plan, serialize_plan
from backend.discipline.rules import RULES, RULES_HASH
from backend.discipline.selection import evaluate_candidate_pool


def _hash(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _portfolio_plan_material(portfolio: PortfolioSnapshot) -> dict:
    """Only account facts that can change a plan belong in its input hash.

    Roll-forward adds audit metadata to the same authoritative broker snapshot
    (price_date, reconciliation status, derivation and sync time).  Those fields
    must not supersede an otherwise identical executable plan.
    """
    return {
        "snapshot_id": portfolio.snapshot_id,
        "trade_date": portfolio.trade_date,
        "nav": portfolio.nav,
        "cash": portfolio.cash,
        "market_value": portfolio.market_value,
        "source": portfolio.source,
        "confirmed": portfolio.confirmed,
        "as_of_date": portfolio.as_of_date,
    }


def _fee_plan_material(fee: FeeSchedule | None) -> dict:
    if fee is None:
        return {"configured": False}
    return {
        "configured": fee.configured,
        "commission_rate": fee.commission_rate,
        "minimum_commission": fee.minimum_commission,
        "etf_commission_rate": fee.etf_commission_rate,
        "etf_minimum_commission": fee.etf_minimum_commission,
        "transfer_fee_rate": fee.transfer_fee_rate,
        "stamp_duty_rate": fee.stamp_duty_rate,
        "safety_multiplier": fee.safety_multiplier,
    }


def _bare(code: str | None) -> str:
    return str(code or "").upper().split(".")[0]


def _asset_type(code: str | None, asset: str | None) -> str:
    return "etf" if asset == "ETF基金" or _bare(code).startswith(("15", "16", "51", "55", "56", "58")) else "stock"


def _fallback_next_day(trade_date: str) -> str:
    current = date.fromisoformat(trade_date) + timedelta(days=1)
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current.isoformat()


def dataset_context(s: Session, dataset_id: str) -> dict:
    dataset = s.get(DailyDataset, dataset_id)
    if dataset is None:
        raise KeyError(dataset_id)
    snapshots = s.exec(select(TrendDailySnapshot).where(
        TrendDailySnapshot.dataset_id == dataset_id)).all()
    memberships = s.exec(select(TrendDailyMembership).where(
        TrendDailyMembership.dataset_id == dataset_id)).all()
    facts = s.exec(select(TushareDailyFact).where(
        TushareDailyFact.dataset_id == dataset_id)).all()
    supplements = s.exec(select(VolatilitySupplement).where(
        VolatilitySupplement.dataset_id == dataset_id)).all()
    snap_by_tm = {row.tm_id: row for row in snapshots}
    fact_by_bare = {_bare(row.ts_code): row for row in facts}
    volatility_by_bare = {_bare(row.instrument_id): row.volatility_up for row in supplements}
    members: dict[str, list[int]] = {}
    for row in memberships:
        members.setdefault(row.membership_type, []).append(row.tm_id)

    market = snap_by_tm.get((members.get("market") or [None])[0])
    candidates: list[dict] = []
    for membership_type in ("warm_to_hot_stock", "warm_to_hot_etf"):
        for tm_id in members.get(membership_type, []):
            row = snap_by_tm.get(tm_id)
            if row is None or not row.code:
                continue
            fact = fact_by_bare.get(_bare(row.code))
            sector = snap_by_tm.get(row.industry_tm_id) if row.industry_tm_id else None
            asset_type = "etf" if membership_type == "warm_to_hot_etf" else "stock"
            candidates.append({
                "code": row.code, "name": row.name, "asset_type": asset_type,
                "data_source": "trend_api", "permission": True,
                "temperature_prev": row.temperature_prev,
                "temperature_curr": row.temperature_curr,
                "phase": row.phase,
                "sector": row.industry_name,
                "sector_temperature": sector.temperature_curr if sector else None,
                "float_market_cap_yi": fact.float_market_cap_yi if fact else None,
                "aum_yi": fact.fund_size_yi if fact else None,
                "amount_yi": fact.amount_yi if fact else None,
                "right_side_days": row.right_side_days, "strength": row.strength,
                "strength_change": row.strength_change,
                "price": fact.close if fact else None,
                "benchmark": ((fact.raw_payload or {}).get("fund_basic") or {}).get("benchmark")
                             if fact else None,
                "benchmark_key": normalize_etf_benchmark(
                    ((fact.raw_payload or {}).get("fund_basic") or {}).get("benchmark")
                    if fact else None
                ),
            })

    pool = evaluate_candidate_pool(candidates)
    eligible = pool["ranked_eligible"]
    duplicate_etfs = pool["duplicate_etfs"]
    market_temperature = market.temperature_curr if market else None
    environment_factor = float(
        RULES["capacity"]["environment_factors"].get(market_temperature, 0.0)
    )
    if environment_factor > 0:
        white_list, watch_list = eligible, duplicate_etfs
    else:
        white_list = []
        watch_list = ([{**row, "capacity_reason": "environment_factor_zero"} for row in eligible]
                      + duplicate_etfs)
    selection = {"white_list": white_list, "watch_list": watch_list,
                 "shadow_pool": pool["shadow_pool"], "rejected": pool["rejected"], "capacity": {
                     "status": "waiting_account", "market_temperature": market_temperature,
                     "environment_factor": environment_factor,
                 }}

    signals: list[dict] = []
    for tm_id in members.get("holding", []):
        row = snap_by_tm.get(tm_id)
        if row is None or not row.code:
            continue
        api_volatility = bool((dataset.capability_flags or {}).get("volatility_supported"))
        volatility_up = (row.volatility_up if api_volatility and row.volatility_up is not None
                         else volatility_by_bare.get(_bare(row.code), row.volatility_up))
        signals.append({
            "code": row.code, "name": row.name,
            "asset_type": _asset_type(row.code, row.asset),
            "temperature_prev": row.temperature_prev,
            "temperature_curr": row.temperature_curr,
            "strength": row.strength, "strength_change": row.strength_change,
            "right_side_days": row.right_side_days,
            "phase": row.phase, "danger": row.danger, "boiling": row.boiling,
            "champagne": row.champagne,
            "volatility_up": volatility_up,
            "price": fact_by_bare.get(_bare(row.code)).close
                     if fact_by_bare.get(_bare(row.code)) else None,
        })
    next_day = ((dataset.source_status or {}).get("tushare") or {}).get("next_trade_date")
    return {"dataset": dataset, "market": market, "selection": selection,
            "candidates": candidates, "signals": signals,
            "execute_date": next_day or _fallback_next_day(dataset.trade_date),
            "supplements": [row.model_dump() for row in supplements]}


def _latest_plan(s: Session, dataset_id: str, *, stage: str | None = None) -> TradePlan | None:
    query = select(TradePlan).where(TradePlan.dataset_id == dataset_id)
    if stage:
        query = query.where(TradePlan.plan_stage == stage)
    return s.exec(query.order_by(TradePlan.created_at.desc())).first()


def ensure_signal_plan(s: Session, dataset_id: str) -> dict:
    ctx = dataset_context(s, dataset_id)
    dataset: DailyDataset = ctx["dataset"]
    if dataset.status not in {"ready", "ready_degraded"}:
        raise ValueError("dataset_not_ready")
    version = ensure_active_version(s)
    input_hash = _hash({"dataset": dataset.dataset_hash, "supplements": ctx["supplements"],
                        "rules": RULES_HASH, "stage": "signal"})
    existing = s.exec(select(TradePlan).where(TradePlan.input_hash == input_hash)).first()
    if existing is not None:
        return serialize_plan(s, existing.plan_id)
    previous = _latest_plan(s, dataset_id)
    plan_id = f"plan_{dataset.trade_date.replace('-', '')}_signal_{uuid4().hex[:8]}"
    warnings: list[str] = []
    market_temperature = ctx["market"].temperature_curr if ctx["market"] else None
    environment_factor = float(
        RULES["capacity"]["environment_factors"].get(market_temperature, 0.0)
    )
    source_dates = {"signal": dataset.trade_date, "trend_animals": dataset.trade_date,
                    "tushare": dataset.trade_date, "account": None}
    plan = TradePlan(
        plan_id=plan_id, signal_date=dataset.trade_date, execute_date=ctx["execute_date"],
        discipline_version=version.version, rules_hash=version.rules_hash,
        status="awaiting_account", dataset_id=dataset_id, plan_stage="signal",
        input_hash=input_hash, supersedes_plan_id=previous.plan_id if previous else None,
        market_mode="pending_account", environment_factor=environment_factor,
        capacity_snapshot={
            "status": "waiting_account",
            "market_temperature": market_temperature,
            "base_new_position_pct": RULES["capacity"]["base_new_position_pct"],
            "environment_factor": environment_factor,
            "per_position_weight": (
                RULES["capacity"]["base_new_position_pct"] * environment_factor
            ),
            "opening_allowed": environment_factor > 0,
        },
        data_health={"lockable": False, "errors": ["account_not_confirmed"],
                     "warnings": warnings,
                     "source_modes": {"trend": dataset.source_mode, "market": "tushare",
                                      "account": "waiting"}},
        selection_snapshot=ctx["selection"],
    )
    s.add(plan); s.flush()
    for signal in ctx["signals"]:
        decision = decide_exit(shares=10_000, signal=signal).as_dict()
        evidence = dict(decision["evidence"]); evidence["signal_only"] = True
        s.add(TradePlanItem(
            plan_id=plan_id, instrument_id=signal["code"], name=signal["name"],
            asset_type=signal["asset_type"], side=decision["action"],
            target_shares=None, reduce_fraction=decision["reduce_fraction"],
            priority=decision["priority"], rule_evidence=evidence,
            source_dates=source_dates,
            data_sources={"trend": dataset.source_mode, "market": "tushare", "account": "waiting"},
        ))
        existing_signal = s.exec(select(SignalSnapshot).where(
            SignalSnapshot.instrument_id == signal["code"],
            SignalSnapshot.as_of_date == dataset.trade_date)).first()
        values = {"temperature_prev": signal.get("temperature_prev"),
                  "temperature_curr": signal.get("temperature_curr"),
                  "strength": signal.get("strength"), "right_side_days": signal.get("right_side_days"),
                  "phase": signal.get("phase"), "danger": signal.get("danger"),
                  "boiling": signal.get("boiling"), "champagne": signal.get("champagne"),
                  "volatility_up": signal.get("volatility_up"), "source": dataset.source_mode,
                  "raw_payload_hash": dataset.dataset_hash}
        if existing_signal is None:
            s.add(SignalSnapshot(instrument_id=signal["code"], as_of_date=dataset.trade_date, **values))
        else:
            for key, value in values.items():
                setattr(existing_signal, key, value)
            s.add(existing_signal)
        s.add(DailyExitSignal(
            plan_id=plan_id, instrument_id=signal["code"], signal_date=dataset.trade_date,
            execute_date=ctx["execute_date"], danger=bool(signal.get("danger")),
            temp_flat_or_below=signal.get("temperature_curr") in {"平", "凉", "寒", "冻"},
            champagne=signal.get("champagne"), boiling=signal.get("boiling"),
            volatility_up=signal.get("volatility_up"),
            profit_signal_count=decision["profit_signal_count"],
            planned_reduce_fraction=decision["reduce_fraction"],
            consecutive_days_by_signal=decision["consecutive_days_by_signal"],
            action_generated=decision["action"], target_shares=0,
            valid_until=ctx["execute_date"],
        ))
    for candidate in ctx["selection"]["white_list"]:
        s.add(TradePlanItem(
            plan_id=plan_id, instrument_id=candidate["code"], name=candidate.get("name") or candidate["code"],
            asset_type=candidate["asset_type"], side="buy", target_shares=None,
            target_weight=None, priority=3,
            rule_evidence={"checks": candidate["evidence"], "signal_only": True},
            source_dates=source_dates,
            data_sources={"trend": dataset.source_mode, "market": "tushare", "account": "waiting"},
        ))
    s.commit()
    return serialize_plan(s, plan_id)


def ensure_executable_plan(s: Session, *, dataset_id: str, portfolio_snapshot_id: int,
                           change_notice: str | None = None,
                           commit: bool = True) -> dict:
    ctx = dataset_context(s, dataset_id)
    dataset: DailyDataset = ctx["dataset"]
    portfolio = s.get(PortfolioSnapshot, portfolio_snapshot_id)
    if portfolio is None or not portfolio.confirmed:
        raise ValueError("account_not_confirmed")
    # Position lots are a durable ledger, not a daily OCR snapshot.  The current
    # open lots remain authoritative until confirmed executions/adjustments
    # change them; an all-cash account is also a valid executable account.
    lots = s.exec(select(PositionLot).where(
        PositionLot.remaining_shares > 0).order_by(
            PositionLot.instrument_id, PositionLot.opened_on, PositionLot.lot_id)).all()
    signal_by_bare = {_bare(row["code"]): row for row in ctx["signals"]}
    position_by_bare: dict[str, dict] = {}
    for lot in lots:
        key = _bare(lot.instrument_id)
        signal = signal_by_bare.get(key)
        row = position_by_bare.get(key)
        if row is None:
            row = {
                "code": (signal.get("code") if signal else lot.instrument_id),
                "name": lot.name,
                "asset_type": lot.asset_type,
                "shares": 0,
                "current_price": signal.get("price") if signal else 0,
            }
            position_by_bare[key] = row
        row["shares"] += lot.remaining_shares
    positions = list(position_by_bare.values())
    fee_material = _fee_plan_material(s.get(FeeSchedule, "default"))
    input_hash = _hash({"dataset": dataset.dataset_hash,
                        "portfolio": _portfolio_plan_material(portfolio),
                        "positions": positions, "supplements": ctx["supplements"],
                        "fee_schedule": fee_material,
                        "rules": RULES_HASH, "stage": "executable"})
    existing = s.exec(select(TradePlan).where(TradePlan.input_hash == input_hash)).first()
    if existing is not None:
        return serialize_plan(s, existing.plan_id)
    unmapped = []
    for key, position in position_by_bare.items():
        if key not in signal_by_bare:
            unmapped.append(position["code"])
    previous = _latest_plan(s, dataset_id)
    payload = {
        "signal_date": dataset.trade_date, "execute_date": ctx["execute_date"],
        "dataset_id": dataset_id, "portfolio_snapshot_id": portfolio_snapshot_id,
        "plan_stage": "executable", "plan_status": "draft", "input_hash": input_hash,
        "supersedes_plan_id": previous.plan_id if previous else None,
        "change_notice": change_notice,
        "market_temperature": ctx["market"].temperature_curr if ctx["market"] else "寒",
        "market_danger": bool(ctx["market"].danger) if ctx["market"] else False,
        "trend_as_of_date": dataset.trade_date, "market_as_of_date": dataset.trade_date,
        "account_as_of_date": portfolio.as_of_date,
        "source_modes": {"trend": dataset.source_mode, "market": "tushare",
                         "account": portfolio.source},
        "account": {"nav": portfolio.nav, "cash": portfolio.cash,
                    "market_value": portfolio.market_value, "confirmed": portfolio.confirmed,
                    "as_of_date": portfolio.as_of_date, "source": portfolio.source},
        "positions": positions, "signals": ctx["signals"], "candidates": ctx["candidates"],
    }
    result = generate_plan(s, payload, commit=commit)
    if unmapped:
        plan = s.get(TradePlan, result["plan_id"])
        health = dict(plan.data_health or {}); errors = list(health.get("errors") or [])
        if "unmapped_positions" not in errors:
            errors.append("unmapped_positions")
        health["errors"] = errors; health["lockable"] = False
        health["unmapped_positions"] = unmapped; plan.data_health = health; s.add(plan)
        if commit:
            s.commit()
        else:
            s.flush()
        result = serialize_plan(s, plan.plan_id)
    for old in s.exec(select(TradePlan).where(
        TradePlan.dataset_id == dataset_id, TradePlan.plan_stage == "executable",
        TradePlan.status == "draft", TradePlan.plan_id != result["plan_id"])).all():
        old.status = "expired"; old.change_notice = "已被新的账户或信号草稿替代"; s.add(old)
    if commit:
        s.commit()
    else:
        s.flush()
    return result


def latest_plan_for_date(s: Session, trade_date: str) -> dict:
    dataset = s.exec(select(DailyDataset).where(DailyDataset.trade_date == trade_date)).first()
    if dataset is None:
        raise KeyError(trade_date)
    plan = _latest_plan(s, dataset.dataset_id)
    if plan is None:
        raise KeyError("plan_not_generated")
    return serialize_plan(s, plan.plan_id)


def confirm_volatility(s: Session, *, dataset_id: str, instrument_id: str,
                       volatility_up: bool, source: str = "manual",
                       evidence: dict | None = None) -> dict:
    dataset = s.get(DailyDataset, dataset_id)
    if dataset is None:
        raise KeyError(dataset_id)
    if (dataset.capability_flags or {}).get("volatility_supported"):
        api_row = next((row for row in s.exec(select(TrendDailySnapshot).where(
            TrendDailySnapshot.dataset_id == dataset_id)).all()
            if _bare(row.code) == _bare(instrument_id)), None)
        if api_row is not None and api_row.volatility_up is not None:
            raise ValueError("api_volatility_is_authoritative")
    existing = s.exec(select(VolatilitySupplement).where(
        VolatilitySupplement.dataset_id == dataset_id,
        VolatilitySupplement.instrument_id == instrument_id)).first()
    if existing is None:
        existing = VolatilitySupplement(
            dataset_id=dataset_id, instrument_id=instrument_id,
            signal_date=dataset.trade_date, volatility_up=volatility_up,
            source=source, evidence=evidence or {},
        )
    else:
        existing.volatility_up = volatility_up
        existing.source = source
        existing.evidence = evidence or {}
        existing.confirmed_at = datetime.utcnow()
    s.add(existing); s.commit()
    signal_plan = ensure_signal_plan(s, dataset_id)
    portfolio = s.exec(select(PortfolioSnapshot).where(
        PortfolioSnapshot.trade_date == dataset.trade_date,
        PortfolioSnapshot.confirmed.is_(True)).order_by(
            PortfolioSnapshot.synced_at.desc())).first()
    executable = None
    if portfolio and portfolio.snapshot_id is not None:
        executable = ensure_executable_plan(
            s, dataset_id=dataset_id, portfolio_snapshot_id=portfolio.snapshot_id,
            change_notice="波动率标签补录后自动重算；已锁定计划未被修改",
        )
    return {"dataset_id": dataset_id, "instrument_id": instrument_id,
            "volatility_up": volatility_up, "signal_plan": signal_plan,
            "executable_plan": executable}
