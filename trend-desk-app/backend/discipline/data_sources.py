"""纪律系统数据权限探针：真实优先，失败原因显式，不做静默替代。"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta

import httpx

from backend.trend_animals.client import TrendAnimalsClient
from backend.trend_animals.errors import TrendAnimalsError

TUSHARE_URL = "http://api.tushare.pro"
FUND_PREFIXES = frozenset({"15", "16", "18", "50", "51", "52", "53", "55", "56", "58"})


def normalize_etf_benchmark(value: str | None) -> str | None:
    """把 fund_basic 业绩基准归一为可审计的同指数分组键。"""
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("（", "(").replace("）", ")").replace("％", "%")
    text = re.sub(r"\([^)]*(?:汇率|估值)[^)]*\)", "", text)
    text = re.sub(r"(?:收益率)?[×xX*]\s*100%", "", text)
    text = text.replace("收益率", "")
    text = re.sub(r"\s+", "", text).strip("+；;，,")
    return text or None


def normalize_tushare_code(code: str) -> str:
    """把趋势动物裸码/OF 码转换为 Tushare 使用的交易所代码。"""
    value = str(code or "").strip().upper()
    bare = value.split(".", 1)[0]
    if len(bare) != 6 or not bare.isdigit():
        return value
    if bare[0] in {"5", "6"}:
        return f"{bare}.SH"
    if bare[0] in {"0", "1", "3"}:
        return f"{bare}.SZ"
    return value


def is_fund_code(code: str) -> bool:
    bare = str(code or "").split(".", 1)[0]
    return len(bare) == 6 and bare.isdigit() and bare[:2] in FUND_PREFIXES


class TushareProbeClient:
    def __init__(self, token: str | None = None, *, client: httpx.Client | None = None,
                 timeout: float = 20.0):
        self.token = token or os.getenv("TUSHARE_TOKEN", "")
        self._client = client or httpx.Client(timeout=timeout)
        self._own = client is None

    def close(self):
        if self._own:
            self._client.close()

    def call(self, api_name: str, *, params: dict, fields: str) -> list[dict]:
        if not self.token:
            raise RuntimeError("not_configured")
        response = self._client.post(TUSHARE_URL, json={
            "api_name": api_name, "token": self.token, "params": params, "fields": fields,
        })
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"api_error:{payload.get('code')}:{payload.get('msg') or ''}")
        data = payload.get("data") or {}
        names, items = data.get("fields") or [], data.get("items") or []
        return [dict(zip(names, row)) for row in items]

    def probe(self, *, end_date: str | None = None) -> dict:
        end = (end_date or date.today().strftime("%Y%m%d")).replace("-", "")
        start = (date(int(end[:4]), int(end[4:6]), int(end[6:])) - timedelta(days=20)).strftime("%Y%m%d")
        result = {"configured": bool(self.token), "interfaces": {}, "latest_trade_date": None}
        if not self.token:
            result["status"] = "not_configured"; return result
        probes = [
            ("trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end},
             "exchange,cal_date,is_open,pretrade_date"),
            ("stock_basic", {"exchange": "", "list_status": "L"},
             "ts_code,symbol,name,market,list_status"),
            ("fund_basic", {"market": "E"},
             "ts_code,name,management,custodian,fund_type,benchmark,status"),
        ]
        calendar = []
        for api, params, fields in probes:
            try:
                rows = self.call(api, params=params, fields=fields)
                result["interfaces"][api] = {"status": "available", "rows": len(rows)}
                if api == "trade_cal":
                    calendar = rows
            except Exception as exc:
                result["interfaces"][api] = {"status": "unavailable", "reason": type(exc).__name__}
        open_days = sorted(str(x["cal_date"]) for x in calendar if int(x.get("is_open") or 0) == 1)
        if open_days:
            result["latest_trade_date"] = open_days[-1]
            trade_date = open_days[-1]
            for api, fields in [
                ("daily_basic", "ts_code,trade_date,close,turnover_rate,circ_mv,total_mv"),
                ("daily", "ts_code,trade_date,close,amount"),
                ("suspend_d", "ts_code,trade_date,suspend_type"),
                ("stk_limit", "ts_code,trade_date,up_limit,down_limit"),
            ]:
                try:
                    rows = self.call(api, params={"trade_date": trade_date}, fields=fields)
                    result["interfaces"][api] = {"status": "available", "rows": len(rows),
                                                   "as_of_date": trade_date}
                except Exception as exc:
                    result["interfaces"][api] = {"status": "unavailable", "reason": type(exc).__name__}
        result["status"] = "available" if result["interfaces"].get("trade_cal", {}).get("status") == "available" else "degraded"
        return result

    def is_trade_day(self, trade_date: str) -> bool:
        day = trade_date.replace("-", "")
        rows = self.call(
            "trade_cal", params={"exchange": "SSE", "start_date": day, "end_date": day},
            fields="exchange,cal_date,is_open,pretrade_date",
        )
        return any(str(row.get("cal_date")) == day and int(row.get("is_open") or 0) == 1
                   for row in rows)

    def next_trade_day(self, trade_date: str) -> str:
        current = date.fromisoformat(trade_date)
        start = (current + timedelta(days=1)).strftime("%Y%m%d")
        end = (current + timedelta(days=14)).strftime("%Y%m%d")
        rows = self.call(
            "trade_cal", params={"exchange": "SSE", "start_date": start, "end_date": end},
            fields="exchange,cal_date,is_open,pretrade_date",
        )
        open_days = sorted(str(row.get("cal_date")) for row in rows
                           if int(row.get("is_open") or 0) == 1 and row.get("cal_date"))
        if not open_days:
            raise RuntimeError("next_trade_day_unavailable")
        value = open_days[0]
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"

    def _latest_fund_share(
        self, code: str, *, day: str, share_start: str,
    ) -> dict | None:
        """取截至 day 的最新基金份额；兼容日频 ETF 与季度/滞后 LOF。"""
        attempts = (
            {"ts_code": code, "start_date": share_start, "end_date": day},
            {"ts_code": code, "end_date": day},
            {"ts_code": code},
        )
        for params in attempts:
            try:
                shares = self.call(
                    "fund_share", params=params, fields="ts_code,trade_date,fd_share",
                )
            except Exception:
                shares = []
            eligible = [
                row for row in shares
                if str(row.get("trade_date") or "") and str(row.get("trade_date")) <= day
            ]
            if eligible:
                return max(eligible, key=lambda row: str(row.get("trade_date") or ""))
        return None

    def fetch_daily_facts(self, *, trade_date: str, codes: list[str]) -> list[dict]:
        """只返回本次纪律数据集涉及的A股/境内ETF事实；接口缺项保留空值。"""
        day = trade_date.replace("-", "")
        wanted = {normalize_tushare_code(code) for code in codes if code}
        stocks = {code for code in wanted if not is_fund_code(code)}
        funds = wanted - stocks
        merged: dict[str, dict] = {code: {"ts_code": code, "trade_date": trade_date,
                                          "raw_payload": {}} for code in wanted}

        def absorb(api: str, rows: list[dict]) -> None:
            for row in rows:
                code = normalize_tushare_code(str(row.get("ts_code") or ""))
                if code not in merged:
                    continue
                merged[code]["raw_payload"][api] = row

        if stocks:
            daily = self.call("daily", params={"trade_date": day},
                              fields="ts_code,trade_date,close,amount")
            basic = self.call("daily_basic", params={"trade_date": day},
                              fields="ts_code,trade_date,close,circ_mv,total_mv")
            absorb("daily", daily); absorb("daily_basic", basic)
        else:
            daily, basic = [], []
        try:
            suspend = self.call("suspend_d", params={"trade_date": day},
                                fields="ts_code,trade_date,suspend_type")
        except Exception:
            suspend = []
        try:
            limits = self.call("stk_limit", params={"trade_date": day},
                               fields="ts_code,trade_date,up_limit,down_limit")
        except Exception:
            limits = []
        absorb("suspend_d", suspend); absorb("stk_limit", limits)

        fund_daily: list[dict] = []
        if funds:
            try:
                fund_meta = self.call(
                    "fund_basic", params={"market": "E", "status": "L"},
                    fields="ts_code,name,fund_type,benchmark,status",
                )
            except Exception:
                fund_meta = []
            absorb("fund_basic", fund_meta)
            try:
                fund_daily = self.call("fund_daily", params={"trade_date": day},
                                       fields="ts_code,trade_date,close,amount")
            except Exception:
                fund_daily = []
            absorb("fund_daily", fund_daily)
            # fund_share：ETF 多为日频，LOF/定开常仅季度披露；部分代码在 tushare 还有披露滞后。
            # 先用 800 天窗口（覆盖约 8 个季报），空结果再无 start_date 回退并本地过滤 end_date。
            share_start = (date.fromisoformat(trade_date) - timedelta(days=800)).strftime("%Y%m%d")
            for code in funds:
                latest = self._latest_fund_share(code, day=day, share_start=share_start)
                if latest:
                    merged[code]["raw_payload"]["fund_share"] = latest

        stock_daily = {normalize_tushare_code(str(x.get("ts_code"))): x for x in daily}
        stock_basic = {normalize_tushare_code(str(x.get("ts_code"))): x for x in basic}
        fund_by_code = {normalize_tushare_code(str(x.get("ts_code"))): x for x in fund_daily}
        suspended = {normalize_tushare_code(str(x.get("ts_code"))) for x in suspend}
        limit_by_code = {
            normalize_tushare_code(str(x.get("ts_code"))): x for x in limits
        }
        out: list[dict] = []
        for code, item in merged.items():
            price_row = fund_by_code.get(code) or stock_daily.get(code) or {}
            basic_row = stock_basic.get(code) or {}
            share_row = item["raw_payload"].get("fund_share") or {}
            fund_meta_row = item["raw_payload"].get("fund_basic") or {}
            close = price_row.get("close") or basic_row.get("close")
            amount = price_row.get("amount")
            # Tushare daily/fund_daily amount 单位千元；换算为亿元。
            amount_yi = float(amount) / 100000.0 if isinstance(amount, (int, float)) else None
            circ_mv = basic_row.get("circ_mv")
            # daily_basic circ_mv 单位万元；换算为亿元。
            float_cap = float(circ_mv) / 10000.0 if isinstance(circ_mv, (int, float)) else None
            fd_share = share_row.get("fd_share")
            # fund_share fd_share 单位万份，近似规模=份额×收盘价。
            fund_size = (float(fd_share) * float(close) / 10000.0
                         if isinstance(fd_share, (int, float)) and isinstance(close, (int, float)) else None)
            limit = limit_by_code.get(code) or {}
            out.append({**item, "close": float(close) if isinstance(close, (int, float)) else None,
                        "amount_yi": amount_yi, "float_market_cap_yi": float_cap,
                        "fund_size_yi": fund_size,
                        "benchmark": fund_meta_row.get("benchmark"),
                        "benchmark_key": normalize_etf_benchmark(fund_meta_row.get("benchmark")),
                        "suspended": code in suspended,
                        "up_limit": limit.get("up_limit"), "down_limit": limit.get("down_limit"),
                        "source_dates": {"tushare": trade_date}})
        return out


def probe_trend_animals(client: TrendAnimalsClient) -> dict:
    result = {"configured": client.configured, "interfaces": {}}
    if not client.configured:
        return {**result, "status": "not_configured"}
    calls = [
        ("api_doc", client.get_api_doc_intro),
        ("update_status", client.get_update_status),
        ("snapshot_billing", client.get_snapshot_billing),
    ]
    statuses = None
    for name, fn in calls:
        try:
            rows = fn()
            count = len(rows) if isinstance(rows, list) else 1
            result["interfaces"][name] = {"status": "available", "rows": count}
            if name == "update_status":
                statuses = rows
        except TrendAnimalsError as exc:
            result["interfaces"][name] = {"status": "unavailable", "reason": exc.code}
    if isinstance(statuses, list):
        result["as_of_dates"] = {
            str(x.get("asset")): x.get("asOfDate") for x in statuses
            if isinstance(x, dict) and x.get("asset")
        }
    result["status"] = "available" if result["interfaces"].get("update_status", {}).get("status") == "available" else "degraded"
    return result
