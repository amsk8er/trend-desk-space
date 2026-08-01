"""Trend Desk 自带的 Wind MCP 只读客户端。

运行时只从服务端 ``WIND_API_KEY`` 环境变量读取密钥，不依赖开发机 skill 目录。
客户端固定访问 Wind 官方 stock/fund MCP，且只暴露 H5 所需的身份解析与前复权日线。
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from backend import config
from backend.us_manual.contracts import UsManualError, canonical_json, sha256


WIND_ENDPOINTS = {
    "stock_data": "https://mcp.wind.com.cn/vserver_stock_data/mcp/",
    "fund_data": "https://mcp.wind.com.cn/vserver_fund_data/mcp/",
}
WIND_CLIENT_VERSION = "trend-desk-us-h5-v2"


def _parse_sse(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise UsManualError("wind_contract_error", "Wind MCP 返回无效 JSON") from exc
        if isinstance(payload, dict):
            return payload
    last: str | None = None
    for line in text.splitlines():
        if line.startswith("data: "):
            last = line[6:]
    if last is None:
        raise UsManualError("wind_contract_error", "Wind MCP 返回既非 JSON 也非 SSE")
    try:
        payload = json.loads(last)
    except json.JSONDecodeError as exc:
        raise UsManualError("wind_contract_error", "Wind MCP SSE data 不是 JSON") from exc
    if not isinstance(payload, dict):
        raise UsManualError("wind_contract_error", "Wind MCP 返回顶层不是对象")
    return payload


def _inner_payload(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("isError"):
        message = str(((result.get("content") or [{}])[0] or {}).get("text") or "Wind 工具执行失败")
        raise UsManualError("wind_tool_error", message[:500], 503)
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        raise UsManualError("wind_contract_error", "Wind MCP 结果缺少 content")
    text = content[0].get("text")
    if not isinstance(text, str):
        raise UsManualError("wind_contract_error", "Wind MCP content 缺少文本")
    try:
        inner = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsManualError("wind_contract_error", "Wind 工具结果不是 JSON") from exc
    if not isinstance(inner, dict):
        raise UsManualError("wind_contract_error", "Wind 工具结果顶层不是对象")
    error = inner.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise UsManualError("wind_tool_error", f"Wind 工具失败：{message}", 503)
    if isinstance(inner.get("mcp_tool_error_code"), int) and inner["mcp_tool_error_code"] != 0:
        raise UsManualError(
            "wind_tool_error",
            str(inner.get("mcp_tool_error_msg") or "Wind 工具失败")[:500],
            503,
        )
    return inner


def _table_rows(inner: dict[str, Any]) -> list[dict[str, Any]]:
    data = inner.get("data")
    if isinstance(data, list):
        if any(not isinstance(row, dict) for row in data):
            raise UsManualError("wind_contract_error", "Wind data 数组包含非对象行")
        return list(data)
    if not isinstance(data, dict):
        raise UsManualError("wind_contract_error", "Wind 结果缺少 data")
    table = data
    # Wind 线上 MCP 的 NL 与 K 线工具会把标准表格包装在
    # ``data.data[0]``；Mock/旧响应则可能直接返回 ``data.columns/rows``。
    nested = data.get("data")
    if isinstance(nested, list):
        if len(nested) != 1 or not isinstance(nested[0], dict):
            raise UsManualError("wind_contract_error", "Wind 返回的表格数量不唯一")
        table = nested[0]
    columns = table.get("columns")
    rows = table.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise UsManualError("wind_contract_error", "Wind 表格缺少 columns/rows")
    names: list[str] = []
    for column in columns:
        name = column.get("name") if isinstance(column, dict) else column
        if not isinstance(name, str) or not name:
            raise UsManualError("wind_contract_error", "Wind 表格列名无效")
        names.append(name)
    out: list[dict[str, Any]] = []
    for values in rows:
        if not isinstance(values, list) or len(values) != len(names):
            raise UsManualError("wind_contract_error", "Wind 表格行列数不一致")
        out.append(dict(zip(names, values, strict=True)))
    return out


def _first(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise UsManualError("wind_daily_contract_error", f"Wind {field} 不是有效数值") from exc
    if not number.is_finite() or number <= 0:
        raise UsManualError("wind_daily_contract_error", f"Wind {field} 不是有限正数")
    return number


def _identity_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


class WindMcpClient:
    """固定路由的 Wind MCP client；测试可注入 MockTransport 和假 key。"""

    def __init__(self, *, transport: httpx.BaseTransport | None = None,
                 api_key: str | None = None):
        self.transport = transport
        self._api_key = (api_key if api_key is not None else os.getenv("WIND_API_KEY", "")).strip()
        if not self._api_key:
            raise UsManualError("wind_not_configured", "服务端未配置 WIND_API_KEY", 503)

    def _request(self, server_type: str, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        endpoint = WIND_ENDPOINTS.get(server_type)
        if endpoint is None:
            raise UsManualError("wind_route_invalid", "H5 不允许该 Wind MCP 路由", 422)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            with httpx.Client(timeout=timeout, transport=self.transport) as client:
                response = client.post(endpoint, headers=headers, content=canonical_json(body))
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise UsManualError("wind_timeout", "Wind MCP 请求超时", 503) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            code = "wind_auth_error" if status in {401, 403} else "wind_unavailable"
            raise UsManualError(code, f"Wind MCP HTTP {status}", 503) from exc
        except httpx.HTTPError as exc:
            raise UsManualError("wind_unavailable", "Wind MCP 网络不可用", 503) from exc
        payload = _parse_sse(response.text)
        if payload.get("error"):
            error = payload["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise UsManualError("wind_rpc_error", f"Wind MCP RPC 失败：{message}", 503)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise UsManualError("wind_contract_error", "Wind MCP 响应缺少 result")
        return result

    def call(self, server_type: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._request(
            server_type,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "trend-desk", "version": WIND_CLIENT_VERSION},
            },
            timeout=min(config.WIND_MCP_TIMEOUT_S, 30),
        )
        result = self._request(
            server_type,
            "tools/call",
            {"name": tool_name, "arguments": arguments, "_meta": {"clientVersion": WIND_CLIENT_VERSION}},
            timeout=config.WIND_MCP_TIMEOUT_S,
        )
        return {"inner": _inner_payload(result), "result_sha256": sha256(result)}

    def resolve_symbol(self, *, asset_type: str, ticker_symbol: str,
                       ticker_name: str | None = None) -> dict[str, Any]:
        ticker = ticker_symbol.strip().upper()
        # Wind 工具契约允许 NER 解析裸美股 ticker。已指定单只标的时不能改走
        # 全市场筛选，也不能自行补交易所后缀；名称不进入问句，避免名称变化
        # 造成同一 ticker 的身份与缓存漂移。
        del ticker_name
        if asset_type == "stock":
            server_type = "stock_data"
            tool_name = "get_stock_basicinfo"
            question = f"{ticker}公司基本档案"
        elif asset_type == "etf":
            server_type = "fund_data"
            tool_name = "get_fund_info"
            question = f"{ticker}基金档案"
        else:
            raise UsManualError("wind_asset_type_invalid", "Wind H5 只支持美股个股或 ETF", 422)
        response = self.call(server_type, tool_name, {"question": question})
        rows = _table_rows(response["inner"])
        exact: list[tuple[dict[str, Any], str]] = []
        for row in rows:
            code = _first(row, ("Wind代码", "WIND代码", "wind_code", "windCode", "WINDCODE"))
            if not isinstance(code, str) or "." not in code:
                continue
            currency = _first(row, (
                "币种", "货币", "注册资本币种", "单位净值币种",
                "currency", "Currency",
            ))
            if currency is not None and str(currency).strip().upper() not in {"美元", "USD", "US DOLLAR"}:
                continue
            if _identity_key(code.rsplit(".", 1)[0]) == _identity_key(ticker):
                exact.append((row, code.upper()))
        unique_codes = sorted({code for _, code in exact})
        if len(unique_codes) != 1:
            raise UsManualError(
                "wind_symbol_unresolved",
                "Wind 返回结果不能唯一确认该美股标的；已阻断，不会猜测交易所后缀",
                409,
                {"ticker_symbol": ticker, "matched_codes": unique_codes},
            )
        matched_row = next(row for row, code in exact if code == unique_codes[0])
        exact_name = _first(matched_row, (
            "证券简称", "基金简称", "中文简称", "名称", "name",
        ))
        # Wind 的美国 ETF 基础资料会返回唯一标准代码（例如 XLV.OF），
        # 但 fund kline 对该代码可能无行；线上契约能够用同一身份行返回的
        # 精确证券简称定位。这里只复用 Wind 自己返回的名称，不做搜索或模糊匹配。
        if asset_type == "etf" and isinstance(exact_name, str) and exact_name.strip():
            kline_locator = exact_name.strip()
            kline_locator_source = "identity_exact_name"
        else:
            kline_locator = unique_codes[0]
            kline_locator_source = "standard_code"
        return {
            "server_type": server_type,
            "tool_name": tool_name,
            "question_hash": sha256(question),
            "wind_symbol": unique_codes[0],
            "kline_locator": kline_locator,
            "kline_locator_source": kline_locator_source,
            "identity": matched_row,
            "response_sha256": response["result_sha256"],
            "raw_response": response["inner"],
        }

    def daily_bars(self, *, asset_type: str, wind_symbol: str,
                   begin_date: date, end_date: date) -> dict[str, Any]:
        if asset_type == "stock":
            server_type, tool_name = "stock_data", "get_stock_kline"
        elif asset_type == "etf":
            server_type, tool_name = "fund_data", "get_fund_kline"
        else:
            raise UsManualError("wind_asset_type_invalid", "Wind H5 只支持美股个股或 ETF", 422)
        arguments = {
            "windcode": wind_symbol,
            "begin_date": begin_date.strftime("%Y%m%d"),
            "end_date": end_date.strftime("%Y%m%d"),
            "period": "10",
            "aftime": "0",
            "issusp": "0",
        }
        response = self.call(server_type, tool_name, arguments)
        rows = _table_rows(response["inner"])
        normalized: list[dict[str, Any]] = []
        for row in rows:
            raw_time = _first(row, ("TIME", "time", "日期", "TRADE_DT"))
            try:
                trade_date = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00")).date()
            except ValueError as exc:
                raise UsManualError("wind_daily_contract_error", "Wind 日线日期无效") from exc
            normalized.append({
                "date": trade_date,
                "open": _decimal(_first(row, ("OPEN", "open", "开盘价")), field="open"),
                "close": _decimal(_first(row, ("MATCH", "CLOSE", "close", "收盘价")), field="close"),
                "high": _decimal(_first(row, ("HIGH", "high", "最高价")), field="high"),
                "low": _decimal(_first(row, ("LOW", "low", "最低价")), field="low"),
            })
        if not normalized:
            raise UsManualError("wind_daily_unavailable", "Wind 未返回前复权日线", 409)
        normalized.sort(key=lambda row: row["date"])
        return {
            "server_type": server_type,
            "tool_name": tool_name,
            "wind_symbol": wind_symbol,
            "query_symbol": wind_symbol,
            "begin_date": begin_date,
            "end_date": end_date,
            "adjustment": "forward",
            "bars": normalized,
            "response_sha256": response["result_sha256"],
            "raw_response": response["inner"],
        }
