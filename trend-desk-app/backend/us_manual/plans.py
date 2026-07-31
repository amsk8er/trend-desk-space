"""H6 美股人工执行清单：不可变分配、锁定预留与趋势退出。"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from backend import config
from backend.db import UsDailyRun, UsManualPlan, UsManualPlanItem
from backend.us_manual import repository
from backend.us_manual.allocation import capacity_snapshot
from backend.us_manual.contracts import (
    US_MANUAL_RULES_VERSION,
    UsManualError,
    decimal_text,
    serialize,
    sha256,
    utc_now,
)
from backend.us_manual.ledger import account_state, record_account_snapshot
from backend.us_manual.risk_anchor import execution_schedule
from backend.us_manual.rules import POLICY, rules_hash


EXECUTABLE_ASSET_TYPES = {"stock", "etf"}
ACTIONABLE_EXITS = {"exit_all", "reduce_25", "reduce_50"}


def require_h6_plan_mutable(plan: UsManualPlan) -> None:
    """H1-H5 rows remain readable but can never be mutated under H6 rules."""
    if plan.rules_version != US_MANUAL_RULES_VERSION:
        raise UsManualError(
            "historical_plan_read_only",
            "H1–H5 历史计划只读，不能锁定、回填成交或改写状态",
            409,
        )


# Backward-compatible import name; its semantics are now H6.
require_h5_plan_mutable = require_h6_plan_mutable


def _locked_buy_tickers(session: Session) -> set[str]:
    plans = session.exec(select(UsManualPlan).where(
        UsManualPlan.rules_version == US_MANUAL_RULES_VERSION,
        UsManualPlan.status.in_(["locked", "partially_executed"]),
    )).all()
    output: set[str] = set()
    for plan in plans:
        output.update(
            item.ticker_symbol
            for item in repository.plan_items(session, plan.plan_id)
            if item.side == "buy" and item.status != "completed"
        )
    return output


def _allocated_items(session: Session, preview_id: str) -> list[Any]:
    rows = [
        row for row in repository.allocation_items(session, preview_id)
        if row.status == "allocated"
    ]
    if not rows:
        raise UsManualError(
            "allocation_empty",
            "该不可变推荐分配没有可执行项；可改为今日不交易",
            422,
        )
    return rows


def _buy_item(
    session: Session, *, plan_id: str, allocation_item: Any, environment_id: str,
) -> UsManualPlanItem:
    candidate = repository.get_candidate(session, allocation_item.candidate_id)
    anchor = repository.get_risk_anchor(session, str(allocation_item.risk_anchor_id))
    if candidate.asset_type not in EXECUTABLE_ASSET_TYPES:
        raise UsManualError("asset_execution_not_enabled", "该资产类型不能进入 H6 手工清单", 422)
    if candidate.screen_status != "ready" or not candidate.gate_passed:
        raise UsManualError("candidate_not_plan_ready", f"{candidate.ticker_symbol} 已不是 H6 合格候选", 409)
    if candidate.asset_type == "etf" and candidate.benchmark_status != "verified":
        raise UsManualError("etf_benchmark_not_verified", f"{candidate.ticker_symbol} ETF 基准证据未核验", 409)
    if anchor.status != "ready" or anchor.candidate_id != candidate.candidate_id:
        raise UsManualError("risk_anchor_not_ready", f"{candidate.ticker_symbol} 风险锚点未就绪", 409)
    if (
        candidate.reference_price_usdt is None
        or candidate.reference_price_at is None
        or candidate.reference_price_source != "bitget_public_quote"
        or candidate.quote_status != "available"
        or candidate.reference_price_usdt != allocation_item.reference_price_usdt
        or anchor.quote_usdt != allocation_item.reference_price_usdt
    ):
        raise UsManualError("allocation_preview_stale", f"{candidate.ticker_symbol} 报价已变化，请重新计算分配", 409)
    if (
        allocation_item.allocation_item_id is None
        or allocation_item.allocated_notional_usdt is None
        or allocation_item.target_quantity is None
        or allocation_item.target_quantity <= 0
        or allocation_item.allocated_notional_usdt <= 0
    ):
        raise UsManualError("allocation_item_incomplete", f"{candidate.ticker_symbol} 分配项不完整", 409)
    venue = dict(candidate.venue_metadata_json or {})
    if not candidate.venue_instrument or venue.get("venue") != "bitget":
        raise UsManualError("venue_metadata_missing", f"{candidate.ticker_symbol} 缺少 Bitget 产品证据", 409)
    return UsManualPlanItem(
        plan_id=plan_id,
        candidate_id=int(candidate.candidate_id),
        side="buy",
        priority=int(allocation_item.priority),
        ticker_symbol=candidate.ticker_symbol,
        ticker_name=candidate.ticker_name,
        asset_type=candidate.asset_type,
        venue="bitget",
        venue_instrument=candidate.venue_instrument,
        venue_metadata_json=venue,
        entry_reference_price=allocation_item.reference_price_usdt,
        entry_reference_at=candidate.reference_price_at,
        entry_reference_source="bitget_public_quote",
        allocation_preview_item_id=int(allocation_item.allocation_item_id),
        risk_anchor_id=anchor.anchor_id,
        environment_id=environment_id,
        risk_anchor_price=anchor.anchor_price_usdt,
        risk_anchor_date=anchor.anchor_date,
        anchor_loss_estimate_usdt=allocation_item.anchor_loss_estimate_usdt,
        stop_price=None,
        stop_source=None,
        stop_distance=allocation_item.anchor_distance,
        risk_budget_usdt=POLICY["risk_budget_usdt_per_trade"],
        target_notional_usdt=allocation_item.allocated_notional_usdt,
        target_quantity=allocation_item.target_quantity,
        estimated_max_loss_usdt=None,
        stop_evidence_json={
            "semantic": "EP3 前期重要低点只用于反推仓位，不是真实止损",
            "risk_anchor": repository.model_payload(anchor),
        },
        reason_json={
            "candidate_evidence": repository.model_payload(candidate),
            "allocation_item": repository.model_payload(allocation_item),
            "real_exit_authority": "trend_animals_temperature_danger_boiling_champagne",
            "manual_only": True,
        },
    )


def create_draft(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    forbidden = {
        "run_id", "items", "candidate_id", "sizing_preview_id", "stop_price", "stop_source",
        "reference_price_usdt", "quantity", "notional", "environment_factor",
    } & set(payload)
    if forbidden:
        raise UsManualError(
            "client_plan_fields_forbidden",
            "H6 计划只能从不可变推荐分配复制，客户端不得传入价格、数量或止损",
            422,
            {"forbidden_fields": sorted(forbidden)},
        )
    unexpected = set(payload) - {"allocation_preview_id", "notes"}
    if unexpected:
        raise UsManualError(
            "unexpected_plan_fields", "H6 计划只接受 allocation_preview_id 和 notes", 422,
            {"unexpected_fields": sorted(unexpected)},
        )
    preview_id = str(payload.get("allocation_preview_id") or "").strip()
    if not preview_id:
        raise UsManualError("allocation_preview_id_required", "必须选择不可变推荐分配", 422)
    if config.US_MANUAL_H6_MODE != "active":
        raise UsManualError("h6_shadow_read_only", "H6 只有 active 模式可生成新买入清单", 409)
    preview = repository.get_allocation_preview(session, preview_id)
    run = repository.get_run(session, preview.run_id)
    if run.rules_version != US_MANUAL_RULES_VERSION or run.status not in {"ready", "ready_degraded"}:
        raise UsManualError("run_not_plan_ready", "只有当前完成的 H6 run 可生成计划", 409)
    environment = repository.get_environment(session, preview.environment_id)
    if environment.status != "ready" or environment.environment_factor <= 0:
        raise UsManualError("market_environment_blocked", "美股整体环境不允许今日新增买入", 409)
    if run.exit_status not in {"ready", "no_holdings"}:
        raise UsManualError("exit_data_blocked", "开放持仓退出字段未完成，新买入失败关闭", 409)
    existing = next((
        row for row in repository.list_plans(session, limit=100)
        if row.rules_version == US_MANUAL_RULES_VERSION
        and row.allocation_preview_id == preview_id
        and row.status in {"draft", "locked", "partially_executed", "completed"}
    ), None)
    if existing is not None:
        return serialize_plan(session, existing)

    allocation_items = _allocated_items(session, preview_id)
    plan_id = f"us-plan-{run.as_of_date.replace('-', '')}-{uuid4().hex[:10]}"
    items = [
        _buy_item(
            session, plan_id=plan_id, allocation_item=row,
            environment_id=environment.environment_id,
        )
        for row in allocation_items
    ]
    snapshot = record_account_snapshot(session, as_of_date=run.as_of_date)
    plan = UsManualPlan(
        plan_id=plan_id,
        signal_date=run.as_of_date,
        intended_execution_date=preview.intended_execution_date,
        run_id=run.run_id,
        status="draft",
        rules_version=US_MANUAL_RULES_VERSION,
        rules_sha256=rules_hash(),
        account_snapshot_id=snapshot.snapshot_id,
        account_snapshot_json=serialize(snapshot.model_dump()),
        allocation_preview_id=preview.allocation_preview_id,
        data_health_json={
            "lockable": True,
            "manual_only": True,
            "draft_reserves_capacity": False,
            "environment": repository.model_payload(environment),
            "exit_status": run.exit_status,
            "recommended_allocation_immutable": True,
        },
        input_hash=sha256({
            "allocation_preview_id": preview.allocation_preview_id,
            "allocation_input_hash": preview.input_hash,
            "rules_version": US_MANUAL_RULES_VERSION,
        }),
        notes=str(payload.get("notes") or "").strip() or None,
    )
    saved, _ = repository.create_plan(session, plan, items)
    return serialize_plan(session, saved)


def _validate_buy_lock(session: Session, plan: UsManualPlan, items: list[UsManualPlanItem]) -> None:
    preview = repository.get_allocation_preview(session, str(plan.allocation_preview_id))
    environment = repository.get_environment(session, preview.environment_id)
    capacity = capacity_snapshot(
        session, environment=environment, intended_execution_date=plan.intended_execution_date,
    )
    account = account_state(session)
    open_tickers = {str(row["ticker_symbol"]) for row in account["open_positions"]}
    locked_tickers = _locked_buy_tickers(session)
    buy_tickers = {item.ticker_symbol for item in items}
    conflicts = buy_tickers & (open_tickers | locked_tickers)
    if conflicts:
        raise UsManualError(
            "existing_position_duplicate", "不允许对已持有或已锁定标的加仓/摊平", 409,
            {"ticker_symbols": sorted(conflicts)},
        )
    if len(items) > capacity["available_ticker_slots"]:
        raise UsManualError("position_capacity_full", "锁定后会超过 20 个不同标的上限", 409)
    planned = sum((item.target_notional_usdt or Decimal("0") for item in items), Decimal("0"))
    if planned > capacity["available_usdt"]:
        raise UsManualError(
            "allocation_capacity_changed",
            "环境日限额、组合敷口或现金可用量已变化，请重新计算分配",
            409,
            {
                "planned_usdt": decimal_text(planned),
                "available_usdt": decimal_text(capacity["available_usdt"]),
            },
        )
    if planned <= 0 or planned > POLICY["max_total_notional_usdt"]:
        raise UsManualError("plan_notional_invalid", "锁定计划名义金额无效", 409)
    for item in items:
        if (
            item.asset_type not in EXECUTABLE_ASSET_TYPES
            or item.target_quantity is None
            or item.target_quantity <= 0
            or item.target_notional_usdt is None
            or item.target_notional_usdt <= 0
            or item.entry_reference_price is None
            or item.risk_anchor_id is None
            or item.environment_id != preview.environment_id
        ):
            raise UsManualError("plan_item_incomplete", f"{item.ticker_symbol} 缺少 H6 冻结证据", 409)


def _validate_sell_lock(session: Session, items: list[UsManualPlanItem]) -> None:
    for item in items:
        if item.exit_decision_id is None or item.target_quantity is None or item.target_quantity <= 0:
            raise UsManualError("exit_plan_incomplete", f"{item.ticker_symbol} 退出计划证据不完整", 409)
        decision = repository.get_exit_decision(session, item.exit_decision_id)
        if decision.status != "pending" or decision.action not in ACTIONABLE_EXITS:
            raise UsManualError("exit_decision_stale", f"{item.ticker_symbol} 退出决定已失效或已执行", 409)
        lot = repository.get_lot(session, decision.lot_id)
        if lot.status != "open" or lot.remaining_quantity < item.target_quantity:
            raise UsManualError("exit_quantity_stale", f"{item.ticker_symbol} 可卖数量已变化", 409)


def _validate_lockable(session: Session, plan: UsManualPlan) -> None:
    require_h6_plan_mutable(plan)
    if plan.status not in {"draft", "locked"}:
        raise UsManualError("plan_not_lockable", f"当前清单状态 {plan.status} 不允许锁定", 409)
    if plan.status == "locked":
        return
    run = repository.get_run(session, plan.run_id)
    if run.rules_version != US_MANUAL_RULES_VERSION or run.status not in {"ready", "ready_degraded"}:
        raise UsManualError("plan_data_stale", "扫描运行或规则版本已失效", 409)
    items = repository.plan_items(session, plan.plan_id)
    if not items:
        raise UsManualError("plan_items_required", "空清单应使用今日不交易确认", 422)
    sides = {item.side for item in items}
    if sides == {"buy"}:
        _validate_buy_lock(session, plan, items)
    elif sides == {"sell"}:
        _validate_sell_lock(session, items)
    else:
        raise UsManualError("mixed_plan_sides_forbidden", "买入和卖出必须生成独立手工清单", 409)


def lock_plan(session: Session, plan_id: str) -> dict[str, Any]:
    plan = repository.get_plan(session, plan_id)
    _validate_lockable(session, plan)
    if plan.status != "locked":
        plan.status = "locked"
        plan.locked_at = utc_now()
        repository.save_plan(session, plan)
    return serialize_plan(session, plan)


def mark_no_execution(session: Session, plan_id: str, *, note: str | None = None) -> dict[str, Any]:
    plan = repository.get_plan(session, plan_id)
    require_h6_plan_mutable(plan)
    if plan.status in {"completed", "partially_executed", "locked"}:
        raise UsManualError("no_execution_not_allowed", "已锁定或已有成交的清单不能改记为无交易", 409)
    plan.status = "no_execution"
    plan.notes = note or plan.notes
    repository.save_plan(session, plan)
    return serialize_plan(session, plan)


def confirm_no_trade_for_run(
    session: Session, *, run_id: str, note: str | None = None,
) -> dict[str, Any]:
    run = repository.get_run(session, run_id)
    if run.rules_version != US_MANUAL_RULES_VERSION:
        raise UsManualError("historical_run_read_only", "H1–H5 历史运行只读", 409)
    if run.status not in {"ready", "ready_degraded"}:
        raise UsManualError("run_not_reviewable", "请先完成当日 H6 采集", 409)
    existing = next((
        row for row in repository.list_plans(session, limit=100)
        if row.run_id == run_id and row.status == "no_execution"
    ), None)
    if existing is not None:
        return serialize_plan(session, existing)
    snapshot = record_account_snapshot(session, as_of_date=run.as_of_date)
    plan = UsManualPlan(
        plan_id=f"us-plan-{run.as_of_date.replace('-', '')}-no-trade-{uuid4().hex[:8]}",
        signal_date=run.as_of_date,
        intended_execution_date=run.as_of_date,
        run_id=run.run_id,
        status="no_execution",
        rules_version=US_MANUAL_RULES_VERSION,
        rules_sha256=rules_hash(),
        account_snapshot_id=snapshot.snapshot_id,
        account_snapshot_json=serialize(snapshot.model_dump()),
        data_health_json={"lockable": False, "manual_only": True, "reason": "no_execution_confirmed"},
        input_hash=sha256({"run_id": run_id, "no_execution": True, "note": note or ""}),
        notes=note or "今日不交易：不放宽 H6 筛选与风控条件。",
    )
    saved, _ = repository.create_plan(session, plan, [])
    return serialize_plan(session, saved)


def create_exit_plan(
    session: Session, *, lot_id: int, as_of_date: str | None = None,
) -> dict[str, Any]:
    from backend.us_manual.exits import position_actions

    lot = repository.get_lot(session, lot_id)
    if lot.rules_version != US_MANUAL_RULES_VERSION:
        raise UsManualError("historical_position_read_only", "H1–H5 历史持仓只读，不伪造 H6 退出证据", 409)
    action = next((
        row for row in position_actions(session, as_of_date=as_of_date)
        if int(row["lot"]["lot_id"]) == lot_id
    ), None)
    if action is None or action.get("action") not in ACTIONABLE_EXITS or not action.get("decision"):
        raise UsManualError("exit_not_triggered", "当前持仓没有可执行的趋势退出决定", 409)
    decision = repository.get_exit_decision(session, str(action["decision"]["decision_id"]))
    existing_item = session.exec(select(UsManualPlanItem).where(
        UsManualPlanItem.exit_decision_id == decision.decision_id,
    ).limit(1)).first()
    if existing_item is not None:
        existing_plan = repository.get_plan(session, existing_item.plan_id)
        if existing_plan.status in {"draft", "locked", "partially_executed", "completed"}:
            return serialize_plan(session, existing_plan)
    run = session.exec(select(UsDailyRun).where(
        UsDailyRun.as_of_date == decision.as_of_date,
        UsDailyRun.rules_version == US_MANUAL_RULES_VERSION,
        UsDailyRun.status.in_(["ready", "ready_degraded"]),
    ).order_by(UsDailyRun.created_at.desc()).limit(1)).first()
    if run is None:
        raise UsManualError("exit_data_missing", "退出数据日没有可审计 H6 run", 409)
    snapshot = record_account_snapshot(session, as_of_date=run.as_of_date)
    plan_id = f"us-exit-{run.as_of_date.replace('-', '')}-{lot_id}-{uuid4().hex[:8]}"
    item = UsManualPlanItem(
        plan_id=plan_id,
        side="sell",
        priority=decision.priority,
        ticker_symbol=lot.ticker_symbol,
        ticker_name=lot.ticker_name,
        asset_type=lot.asset_type,
        venue="bitget",
        venue_instrument=lot.venue_instrument,
        venue_metadata_json={"source": "confirmed_local_lot"},
        exit_decision_id=decision.decision_id,
        target_notional_usdt=decision.planned_quantity * lot.average_cost_usdt,
        target_quantity=decision.planned_quantity,
        reason_json={
            "exit_lot_id": lot_id,
            "exit_decision": repository.model_payload(decision),
            "expected_sell_proceeds_available_before_confirm": False,
            "manual_only": True,
        },
    )
    plan = UsManualPlan(
        plan_id=plan_id,
        signal_date=run.as_of_date,
        intended_execution_date=decision.intended_execution_date,
        run_id=run.run_id,
        status="draft",
        rules_version=US_MANUAL_RULES_VERSION,
        rules_sha256=rules_hash(),
        account_snapshot_id=snapshot.snapshot_id,
        account_snapshot_json=serialize(snapshot.model_dump()),
        data_health_json={
            "lockable": True,
            "manual_only": True,
            "exit_decision_id": decision.decision_id,
            "real_exit_authority": "trend_animals",
        },
        input_hash=sha256({
            "exit_decision_id": decision.decision_id,
            "quantity": decimal_text(decision.planned_quantity),
        }),
        notes="趋势退出清单：用户在 Bitget 手工卖出并回填成交。",
    )
    saved, _ = repository.create_plan(session, plan, [item])
    return serialize_plan(session, saved)


def serialize_plan(session: Session, plan: UsManualPlan) -> dict[str, Any]:
    payload = repository.model_payload(plan)
    if plan.rules_version == US_MANUAL_RULES_VERSION:
        # H6 owns the execution-date contract, so malformed dates are a real
        # invariant violation and must remain fail closed.
        payload["execution_schedule"] = execution_schedule(
            signal_date=plan.signal_date,
            intended_execution_date=plan.intended_execution_date,
            generated_at=plan.created_at,
        )
    else:
        # H1-H5 rows pre-date the H6 timezone contract. They remain readable
        # even when an old free-form date cannot map to an XNYS session.
        try:
            payload["execution_schedule"] = execution_schedule(
                signal_date=plan.signal_date,
                intended_execution_date=plan.intended_execution_date,
                generated_at=plan.created_at,
            )
        except (UsManualError, TypeError, ValueError):
            payload["execution_schedule"] = None
    payload["items"] = [
        repository.model_payload(item) for item in repository.plan_items(session, plan.plan_id)
    ]
    payload["legacy_read_only"] = plan.rules_version != US_MANUAL_RULES_VERSION
    payload["reservation_active"] = plan.status in {"locked", "partially_executed"}
    payload["manual_only_notice"] = "清单不代表订单；请在 Bitget 手工交易后回填真实成交。"
    return payload
