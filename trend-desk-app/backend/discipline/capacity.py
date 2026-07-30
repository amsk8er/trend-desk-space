"""环境、强共振与新仓容量计算。"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from backend.discipline.rules import RULES

TEMP_RANK = {"冻": 0, "寒": 1, "凉": 2, "平": 3, "温": 4, "热": 5, "沸": 6}


@dataclass(frozen=True)
class Capacity:
    mode: str
    environment_factor: float
    per_position_weight: float
    max_new_tools: int
    max_added_weight: float
    available_weight: float
    remaining_tool_slots: int
    allowed_new_tools: int

    def as_dict(self) -> dict:
        return asdict(self)


def is_strong_resonance(*, market_temperature: str | None, sector_temperature: str | None,
                        warm_to_hot_count: int, market_danger: bool,
                        sector_danger: bool) -> bool:
    return bool(
        TEMP_RANK.get(market_temperature or "", -1) >= TEMP_RANK["温"]
        and TEMP_RANK.get(sector_temperature or "", -1) >= TEMP_RANK["热"]
        and warm_to_hot_count >= 5
        and not market_danger
        and not sector_danger
    )


def resonance_by_sector(candidates: list[dict], *, market_temperature: str | None,
                        market_danger: bool = False,
                        sector_dangers: dict[str, bool] | None = None) -> dict[str, dict]:
    """按板块统计温转热个股；双创影子股计数但后续仍不得进入实盘候选。"""
    sector_dangers = sector_dangers or {}
    grouped: dict[str, dict] = {}
    for row in candidates:
        if (row.get("asset_type") or "stock") != "stock":
            continue
        if row.get("temperature_prev") != "温" or row.get("temperature_curr") != "热":
            continue
        sector = str(row.get("sector") or row.get("industry") or "")
        if not sector:
            continue
        g = grouped.setdefault(sector, {"warm_to_hot_count": 0, "shadow_count": 0,
                                        "sector_temperature": row.get("sector_temperature")})
        g["warm_to_hot_count"] += 1
        code = str(row.get("code") or "")
        if code.startswith(("300", "301", "688")) or not row.get("permission", True):
            g["shadow_count"] += 1
        if g.get("sector_temperature") is None and row.get("sector_temperature") is not None:
            g["sector_temperature"] = row.get("sector_temperature")
    for sector, g in grouped.items():
        g["strong_resonance"] = is_strong_resonance(
            market_temperature=market_temperature,
            sector_temperature=g.get("sector_temperature"),
            warm_to_hot_count=g["warm_to_hot_count"], market_danger=market_danger,
            sector_danger=bool(sector_dangers.get(sector)),
        )
    return grouped


def calculate_capacity(*, market_temperature: str, current_weight: float,
                       current_tools: int, resonance: bool = False) -> Capacity:
    cfg = RULES["capacity"]
    factor = float(cfg["environment_factors"].get(market_temperature, 0.0))
    per = float(cfg["base_new_position_pct"]) * factor
    mode = "resonance" if resonance else "normal"
    limits = cfg[mode]
    available = max(0.0, float(cfg["max_total_weight"]) - max(0.0, current_weight))
    slots = max(0, int(cfg["max_tools"]) - max(0, current_tools))
    by_weight = int(min(available, float(limits["max_added_weight"])) // per) if per > 0 else 0
    allowed = min(int(limits["max_new_tools"]), slots, by_weight)
    return Capacity(
        mode=mode, environment_factor=factor, per_position_weight=per,
        max_new_tools=int(limits["max_new_tools"]),
        max_added_weight=float(limits["max_added_weight"]),
        available_weight=round(available, 8), remaining_tool_slots=slots,
        allowed_new_tools=allowed,
    )
