"""按交易日计算离场动作；全清短路，止盈同基数 25%×N。"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from backend.discipline.rules import RULES


@dataclass(frozen=True)
class ExitDecision:
    action: str
    target_shares: int
    reduce_fraction: float
    priority: int
    profit_signal_count: int
    active_profit_signals: list[str]
    consecutive_days_by_signal: dict[str, int]
    volatility_complete: bool
    evidence: dict

    def as_dict(self) -> dict:
        return asdict(self)


def _consecutive(today: dict, previous: dict | None) -> dict[str, int]:
    previous = previous or {}
    prev_counts = previous.get("consecutive_days_by_signal") or {}
    return {
        key: (int(prev_counts.get(key, 0)) + 1 if today.get(key) is True else 0)
        for key in RULES["exit"]["profit_signals"]
    }


def decide_exit(*, shares: int, signal: dict, previous: dict | None = None) -> ExitDecision:
    cfg = RULES["exit"]
    shares = max(0, int(shares))
    temp = signal.get("temperature_curr") or signal.get("temperature_status")
    full = bool(signal.get("danger")) or temp in cfg["full_exit_temperatures"] or bool(signal.get("temp_flat_or_below"))
    counts = _consecutive(signal, previous)
    active = [k for k in cfg["profit_signals"] if signal.get(k) is True]
    # 波动率放大已融入「沸」：不再要求补录，volatility_complete 恒为 True（兼容旧字段）。
    volatility_complete = True
    evidence = {
        "danger": bool(signal.get("danger")), "temperature_curr": temp,
        "strength": signal.get("strength"),
        "strength_change": signal.get("strength_change"),
        "profit_signals": {k: signal.get(k) for k in cfg["profit_signals"]},
        "same_base_shares": shares, "rules_version": RULES["version"],
    }
    if full:
        return ExitDecision("sell_all", shares, 1.0, cfg["full_exit_priority"], 0, [], counts,
                            volatility_complete, evidence)
    n = len(active)
    if n:
        fraction = min(0.75, cfg["fraction_per_signal"] * n)
        raw = shares * fraction
        lot = int(cfg["round_lot"])
        target = int(raw // lot) * lot
        action = "reduce" if target > 0 else "manual_review"
        evidence["raw_target_shares"] = raw
        evidence["rounding_difference"] = raw - target
        return ExitDecision(action, target, fraction, cfg["reduce_priority"], n, active, counts,
                            volatility_complete, evidence)
    return ExitDecision("hold", 0, 0.0, cfg["hold_priority"], 0, [], counts,
                        volatility_complete, evidence)


def expire_danger_instruction(*, previous_signal: dict, refreshed_signal: dict,
                              execution_found: bool) -> dict:
    """危险全清只在下一开盘有效；信号消失后不补卖，但记录违规。"""
    was_danger = bool(previous_signal.get("danger"))
    still_danger = bool(refreshed_signal.get("danger"))
    missed = was_danger and not execution_found
    return {
        "generate_catchup_sell": bool(missed and still_danger),
        "violation": "missed_danger_exit" if missed else None,
        "expired_without_catchup": bool(missed and not still_danger),
    }
