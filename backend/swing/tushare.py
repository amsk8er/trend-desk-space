"""TUSHARE 主源前复权日线（直接打 HTTP，不装 tushare 包）。

数据源决策（设计文档 §3.3）：tushare 官方接口稳定，免费号 daily + adj_factor 都可调。
存**原始价 + 绝对因子**、读时合成前复权 → 根治除权日跳空被误判成摆动低点。

- POST http://api.tushare.pro，body {"api_name","token","params","fields"}。
- 返回 {"code":0,"data":{"fields":[...],"items":[...]}}，**按日期倒序（最新在前）**。
- _parse：daily(原始 OHLCV) + adj_factor(绝对因子) 按 trade_date 合并、**反转成升序**（纯函数，可单测）。
- fetch_tushare：分别调两接口 + _parse（唯一打网络的入口），token 取 os.environ["TUSHARE_TOKEN"]。
"""
from __future__ import annotations

import os

import httpx

_API_URL = "http://api.tushare.pro"
_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
_ADJ_FIELDS = "ts_code,trade_date,adj_factor"
# ETF/基金接口（2000 积分解锁）：fund_daily 返回未复权原始价、fund_adj 是复权因子，
# 字段名与 daily/adj_factor 一致 → _parse 零改动复用。
_FUND_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
_FUND_ADJ_FIELDS = "ts_code,trade_date,adj_factor"


class ProviderError(Exception):
    """tushare 获取/解析失败（积分不足 / 空数据 / 缺 token / 非 200 等）。"""


def _fmt_date(d: str) -> str:
    """'YYYY-MM-DD' → 'YYYYMMDD'（tushare 入参格式）。"""
    return d.replace("-", "")


def _rows_of(resp: dict, what: str) -> tuple[list[str], list[list]]:
    """校验 tushare 响应、取 (fields, items)；code!=0 / 空 items → ProviderError。"""
    if not isinstance(resp, dict) or resp.get("code") != 0:
        msg = (resp or {}).get("msg") if isinstance(resp, dict) else None
        raise ProviderError(f"tushare {what} 返回错误：code={resp.get('code') if isinstance(resp, dict) else resp!r}，msg={msg!r}")
    data = resp.get("data") or {}
    items = data.get("items") or []
    if not items:
        raise ProviderError(f"tushare {what} 返回空 items（非交易区间或代码无数据）")
    return data.get("fields") or [], items


def _parse(daily_resp: dict, adj_resp: dict) -> list[dict]:
    """daily + adj_factor 两个响应 → 升序的 RawBar dict 列表（合并因子）。

    每个 dict：ts_code, trade_date('YYYY-MM-DD'), open/high/low/close, vol, amount, adj_factor。
    """
    daily_fields, daily_items = _rows_of(daily_resp, "daily")
    adj_fields, adj_items = _rows_of(adj_resp, "adj_factor")

    # adj_factor 按 trade_date(YYYYMMDD) 索引
    adj_idx = adj_fields.index("adj_factor")
    adj_date_idx = adj_fields.index("trade_date")
    factor_by_date = {row[adj_date_idx]: float(row[adj_idx]) for row in adj_items}

    col = {name: i for i, name in enumerate(daily_fields)}
    rows: list[dict] = []
    for it in daily_items:
        raw_date = it[col["trade_date"]]          # YYYYMMDD
        factor = factor_by_date.get(raw_date)
        if factor is None:
            continue  # 无对应因子的交易日跳过（极少见，缺因子无法合成 qfq）
        rows.append({
            "ts_code": it[col["ts_code"]],
            "trade_date": f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}",
            "open": float(it[col["open"]]),
            "high": float(it[col["high"]]),
            "low": float(it[col["low"]]),
            "close": float(it[col["close"]]),
            "vol": float(it[col["vol"]]),
            "amount": float(it[col["amount"]]),
            "adj_factor": factor,
        })
    if not rows:
        raise ProviderError("tushare daily/adj_factor 合并后无可用行")
    rows.sort(key=lambda r: r["trade_date"])  # 倒序 → 升序
    return rows


def _call(client: httpx.Client, token: str, api_name: str, code: str,
          start: str, end: str, fields: str) -> dict:
    body = {
        "api_name": api_name,
        "token": token,
        "params": {"ts_code": code, "start_date": _fmt_date(start), "end_date": _fmt_date(end)},
        "fields": fields,
    }
    resp = client.post(_API_URL, json=body)
    if resp.status_code != 200:
        raise ProviderError(f"tushare {api_name} 非 200：{resp.status_code}")
    return resp.json()


def _fetch(
    code: str,
    start: str,
    end: str,
    *,
    daily_api: str,
    adj_api: str,
    daily_fields: str,
    adj_fields: str,
    client: httpx.Client | None,
    timeout: float,
) -> list[dict]:
    """通用：调「日线接口 + 复权因子接口」两次 → 合并升序 RawBar dict 列表。

    个股(daily/adj_factor) 与 ETF(fund_daily/fund_adj) 共用此体——字段名一致、
    _parse 复用。token 取环境变量；client 可注入（测试用 MockTransport）。
    """
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise ProviderError("缺 TUSHARE_TOKEN（应在 secrets.env 配置并由 load_secrets_env 注入）")

    own = client is None
    cli = client or httpx.Client(timeout=timeout)
    try:
        daily_resp = _call(cli, token, daily_api, code, start, end, daily_fields)
        adj_resp = _call(cli, token, adj_api, code, start, end, adj_fields)
    except httpx.HTTPError as e:
        raise ProviderError(f"tushare 请求失败：{e}") from e
    finally:
        if own:
            cli.close()

    return _parse(daily_resp, adj_resp)


def fetch_tushare(
    code: str,
    start: str,
    end: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 15.0,
) -> list[dict]:
    """个股：daily + adj_factor → 合并升序 RawBar dict 列表。code 形如 '600519.SH'。"""
    return _fetch(code, start, end, daily_api="daily", adj_api="adj_factor",
                  daily_fields=_DAILY_FIELDS, adj_fields=_ADJ_FIELDS,
                  client=client, timeout=timeout)


def fetch_tushare_fund(
    code: str,
    start: str,
    end: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 15.0,
) -> list[dict]:
    """ETF/基金：fund_daily + fund_adj → 合并升序 RawBar dict 列表。code 形如 '159325.SZ'。

    2000 积分解锁；与个股完全同构（fund_daily 未复权原始价 + fund_adj 因子合成前复权）。
    """
    return _fetch(code, start, end, daily_api="fund_daily", adj_api="fund_adj",
                  daily_fields=_FUND_DAILY_FIELDS, adj_fields=_FUND_ADJ_FIELDS,
                  client=client, timeout=timeout)


def fetch_basic_name(code: str, *, asset: str, client: httpx.Client | None = None,
                     timeout: float = 15.0) -> str:
    """取中文名（个股 stock_basic / ETF fund_basic，按 ts_code 过滤）。

    脱离东财取名。名称是锦上添花——**任何失败一律返空串**，不抛、不影响出图。
    asset: 'fund' → fund_basic，否则 → stock_basic。
    """
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        return ""
    api = "fund_basic" if asset == "fund" else "stock_basic"
    own = client is None
    cli = client or httpx.Client(timeout=timeout)
    try:
        resp = cli.post(_API_URL, json={
            "api_name": api, "token": token,
            "params": {"ts_code": code}, "fields": "ts_code,name",
        })
        payload = resp.json() or {}
        if payload.get("code") != 0:
            return ""
        data = payload.get("data") or {}
        fields = data.get("fields") or []
        items = data.get("items") or []
        if not items or "name" not in fields:
            return ""
        return items[0][fields.index("name")] or ""
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return ""
    finally:
        if own:
            cli.close()
