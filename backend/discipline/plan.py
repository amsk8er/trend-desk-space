"""生成、读取并锁定每日唯一行动计划。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from backend.db import (
    DisciplineVersion, FeeSchedule, PortfolioSnapshot, TradePlan, TradePlanItem,
    DailyExitSignal, SignalSnapshot,
)
from backend.discipline.capacity import calculate_capacity, resonance_by_sector
from backend.discipline.exits import decide_exit
from backend.discipline.rules import (
    EFFECTIVE_FROM, RULES, RULES_HASH, RULES_VERSION, SOURCE_PATH, source_hash,
)
from backend.discipline.selection import select_candidates


def ensure_active_version(s: Session) -> DisciplineVersion:
    existing = s.get(DisciplineVersion, RULES_VERSION)
    if existing is None:
        payload = dict(RULES)
        payload["source_hash"] = source_hash()
        existing = DisciplineVersion(
            version=RULES_VERSION, effective_from=EFFECTIVE_FROM, status="active",
            source_path=str(SOURCE_PATH), rules_json=payload, rules_hash=RULES_HASH,
        )
        s.add(existing)
    elif existing.status != "active":
        existing.status = "active"
        s.add(existing)
    previous_active = s.exec(select(DisciplineVersion).where(
        DisciplineVersion.status == "active",
        DisciplineVersion.version != RULES_VERSION,
    )).all()
    for previous in previous_active:
        previous.status = "retired"
        s.add(previous)
    s.commit()
    s.refresh(existing)
    return existing


def _norm_code(code: str) -> str:
    return str(code or "").strip().upper()


def _source_dates(signal_date: str, payload: dict) -> dict:
    return {
        "signal": signal_date,
        "trend_animals": payload.get("trend_as_of_date") or signal_date,
        "tushare": payload.get("market_as_of_date") or signal_date,
        "account": payload.get("account_as_of_date") or signal_date,
    }


def generate_plan(s: Session, payload: dict) -> dict:
    version = ensure_active_version(s)
    input_hash = payload.get("input_hash")
    if input_hash:
        existing = s.exec(select(TradePlan).where(TradePlan.input_hash == input_hash)
                          .order_by(TradePlan.created_at.desc())).first()
        if existing is not None:
            return serialize_plan(s, existing.plan_id)
    signal_date = payload["signal_date"]
    execute_date = payload["execute_date"]
    account = payload.get("account") or {}
    nav = float(account.get("nav") or 0)
    cash = float(account.get("cash") or 0)
    market_value = float(account.get("market_value") or max(0.0, nav - cash))
    account_confirmed = bool(account.get("confirmed"))
    snapshot_id = payload.get("portfolio_snapshot_id")
    if snapshot_id is not None:
        snapshot = s.get(PortfolioSnapshot, int(snapshot_id))
        if snapshot is None:
            raise ValueError("portfolio_snapshot_not_found")
        nav, cash, market_value = snapshot.nav, snapshot.cash, snapshot.market_value
        account_confirmed = snapshot.confirmed
    else:
        snapshot = PortfolioSnapshot(
            trade_date=signal_date, nav=nav, cash=cash, market_value=market_value,
            source=account.get("source") or "manual", confirmed=account_confirmed,
            as_of_date=account.get("as_of_date") or signal_date,
        )
        s.add(snapshot); s.flush()

    signals = {_norm_code(x.get("code") or x.get("instrument_id")): x
               for x in payload.get("signals", [])}
    positions = payload.get("positions", [])
    exit_rows: list[tuple[dict, dict]] = []
    released_value = 0.0
    unmapped: list[str] = []
    for p in positions:
        code = _norm_code(p.get("code") or p.get("instrument_id"))
        if not code:
            unmapped.append(p.get("name") or "unknown")
            continue
        signal = signals.get(code)
        if signal is None:
            signal = {"temperature_curr": None, "volatility_up": None}
        existing_snapshot = s.exec(select(SignalSnapshot).where(
            SignalSnapshot.instrument_id == code,
            SignalSnapshot.as_of_date == signal_date,
        )).first()
        if existing_snapshot is None:
            raw_hash = hashlib.sha256(json.dumps(signal, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            s.add(SignalSnapshot(
                instrument_id=code, as_of_date=signal_date,
                temperature_prev=signal.get("temperature_prev"),
                temperature_curr=signal.get("temperature_curr"),
                strength=signal.get("strength"), right_side_days=signal.get("right_side_days"),
                phase=signal.get("phase"), danger=signal.get("danger"),
                boiling=signal.get("boiling"), champagne=signal.get("champagne"),
                volatility_up=signal.get("volatility_up"),
                source=(payload.get("source_modes") or {}).get("trend") or "unknown",
                raw_payload_hash=raw_hash,
            ))
        decision = decide_exit(shares=int(p.get("shares") or 0), signal=signal,
                               previous=p.get("previous_exit_signal"))
        record = decision.as_dict()
        exit_rows.append((p, record))
        if record["action"] in {"sell_all", "reduce"}:
            released_value += record["target_shares"] * float(p.get("current_price") or 0)

    projected_weight = max(0.0, (market_value - released_value) / nav) if nav > 0 else 1.0
    current_tools_after_exit = sum(1 for p, d in exit_rows if d["action"] != "sell_all")
    resonance_evidence = resonance_by_sector(
        payload.get("candidates", []), market_temperature=payload.get("market_temperature"),
        market_danger=bool(payload.get("market_danger")),
        sector_dangers=payload.get("sector_dangers") or {},
    )
    resonance = (bool(payload["strong_resonance"]) if "strong_resonance" in payload
                 else any(x["strong_resonance"] for x in resonance_evidence.values()))
    market_temperature = payload.get("market_temperature") or "寒"
    cap = calculate_capacity(
        market_temperature=market_temperature,
        current_weight=projected_weight,
        current_tools=current_tools_after_exit,
        resonance=resonance,
    )
    selected = select_candidates(payload.get("candidates", []), cap)
    # 选股页「A股大类环境」读 capacity_snapshot.market_temperature；
    # Capacity.as_dict() 不含该字段，需显式并入（与 signal 草稿对齐）。
    capacity_snapshot = {
        **cap.as_dict(),
        "market_temperature": market_temperature,
        "base_new_position_pct": RULES["capacity"]["base_new_position_pct"],
        "opening_allowed": cap.environment_factor > 0,
    }

    dates = _source_dates(signal_date, payload)
    date_errors = [f"{k}:{v}" for k, v in dates.items() if k != "signal" and v != signal_date]
    errors = []
    if not account_confirmed:
        errors.append("account_not_confirmed")
    if nav <= 0:
        errors.append("invalid_nav")
    if unmapped:
        errors.append("unmapped_positions")
    if version.status != "active" or version.rules_hash != RULES_HASH:
        errors.append("discipline_version_mismatch")
    if date_errors:
        errors.append("source_date_mismatch")
    source_modes = payload.get("source_modes") or {}
    uses_mock = any("mock" in str(value).lower() for value in source_modes.values())
    allow_mock = bool(payload.get("allow_mock_data_for_testing"))
    if uses_mock and not allow_mock:
        errors.append("mock_data_not_allowed")
    data_health = {
        "lockable": not errors,
        "errors": errors,
        # 波动率放大已融入「沸」，不再因缺失补录产生 warning。
        "warnings": [],
        "unmapped_positions": unmapped,
        "date_mismatches": date_errors,
        "source_modes": source_modes,
        "testing_mode": allow_mock,
        "sell_only_allowed": any(d["action"] in {"sell_all", "reduce"} for _, d in exit_rows),
        "resonance_evidence": resonance_evidence,
    }
    plan_id = f"plan_{signal_date.replace('-', '')}_{uuid4().hex[:10]}"
    plan = TradePlan(
        plan_id=plan_id, signal_date=signal_date, execute_date=execute_date,
        discipline_version=version.version, rules_hash=version.rules_hash,
        status=payload.get("plan_status") or "draft",
        dataset_id=payload.get("dataset_id"), portfolio_snapshot_id=snapshot.snapshot_id,
        plan_stage=payload.get("plan_stage") or "executable", input_hash=input_hash,
        supersedes_plan_id=payload.get("supersedes_plan_id"),
        market_mode=cap.mode, environment_factor=cap.environment_factor,
        capacity_snapshot=capacity_snapshot, data_health=data_health, selection_snapshot=selected,
        change_notice=payload.get("change_notice"),
    )
    s.add(plan); s.flush()

    items: list[TradePlanItem] = []
    for p, d in exit_rows:
        code = _norm_code(p.get("code") or p.get("instrument_id"))
        item = TradePlanItem(
            plan_id=plan_id, instrument_id=code, name=p.get("name") or code,
            asset_type=p.get("asset_type") or "stock", side=d["action"],
            target_shares=d["target_shares"], reduce_fraction=d["reduce_fraction"],
            priority=d["priority"], rule_evidence=d["evidence"], source_dates=dates,
            data_sources=payload.get("source_modes") or {},
        )
        s.add(item); s.flush(); items.append(item)
        sig = signals.get(code) or {}
        s.add(DailyExitSignal(
            plan_id=plan_id, instrument_id=code, signal_date=signal_date, execute_date=execute_date,
            danger=bool(sig.get("danger")),
            temp_flat_or_below=(sig.get("temperature_curr") in RULES["exit"]["full_exit_temperatures"]),
            champagne=sig.get("champagne"), boiling=sig.get("boiling"),
            volatility_up=sig.get("volatility_up"),
            profit_signal_count=d["profit_signal_count"], planned_reduce_fraction=d["reduce_fraction"],
            consecutive_days_by_signal=d["consecutive_days_by_signal"],
            action_generated=d["action"], target_shares=d["target_shares"], valid_until=execute_date,
        ))

    remaining_cash = cash
    actionable_white_list = []
    fee_schedule = s.get(FeeSchedule, "default")
    for c in list(selected["white_list"]):
        price = float(c.get("price") or c.get("current_price") or 0)
        budget = min(nav * cap.per_position_weight, remaining_cash)
        shares = int(budget // price // 100) * 100 if price > 0 else 0
        estimated_fee = 0.0
        if shares > 0 and fee_schedule is not None and fee_schedule.configured:
            from backend.discipline.ledger import estimate_execution_fee
            while shares > 0:
                gross = shares * price
                estimated_fee = estimate_execution_fee(
                    fee_schedule,
                    code=str(c.get("code") or ""),
                    side="buy",
                    gross_amount=gross,
                )
                if gross + estimated_fee <= remaining_cash + 1e-9:
                    break
                shares -= 100
        if shares <= 0:
            selected["watch_list"].append({
                **c,
                "capacity_reason": "insufficient_cash",
            })
            continue
        actionable_white_list.append(c)
        gross = shares * price
        remaining_cash = round(remaining_cash - gross - estimated_fee, 2)
        evidence = {"checks": c["evidence"], "capacity": cap.as_dict(),
                    "ranking": {"strength": c.get("strength"), "amount_yi": c.get("amount_yi"),
                                "overlap_exposure": c.get("overlap_exposure", 0)},
                    "cash": {"estimated_gross": gross, "estimated_fee": estimated_fee,
                             "estimated_cash_required": gross + estimated_fee,
                             "remaining_cash_after": remaining_cash}}
        item = TradePlanItem(
            plan_id=plan_id, instrument_id=_norm_code(c.get("code")),
            name=c.get("name") or c.get("code"), asset_type=c["asset_type"], side="buy",
            target_weight=cap.per_position_weight, target_shares=shares, priority=3,
            rule_evidence=evidence, source_dates=dates,
            data_sources=payload.get("source_modes") or {},
        )
        s.add(item); s.flush(); items.append(item)

    selected["white_list"] = actionable_white_list
    # JSON columns do not track in-place changes. Assign a fresh object so the
    # cash-constrained white/watch lists are persisted for API and email reads.
    plan.selection_snapshot = json.loads(json.dumps(selected, ensure_ascii=False))
    flag_modified(plan, "selection_snapshot")
    s.add(plan)
    s.commit()
    return serialize_plan(s, plan_id, selection=selected)


def _market_temperature_from_dataset(s: Session, dataset_id: str | None) -> str | None:
    """从每日数据集的 market 成员快照取 A 股大类温度（兼容旧 executable 计划）。"""
    if not dataset_id:
        return None
    from backend.db import TrendDailyMembership, TrendDailySnapshot
    tm_id = s.exec(select(TrendDailyMembership.tm_id).where(
        TrendDailyMembership.dataset_id == dataset_id,
        TrendDailyMembership.membership_type == "market",
    )).first()
    if tm_id is None:
        return None
    row = s.exec(select(TrendDailySnapshot).where(
        TrendDailySnapshot.dataset_id == dataset_id,
        TrendDailySnapshot.tm_id == tm_id,
    )).first()
    return row.temperature_curr if row is not None else None


def serialize_plan(s: Session, plan_id: str, *, selection: dict | None = None) -> dict:
    plan = s.get(TradePlan, plan_id)
    if plan is None:
        raise KeyError(plan_id)
    items = s.exec(select(TradePlanItem).where(TradePlanItem.plan_id == plan_id)
                   .order_by(TradePlanItem.priority, TradePlanItem.item_id)).all()
    out = plan.model_dump()
    out["items"] = [x.model_dump() for x in items]
    if selection is not None:
        out["selection"] = selection
    # 选股页仓位展示：有账户快照时带上净值摘要（无则 null，前端显示「待账户」）。
    account = None
    if plan.portfolio_snapshot_id is not None:
        snap = s.get(PortfolioSnapshot, plan.portfolio_snapshot_id)
        if snap is not None:
            account = {
                "nav": snap.nav, "cash": snap.cash, "market_value": snap.market_value,
                "confirmed": snap.confirmed, "as_of_date": snap.as_of_date,
                "source": snap.source,
            }
    out["account"] = account
    # 旧 executable 计划 capacity_snapshot 缺 market_temperature → 回填后前端才显示「凉」等。
    cap = dict(out.get("capacity_snapshot") or {})
    if not cap.get("market_temperature"):
        recovered = _market_temperature_from_dataset(s, plan.dataset_id)
        if recovered:
            cap["market_temperature"] = recovered
            if "base_new_position_pct" not in cap:
                cap["base_new_position_pct"] = RULES["capacity"]["base_new_position_pct"]
            if "opening_allowed" not in cap:
                env = float(cap.get("environment_factor") or plan.environment_factor or 0)
                cap["opening_allowed"] = env > 0
            out["capacity_snapshot"] = cap
    return out


def lock_plan(s: Session, plan_id: str) -> dict:
    plan = s.get(TradePlan, plan_id)
    if plan is None:
        raise KeyError(plan_id)
    if plan.status == "locked":
        return serialize_plan(s, plan_id)
    if plan.status != "draft":
        raise ValueError(f"plan_not_draft:{plan.status}")
    health = plan.data_health or {}
    if not health.get("lockable"):
        # 风险退出不被买入链路阻断，但账户/代码/日期仍是锁定硬门。
        raise ValueError("data_health_blocked:" + ",".join(health.get("errors") or []))
    version = s.get(DisciplineVersion, plan.discipline_version)
    if version is None or version.status != "active" or version.rules_hash != plan.rules_hash:
        raise ValueError("discipline_version_mismatch")
    plan.status = "locked"
    plan.locked_at = datetime.utcnow()
    s.add(plan); s.commit()
    return serialize_plan(s, plan_id)
