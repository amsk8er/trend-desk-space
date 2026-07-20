# backend/api/swing.py
# GET /api/swing —— 重要低点标注页的数据源。
# 输代码+区间 → 东财前复权日线 → detect() → 组装贴 lightweight-charts 的 JSON。
# 与现有读路由（read.py）一致：纯函数 build_swing_response 可单测，HTTP 包装薄。
from datetime import date, timedelta

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from backend.swing.detector import detect
from backend.swing.data import fetch_daily, normalize_code, resolve_name
from backend.swing.eastmoney import EastmoneyError
from backend.swing.tushare import ProviderError

router = APIRouter(prefix="/api")

_DATE_FMT = "%Y-%m-%d"


def _fmt(ts) -> str:
    """DatetimeIndex 元素 → 'YYYY-MM-DD'（贴 lightweight-charts time 格式）。"""
    return ts.strftime(_DATE_FMT)


def build_swing_response(df, *, code: str, start: str, end: str,
                         k: int = 2, breakout_pct: float = 0.0,
                         name: str = "") -> dict:
    """OHLCV DataFrame → 前端可直接渲染的标注 JSON（纯函数，不打网络）。"""
    result = detect(df, k=k, breakout_pct=breakout_pct)
    index = list(df.index)

    ohlc = [
        {
            "time": _fmt(ts),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
        }
        for ts, row in zip(index, df.itertuples(index=False))
    ]

    def _lows(swings):
        return [{"time": _fmt(s.date), "price": float(s.price)} for s in swings]

    stop_ladder = [
        {"time": _fmt(index[pos]), "stop": float(stop)}
        for pos, stop in result["stop_ladder"]
    ]

    return {
        "code": code,
        "name": name,
        "start": start,
        "end": end,
        "ohlc": ohlc,
        "important_lows": _lows(result["important_lows"]),
        "minor_lows": _lows(result["minor_lows"]),
        "stop_ladder": stop_ladder,
    }


@router.get("/swing")
def api_swing(
    code: str = Query(..., description="股票代码，形如 600519.SH"),
    start: str | None = Query(None, description="起始日 YYYY-MM-DD，默认 today−365"),
    end: str | None = Query(None, description="结束日 YYYY-MM-DD，默认 today"),
    k: int = Query(2, ge=1, description="fractal 半径"),
    breakout_pct: float = Query(0.0, ge=0.0, description="强势突破阈值（方案②）"),
):
    today = date.today()
    start = start or (today - timedelta(days=365)).strftime(_DATE_FMT)
    end = end or today.strftime(_DATE_FMT)
    code = normalize_code(code)  # 裸码补后缀，name/取数都用规范化代码
    try:
        df = fetch_daily(code, start, end)
    except (ProviderError, EastmoneyError) as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    name = resolve_name(code)  # tushare 取名（ETF→fund_basic、个股→stock_basic）；失败返空
    return build_swing_response(df, code=code, name=name, start=start, end=end,
                                k=k, breakout_pct=breakout_pct)
