"""个股/ETF 硬门、影子池、排序和容量截断。"""
from __future__ import annotations

from backend.discipline.capacity import TEMP_RANK, Capacity
from backend.discipline.rules import RULES


def _value(c: dict, *names):
    for name in names:
        if c.get(name) is not None:
            return c[name]
    return None


def _evidence(rule: str, value, passed: bool, source: str = "") -> dict:
    return {"rule": rule, "value": value, "passed": passed, "source": source}


def evaluate_candidate(candidate: dict) -> dict:
    """返回保留全部失败原因的可审计判断；缺字段一律不通过。"""
    c = dict(candidate)
    code = str(c.get("code") or c.get("instrument_id") or "")
    asset = c.get("asset_type") or ("etf" if c.get("market") in {"ETF基金", "ETF"} else "stock")
    src = c.get("data_source") or "unknown"
    tests: list[dict] = []
    shadow = asset == "stock" and (code.startswith("300") or code.startswith("301") or code.startswith("688"))
    permission = bool(c.get("permission", not shadow)) and not shadow
    tests.append(_evidence("permission", permission, permission, src))
    prev = _value(c, "temperature_prev", "trend_temperature_prev")
    curr = _value(c, "temperature_curr", "temperature_status")
    warm_hot = prev == "温" and curr == "热"
    tests.append(_evidence("warm_to_hot", f"{prev}->{curr}", warm_hot, "trend_animals"))
    not_jump = not (prev == "温" and curr == "沸")
    tests.append(_evidence("not_warm_to_boiling", f"{prev}->{curr}", not_jump, "trend_animals"))
    phase = _value(c, "phase", "trend_phase", "jieqi")
    phase_order = RULES["selection"]["phase_order"]
    phase_limit = RULES["selection"]["max_entry_phase_exclusive"]
    phase_allowed = (
        phase in phase_order
        and phase_order.index(str(phase)) < phase_order.index(phase_limit)
    )
    tests.append(_evidence("phase<大暑", phase, phase_allowed, "trend_animals"))

    if asset == "etf":
        cfg = RULES["selection"]["etf"]
        aum = _value(c, "aum_yi", "fund_size_yi")
        amount = _value(c, "amount_yi", "turnover_yi")
        strength = _value(c, "strength", "trend_strength")
        tests += [
            _evidence("aum_yi>=25", aum, aum is not None and float(aum) >= cfg["min_aum_yi"], "tushare"),
            _evidence("amount_yi>=2", amount, amount is not None and float(amount) >= cfg["min_amount_yi"], "tushare"),
            _evidence("strength>=80", strength, strength is not None and float(strength) >= cfg["min_strength"], "trend_animals"),
        ]
    else:
        cfg = RULES["selection"]["stock"]
        sector_temp = _value(c, "sector_temperature", "industry_temperature")
        cap = _value(c, "float_market_cap_yi", "market_cap_yi")
        amount = _value(c, "amount_yi", "turnover_yi")
        right = _value(c, "right_side_days", "days_since_trend_entry")
        tests += [
            _evidence("sector_temperature>=温", sector_temp,
                      sector_temp is not None and TEMP_RANK.get(str(sector_temp), -1) >= TEMP_RANK["温"], "trend_animals"),
            _evidence("float_market_cap_yi>=300", cap, cap is not None and float(cap) >= cfg["min_float_market_cap_yi"], "tushare"),
            _evidence("amount_yi>=5", amount, amount is not None and float(amount) >= cfg["min_amount_yi"], "tushare"),
            _evidence("right_side_days<=10", right, right is not None and int(right) <= cfg["max_right_side_days"], "trend_animals"),
        ]
    failures = [t for t in tests if not t["passed"]]
    c.update({"asset_type": asset, "shadow": shadow, "eligible": not failures,
              "evidence": tests, "failed_rules": failures})
    return c


def deduplicate_same_index_etfs(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """同一基准指数只留一只；缺基准时不根据名称猜测。"""
    benchmark_groups: dict[str, list[dict]] = {}
    non_grouped: list[dict] = []
    for row in candidates:
        key = row.get("benchmark_key") if row.get("asset_type") == "etf" else None
        if key:
            benchmark_groups.setdefault(str(key), []).append(row)
        else:
            non_grouped.append(row)
    duplicates: list[dict] = []
    kept = list(non_grouped)
    for key, rows in benchmark_groups.items():
        rows.sort(key=lambda c: (
            -(float(_value(c, "strength", "trend_strength") or -1)),
            -(float(_value(c, "amount_yi", "turnover_yi") or -1)),
            -(float(_value(c, "aum_yi", "fund_size_yi") or -1)),
            str(c.get("code") or ""),
        ))
        winner = rows[0]
        kept.append(winner)
        duplicates.extend({
            **row,
            "capacity_reason": "same_index_duplicate",
            "benchmark_key": key,
            "replaced_by": winner.get("code"),
        } for row in rows[1:])
    return kept, duplicates


def select_candidates(candidates: list[dict], capacity: Capacity) -> dict:
    evaluated = [evaluate_candidate(c) for c in candidates]
    shadow_pool = [c for c in evaluated if c["shadow"]]
    eligible = [c for c in evaluated if c["eligible"] and not c["shadow"]]
    rejected = [c for c in evaluated if not c["eligible"] and not c["shadow"]]
    eligible.sort(key=lambda c: (
        -(float(_value(c, "strength", "trend_strength") or -1)),
        -(float(_value(c, "amount_yi", "turnover_yi") or -1)),
        float(c.get("overlap_exposure") or 0),
        str(c.get("code") or ""),
    ))
    # 同一基准指数的 ETF 只保留一只。组内先择强，再看成交额、规模和代码；
    # 缺少基准时不根据名称猜测，也不自动合并。
    deduplicated, duplicate_etfs = deduplicate_same_index_etfs(eligible)
    eligible = sorted(deduplicated, key=lambda c: (
        -(float(_value(c, "strength", "trend_strength") or -1)),
        -(float(_value(c, "amount_yi", "turnover_yi") or -1)),
        str(c.get("code") or ""),
    ))
    take = capacity.allowed_new_tools
    white = eligible[:take]
    watch = [*eligible[take:], *duplicate_etfs]
    return {"white_list": white, "watch_list": watch, "shadow_pool": shadow_pool,
            "rejected": rejected, "capacity": capacity.as_dict()}
