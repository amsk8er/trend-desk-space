"""趋势动物字段到 Trend Desk 现有行契约的显式映射。"""
from __future__ import annotations


def _int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tags(row: dict, *, include_warm_transition: bool = False) -> list[str]:
    tags: list[str] = ["温转热"] if include_warm_transition else []
    if row.get("stopwinFlagByDangerSignal") is True:
        tags.append("危险信号")
    if row.get("stopwinFlagByBoilingTemperature") is True:
        tags.append("沸")
    if row.get("stopwinFlagByPopChampagne") is True:
        tags.append("开香槟")
    return tags


def holding_row(row: dict, *, update_dt: str | None = None) -> dict:
    exact_strength = _float(row.get("trendStrengthLocalCurr"))
    return {
        "tm_id": _int(row.get("tmId")),
        "code": row.get("tickerSymbol"),
        "name": row.get("tickerName") or "",
        "market": row.get("asset"),
        "temperature_status": row.get("trendTemperatureCurr"),
        "strength": round(exact_strength) if exact_strength is not None else None,
        "right_side_days": _int(row.get("daysSinceTrendEntry")),
        # gainSinceTrendEntry 单位未由官方文档定义，不写 right_side_gain_pct。
        "right_side_gain_pct": None,
        "jieqi": row.get("trendPhaseCurr"),
        "sector": row.get("industryName"),
        "as_of_date": row.get("asOfDate"),
        "update_dt": update_dt,
        "data_source": "trend_api",
        "raw_fields": {
            "data_source": "trend_api",
            "tags": _tags(row),
            "trend_temperature_prev": row.get("trendTemperaturePrev"),
            "trend_strength_exact": exact_strength,
            "trend_strength_change_raw": row.get("trendStrengthLocalChange"),
            "api_flags": {
                "danger": row.get("stopwinFlagByDangerSignal"),
                "boiling": row.get("stopwinFlagByBoilingTemperature"),
                "champagne": row.get("stopwinFlagByPopChampagne"),
            },
            "signal_unavailable": ["volatility"],
        },
    }


def candidate_row(row: dict, *, combo_name: str) -> dict:
    exact_strength = _float(row.get("trendStrengthLocalCurr"))
    return {
        "row_type": "instrument",
        "market": "A股",
        "code": row.get("tickerSymbol"),
        "name": row.get("tickerName"),
        "sector": row.get("industryName"),
        "temperature_status": row.get("trendTemperatureCurr"),
        "strength": round(exact_strength) if exact_strength is not None else None,
        "right_side_days": _int(row.get("daysSinceTrendEntry")),
        "right_side_gain_pct": None,
        "jieqi": row.get("trendPhaseCurr"),
        "raw_fields": {
            "data_source": "trend_api",
            "tm_id": _int(row.get("tmId")),
            "as_of_date": row.get("asOfDate"),
            "industry_tm_id": _int(row.get("industryTmId")),
            "market_cap_yi": _float(row.get("marketCap")),
            "turnover_yi": _float(row.get("amount1d")),
            # return1d 的数值单位官方文档未提供；只留原值，不映射到带百分号语义的字段。
            "daily_change_pct": None,
            "return1d_raw": row.get("return1d"),
            "is_trend_right_side": row.get("isTrendRightSide"),
            "trend_strength_exact": exact_strength,
            "tags": _tags(row, include_warm_transition=True),
            "source_combo": combo_name,
        },
    }
