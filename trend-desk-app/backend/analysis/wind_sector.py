"""Purpose-built Wind index validation for the top A-share industries.

The route is deliberately narrow: one industry name per
``index_data.get_index_price_indicators`` call.  Trend Animals remains the
source of the proprietary temperature/strength/phase signals.  The default
runtime uses the user's local ``wind-mcp-skill`` CLI; the HTTP client remains
only as an explicit ``WIND_VALIDATION_MODE=direct`` compatibility path.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from backend import config


ENDPOINT = "https://mcp.wind.com.cn/vserver_index_data/mcp/"
CLIENT_VERSION = "trend-desk-industry-heat-v1"
INDICATORS = (
    "5日涨跌幅,20日涨跌幅,成交额,量比,上涨家数,下跌家数,平盘家数,"
    "当日主力净流入额,当日主力净流入占比"
)


class IndustryWindError(RuntimeError):
    pass


class WindSkillAuthError(IndustryWindError):
    pass


class WindSkillMappingError(IndustryWindError):
    pass


class WindSkillRuntimeError(IndustryWindError):
    pass


def configured() -> bool:
    if config.WIND_VALIDATION_MODE == "off":
        return False
    if config.WIND_VALIDATION_MODE == "direct":
        return bool(os.getenv("WIND_API_KEY", "").strip())
    return bool(shutil.which("node") and (config.WIND_SKILL_DIR / "scripts" / "cli.mjs").exists())


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _parse_envelope(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("{"):
        payload = json.loads(stripped)
        if isinstance(payload, dict):
            return payload
    events = [line[6:] for line in text.splitlines() if line.startswith("data: ")]
    if not events:
        raise IndustryWindError("Wind 返回既非 JSON 也非 SSE")
    payload = json.loads(events[-1])
    if not isinstance(payload, dict):
        raise IndustryWindError("Wind 返回顶层不是对象")
    return payload


def _inner(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("isError"):
        message = str(((result.get("content") or [{}])[0] or {}).get("text") or "Wind 工具失败")
        raise IndustryWindError(message[:500])
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        raise IndustryWindError("Wind 结果缺少 content")
    payload = json.loads(str(content[0].get("text") or "{}"))
    if not isinstance(payload, dict):
        raise IndustryWindError("Wind 工具结果不是对象")
    if payload.get("error"):
        raise IndustryWindError(str(payload["error"])[:500])
    if isinstance(payload.get("mcp_tool_error_code"), int) and payload["mcp_tool_error_code"] != 0:
        raise IndustryWindError(str(payload.get("mcp_tool_error_msg") or "Wind 工具失败")[:500])
    return payload


def _rows(inner: dict[str, Any]) -> list[dict[str, Any]]:
    data = inner.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        raise IndustryWindError("Wind 结果缺少 data")
    table = data
    if isinstance(data.get("data"), list):
        nested = [row for row in data["data"] if isinstance(row, dict)]
        if len(nested) != 1:
            raise IndustryWindError("Wind 返回表格数量不唯一")
        table = nested[0]
    columns, values = table.get("columns"), table.get("rows")
    if not isinstance(columns, list) or not isinstance(values, list):
        raise IndustryWindError("Wind 表格缺少 columns/rows")
    names = [str(column.get("name") if isinstance(column, dict) else column) for column in columns]
    return [dict(zip(names, row, strict=True)) for row in values
            if isinstance(row, list) and len(row) == len(names)]


def _metrics_from_inner(inner: dict[str, Any]) -> dict[str, float | None]:
    rows = _rows(inner)
    if not rows:
        raise IndustryWindError("Wind 未返回行业指数指标")
    row = rows[0]
    return {
        "return_5d_pct": _pick(row, "5日涨跌幅"),
        "return_20d_pct": _pick(row, "20日涨跌幅"),
        "turnover_amount": _pick(row, "成交额"),
        "volume_ratio": _pick(row, "量比"),
        "advancers": _pick(row, "上涨家数"),
        "decliners": _pick(row, "下跌家数"),
        "unchanged": _pick(row, "平盘家数"),
        "main_inflow_amount": _pick(row, "当日主力净流入额"),
        "main_inflow_ratio_pct": _pick(row, "当日主力净流入占比"),
    }


def _validation_result(inner: dict[str, Any], *, wind_code: str, wind_name: str | None = None) -> dict[str, Any]:
    metrics = _metrics_from_inner(inner)
    scored = score_metrics(metrics)
    if not scored["coverage"]:
        raise IndustryWindError("Wind 行业指数未返回可用的量价、广度或资金指标")
    return {
        "status": "verified" if scored["coverage"] >= 0.75 else "partial",
        "score": scored["score"], "coverage": scored["coverage"],
        "metrics": metrics, "components": scored["components"],
        "response_hash": _hash(inner), "verified_at": datetime.utcnow(),
        "wind_code": wind_code, "wind_name": wind_name,
    }


def _number(value: Any) -> float | None:
    if value in (None, "", "--", "—"):
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    multiplier = 1.0
    if text.endswith("亿"):
        multiplier, text = 100_000_000.0, text[:-1]
    elif text.endswith("万"):
        multiplier, text = 10_000.0, text[:-1]
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group()) * multiplier
    return number if math.isfinite(number) else None


def _pick(row: dict[str, Any], name: str) -> float | None:
    if name in row:
        return _number(row[name])
    normalized = name.replace("%", "").lower()
    for key, value in row.items():
        if str(key).replace("%", "").lower() == normalized:
            return _number(value)
    return None


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def score_metrics(metrics: dict[str, float | None]) -> dict[str, Any]:
    components: list[tuple[str, float, float]] = []
    day5, day20 = metrics.get("return_5d_pct"), metrics.get("return_20d_pct")
    if day5 is not None or day20 is not None:
        price = 50.0 + (day5 or 0.0) * 4.0 + (day20 or 0.0) * 1.5
        components.append(("price", _clamp(price), 0.35))
    ratio = metrics.get("volume_ratio")
    if ratio is not None:
        components.append(("volume", _clamp(50.0 + (ratio - 1.0) * 50.0), 0.25))
    up, down, flat = metrics.get("advancers"), metrics.get("decliners"), metrics.get("unchanged")
    breadth_total = sum(value or 0.0 for value in (up, down, flat))
    if up is not None and breadth_total > 0:
        components.append(("breadth", _clamp(up / breadth_total * 100.0), 0.20))
    flow_ratio = metrics.get("main_inflow_ratio_pct")
    if flow_ratio is not None:
        components.append(("flow", _clamp(50.0 + flow_ratio * 3.0), 0.20))
    coverage = sum(weight for _, _, weight in components)
    score = (sum(value * weight for _, value, weight in components) / coverage
             if coverage else None)
    return {
        "score": round(score, 2) if score is not None else None,
        "coverage": round(coverage, 2),
        "components": {name: round(value, 2) for name, value, _ in components},
    }


class WindSectorClient:
    def __init__(self, *, api_key: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.api_key = (api_key if api_key is not None else os.getenv("WIND_API_KEY", "")).strip()
        if not self.api_key:
            raise IndustryWindError("服务端未配置 WIND_API_KEY")
        self.transport = transport

    def _request(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            with httpx.Client(timeout=timeout, transport=self.transport) as client:
                response = client.post(ENDPOINT, headers=headers, content=_canonical(body))
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise IndustryWindError(f"Wind MCP 网络失败：{type(exc).__name__}") from exc
        payload = _parse_envelope(response.text)
        if payload.get("error"):
            raise IndustryWindError(str(payload["error"])[:500])
        result = payload.get("result")
        if not isinstance(result, dict):
            raise IndustryWindError("Wind MCP 响应缺少 result")
        return result

    def validate(self, industry_name: str, *, windcode: str | None = None) -> dict[str, Any]:
        timeout = min(config.WIND_MCP_TIMEOUT_S, 20)
        self._request("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "trend-desk", "version": CLIENT_VERSION},
        }, timeout=timeout)
        result = self._request("tools/call", {
            "name": "get_index_price_indicators",
            "arguments": {"windcode": windcode or industry_name, "indexes": INDICATORS},
            "_meta": {"clientVersion": CLIENT_VERSION},
        }, timeout=timeout)
        inner = _inner(result)
        return _validation_result(
            inner, wind_code=windcode or industry_name, wind_name=industry_name,
        )


class WindSkillCliClient:
    """Call the user's installed wind-mcp-skill CLI, not a project-owned API key."""

    def __init__(self, *, skill_dir: Path | None = None, timeout_s: float | None = None):
        self.skill_dir = Path(skill_dir or config.WIND_SKILL_DIR).expanduser()
        self.timeout_s = float(timeout_s or config.WIND_SKILL_CLI_TIMEOUT_S)
        self.node = shutil.which("node")
        self.cli = self.skill_dir / "scripts" / "cli.mjs"
        if not self.node or not self.cli.exists():
            raise WindSkillRuntimeError(f"本地 Wind Skill CLI 不可用：{self.cli}")

    def _call(self, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        command = [
            self.node, "scripts/cli.mjs", "call", "index_data", tool,
            json.dumps(params, ensure_ascii=False, separators=(",", ":")),
        ]
        try:
            completed = subprocess.run(
                command, cwd=self.skill_dir, capture_output=True, text=True,
                timeout=self.timeout_s, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WindSkillRuntimeError(f"Wind Skill CLI 超时：{tool}") from exc
        except OSError as exc:
            raise WindSkillRuntimeError(f"Wind Skill CLI 启动失败：{type(exc).__name__}") from exc

        output = (completed.stdout or "").strip()
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise WindSkillRuntimeError("Wind Skill CLI 返回不是JSON") from exc
        if completed.returncode != 0 or payload.get("ok") is False:
            error = payload.get("error") or {}
            code = str(error.get("code") or "UNKNOWN")
            action = str(error.get("agent_action") or "Wind Skill CLI 调用失败")
            if code == "AUTH_ERROR":
                raise WindSkillAuthError(action[:500])
            raise WindSkillRuntimeError(f"{code}: {action[:450]}")
        if payload.get("isError"):
            raise WindSkillRuntimeError("Wind Skill 返回工具错误")
        content = payload.get("content")
        if not isinstance(content, list) or not content or not isinstance(content[0], dict):
            raise WindSkillRuntimeError("Wind Skill 返回缺少content")
        text = str(content[0].get("text") or "{}")
        try:
            inner = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WindSkillRuntimeError("Wind Skill content不是JSON") from exc
        if not isinstance(inner, dict):
            raise WindSkillRuntimeError("Wind Skill content不是对象")
        if inner.get("error") or inner.get("mcp_tool_error_code") not in (None, 0):
            message = inner.get("error") or inner.get("mcp_tool_error_msg") or "Wind工具错误"
            raise WindSkillRuntimeError(str(message)[:500])
        return inner

    def resolve_index(self, industry_name: str) -> dict[str, str]:
        inner = self._call("get_index_basicinfo", {
            "question": f"Wind行业分类中{industry_name}行业指数的Wind代码、证券简称和证券全称",
        })
        candidates = []
        for row in _rows(inner):
            code = str(row.get("Wind代码") or "").strip()
            short = str(row.get("证券简称") or "").strip()
            full = str(row.get("证券全称") or "").strip()
            if not code:
                continue
            score = 0
            if short == industry_name:
                score += 100
            elif industry_name in short:
                score += 60
            if industry_name in full:
                score += 30
            if code.startswith("882"):
                score += 15
            if any(token in f"{short}{full}" for token in ("全球", "全A", "增强")):
                score -= 40
            if score > 0:
                candidates.append((score, code, short or full))
        if not candidates:
            raise WindSkillMappingError(f"Wind未找到行业指数映射：{industry_name}")
        candidates.sort(key=lambda item: (-item[0], item[1]))
        _, code, name = candidates[0]
        return {"wind_code": code, "wind_name": name}

    def validate(self, industry_name: str, *, windcode: str | None = None) -> dict[str, Any]:
        mapping = {"wind_code": windcode, "wind_name": None} if windcode else self.resolve_index(industry_name)
        inner = self._call("get_index_price_indicators", {
            "windcode": mapping["wind_code"], "indexes": INDICATORS,
        })
        return _validation_result(
            inner, wind_code=str(mapping["wind_code"]), wind_name=mapping.get("wind_name"),
        )


def default_client() -> WindSectorClient | WindSkillCliClient:
    if config.WIND_VALIDATION_MODE == "skill_cli":
        return WindSkillCliClient()
    if config.WIND_VALIDATION_MODE == "direct":
        return WindSectorClient()
    raise IndustryWindError("Wind行业验证已关闭")
