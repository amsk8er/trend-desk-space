"""把趋势动物的美股身份种子与公开 xStock 交易场所清单做保守交集。"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_symbol(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", _text(value).upper())


def _okx_stock_rows(rows: list[dict]) -> list[dict]:
    """提取 OKX 公共接口文档中 instCategory=3（Stocks）的 live Spot 标的。"""
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or _text(row.get("instCategory")) != "3":
            continue
        if _text(row.get("state")).lower() != "live":
            continue
        base = _text(row.get("baseCcy")).upper()
        if not base.startswith("X") or len(base) == 1:
            continue
        out.append({
            "venue": "okx",
            "venue_instrument": _text(row.get("instId")),
            "underlying_symbol": base[1:],
            "base_coin": base,
            "quote_coin": _text(row.get("quoteCcy")).upper(),
            "min_size": row.get("minSz"),
            "lot_size": row.get("lotSz"),
            "tick_size": row.get("tickSz"),
        })
    return out


def _bitget_stock_rows(rows: list[dict]) -> list[dict]:
    """提取 Bitget 公共接口标记为 stock/reality 的 online Spot 标的。"""
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        is_stock = _text(row.get("symbolType")).lower() == "stock"
        is_reality = _text(row.get("isReality")).lower() == "yes"
        if not (is_stock and is_reality) or _text(row.get("status")).lower() != "online":
            continue
        base = _text(row.get("baseCoin"))
        if not base.lower().startswith("r") or len(base) == 1:
            continue
        out.append({
            "venue": "bitget",
            "venue_instrument": _text(row.get("symbol")),
            "underlying_symbol": base[1:].upper(),
            "base_coin": base,
            "quote_coin": _text(row.get("quoteCoin")).upper(),
            "min_trade_usdt": row.get("minTradeUSDT"),
            "price_precision": row.get("pricePrecision"),
            "quantity_precision": row.get("quantityPrecision"),
        })
    return out


def _match_seed(underlying_symbol: str, *, exact: dict[str, list[dict]],
                normalized: dict[str, list[dict]]) -> tuple[str, dict | None, list[str]]:
    exact_matches = exact.get(underlying_symbol, [])
    if len(exact_matches) == 1:
        return "exact", exact_matches[0], []
    normal_matches = normalized.get(_normalized_symbol(underlying_symbol), [])
    if len(normal_matches) == 1:
        return "normalized_unverified", normal_matches[0], []
    candidates = sorted({_text(row.get("tickerSymbol")) for row in normal_matches})
    return "unmatched" if not candidates else "ambiguous", None, candidates


def build_public_xstock_pool(*, universe_seed: dict, okx_rows: list[dict],
                             bitget_rows: list[dict]) -> dict:
    """构建严格代码交集；标点清洗匹配一律留在待核对区，不进入候选池。"""
    instruments = universe_seed.get("instruments") or []
    exact: dict[str, list[dict]] = defaultdict(list)
    normalized: dict[str, list[dict]] = defaultdict(list)
    for instrument in instruments:
        if not isinstance(instrument, dict) or not _text(instrument.get("tickerSymbol")):
            continue
        symbol = _text(instrument["tickerSymbol"]).upper()
        exact[symbol].append(instrument)
        normalized[_normalized_symbol(symbol)].append(instrument)

    venue_rows = {
        "okx": _okx_stock_rows(okx_rows),
        "bitget": _bitget_stock_rows(bitget_rows),
    }
    matched: dict[int, dict] = {}
    unresolved: list[dict] = []
    venue_stats: dict[str, dict] = {}
    for venue, rows in venue_rows.items():
        stats = {"public_stock_instruments": len(rows), "exact_matches": 0,
                 "normalized_unverified": 0, "unmatched_or_ambiguous": 0}
        for venue_row in rows:
            method, source, alternatives = _match_seed(
                venue_row["underlying_symbol"], exact=exact, normalized=normalized,
            )
            if method == "exact" and source is not None:
                tm_id = int(source["tmId"])
                candidate = matched.setdefault(tm_id, {
                    "tmId": tm_id,
                    "tickerSymbol": source.get("tickerSymbol"),
                    "tickerName": source.get("tickerName"),
                    "asset": source.get("asset"),
                    "root_asset": source.get("root_asset"),
                    "asOfDate": source.get("asOfDate"),
                    "venues": [],
                })
                candidate["venues"].append({**venue_row, "match_method": method})
                stats["exact_matches"] += 1
                continue
            if method == "normalized_unverified":
                stats["normalized_unverified"] += 1
            else:
                stats["unmatched_or_ambiguous"] += 1
            unresolved.append({
                "venue": venue,
                "venue_instrument": venue_row["venue_instrument"],
                "underlying_symbol": venue_row["underlying_symbol"],
                "match_method": method,
                "trend_animals_candidate": ({
                    "tmId": source.get("tmId"), "tickerSymbol": source.get("tickerSymbol"),
                    "tickerName": source.get("tickerName"),
                } if source is not None else None),
                "ambiguous_symbols": alternatives,
            })
        venue_stats[venue] = stats

    candidates = sorted(matched.values(), key=lambda row: (str(row["tickerSymbol"]), row["tmId"]))
    dual_venue = sum(1 for row in candidates if len(row["venues"]) > 1)
    return {
        "schema_version": 1,
        "scope": "trend_animals_us_xstock_public_intersection_v1",
        "trend_animals_as_of_date": universe_seed.get("as_of_date"),
        "trend_animals_seed_instrument_count": universe_seed.get("instrument_count"),
        "venue_stats": venue_stats,
        "public_candidate_count": len(candidates),
        "dual_venue_candidate_count": dual_venue,
        "candidates": candidates,
        "unresolved": unresolved,
        "execution_guard": (
            "这是公开上市清单与趋势动物同日身份种子的严格代码交集；"
            "未验证账户地域、产品资格、API 下单支持、余额或订单权限，禁止自动下单。"
        ),
    }
