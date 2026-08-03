"""Bitget 公共产品与报价适配器。

只有固定的公开主机和 GET 请求；没有 API key、签名、账户、订单或撤单代码。报价
仅为计划计算参考，用户仍需在 Bitget 页面核对实际可成交价格。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import threading
import time as time_module
from typing import Any

import httpx

from backend import config
from backend.us_manual.contracts import UsManualError, decimal_text


BITGET_PUBLIC_TICKERS_URL = "https://api.bitget.com/api/v3/market/tickers"
BITGET_PUBLIC_INSTRUMENTS_URL = "https://api.bitget.com/api/v3/market/instruments"
BITGET_PUBLIC_CANDLES_URL = "https://api.bitget.com/api/v3/market/candles"
BITGET_PUBLIC_HISTORY_CANDLES_URL = "https://api.bitget.com/api/v3/market/history-candles"
BITGET_CANDLE_PAGE_LIMIT = 100
BITGET_CANDLE_MAX_RANGE = timedelta(days=89)
BITGET_CANDLE_MAX_PAGES = 64
BITGET_CANDLE_MAX_WORKERS = 8
BITGET_PUBLIC_MIN_REQUEST_INTERVAL_SECONDS = 0.055
_BITGET_REQUEST_LOCK = threading.Lock()
_BITGET_LAST_REQUEST_AT = 0.0


def _pace_public_request() -> None:
    global _BITGET_LAST_REQUEST_AT
    with _BITGET_REQUEST_LOCK:
        now = time_module.monotonic()
        wait = BITGET_PUBLIC_MIN_REQUEST_INTERVAL_SECONDS - (now - _BITGET_LAST_REQUEST_AT)
        if wait > 0:
            time_module.sleep(wait)
        _BITGET_LAST_REQUEST_AT = time_module.monotonic()


def _public_get(url: str, *, params: dict[str, str],
                transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=config.US_MANUAL_QUOTE_TIMEOUT_S, transport=transport) as client:
            if transport is None:
                _pace_public_request()
            response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise UsManualError("bitget_public_unavailable", "Bitget 公共行情服务暂不可用", 503) from exc
    except ValueError as exc:
        raise UsManualError("bitget_public_contract_error", "Bitget 公共接口返回不是 JSON") from exc
    if not isinstance(payload, dict) or str(payload.get("code")) != "00000":
        raise UsManualError("bitget_public_contract_error", "Bitget 公共接口返回契约异常")
    return payload


def fetch_public_instruments(*, transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    """每日直接刷新无需鉴权的 Bitget Spot 产品清单。"""
    payload = _public_get(
        BITGET_PUBLIC_INSTRUMENTS_URL,
        params={"category": "SPOT"},
        transport=transport,
    )
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise UsManualError("bitget_public_contract_error", "Bitget 公共产品清单缺少 data 数组")
    if any(not isinstance(row, dict) for row in rows):
        raise UsManualError("bitget_public_contract_error", "Bitget 公共产品清单含非对象行")
    return rows


def _decimal(value: Any) -> Decimal:
    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise UsManualError("quote_contract_error", "Bitget 公开报价缺少有效最新价") from exc
    if not value.is_finite() or value <= 0:
        raise UsManualError("quote_contract_error", "Bitget 公开报价缺少有效最新价")
    return value


def fetch_public_quote(venue_instrument: str, *, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """读取单一、已经过本地严格交集验证的 Bitget 公开报价。"""
    if not venue_instrument or not venue_instrument.isalnum() or len(venue_instrument) > 40:
        raise UsManualError("venue_instrument_invalid", "Bitget 产品代码格式无效", 422)
    try:
        payload = _public_get(
            BITGET_PUBLIC_TICKERS_URL,
            params={"category": "SPOT", "symbol": venue_instrument},
            transport=transport,
        )
    except UsManualError as exc:
        if exc.code == "bitget_public_unavailable":
            raise UsManualError("quote_unavailable", "Bitget 公开报价暂不可用；该候选暂不能生成仓位计划", 503) from exc
        raise UsManualError("quote_contract_error", exc.message, exc.status_code) from exc
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise UsManualError("quote_unavailable", "Bitget 未返回该产品的公开报价", 404)
    row = rows[0]
    returned_symbol = str(row.get("symbol") or "").upper()
    if returned_symbol != venue_instrument.upper():
        raise UsManualError("quote_contract_error", "Bitget 公开报价产品代码不匹配")
    price = _decimal(row.get("lastPrice", row.get("lastPr", row.get("last"))))
    return {
        "venue": "bitget",
        "venue_instrument": venue_instrument.upper(),
        "reference_price": decimal_text(price),
        "quoted_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": "bitget_public_quote",
        "notice": "公开报价仅供参考；请在 Bitget 下单界面确认产品资格和实际可成交价格。",
    }


def _millis(value: datetime) -> str:
    if value.tzinfo is None:
        raise UsManualError("bitget_time_invalid", "Bitget K 线查询时间必须带时区", 422)
    return str(int(value.astimezone(timezone.utc).timestamp() * 1000))


def fetch_public_candles(
    venue_instrument: str,
    *,
    interval: str,
    start_time: datetime,
    end_time: datetime,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """读取并分页归并 rToken 公开历史 K 线；只允许 1D/1H。

    Bitget 的历史端点每页最多 100 根且单次时间范围不能超过 90 天。
    H6 的 120 自然日证据窗因此按时间段切分，并在每段内按最早返回
    时间戳向前翻页。重复边界必须内容一致，否则失败关闭。
    """
    if not venue_instrument or not venue_instrument.isalnum() or len(venue_instrument) > 40:
        raise UsManualError("venue_instrument_invalid", "Bitget 产品代码格式无效", 422)
    if interval not in {"1D", "1H"}:
        raise UsManualError("bitget_interval_invalid", "H6 只允许 Bitget 1D 或 1H K 线", 422)
    if start_time >= end_time:
        raise UsManualError("bitget_time_invalid", "Bitget K 线开始时间必须早于结束时间", 422)
    range_start_ms = int(_millis(start_time))
    range_end_ms = int(_millis(end_time))
    segment_span = timedelta(hours=95) if interval == "1H" else BITGET_CANDLE_MAX_RANGE
    segment_span_ms = int(segment_span.total_seconds() * 1000)
    segments: list[tuple[int, int]] = []
    cursor_ms = range_start_ms
    while cursor_ms < range_end_ms:
        segment_end_ms = min(range_end_ms, cursor_ms + segment_span_ms)
        segments.append((cursor_ms, segment_end_ms))
        cursor_ms = segment_end_ms

    def fetch_segment(bounds: tuple[int, int]) -> tuple[dict[int, list[Any]], int]:
        segment_start_ms, segment_end_ms = bounds
        page_end_ms = segment_end_ms
        segment_rows: dict[int, list[Any]] = {}
        pages = 0
        while page_end_ms > segment_start_ms:
            pages += 1
            if pages > BITGET_CANDLE_MAX_PAGES:
                raise UsManualError(
                    "bitget_candles_contract_error",
                    "Bitget K 线单段分页超过安全上限",
                    409,
                )
            payload = _public_get(
                BITGET_PUBLIC_HISTORY_CANDLES_URL,
                params={
                    "category": "SPOT",
                    "symbol": venue_instrument,
                    "interval": interval,
                    "startTime": str(segment_start_ms),
                    "endTime": str(page_end_ms),
                    "type": "market",
                    "limit": str(BITGET_CANDLE_PAGE_LIMIT),
                },
                transport=transport,
            )
            page = payload.get("data")
            if not isinstance(page, list):
                raise UsManualError("bitget_candles_contract_error", "Bitget K 线缺少 data 数组")
            if not page:
                break
            page_timestamps: list[int] = []
            for raw in page:
                if not isinstance(raw, list) or len(raw) < 5:
                    raise UsManualError("bitget_candles_contract_error", "Bitget K 线行字段数量不足")
                try:
                    timestamp_ms = int(str(raw[0]))
                except (TypeError, ValueError) as exc:
                    raise UsManualError("bitget_candles_contract_error", "Bitget K 线时间戳无效") from exc
                page_timestamps.append(timestamp_ms)
                if range_start_ms <= timestamp_ms <= range_end_ms:
                    previous = segment_rows.get(timestamp_ms)
                    if previous is not None and previous != raw:
                        raise UsManualError(
                            "bitget_candles_contract_error",
                            "Bitget K 线分页边界出现同时间戳不同内容",
                        )
                    segment_rows[timestamp_ms] = raw
            oldest = min(page_timestamps)
            if len(page) < BITGET_CANDLE_PAGE_LIMIT or oldest <= segment_start_ms:
                break
            # Bitget may round endTime to an interval boundary.  Deliberately
            # overlap the oldest candle by one timestamp, then verify/deduplicate
            # it above; subtracting 1 ms can skip the immediately prior candle.
            next_page_end_ms = oldest
            if next_page_end_ms >= page_end_ms:
                raise UsManualError("bitget_candles_contract_error", "Bitget K 线分页游标未前进")
            page_end_ms = next_page_end_ms
        return segment_rows, pages

    raw_by_timestamp: dict[int, list[Any]] = {}
    try:
        # Keep mocked transports deterministic; real public calls use bounded
        # concurrency well below Bitget's published 20 requests/sec/IP limit.
        workers = 1 if transport is not None else min(BITGET_CANDLE_MAX_WORKERS, len(segments))
        if workers <= 1:
            results = [fetch_segment(segment) for segment in segments]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(fetch_segment, segments))
        if sum(page_count for _, page_count in results) > BITGET_CANDLE_MAX_PAGES:
            raise UsManualError("bitget_candles_contract_error", "Bitget K 线总分页超过安全上限", 409)
        for segment_rows, _ in results:
            for timestamp_ms, raw in segment_rows.items():
                previous = raw_by_timestamp.get(timestamp_ms)
                if previous is not None and previous != raw:
                    raise UsManualError(
                        "bitget_candles_contract_error",
                        "Bitget K 线分段边界出现同时间戳不同内容",
                    )
                raw_by_timestamp[timestamp_ms] = raw
    except UsManualError as exc:
        if exc.code == "bitget_public_unavailable":
            raise UsManualError("bitget_candles_unavailable", "Bitget 公开 K 线暂不可用", 503) from exc
        if exc.code.startswith("bitget_candles_"):
            raise
        raise UsManualError("bitget_candles_contract_error", exc.message, exc.status_code) from exc
    raw_rows = [raw_by_timestamp[key] for key in sorted(raw_by_timestamp)]
    if not raw_rows:
        raise UsManualError("bitget_candles_unavailable", f"Bitget 未返回 {interval} K 线", 404)
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in raw_rows:
        if not isinstance(raw, list) or len(raw) < 5:
            raise UsManualError("bitget_candles_contract_error", "Bitget K 线行字段数量不足")
        try:
            timestamp_ms = int(str(raw[0]))
        except (TypeError, ValueError) as exc:
            raise UsManualError("bitget_candles_contract_error", "Bitget K 线时间戳无效") from exc
        if timestamp_ms in seen:
            raise UsManualError("bitget_candles_contract_error", "Bitget K 线包含重复时间戳")
        seen.add(timestamp_ms)
        rows.append({
            "timestamp": datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc),
            "open": _decimal(raw[1]),
            "high": _decimal(raw[2]),
            "low": _decimal(raw[3]),
            "close": _decimal(raw[4]),
            "volume": str(raw[5]) if len(raw) > 5 else None,
            "turnover": str(raw[6]) if len(raw) > 6 else None,
            "interval": interval,
            "venue_instrument": venue_instrument.upper(),
        })
    return sorted(rows, key=lambda row: row["timestamp"])
