"""今日纪律卡片：固定 Q1–Q4 投影，只读库、不调 LLM、不付费。

给看板 / Agent / 后续 Chat 纪律读工具共用。默认不回传 evidence 全量。
"""
from __future__ import annotations

from collections import Counter

from sqlmodel import Session, select

from backend.db import DailyDataset, TradePlan
from backend.discipline.daily_data import china_trade_date, serialize_dataset
from backend.discipline.plan import serialize_plan
from backend.discipline.rules import RULES_VERSION


def bare_code(code: str | None) -> str:
    return str(code or "").upper().split(".", 1)[0]


def _pick_dataset(s: Session, trade_date: str) -> DailyDataset | None:
    """优先精确交易日；若当日无行，回退到最近一条 ready 数据集。"""
    exact = s.exec(select(DailyDataset).where(DailyDataset.trade_date == trade_date)).first()
    if exact is not None:
        return exact
    return s.exec(
        select(DailyDataset)
        .where(DailyDataset.status.in_(["ready", "ready_degraded"]))
        .where(DailyDataset.trade_date <= trade_date)
        .order_by(DailyDataset.trade_date.desc())
    ).first()


def _pick_plan(s: Session, dataset_id: str) -> TradePlan | None:
    """可执行草稿/锁定优先，否则最新任意阶段计划。"""
    for stage in ("executable", "signal"):
        row = s.exec(
            select(TradePlan)
            .where(TradePlan.dataset_id == dataset_id, TradePlan.plan_stage == stage)
            .order_by(TradePlan.created_at.desc())
        ).first()
        if row is not None:
            return row
    return s.exec(
        select(TradePlan)
        .where(TradePlan.dataset_id == dataset_id)
        .order_by(TradePlan.created_at.desc())
    ).first()


def _one_liner_opening(*, market_temperature: str | None, environment_factor: float,
                       opening_allowed: bool, per_position_weight: float) -> str:
    temp = market_temperature or "未知"
    pct = f"{per_position_weight * 100:.2f}".rstrip("0").rstrip(".")
    if not opening_allowed:
        return f"环境{temp}，系数 {environment_factor:.2f}，今日禁止开仓"
    return f"环境{temp}，系数 {environment_factor:.2f}，单票仓位 {pct}%"


def _q1_opening(plan: dict) -> dict:
    cap = dict(plan.get("capacity_snapshot") or {})
    market_temperature = cap.get("market_temperature")
    environment_factor = float(
        cap.get("environment_factor")
        if cap.get("environment_factor") is not None
        else plan.get("environment_factor") or 0
    )
    base = float(cap.get("base_new_position_pct") or 0.05)
    per = float(
        cap.get("per_position_weight")
        if cap.get("per_position_weight") is not None
        else base * environment_factor
    )
    opening_allowed = cap.get("opening_allowed")
    if opening_allowed is None:
        opening_allowed = per > 0
    return {
        "market_temperature": market_temperature,
        "environment_factor": environment_factor,
        "per_position_weight": per,
        "base_new_position_pct": base,
        "opening_allowed": bool(opening_allowed),
        "one_liner": _one_liner_opening(
            market_temperature=market_temperature,
            environment_factor=environment_factor,
            opening_allowed=bool(opening_allowed),
            per_position_weight=per,
        ),
    }


def _buy_by_bare(plan: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in plan.get("items") or []:
        if item.get("side") != "buy":
            continue
        out[bare_code(item.get("instrument_id"))] = item
    return out


def _q2_whitelist(plan: dict, *, verbose: bool) -> dict:
    sel = plan.get("selection_snapshot") or plan.get("selection") or {}
    white = list(sel.get("white_list") or [])
    buy_map = _buy_by_bare(plan)
    account = plan.get("account") or {}
    nav = account.get("nav")
    per = float((plan.get("capacity_snapshot") or {}).get("per_position_weight") or 0)
    items = []
    for row in white:
        code = row.get("code")
        bare = bare_code(code)
        buy = buy_map.get(bare) or {}
        shares = buy.get("target_shares")
        if shares is None and plan.get("plan_stage") != "executable":
            shares = None
        weight = buy.get("target_weight")
        if weight is None:
            weight = per if per else None
        budget = None
        if nav is not None and weight is not None:
            budget = float(nav) * float(weight)
        price = row.get("price")
        is_stock = (row.get("asset_type") or "stock") != "etf"
        entry = {
            "code": code,
            "name": row.get("name"),
            "asset_type": row.get("asset_type") or "stock",
            "price": price,
            "sector": row.get("sector") if is_stock else None,
            "sector_temperature": row.get("sector_temperature") if is_stock else None,
            "target_shares": shares,
            "target_lots": (int(shares) // 100) if isinstance(shares, (int, float)) else None,
            "target_weight": weight,
            "budget": budget,
            "note": None,
        }
        if isinstance(shares, (int, float)) and shares == 0 and budget and price:
            entry["note"] = "预算不足1手"
        elif plan.get("plan_stage") != "executable" or nav is None:
            entry["note"] = "待账户"
        if verbose:
            entry["strength"] = row.get("strength")
            entry["strength_change"] = row.get("strength_change")
            entry["amount_yi"] = row.get("amount_yi")
            entry["phase"] = row.get("phase")
        items.append(entry)
    return {
        "count": len(items),
        "nav": nav,
        "plan_stage": plan.get("plan_stage"),
        "items": items,
    }


def _profit_signals(evidence: dict | None) -> dict:
    evidence = evidence or {}
    profit = evidence.get("profit_signals") or {}
    return {
        "danger": bool(evidence.get("danger")),
        "champagne": profit.get("champagne"),
        "boiling": profit.get("boiling") if "boiling" in profit else evidence.get("boiling"),
    }


def _q3_exits(plan: dict) -> dict:
    items = []
    for item in plan.get("items") or []:
        side = item.get("side")
        if side == "buy":
            continue
        evidence = item.get("rule_evidence") or {}
        signals = _profit_signals(evidence)
        items.append({
            "code": item.get("instrument_id"),
            "name": item.get("name"),
            "side": side,
            "reduce_fraction": item.get("reduce_fraction"),
            "target_shares": item.get("target_shares"),
            "signals": signals,
            "strength": evidence.get("strength"),
            "strength_change": evidence.get("strength_change"),
        })
    return {"count": len(items), "items": items}


def _top_reject_reasons(rejected: list[dict], *, limit: int = 8) -> list[dict]:
    counter: Counter[str] = Counter()
    for row in rejected:
        rules = row.get("failed_rules") or []
        if not rules:
            counter["unknown"] += 1
            continue
        for fr in rules:
            rule = (fr or {}).get("rule") or "unknown"
            counter[str(rule)] += 1
    return [{"rule": rule, "count": count} for rule, count in counter.most_common(limit)]


def _q4_watch_rejected(plan: dict) -> dict:
    sel = plan.get("selection_snapshot") or plan.get("selection") or {}
    watch = list(sel.get("watch_list") or [])
    rejected = list(sel.get("rejected") or [])
    shadow = list(sel.get("shadow_pool") or [])
    return {
        "watch_count": len(watch),
        "rejected_count": len(rejected),
        "shadow_count": len(shadow),
        "top_reject_reasons": _top_reject_reasons(rejected),
    }


def _empty_card(trade_date: str, *, note: str) -> dict:
    return {
        "trade_date": trade_date,
        "dataset": None,
        "plan": None,
        "answers": {
            "Q1_opening": None,
            "Q2_whitelist": None,
            "Q3_exits": None,
            "Q4_watch_rejected_summary": None,
        },
        "meta": {
            "rules_version": RULES_VERSION,
            "source": "discipline",
            "llm_required": False,
            "note": note,
        },
    }


def build_day_card(
    s: Session, *, trade_date: str | None = None, verbose: bool = False,
) -> dict:
    """组装固定纪律问题投影。"""
    day = trade_date or china_trade_date()
    dataset = _pick_dataset(s, day)
    if dataset is None:
        return _empty_card(day, note="no_dataset")

    dataset_view = {
        "dataset_id": dataset.dataset_id,
        "trade_date": dataset.trade_date,
        "status": dataset.status,
        "can_generate_plan": dataset.status in {"ready", "ready_degraded"},
        "source_mode": dataset.source_mode,
    }
    # 轻量状态字段（不整包 serialize_dataset，避免把大对象默认塞给 Agent）
    try:
        full = serialize_dataset(s, dataset, cached=True)
        dataset_view["error_message"] = full.get("error_message")
        dataset_view["approved_budget"] = full.get("approved_budget")
        dataset_view["actual_cost"] = full.get("actual_cost")
    except Exception:  # noqa: BLE001
        pass

    plan_row = _pick_plan(s, dataset.dataset_id)
    if plan_row is None:
        return {
            "trade_date": dataset.trade_date,
            "dataset": dataset_view,
            "plan": None,
            "answers": {
                "Q1_opening": None,
                "Q2_whitelist": None,
                "Q3_exits": None,
                "Q4_watch_rejected_summary": None,
            },
            "meta": {
                "rules_version": RULES_VERSION,
                "source": "discipline",
                "llm_required": False,
                "note": "dataset_without_plan",
            },
        }

    plan = serialize_plan(s, plan_row.plan_id)
    return {
        "trade_date": dataset.trade_date,
        "dataset": dataset_view,
        "plan": {
            "plan_id": plan.get("plan_id"),
            "plan_stage": plan.get("plan_stage"),
            "status": plan.get("status"),
            "signal_date": plan.get("signal_date"),
            "execute_date": plan.get("execute_date"),
            "discipline_version": plan.get("discipline_version"),
        },
        "answers": {
            "Q1_opening": _q1_opening(plan),
            "Q2_whitelist": _q2_whitelist(plan, verbose=verbose),
            "Q3_exits": _q3_exits(plan),
            "Q4_watch_rejected_summary": _q4_watch_rejected(plan),
        },
        "meta": {
            "rules_version": RULES_VERSION,
            "source": "discipline",
            "llm_required": False,
            "verbose": bool(verbose),
            "note": None,
        },
    }
