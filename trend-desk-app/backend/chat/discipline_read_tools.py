"""Chat 纪律只读工具：薄封装 build_day_card，不付费、不调 LLM。

入参：
  trade_date?: str  — 缺省用东八区今日
  code?: str        — whitelist/exits 可按代码过滤（裸码或带后缀）
  verbose?: bool    — 仅 read_day_card / read_whitelist 透传
"""
from __future__ import annotations

from sqlmodel import Session

from backend.discipline.day_card import bare_code, build_day_card


def _trade_date(args: dict) -> str | None:
    value = args.get("trade_date")
    if value is None or value == "":
        return None
    return str(value)


def _verbose(args: dict) -> bool:
    return bool(args.get("verbose"))


def _filter_by_code(items: list[dict], code: str | None, *, key: str = "code") -> list[dict]:
    if not code:
        return items
    target = bare_code(code)
    return [row for row in items if bare_code(row.get(key)) == target]


def read_day_card_tool(s: Session, args: dict) -> dict:
    """Q1–Q4 完整纪律卡片。"""
    card = build_day_card(s, trade_date=_trade_date(args), verbose=_verbose(args))
    return {"ok": True, "tool": "read_day_card", "card": card}


def read_opening_tool(s: Session, args: dict) -> dict:
    """Q1：环境与开仓许可。"""
    card = build_day_card(s, trade_date=_trade_date(args), verbose=False)
    q1 = (card.get("answers") or {}).get("Q1_opening")
    if q1 is None:
        return {
            "ok": False,
            "tool": "read_opening",
            "error": "尚无开仓结论：数据集未就绪或还没有计划。可先看纪律台「今日」是否已采集。",
            "trade_date": card.get("trade_date"),
            "dataset": card.get("dataset"),
            "plan": card.get("plan"),
            "meta": card.get("meta"),
        }
    return {
        "ok": True,
        "tool": "read_opening",
        "trade_date": card.get("trade_date"),
        "dataset": card.get("dataset"),
        "plan": card.get("plan"),
        "answer": q1,
        "meta": card.get("meta"),
    }


def read_whitelist_tool(s: Session, args: dict) -> dict:
    """Q2：可买白名单与目标手数；可选 code 过滤。"""
    card = build_day_card(s, trade_date=_trade_date(args), verbose=_verbose(args))
    q2 = (card.get("answers") or {}).get("Q2_whitelist")
    if q2 is None:
        return {
            "ok": False,
            "tool": "read_whitelist",
            "error": "尚无白名单：数据集未就绪或还没有计划。",
            "trade_date": card.get("trade_date"),
            "dataset": card.get("dataset"),
            "plan": card.get("plan"),
            "meta": card.get("meta"),
        }
    code = args.get("code")
    items = _filter_by_code(list(q2.get("items") or []), code)
    if code and not items:
        return {
            "ok": False,
            "tool": "read_whitelist",
            "error": f"白名单中没有代码 {code}（已做 .SH/.SZ 归一）。",
            "trade_date": card.get("trade_date"),
            "answer": {**q2, "items": [], "count": 0, "filtered_code": code},
            "meta": card.get("meta"),
        }
    return {
        "ok": True,
        "tool": "read_whitelist",
        "trade_date": card.get("trade_date"),
        "dataset": card.get("dataset"),
        "plan": card.get("plan"),
        "answer": {
            **q2,
            "items": items,
            "count": len(items),
            "filtered_code": code or None,
        },
        "meta": card.get("meta"),
    }


def read_exits_tool(s: Session, args: dict) -> dict:
    """Q3：持仓离场动作；可选 code 过滤。"""
    card = build_day_card(s, trade_date=_trade_date(args), verbose=False)
    q3 = (card.get("answers") or {}).get("Q3_exits")
    if q3 is None:
        return {
            "ok": False,
            "tool": "read_exits",
            "error": "尚无离场信号：数据集未就绪、还没有计划，或计划里没有持仓侧动作。",
            "trade_date": card.get("trade_date"),
            "dataset": card.get("dataset"),
            "plan": card.get("plan"),
            "meta": card.get("meta"),
        }
    code = args.get("code")
    items = _filter_by_code(list(q3.get("items") or []), code, key="code")
    if code and not items:
        return {
            "ok": False,
            "tool": "read_exits",
            "error": f"离场列表中没有代码 {code}（已做 .SH/.SZ 归一）。",
            "trade_date": card.get("trade_date"),
            "answer": {**q3, "items": [], "count": 0, "filtered_code": code},
            "meta": card.get("meta"),
        }
    return {
        "ok": True,
        "tool": "read_exits",
        "trade_date": card.get("trade_date"),
        "dataset": card.get("dataset"),
        "plan": card.get("plan"),
        "answer": {
            **q3,
            "items": items,
            "count": len(items),
            "filtered_code": code or None,
        },
        "meta": card.get("meta"),
    }


def read_plan_status_tool(s: Session, args: dict) -> dict:
    """仅数据集 + 计划元数据，不含候选全量。"""
    card = build_day_card(s, trade_date=_trade_date(args), verbose=False)
    return {
        "ok": bool(card.get("dataset") or card.get("plan")),
        "tool": "read_plan_status",
        "trade_date": card.get("trade_date"),
        "dataset": card.get("dataset"),
        "plan": card.get("plan"),
        "meta": card.get("meta"),
        "error": None if (card.get("dataset") or card.get("plan")) else (
            (card.get("meta") or {}).get("note") or "no_dataset"
        ),
    }


DISCIPLINE_READ_TOOLS = {
    "read_day_card": read_day_card_tool,
    "read_opening": read_opening_tool,
    "read_whitelist": read_whitelist_tool,
    "read_exits": read_exits_tool,
    "read_plan_status": read_plan_status_tool,
}
