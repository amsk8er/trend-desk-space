from dataclasses import dataclass
from typing import Callable, Any
from sqlmodel import Session, select
from backend.db import OcrJob, OcrRow
from backend.pipeline.state import get_state, transition, NodeStatus, NODES
from backend.chat.discipline_read_tools import DISCIPLINE_READ_TOOLS
from backend.ocr import review as _review
from backend.ocr.runner import schedule_ocr_run
from backend.risk.sizing import STOP_SOURCES
from backend.chat.read_tools import norm_code as _norm_code


class ToolForbidden(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    needs_confirm: bool
    fn: Callable[[Session, dict], Any]


def _query_pipeline_state(s: Session, args: dict) -> dict:
    return {"state": get_state(s, args["batch_id"])}


def _rerun_node(s: Session, args: dict) -> dict:
    # Generic node reset (flips status to TODO only). For OCR use rerun_ocr —
    # that one actually re-executes; this one does not.
    node = args["node_id"]
    if node not in NODES:
        raise ToolForbidden(f"unknown node {node}")
    transition(s, batch_id=args["batch_id"], node=node, to=NodeStatus.TODO)
    return {"ok": True}


def _rerun_ocr(s: Session, args: dict) -> dict:
    # Real OCR rerun: resets target screenshots and kicks off a background run.
    # indices = 截图编号(0基)列表；省略 = 全部未成功(skip/failed)。
    queued = schedule_ocr_run(args["batch_id"], args.get("indices"))
    return {"ok": True, "queued": queued, "note": "已后台触发，进度在 OCR 面板看"}


def _set_stop_refs(s: Session, args: dict) -> dict:
    # 止损参考录入（纪律 v2 §五）：截图不含 MA21/20日低点等，由用户对话报数。
    # 写进该批次该 code 全部 OcrRow.raw_fields["stop_refs"]，B 筛 sizing 由此取数。
    refs = args.get("stop_refs") or {}
    bad = [k for k in refs if k not in STOP_SOURCES]
    if bad:
        raise ToolForbidden(
            f"未知止损源 {bad}；只允许 {sorted(STOP_SOURCES)}（分时低点不作止损依据）")
    # code 归一匹配：用户报 601166、库里存 601166.SH（或反过来）都要命中。
    # 实盘 batch_20260612_2222 的 updated_rows=0 根因之一就是后缀不一致。
    target = _norm_code(args["code"])
    candidates = s.exec(select(OcrRow).join(OcrJob).where(
        OcrJob.batch_id == args["batch_id"], OcrRow.code.is_not(None))).all()
    rows = [r for r in candidates if _norm_code(r.code) == target]
    for r in rows:
        rf = dict(r.raw_fields or {})
        rf["stop_refs"] = {**(rf.get("stop_refs") or {}), **refs}
        if args.get("price") is not None:
            rf["price"] = args["price"]
        r.raw_fields = rf  # JSON column: reassign so the change is persisted
        s.add(r)
    s.commit()
    note = ("已写入，重跑 B 过滤后生效" if rows else
            "该批次没有这个代码的行——先确认 code 是否带正确后缀"
            "（A股 .SH/.SZ）；本工具已对后缀做归一匹配，仍为 0 说明该批次确实没有此代码")
    return {"updated_rows": len(rows), "code": args["code"], "stop_refs": refs, "note": note}


def _approve_rows(s: Session, args: dict) -> dict:
    _review.approve(s, row_ids=args["row_ids"], reason=args.get("reason", ""))
    return {"approved": args["row_ids"]}


def _reject_rows(s: Session, args: dict) -> dict:
    _review.reject(s, row_ids=args["row_ids"], reason=args["reason"])
    return {"rejected": args["row_ids"]}


# read_log / switch_model 已移除：空壳与假动作会诱导模型白调一轮再编答案。
# 纪律读工具：薄封装 day_card，只读库、不付费（见 docs/agent-api-contract.md）。
REGISTRY: dict[str, ToolSpec] = {
    "read_day_card": ToolSpec("read_day_card", False, DISCIPLINE_READ_TOOLS["read_day_card"]),
    "read_opening": ToolSpec("read_opening", False, DISCIPLINE_READ_TOOLS["read_opening"]),
    "read_whitelist": ToolSpec("read_whitelist", False, DISCIPLINE_READ_TOOLS["read_whitelist"]),
    "read_exits": ToolSpec("read_exits", False, DISCIPLINE_READ_TOOLS["read_exits"]),
    "read_plan_status": ToolSpec("read_plan_status", False, DISCIPLINE_READ_TOOLS["read_plan_status"]),
    "query_pipeline_state": ToolSpec("query_pipeline_state", False, _query_pipeline_state),
    "rerun_node": ToolSpec("rerun_node", True, _rerun_node),
    "rerun_ocr": ToolSpec("rerun_ocr", True, _rerun_ocr),
    "set_stop_refs": ToolSpec("set_stop_refs", True, _set_stop_refs),
    "approve_rows": ToolSpec("approve_rows", True, _approve_rows),
    "reject_rows": ToolSpec("reject_rows", True, _reject_rows),
}


def run_tool(s: Session, name: str, args: dict):
    spec = REGISTRY.get(name)
    if not spec:
        raise ToolForbidden(f"tool not whitelisted: {name}")
    return spec.fn(s, args)
