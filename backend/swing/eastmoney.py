"""东财前复权日线获取（httpx）。

数据源决策：东财 HTTP（已实测可达、前复权 fqt=1），不引入 baostock / tushare。
- to_secid:      "600519.SH" → "1.600519" / "000001.SZ" → "0.000001"
- parse_klines:  东财响应 dict → OHLCV DataFrame（纯函数，可单测）
- fetch_daily:   secid + GET push2his.../kline/get + parse（唯一打网络的入口）

🔴 fqt=1（前复权）硬约束：防除权日跳空被误判成摆动低点。
"""
from __future__ import annotations

import time

import httpx
import pandas as pd

# 东财日线 K 线接口
_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# klines 字段序：日期,开,收,高,低,成交量,成交额,...
_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57"
_KLT_DAILY = 101  # 日线
_FQT_QFQ = 1      # 前复权
# 兜底降级时的指数退避（秒）：东财瞬时抖动可持续十几秒，零/短间隔重试躲不过
_BACKOFF = (0.5, 1.0, 2.0, 3.0, 5.0)

# 东财反爬：缺 UA + Referer 会被服务器直接断连（实测 curl exit 52 → 加上即 200）。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}


class EastmoneyError(Exception):
    """东财获取/解析失败（无效代码 / 空数据 / 非 200 等）。"""


def to_secid(code: str) -> str:
    """'600519.SH' → '1.600519'（沪），'000001.SZ' → '0.000001'（深）。"""
    if not isinstance(code, str) or "." not in code:
        raise EastmoneyError(f"代码格式错误（需形如 600519.SH）：{code!r}")
    num, _, suffix = code.partition(".")
    suffix = suffix.upper()
    market = {"SH": "1", "SZ": "0"}.get(suffix)
    if market is None or not num.isdigit():
        raise EastmoneyError(f"无法识别的代码：{code!r}")
    return f"{market}.{num}"


def parse_klines(payload: dict) -> pd.DataFrame:
    """东财响应 dict → DataFrame（DatetimeIndex，列 open/high/low/close/volume）。

    东财 klines 每行「日期,开,收,高,低,量,额」，重排成 OHLCV。
    """
    data = (payload or {}).get("data")
    if not data:
        raise EastmoneyError("东财返回空 data（代码无效或无该区间数据）")
    klines = data.get("klines") or []
    if not klines:
        raise EastmoneyError("东财返回空 klines（非交易区间或代码无数据）")

    rows = []
    dates = []
    for line in klines:
        parts = line.split(",")
        # 日期, 开, 收, 高, 低, 量, ...
        d, o, c, h, low, vol = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
        dates.append(d)
        rows.append((float(o), float(h), float(low), float(c), float(vol)))

    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(dates)
    return df


def fetch_daily(
    code: str,
    start: str,
    end: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
    retries: int = 3,
    _sleep=time.sleep,
) -> pd.DataFrame:
    """拉前复权日线 → OHLCV DataFrame。

    code 形如 '600519.SH'；start/end 形如 'YYYY-MM-DD'。
    client 可注入（测试用 mock transport）；不传则临时建一个。
    retries：东财间歇性断连时的额外重试次数（实测会抽风）。
    _sleep：退避用，可注入 no-op 便于测试（默认 time.sleep）。
    """
    secid = to_secid(code)
    params = {
        "secid": secid,
        "klt": _KLT_DAILY,
        "fqt": _FQT_QFQ,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": _FIELDS2,
        "beg": start.replace("-", ""),
        "end": end.replace("-", ""),
    }

    own = client is None
    cli = client or httpx.Client(timeout=timeout)
    try:
        last_err: httpx.HTTPError | None = None
        attempts = max(1, retries)
        for attempt in range(attempts):
            try:
                resp = cli.get(_KLINE_URL, params=params, headers=_HEADERS)
                break
            except httpx.HTTPError as e:
                last_err = e  # 间歇断连 → 指数退避后重试
                if attempt < attempts - 1:
                    _sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
        else:
            raise EastmoneyError(f"东财请求失败（重试 {retries} 次）：{last_err}") from last_err
    finally:
        if own:
            cli.close()

    if resp.status_code != 200:
        raise EastmoneyError(f"东财非 200：{resp.status_code}")
    return parse_klines(resp.json())


def fetch_name(
    code: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
    retries: int = 3,
    _sleep=time.sleep,
) -> str:
    """取股票/ETF 中文名（东财 kline 响应 data.name，个股+ETF 都覆盖）。

    名称是信息面板的锦上添花——**任何失败都吞掉返回空串**，不让取名拖垮整页。
    只拉一小段（近几日）即可拿到 name；带退避抵御东财瞬时断连。
    code 需已带后缀（endpoint 调 normalize_code 后再传）。
    """
    try:
        secid = to_secid(code)
    except EastmoneyError:
        return ""
    params = {
        "secid": secid, "klt": _KLT_DAILY, "fqt": _FQT_QFQ,
        "fields1": "f1,f2,f3,f4,f5,f6", "fields2": _FIELDS2,
        "beg": "0", "end": "20500101", "lmt": "1",
    }
    own = client is None
    cli = client or httpx.Client(timeout=timeout)
    try:
        attempts = max(1, retries)
        for attempt in range(attempts):
            try:
                resp = cli.get(_KLINE_URL, params=params, headers=_HEADERS)
                break
            except httpx.HTTPError:
                if attempt < attempts - 1:
                    _sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
        else:
            return ""
    finally:
        if own:
            cli.close()

    if resp.status_code != 200:
        return ""
    data = (resp.json() or {}).get("data") or {}
    return data.get("name") or ""
