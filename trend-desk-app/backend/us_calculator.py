"""Read-only US market data and technical levels for the position calculator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any

import httpx

from backend import config


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
NASDAQ_HISTORY_URL = "https://api.nasdaq.com/api/quote/{ticker}/historical"
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")


class UsCalculatorError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def normalize_ticker(value: str) -> str:
    ticker = value.strip().upper()
    if not TICKER_RE.fullmatch(ticker):
        raise UsCalculatorError(
            "ticker_invalid",
            "请输入有效美股代码，例如 NVDA、BRK-B 或 SPY。",
            422,
        )
    return ticker


def _positive(value: Any) -> float | None:
    try:
        number = float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _ema(values: list[float], period: int) -> list[float | None]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    current = values[0]
    out: list[float | None] = []
    for index, value in enumerate(values):
        current = value if index == 0 else alpha * value + (1 - alpha) * current
        out.append(current if index + 1 >= period else None)
    return out


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period:
        return None
    ranges: list[float] = []
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        ranges.append(max(highs[index] - lows[index], abs(highs[index] - previous), abs(lows[index] - previous)))
    current = sum(ranges[:period]) / period
    for value in ranges[period:]:
        current = ((period - 1) * current + value) / period
    return current


def _chart_payload(payload: dict[str, Any], ticker: str) -> dict[str, Any]:
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        raise UsCalculatorError("market_contract_error", "美股行情返回格式异常。")
    error = chart.get("error")
    if error:
        description = error.get("description") if isinstance(error, dict) else str(error)
        raise UsCalculatorError("ticker_not_found", str(description or "未找到该美股代码。"), 404)
    results = chart.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise UsCalculatorError("ticker_not_found", f"未找到美股代码 {ticker}。", 404)
    return results[0]


def _normalize_bars(result: dict[str, Any]) -> list[dict[str, Any]]:
    timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    quotes = indicators.get("quote") if isinstance(indicators, dict) else None
    if not isinstance(timestamps, list) or not isinstance(quotes, list) or not quotes or not isinstance(quotes[0], dict):
        raise UsCalculatorError("market_contract_error", "美股日线缺少必要字段。")
    quote = quotes[0]
    adjusted_groups = indicators.get("adjclose") if isinstance(indicators, dict) else None
    adjusted = adjusted_groups[0].get("adjclose") if (
        isinstance(adjusted_groups, list) and adjusted_groups
        and isinstance(adjusted_groups[0], dict)
    ) else None
    fields = {key: quote.get(key) for key in ("open", "high", "low", "close", "volume")}
    if any(not isinstance(values, list) for values in fields.values()):
        raise UsCalculatorError("market_contract_error", "美股日线 OHLCV 格式异常。")

    bars: list[dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        try:
            raw_open = _positive(fields["open"][index])
            raw_high = _positive(fields["high"][index])
            raw_low = _positive(fields["low"][index])
            raw_close = _positive(fields["close"][index])
            volume = float(fields["volume"][index] or 0)
            ts = int(timestamp)
        except (IndexError, TypeError, ValueError):
            continue
        if None in {raw_open, raw_high, raw_low, raw_close}:
            continue
        factor = 1.0
        if isinstance(adjusted, list) and index < len(adjusted):
            adjusted_close = _positive(adjusted[index])
            if adjusted_close is not None and raw_close:
                factor = adjusted_close / raw_close
        bars.append({
            "time": datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat(),
            "open": round(raw_open * factor, 4),
            "high": round(raw_high * factor, 4),
            "low": round(raw_low * factor, 4),
            "close": round(raw_close * factor, 4),
            "volume": max(0, round(volume)),
        })
    if len(bars) < 20:
        raise UsCalculatorError("market_history_short", "该代码可用日线不足 20 根，暂不能计算止损预设。", 409)
    return bars


def build_market_snapshot(payload: dict[str, Any], raw_ticker: str) -> dict[str, Any]:
    ticker = normalize_ticker(raw_ticker)
    result = _chart_payload(payload, ticker)
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    bars = _normalize_bars(result)
    closes = [row["close"] for row in bars]
    highs = [row["high"] for row in bars]
    lows = [row["low"] for row in bars]
    ema_by_period = {period: _ema(closes, period) for period in (10, 20, 50, 200)}
    atr14 = _atr(highs, lows, closes)
    for index, bar in enumerate(bars):
        bar["ema10"] = round(ema_by_period[10][index], 4) if ema_by_period[10][index] is not None else None
        bar["ema20"] = round(ema_by_period[20][index], 4) if ema_by_period[20][index] is not None else None
        bar["ema50"] = round(ema_by_period[50][index], 4) if ema_by_period[50][index] is not None else None
        bar["ema200"] = round(ema_by_period[200][index], 4) if ema_by_period[200][index] is not None else None

    latest = bars[-1]
    market_price = _positive(meta.get("regularMarketPrice")) or latest["close"]
    market_time = meta.get("regularMarketTime")
    as_of = (
        datetime.fromtimestamp(int(market_time), tz=timezone.utc).isoformat()
        if market_time else f"{latest['time']}T00:00:00+00:00"
    )
    short_stop = market_price - atr14 if atr14 is not None else None
    medium_stop = market_price - 1.6 * atr14 if atr14 is not None else None
    return {
        "ticker": str(meta.get("symbol") or ticker).upper(),
        "name": str(meta.get("shortName") or meta.get("longName") or ticker),
        "exchange": str(meta.get("fullExchangeName") or meta.get("exchangeName") or "US"),
        "currency": str(meta.get("currency") or "USD"),
        "price": round(market_price, 4),
        "as_of": as_of,
        "source": "Yahoo Finance chart",
        "source_notice": "公开行情可能延迟，仅用于盘前仓位测算；下单前请以券商可成交价为准。",
        "indicators": {
            "ema10": latest["ema10"],
            "ema20": latest["ema20"],
            "ema50": latest["ema50"],
            "ema200": latest["ema200"],
            "atr14": round(atr14, 4) if atr14 is not None else None,
            "atr_short_stop": round(short_stop, 4) if short_stop and short_stop > 0 else None,
            "atr_medium_stop": round(medium_stop, 4) if medium_stop and medium_stop > 0 else None,
        },
        "bars": bars[-260:],
    }


def _nasdaq_chart_payload(payload: dict[str, Any], ticker: str) -> dict[str, Any] | None:
    data = payload.get("data")
    table = data.get("tradesTable") if isinstance(data, dict) else None
    rows = table.get("rows") if isinstance(table, dict) else None
    if not isinstance(rows, list) or not rows:
        return None

    bars: list[tuple[datetime, float, float, float, float, int]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            date = datetime.strptime(str(row.get("date")), "%m/%d/%Y").replace(tzinfo=timezone.utc)
            prices = [
                float(re.sub(r"[^0-9.\-]", "", str(row.get(key) or "")))
                for key in ("open", "high", "low", "close")
            ]
            volume = int(re.sub(r"[^0-9]", "", str(row.get("volume") or "0")) or "0")
        except (TypeError, ValueError):
            continue
        if any(not math.isfinite(value) or value <= 0 for value in prices):
            continue
        bars.append((date, *prices, max(0, volume)))
    bars.sort(key=lambda item: item[0])
    if not bars:
        return None

    return {
        "chart": {
            "error": None,
            "result": [{
                "meta": {
                    "symbol": str(data.get("symbol") or ticker),
                    "shortName": str(data.get("symbol") or ticker),
                    "currency": "USD",
                    "fullExchangeName": "Nasdaq public market data",
                    "regularMarketPrice": bars[-1][4],
                    "regularMarketTime": int(bars[-1][0].timestamp()),
                },
                "timestamp": [int(item[0].timestamp()) for item in bars],
                "indicators": {"quote": [{
                    "open": [item[1] for item in bars],
                    "high": [item[2] for item in bars],
                    "low": [item[3] for item in bars],
                    "close": [item[4] for item in bars],
                    "volume": [item[5] for item in bars],
                }]},
            }],
        },
    }


def _fetch_nasdaq_snapshot(client: httpx.Client, ticker: str) -> dict[str, Any]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=550)
    nasdaq_ticker = ticker.replace("-", ".")
    headers = {
        "User-Agent": "Mozilla/5.0 TrendDesk/0.1",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.nasdaq.com",
        "Referer": f"https://www.nasdaq.com/market-activity/stocks/{nasdaq_ticker.lower()}/historical",
    }
    for asset_class in ("stocks", "etf"):
        response = client.get(
            NASDAQ_HISTORY_URL.format(ticker=nasdaq_ticker),
            params={
                "assetclass": asset_class,
                "fromdate": start.isoformat(),
                "todate": end.isoformat(),
                "limit": "5000",
            },
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            continue
        chart_payload = _nasdaq_chart_payload(payload, ticker)
        if chart_payload is None:
            continue
        result = build_market_snapshot(chart_payload, ticker)
        result["source"] = "Nasdaq public historical API"
        result["source_notice"] = "Nasdaq 公开日线为备用行情源，仅用于盘前仓位测算；下单前请以券商可成交价为准。"
        return result
    raise UsCalculatorError("ticker_not_found", f"未找到美股代码 {ticker}。", 404)


def fetch_us_market_snapshot(
    raw_ticker: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    ticker = normalize_ticker(raw_ticker)
    with httpx.Client(timeout=config.US_MANUAL_QUOTE_TIMEOUT_S, transport=transport) as client:
        try:
            response = client.get(
                YAHOO_CHART_URL.format(ticker=ticker),
                params={
                    "range": "1y",
                    "interval": "1d",
                    "includePrePost": "false",
                    "events": "div,splits",
                },
                headers={"User-Agent": "Mozilla/5.0 TrendDesk/0.1"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Yahoo response is not an object")
            return build_market_snapshot(payload, ticker)
        except (httpx.HTTPError, ValueError):
            pass

        try:
            return _fetch_nasdaq_snapshot(client, ticker)
        except UsCalculatorError:
            raise
        except httpx.TimeoutException as exc:
            raise UsCalculatorError("market_timeout", "美股行情请求超时，请稍后重试。", 503) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise UsCalculatorError("market_unavailable", "美股行情暂不可用，请稍后重试。", 503) from exc
