"""H6 环境总容量和不可变多候选推荐分配预览。"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from backend import config
from backend.db import (
    UsAllocationPreview,
    UsAllocationPreviewItem,
    UsManualExecution,
    UsManualPlan,
)
from backend.us_manual import repository
from backend.us_manual.contracts import (
    US_MANUAL_RULES_VERSION,
    UsManualError,
    decimal_text,
    parse_decimal,
    serialize,
    sha256,
)
from backend.us_manual.ledger import account_state
from backend.us_manual.risk_anchor import execution_schedule, next_us_session
from backend.us_manual.rules import POLICY


def _precision(value: Any) -> int:
    if isinstance(value, bool):
        raise UsManualError("venue_precision_invalid", "Bitget 数量精度无效", 422)
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise UsManualError("venue_precision_missing", "Bitget 产品缺少数量精度", 422) from exc
    if result < 0 or result > 12:
        raise UsManualError("venue_precision_invalid", "Bitget 数量精度超出 0–12", 422)
    return result


def _floor(value: Decimal, precision: int) -> Decimal:
    return value.quantize(Decimal("1").scaleb(-precision), rounding=ROUND_DOWN)


def _confirmed_for_item(session: Session, item_id: int) -> Decimal:
    rows = session.exec(select(UsManualExecution).where(
        UsManualExecution.plan_item_id == item_id,
        UsManualExecution.confirmed.is_(True),
        UsManualExecution.side == "buy",
    )).all()
    return sum((row.gross_usdt for row in rows), Decimal("0"))


def locked_buy_reservations(
    session: Session, *, intended_execution_date: str | None = None,
) -> dict[str, Any]:
    all_reserved = Decimal("0")
    session_reserved = Decimal("0")
    session_confirmed = Decimal("0")
    locked_tickers: set[str] = set()
    plans = session.exec(select(UsManualPlan).where(
        UsManualPlan.rules_version == US_MANUAL_RULES_VERSION,
        UsManualPlan.status.in_(["locked", "partially_executed"]),
    )).all()
    for plan in plans:
        for item in repository.plan_items(session, plan.plan_id):
            if item.side != "buy" or item.target_notional_usdt is None or item.item_id is None:
                continue
            confirmed = _confirmed_for_item(session, item.item_id)
            remaining = max(Decimal("0"), item.target_notional_usdt - confirmed)
            all_reserved += remaining
            if remaining > 0:
                locked_tickers.add(item.ticker_symbol)
            if intended_execution_date and plan.intended_execution_date == intended_execution_date:
                session_reserved += remaining
                session_confirmed += confirmed
    return {
        "all_locked_unfilled_usdt": all_reserved,
        "session_locked_unfilled_usdt": session_reserved,
        "session_confirmed_usdt": session_confirmed,
        "locked_buy_tickers": sorted(locked_tickers),
    }


def capacity_snapshot(session: Session, *, environment: Any,
                      intended_execution_date: str) -> dict[str, Any]:
    account = account_state(session)
    reservations = locked_buy_reservations(
        session, intended_execution_date=intended_execution_date,
    )
    factor = parse_decimal(environment.environment_factor, field="environment_factor", non_negative=True)
    daily_limit = POLICY["account_usdt"] * factor
    daily_remaining = max(
        Decimal("0"),
        daily_limit
        - reservations["session_locked_unfilled_usdt"]
        - reservations["session_confirmed_usdt"],
    )
    portfolio_remaining = max(
        Decimal("0"),
        POLICY["max_total_notional_usdt"]
        - account["open_cost_usdt"]
        - reservations["all_locked_unfilled_usdt"],
    )
    cash_remaining = max(
        Decimal("0"),
        account["cash_usdt"] - reservations["all_locked_unfilled_usdt"],
    )
    available = max(Decimal("0"), min(daily_remaining, portfolio_remaining, cash_remaining))
    open_tickers = {str(row["ticker_symbol"]) for row in account["open_positions"]}
    occupied_tickers = open_tickers | set(reservations["locked_buy_tickers"])
    slots = max(0, int(POLICY["max_open_positions"]) - len(occupied_tickers))
    return {
        "daily_limit_usdt": daily_limit,
        "daily_remaining_usdt": daily_remaining,
        "portfolio_remaining_usdt": portfolio_remaining,
        "cash_remaining_usdt": cash_remaining,
        "available_usdt": available,
        "open_ticker_count": int(account["position_count"]),
        "occupied_ticker_count": len(occupied_tickers),
        "available_ticker_slots": slots,
        "reservations": reservations,
        "account": account,
    }


def _parse_exclusions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("exclusions") or []
    if not isinstance(raw, list):
        raise UsManualError("allocation_exclusions_invalid", "排除候选必须是数组", 422)
    output: list[dict[str, Any]] = []
    seen: set[int] = set()
    for value in raw:
        if not isinstance(value, dict):
            raise UsManualError("allocation_exclusion_invalid", "排除记录必须是对象", 422)
        try:
            candidate_id = int(value.get("candidate_id"))
        except (TypeError, ValueError) as exc:
            raise UsManualError("candidate_id_invalid", "排除记录的 candidate_id 无效", 422) from exc
        reason = str(value.get("reason") or "").strip()
        if not reason or len(reason) > 500:
            raise UsManualError("allocation_exclusion_reason_required", "排除候选必须填写 1–500 字理由", 422)
        if candidate_id in seen:
            raise UsManualError("allocation_exclusion_duplicate", "同一候选不能重复排除", 422)
        seen.add(candidate_id)
        output.append({"candidate_id": candidate_id, "reason": reason})
    return sorted(output, key=lambda row: row["candidate_id"])


def _serialize_preview(session: Session, preview: UsAllocationPreview) -> dict[str, Any]:
    value = repository.model_payload(preview)
    run = repository.get_run(session, preview.run_id)
    value["execution_schedule"] = execution_schedule(
        signal_date=run.as_of_date,
        intended_execution_date=preview.intended_execution_date,
        generated_at=preview.created_at,
    )
    value["items"] = []
    for row in repository.allocation_items(session, preview.allocation_preview_id):
        item = repository.model_payload(row)
        candidate = repository.get_candidate(session, row.candidate_id)
        item["candidate"] = repository.model_payload(candidate)
        value["items"].append(item)
    value["notice"] = "推荐分配只生成 Bitget 手工执行清单，不读取账户、不提交订单。"
    return value


def _clone_without_backfill(
    session: Session, *, base: UsAllocationPreview, exclusions: list[dict[str, Any]],
    input_hash: str,
) -> dict[str, Any]:
    excluded = {row["candidate_id"]: row["reason"] for row in exclusions}
    base_items = repository.allocation_items(session, base.allocation_preview_id)
    valid_ids = {row.candidate_id for row in base_items if row.status == "allocated"}
    if not set(excluded).issubset(valid_ids):
        raise UsManualError("allocation_exclusion_not_in_base", "只能排除基础预览中已分配的候选", 422)
    preview_id = f"us-allocation-{uuid4().hex[:16]}"
    allocated = Decimal("0")
    items: list[UsAllocationPreviewItem] = []
    for row in base_items:
        is_excluded = row.candidate_id in excluded and row.status == "allocated"
        notional = Decimal("0") if is_excluded else (row.allocated_notional_usdt or Decimal("0"))
        allocated += notional
        items.append(UsAllocationPreviewItem(
            allocation_preview_id=preview_id,
            candidate_id=row.candidate_id,
            risk_anchor_id=row.risk_anchor_id,
            priority=row.priority,
            status="excluded" if is_excluded else row.status,
            reason_code="user_excluded_without_backfill" if is_excluded else row.reason_code,
            reference_price_usdt=row.reference_price_usdt,
            anchor_price_usdt=row.anchor_price_usdt,
            anchor_distance=row.anchor_distance,
            risk_ceiling_usdt=row.risk_ceiling_usdt,
            target_notional_usdt=row.target_notional_usdt,
            allocated_notional_usdt=None if is_excluded else row.allocated_notional_usdt,
            target_quantity=None if is_excluded else row.target_quantity,
            anchor_loss_estimate_usdt=None if is_excluded else row.anchor_loss_estimate_usdt,
            minimum_notional_usdt=row.minimum_notional_usdt,
            evidence_json={
                **dict(row.evidence_json or {}),
                "base_preview_id": base.allocation_preview_id,
                "exclusion_reason": excluded.get(row.candidate_id),
                "automatic_backfill": False,
            },
        ))
    preview = UsAllocationPreview(
        allocation_preview_id=preview_id,
        run_id=base.run_id,
        environment_id=base.environment_id,
        intended_execution_date=base.intended_execution_date,
        input_hash=input_hash,
        daily_limit_usdt=base.daily_limit_usdt,
        daily_remaining_usdt=base.daily_remaining_usdt,
        portfolio_remaining_usdt=base.portfolio_remaining_usdt,
        cash_remaining_usdt=base.cash_remaining_usdt,
        available_usdt=base.available_usdt,
        allocated_usdt=allocated,
        open_ticker_count=base.open_ticker_count,
        available_ticker_slots=base.available_ticker_slots,
        exclusions_json=exclusions,
        backfill_requested=False,
        snapshot_json={
            **dict(base.snapshot_json or {}),
            "base_preview_id": base.allocation_preview_id,
            "remaining_capacity_not_auto_filled_usdt": decimal_text(base.allocated_usdt - allocated),
        },
    )
    saved, _ = repository.save_allocation_preview(session, preview, items)
    return _serialize_preview(session, saved)


def create_allocation_preview(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    forbidden = {
        "reference_price", "reference_price_usdt", "risk_anchor_price", "environment_factor",
        "quantity", "notional", "stop_price", "stop_source",
    } & set(payload)
    if forbidden:
        raise UsManualError(
            "client_allocation_fields_forbidden",
            "报价、锚点、环境系数、数量和金额只能由服务端冻结",
            422,
            {"forbidden_fields": sorted(forbidden)},
        )
    run_id = str(payload.get("run_id") or "").strip()
    run = repository.get_run(session, run_id)
    if run.rules_version != US_MANUAL_RULES_VERSION or run.status not in {"ready", "ready_degraded"}:
        raise UsManualError("run_not_plan_ready", "只有当前完成的 H6 run 可以生成推荐分配", 409)
    latest = repository.latest_run(session)
    if latest is None or latest.run_id != run.run_id:
        raise UsManualError("historical_run_read_only", "只能从当前活动 H6 run 生成推荐分配", 409)
    if config.US_MANUAL_H6_MODE != "active":
        raise UsManualError("h6_shadow_read_only", "H6 off/shadow 只生成证据，不生成买入分配", 409)
    environment = repository.environment_for_run(session, run.run_id)
    if environment is None or environment.status != "ready" or environment.environment_factor <= 0:
        raise UsManualError("market_environment_blocked", "美股整体环境不允许今日新增买入", 409)
    if repository.open_lots(session) and run.exit_status not in {"ready", "no_holdings"}:
        raise UsManualError("exit_data_blocked", "开放持仓退出字段未完成，买入计划失败关闭", 409)

    exclusions = _parse_exclusions(payload)
    raw_backfill = payload.get("backfill", False)
    if not isinstance(raw_backfill, bool):
        raise UsManualError("allocation_backfill_invalid", "backfill 必须是布尔值", 422)
    backfill = raw_backfill
    base_preview_id = str(payload.get("base_preview_id") or "").strip()
    base: UsAllocationPreview | None = None
    if exclusions:
        if not base_preview_id:
            raise UsManualError("base_preview_required", "排除或补位必须绑定原推荐预览", 422)
        base = repository.get_allocation_preview(session, base_preview_id)
        if base.run_id != run.run_id:
            raise UsManualError("allocation_run_mismatch", "原推荐预览不属于当前 H6 run", 409)
        allocated_ids = {
            row.candidate_id
            for row in repository.allocation_items(session, base.allocation_preview_id)
            if row.status == "allocated"
        }
        if not {row["candidate_id"] for row in exclusions}.issubset(allocated_ids):
            raise UsManualError("allocation_exclusion_not_in_base", "只能排除原预览中已分配的候选", 422)
    elif backfill:
        raise UsManualError("allocation_backfill_without_exclusion", "补位必须同时指定被排除候选", 422)
    if exclusions and not backfill:
        assert base is not None
        input_hash = sha256({
            "base_preview_id": base_preview_id,
            "exclusions": exclusions,
            "backfill": False,
            "rules_version": US_MANUAL_RULES_VERSION,
        })
        existing = repository.allocation_by_input(session, input_hash)
        if existing is not None:
            return _serialize_preview(session, existing)
        return _clone_without_backfill(
            session, base=base, exclusions=exclusions, input_hash=input_hash,
        )

    intended = next_us_session(run.as_of_date)
    capacity = capacity_snapshot(session, environment=environment, intended_execution_date=intended)
    existing_tickers = {
        *{row["ticker_symbol"] for row in capacity["account"]["open_positions"]},
        *set(capacity["reservations"]["locked_buy_tickers"]),
    }
    excluded = {row["candidate_id"]: row["reason"] for row in exclusions}
    candidates = [
        row for row in repository.list_candidates(session, run.run_id)
        if row.asset_type in {"stock", "etf"} and row.screen_status == "ready"
    ]
    candidates.sort(key=lambda row: (
        row.observation_rank if row.observation_rank is not None else 1_000_000,
        row.ticker_symbol,
        row.tm_id,
    ))
    anchor_state = [
        repository.model_payload(repository.latest_risk_anchor(session, int(row.candidate_id)))
        if repository.latest_risk_anchor(session, int(row.candidate_id)) is not None else None
        for row in candidates
    ]
    input_hash = sha256({
        "run_id": run.run_id,
        "environment_id": environment.environment_id,
        "capacity": serialize(capacity),
        "candidate_ids": [row.candidate_id for row in candidates],
        "anchors": anchor_state,
        "base_preview_id": base_preview_id or None,
        "exclusions": exclusions,
        "backfill": backfill,
        "rules_version": US_MANUAL_RULES_VERSION,
    })
    existing = repository.allocation_by_input(session, input_hash)
    if existing is not None:
        return _serialize_preview(session, existing)

    preview_id = f"us-allocation-{uuid4().hex[:16]}"
    remaining = capacity["available_usdt"]
    slots = capacity["available_ticker_slots"]
    allocated_count = 0
    allocated_total = Decimal("0")
    items: list[UsAllocationPreviewItem] = []
    for priority, candidate in enumerate(candidates, 1):
        reason: str | None = None
        status = "skipped"
        anchor = repository.latest_risk_anchor(session, int(candidate.candidate_id))
        venue = candidate.venue_metadata_json or {}
        minimum = max(
            POLICY["min_single_notional_usdt"],
            parse_decimal(venue.get("min_trade_usdt", "0"), field="min_trade_usdt", non_negative=True),
        )
        reference = candidate.reference_price_usdt
        risk_ceiling: Decimal | None = None
        target: Decimal | None = None
        allocated: Decimal | None = None
        quantity: Decimal | None = None
        anchor_loss: Decimal | None = None
        if int(candidate.candidate_id) in excluded:
            reason = "user_excluded_with_backfill"
            status = "excluded"
        elif candidate.ticker_symbol in existing_tickers:
            reason = "existing_position_duplicate"
        elif allocated_count >= slots:
            reason = "position_capacity_full"
        elif remaining < POLICY["min_single_notional_usdt"]:
            reason = "daily_capacity_below_policy_floor"
        elif candidate.asset_type == "etf" and candidate.benchmark_status != "verified":
            reason = "etf_benchmark_not_verified"
        elif reference is None or candidate.quote_status != "available":
            reason = "quote_unavailable"
        elif anchor is None or anchor.status != "ready" or anchor.anchor_price_usdt is None:
            reason = "risk_anchor_not_ready"
        elif anchor.quote_usdt != reference:
            reason = "risk_anchor_stale"
        else:
            distance = (reference - anchor.anchor_price_usdt) / reference
            if distance <= 0:
                reason = "risk_anchor_not_below_quote"
            else:
                risk_ceiling = POLICY["risk_budget_usdt_per_trade"] / distance
                target = min(POLICY["target_single_notional_usdt"], risk_ceiling)
                if target < minimum:
                    reason = "risk_ceiling_below_minimum"
                else:
                    attempted = min(target, remaining)
                    precision = _precision(venue.get("quantity_precision"))
                    quantity = _floor(attempted / reference, precision)
                    allocated = quantity * reference
                    if quantity <= 0 or allocated < minimum:
                        reason = "allocation_below_minimum"
                        quantity = allocated = None
                    else:
                        status = "allocated"
                        anchor_loss = quantity * (reference - anchor.anchor_price_usdt)
                        remaining -= allocated
                        allocated_total += allocated
                        allocated_count += 1
        items.append(UsAllocationPreviewItem(
            allocation_preview_id=preview_id,
            candidate_id=int(candidate.candidate_id),
            risk_anchor_id=anchor.anchor_id if anchor is not None else None,
            priority=priority,
            status=status,
            reason_code=reason,
            reference_price_usdt=reference,
            anchor_price_usdt=anchor.anchor_price_usdt if anchor is not None else None,
            anchor_distance=anchor.anchor_distance if anchor is not None else None,
            risk_ceiling_usdt=risk_ceiling,
            target_notional_usdt=target,
            allocated_notional_usdt=allocated,
            target_quantity=quantity,
            anchor_loss_estimate_usdt=anchor_loss,
            minimum_notional_usdt=minimum,
            evidence_json={
                "candidate_rank": candidate.observation_rank,
                "exclusion_reason": excluded.get(int(candidate.candidate_id)),
                "remaining_after_usdt": decimal_text(remaining),
                "semantic": "anchor_loss_estimate is not a maximum loss or exit stop",
            },
        ))
    preview = UsAllocationPreview(
        allocation_preview_id=preview_id,
        run_id=run.run_id,
        environment_id=environment.environment_id,
        intended_execution_date=intended,
        input_hash=input_hash,
        daily_limit_usdt=capacity["daily_limit_usdt"],
        daily_remaining_usdt=capacity["daily_remaining_usdt"],
        portfolio_remaining_usdt=capacity["portfolio_remaining_usdt"],
        cash_remaining_usdt=capacity["cash_remaining_usdt"],
        available_usdt=capacity["available_usdt"],
        allocated_usdt=allocated_total,
        open_ticker_count=capacity["open_ticker_count"],
        available_ticker_slots=capacity["available_ticker_slots"],
        exclusions_json=exclusions,
        backfill_requested=backfill,
        snapshot_json={
            "capacity": serialize(capacity),
            "base_preview_id": base_preview_id or None,
            "ranking": "strength_desc_days_asc_amount_desc_size_desc_ticker",
            "allocated_count": allocated_count,
            "remaining_unallocated_usdt": decimal_text(remaining),
            "draft_reserves_capacity": False,
        },
    )
    saved, _ = repository.save_allocation_preview(session, preview, items)
    return _serialize_preview(session, saved)


def allocation_payload(session: Session, preview_id: str) -> dict[str, Any]:
    return _serialize_preview(session, repository.get_allocation_preview(session, preview_id))
