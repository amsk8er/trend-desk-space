"""美股手工执行台的内部契约、序列化与失败语义。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


BASE_SIGNAL_FIELDS = (
    "daysSinceTrendEntry",
    "industryTrendTemperatureCurr",
)
ETF_BASE_SIGNAL_FIELDS = (
    "daysSinceTrendEntry",
)
LEGACY_BASE_SIGNAL_FIELDS = (
    "trendTemperatureCurr",
    "trendTemperaturePrev",
    "daysSinceTrendEntry",
)

# 第一阶段对个股用右侧自然日与所属行业温度做门槛，对 ETF 只用右侧自然日；
# 第二阶段仅对通过门槛的标的请求排序、流动性、标签和节气字段。
#
# ``*Curr`` 是当前实时计费表里的正式字段名。历史返回中曾出现过不带
# ``Curr`` 的同义字段，实际请求字段会在预检时仅从实时计费表确认的别名中选择。
ENRICHMENT_FIELDS = (
    "amount1d",
    "marketCap",
    "industryTmId",
    "industryName",
    "trendStrengthLocalCurr",
    "industryTrendStrengthLocalCurr",
    "tickerLabels",
    "trendPhaseCurr",
)
ETF_ENRICHMENT_FIELDS = (
    "amount1d",
    "marketCap",
    "trendStrengthLocalCurr",
    "tickerLabels",
    "trendPhaseCurr",
)

# 键是当前规范字段名；值按优先级列出趋势动物已出现过的同义字段名。
# 预检只会使用实时计费表实际列出的名字，不能据此猜测或免费补全字段。
ENRICHMENT_FIELD_ALIASES = {
    "industryTrendTemperatureCurr": (
        "industryTrendTemperatureCurr",
        "industryTrendTemperature",
        "industryTemperatureCurr",
    ),
    "industryTrendStrengthLocalCurr": (
        "industryTrendStrengthLocalCurr",
        "industryTrendStrengthLocal",
        "industryStrengthLocalCurr",
    ),
}

US_MANUAL_SCOPE = "bitget_us_manual_h6"
US_MANUAL_H5_SCOPE = "bitget_us_manual_h5"
US_MANUAL_H4_SCOPE = "bitget_us_manual_h4"
US_MANUAL_H3_SCOPE = "bitget_us_manual_h3"
US_MANUAL_H2_SCOPE = "bitget_us_manual_h2"
US_MANUAL_LEGACY_SCOPE = "bitget_us_manual"
US_MANUAL_LEGACY_RULES_VERSION = "us-manual-h1"
US_MANUAL_H2_RULES_VERSION = "us-manual-h2"
US_MANUAL_H4_RULES_VERSION = "us-manual-h4"
US_MANUAL_H5_RULES_VERSION = "us-manual-h5"
US_MANUAL_RULES_VERSION = "us-manual-h6"
US_RISK_ANCHOR_ALGORITHM_VERSION = "bitget-rtoken-ep3-v1"
US_STOP_ALGORITHM_VERSION = "wind-ep3-bitget-rtoken-v1"
US_STOP_DEVIATION_THRESHOLD = Decimal("0.015")
US_COMBO_NAMES = {
    "温转热(美股)": "美股组合",
    "温转热(美国ETF)": "美国ETF组合",
}
MANUAL_ONLY_NOTICE = "只生成清单，不会下单。Trend Desk 不会向 Bitget 发送订单。"

MARKET_ENVIRONMENT_FIELDS = (
    "trendTemperatureCurr",
    "trendStrengthLocalCurr",
    "trendPhaseCurr",
    "tickerLabels",
    "daysSinceTrendEntry",
)
HOLDING_EXIT_FIELDS = (
    "trendTemperatureCurr",
    "stopwinFlagByDangerSignal",
    "stopwinFlagByBoilingTemperature",
    "stopwinFlagByPopChampagne",
)


_ERROR_NEXT_ACTIONS = {
    "historical_candidate_read_only": "返回当前 H6 观察清单重新选择候选",
    "historical_position_read_only": "只查看历史持仓；不要用 H6 规则改写历史证据",
    "historical_run_read_only": "采集当前 H6 数据日后再继续",
    "legacy_read_only": "只查看 H1–H5 历史记录，并使用当前 H6 运行",
    "h6_shadow_read_only": "完成三个真实美股数据日 shadow 核验后再切换 active",
    "market_environment_blocked": "等待美股整体温度证据完整；本日不开新仓",
    "exit_data_blocked": "先补齐所有开放持仓的当日趋势退出字段",
    "risk_anchor_not_ready": "重新读取 Bitget 公开报价、1D 和 1H 后生成风险锚点",
    "risk_anchor_not_confirmed": "保持观察；不得用未确认低点反推仓位",
    "risk_anchor_not_below_quote": "保持观察并等待新的合格 EP3 低点",
    "allocation_preview_stale": "按当前报价、锚点和容量重新生成推荐分配",
    "etf_benchmark_not_verified": "等待每日采集自动补齐 SEC 身份和权威跟踪指数证据",
    "etf_benchmark_missing": "重新运行每日采集自动核验；跟踪指数缺失时保持失败关闭",
    "sec_user_agent_not_configured": (
        "在服务端配置含联系邮箱的 US_MANUAL_SEC_USER_AGENT，"
        "或确认使用现有通知邮箱后重试"
    ),
    "structure_stop_retired": "返回当前持仓退出区；真实卖出只按趋势温度纪律",
}


def _next_action_for(code: str) -> dict[str, str]:
    detail = _ERROR_NEXT_ACTIONS.get(code)
    if detail is None:
        if code.startswith("etf_") or code.startswith("sec_"):
            detail = "重新运行每日采集自动核验 ETF 权威证据；不能用猜测或自由文本绕过"
        elif code.startswith("risk_anchor_") or code.startswith("bitget_") or code.startswith("quote_"):
            detail = "稍后重新读取 Bitget 公开行情证据；该候选暂不进入分配"
        elif code.startswith("allocation_") or code.startswith("position_capacity_") or code.startswith("cash_"):
            detail = "刷新当前现金、持仓、锁定预留和当日额度后重新计算分配"
        elif code.startswith("exit_") or code.startswith("oversell_"):
            detail = "刷新持仓及趋势退出证据后重新生成手工卖出清单"
        elif code.startswith("historical_") or code.startswith("legacy_"):
            detail = "保持历史数据只读，并切换到当前 H6 运行"
        elif code.endswith("_required") or code.endswith("_invalid"):
            detail = "修正请求字段后重试；服务端不会代填交易价格、数量或规则参数"
        elif code.endswith("_stale") or code.endswith("_changed") or code.endswith("_mismatch"):
            detail = "刷新当前证据并从最新不可变快照重新开始"
        elif code.endswith("_unavailable") or code.endswith("_timeout"):
            detail = "保留失败关闭状态，稍后重试该数据源"
        else:
            detail = "不要绕过失败关闭；按错误原因补齐证据后重试"
    return {"code": f"{code}_next", "title": "唯一下一步", "detail": detail}


@dataclass(slots=True)
class UsManualError(Exception):
    """可安全暴露给前端的预期业务失败。"""

    code: str
    message: str
    status_code: int = 409
    detail: dict[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        context = serialize(self.detail or {})
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "context": context,
            "next_action": _next_action_for(self.code),
        }
        if self.detail:
            payload["detail"] = context
        return payload


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_decimal(value: Any, *, field: str, positive: bool = False,
                  non_negative: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        raise UsManualError("invalid_decimal", f"{field} 必须是十进制定点数", 422)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise UsManualError("invalid_decimal", f"{field} 必须是十进制定点数", 422) from exc
    if not result.is_finite():
        raise UsManualError("invalid_decimal", f"{field} 必须是有限数值", 422)
    if positive and result <= 0:
        raise UsManualError("invalid_decimal", f"{field} 必须大于 0", 422)
    if non_negative and result < 0:
        raise UsManualError("invalid_decimal", f"{field} 不能小于 0", 422)
    return result


def decimal_text(value: Decimal | int | str | float | None) -> str | None:
    if value is None:
        return None
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    # format(..., "f") avoids scientific notation for small fractional shares.
    return format(number, "f")


def serialize(value: Any) -> Any:
    """API 统一用字符串交付 Decimal，避免 JavaScript 浮点悄悄改变散股数。"""
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [serialize(item) for item in value]
    if hasattr(value, "model_dump"):
        return serialize(value.model_dump())
    return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None
