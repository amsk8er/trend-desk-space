"""swing 取数层统一底座：本地缓存 + tushare 主源 + 东财兜底 + 前复权合成。

设计文档 §3.3。endpoint 只 import 本模块的 `fetch_daily`，不再直连 provider。

- rows_to_qfq_df：RawBar(原始价+因子) → 前复权 OHLCV DataFrame（纯函数）。
- upsert_bars / read_cached / is_fresh：DailyBar 缓存读写。
- fetch_daily：缓存命中 → tushare 回源落库 → 东财兜底 → 都失败抛错。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from sqlmodel import Session, select

from backend.db import DailyBar
from backend.engine import engine
from backend.swing.eastmoney import EastmoneyError
from backend.swing.eastmoney import fetch_daily as _eastmoney_fetch
from backend.swing.tushare import (
    ProviderError, fetch_basic_name, fetch_tushare, fetch_tushare_fund,
)


def _eastmoney_fallback(code: str, start: str, end: str) -> "pd.DataFrame":
    """东财兜底默认 provider：多给重试（5 次 + 指数退避），扛东财持续抽风。

    ETF 只能走东财（tushare 免费号无 ETF），首拉必须尽量成功才能落缓存——
    一旦缓存，后续同窗查询走本地不再碰东财。
    """
    return _eastmoney_fetch(code, start, end, retries=5)

# RawBar dict → DailyBar 列的字段（ts_code/trade_date 单独处理）
_BAR_COLS = ("open", "high", "low", "close", "vol", "amount", "adj_factor")

# 裸数字码首位 → 交易所后缀（A 股个股 + A 股 ETF/基金，swing 取数范围）
#   6/5 开头 → 沪市（股票 6xx；基金 51/56/58…）；0/3/1 开头 → 深市（股票 0/3；基金 15/16/18）
_SH_HEADS = ("6", "5")
_SZ_HEADS = ("0", "3", "1")

# ETF/LOF/基金代码段（数字前两位）：深市 15/16/18，沪市 50/51/52/53/55/56/58。
# 路由 fund_daily 用。按前两位精度判（非首位）——可转债 11/12 不在集合，天然排除；
# 沪市无 5 开头个股、深市无 15/16/18 开头个股，故前两位判据不与个股冲突。
_FUND_PREFIXES = frozenset({"15", "16", "18", "50", "51", "52", "53", "55", "56", "58"})


def _is_fund(code: str) -> bool:
    """判定是否 ETF/LOF/基金（→ 走 tushare fund_daily/fund_adj）。

    约定传入已 normalize_code 的带后缀码（如 '159325.SZ'）。取数字段前两位判集合；
    非 6 位数字段一律 False（让个股/未知码走 daily 或下游报错）。
    """
    if not isinstance(code, str):
        return False
    num = code.partition(".")[0]
    if len(num) != 6 or not num.isdigit():
        return False
    return num[:2] in _FUND_PREFIXES


def _tushare_dispatch(code, start, end, *, fund_fn=fetch_tushare_fund, stock_fn=fetch_tushare):
    """tushare 主源路由：ETF/基金 → fund_daily+fund_adj，个股 → daily+adj_factor。

    fund_fn/stock_fn 可注入（测试零网络验证路由）。fetch_daily 默认走此 dispatch。
    """
    provider = fund_fn if _is_fund(code) else stock_fn
    return provider(code, start, end)


def resolve_name(code, *, name_fn=fetch_basic_name) -> str:
    """取中文名（脱离东财）：ETF→fund_basic、个股→stock_basic。失败返空，不影响出图。

    约定传入已 normalize_code 的带后缀码。name_fn 可注入（测试零网络）。
    """
    asset = "fund" if _is_fund(code) else "stock"
    return name_fn(code, asset=asset)


def normalize_code(code: str) -> str:
    """裸数字码补交易所后缀；已带后缀只统一大写。

    前端可能透传用户输入的裸码（如 ETF '159325'）。tushare 用 ts_code、东财
    to_secid 都需要 '.SH/.SZ' 后缀——缺后缀时东财直接报格式错导致整体失败。
    无法识别的（非 6 位纯数字）原样返回，让下游报清晰的格式错误。
    """
    if not isinstance(code, str):
        return code
    c = code.strip().upper()
    if "." in c:
        return c
    if c.isdigit() and len(c) == 6:
        if c[0] in _SH_HEADS:
            return f"{c}.SH"
        if c[0] in _SZ_HEADS:
            return f"{c}.SZ"
    return c


def rows_to_qfq_df(rows: list[dict]) -> pd.DataFrame:
    """RawBar dict 列表（升序）→ 前复权 OHLCV DataFrame。

    前复权公式：qfq[i] = raw[i] × adj_factor[i] / adj_factor[窗口内最新]。
    对 O/H/L/C 各算一次；volume 不复权。最新 bar 的 qfq == raw。
    输出 DatetimeIndex + 列 open/high/low/close/volume（与 detector.detect 期望一致）。
    """
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    rows = sorted(rows, key=lambda r: r["trade_date"])
    latest_factor = rows[-1]["adj_factor"]

    recs = []
    dates = []
    for r in rows:
        ratio = r["adj_factor"] / latest_factor
        dates.append(r["trade_date"])
        recs.append((
            r["open"] * ratio,
            r["high"] * ratio,
            r["low"] * ratio,
            r["close"] * ratio,
            r["vol"],
        ))
    df = pd.DataFrame(recs, columns=["open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(dates)
    return df


def _bar_to_row(bar: DailyBar) -> dict:
    """DailyBar ORM → RawBar dict（与 tushare._parse 输出同形，附 source）。"""
    row = {"ts_code": bar.ts_code, "trade_date": bar.trade_date}
    for c in _BAR_COLS:
        row[c] = getattr(bar, c)
    row["source"] = bar.source or "tushare"
    return row


def upsert_bars(session: Session, code: str, rows: list[dict], source: str = "tushare") -> None:
    """按 (ts_code,trade_date) 去重写入 DailyBar；已存在则更新原始价/因子。

    防混源：同标的若已有**异源**行（如先东财后 tushare），先清掉旧行再写——
    否则 tushare 真因子(8.x) 与东财 factor=1 同窗共存，rows_to_qfq_df 按 max
    因子缩放会把东财 qfq 行 ×1/8 价格崩坏。
    调用方负责 session.commit()（与现有路由的 with Session(...) 风格一致）。
    """
    prior = session.exec(select(DailyBar).where(DailyBar.ts_code == code)).first()
    if prior is not None and (prior.source or "tushare") != source:
        for b in session.exec(select(DailyBar).where(DailyBar.ts_code == code)).all():
            session.delete(b)
        session.flush()  # 让后续按日期查不到已删行

    for r in rows:
        existing = session.exec(
            select(DailyBar).where(
                DailyBar.ts_code == code,
                DailyBar.trade_date == r["trade_date"],
            )
        ).first()
        if existing is None:
            session.add(DailyBar(
                ts_code=code, trade_date=r["trade_date"], source=source,
                **{c: r[c] for c in _BAR_COLS},
            ))
        else:
            for c in _BAR_COLS:
                setattr(existing, c, r[c])
            existing.source = source
            session.add(existing)


def _eastmoney_df_to_rows(code: str, df: pd.DataFrame) -> list[dict]:
    """东财 qfq DataFrame → RawBar dict（qfq 当 raw、adj_factor=1、amount 缺省 0）。

    东财返回的已是前复权价、无因子 → 存 factor=1，rows_to_qfq_df 读出 ratio=1
    原样返回（最新基准也是 1）。source='eastmoney' 由 upsert 标记。
    """
    rows: list[dict] = []
    for ts, row in zip(df.index, df.itertuples(index=False)):
        rows.append({
            "ts_code": code,
            "trade_date": ts.strftime("%Y-%m-%d"),
            "open": float(row.open), "high": float(row.high),
            "low": float(row.low), "close": float(row.close),
            "vol": float(row.volume), "amount": 0.0, "adj_factor": 1.0,
        })
    return rows


def read_cached(session: Session, code: str, start: str, end: str) -> list[dict]:
    """取窗口 [start,end] 内已缓存行，按 trade_date 升序返回 RawBar dict。"""
    bars = session.exec(
        select(DailyBar)
        .where(
            DailyBar.ts_code == code,
            DailyBar.trade_date >= start,
            DailyBar.trade_date <= end,
        )
        .order_by(DailyBar.trade_date)
    ).all()
    return [_bar_to_row(b) for b in bars]


def is_fresh(rows: list[dict], end: str) -> bool:
    """缓存是否覆盖到所需尾部。

    v1 简单规则：缓存最大 trade_date >= min(end, 今天) 即新鲜。
    周末/节假日 end 可能晚于最后交易日 → 用 today 兜底避免永远判缺尾。
    （后续可加 per-symbol last_attempt 优化，避免非交易日重复回源。）
    """
    if not rows:
        return False
    max_date = max(r["trade_date"] for r in rows)
    today = date.today().strftime("%Y-%m-%d")
    needed = min(end, today)
    return max_date >= needed


def fetch_daily(
    code: str,
    start: str,
    end: str,
    *,
    tushare_fn=_tushare_dispatch,
    eastmoney_fn=_eastmoney_fallback,
) -> pd.DataFrame:
    """取数层统一入口：替代 endpoint 原来 import 的 eastmoney.fetch_daily。

    顺序：
      ① 读缓存，新鲜 → rows_to_qfq_df 返回（不回源）；
      ② 否则 tushare 拉 raw+factor → upsert 落库 → 合成 qfq 返回；
      ③ tushare 失败 → 东财兜底（直接返回其 qfq df，**不缓存**）；
      ④ 都失败 → 抛 ProviderError。

    provider 可注入（测试用 fake）；返回前复权 OHLCV DataFrame（与 detector.detect 期望一致）。
    """
    code = normalize_code(code)  # 裸码补后缀，否则东财兜底因格式错失败（ETF 159325 坑）

    # ① 缓存
    with Session(engine) as s:
        cached = read_cached(s, code, start, end)
    if is_fresh(cached, end):
        return rows_to_qfq_df(cached)

    # ② tushare 主源 → 落库
    try:
        rows = tushare_fn(code, start, end)
        with Session(engine) as s:
            upsert_bars(s, code, rows)
            s.commit()
        return rows_to_qfq_df(rows)
    except ProviderError as tushare_err:
        # ③ 东财兜底（已是 qfq、无因子）→ 也入缓存，下次同窗命中本地不再打东财
        if eastmoney_fn is not None:
            try:
                df = eastmoney_fn(code, start, end)
            except EastmoneyError as east_err:
                raise ProviderError(
                    f"tushare 与东财均失败：tushare={tushare_err}；东财={east_err}"
                ) from east_err
            rows = _eastmoney_df_to_rows(code, df)
            if rows:
                with Session(engine) as s:
                    upsert_bars(s, code, rows, source="eastmoney")
                    s.commit()
            return df
        # ④ 无兜底可用
        raise
