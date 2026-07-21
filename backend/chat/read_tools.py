"""Chatbox read-only tools over pipeline results (spec 2026-07-02).

Each tool mirrors a read function in backend/api/read.py but reshapes the
payload for a conversation: white/watch lists go out in full (they are short),
rejection lists are grouped by check_id — a batch can reject 100+ rows and the
model cares about *why*, not the row dump. Per-row detail is fetched by passing
`code`. Empty results come back as {"error": ...} dicts the model can read
aloud (conversation._safe_run already guards real exceptions).
"""
from sqlmodel import Session, select

from backend.api import read as read_api
from backend.db import TrendBrief


def norm_code(code: str | None) -> str:
    """"601869.SH" / "300747.SZ" → "601869" (main-DB codes carry no suffix)."""
    return (code or "").split(".")[0].strip()


def _filter_by_code(items: list[dict], code: str) -> list[dict]:
    target = norm_code(code)
    return [r for r in items if norm_code(r.get("code")) == target]


def group_rejected(rejected: list[dict]) -> dict:
    """[{name, reasons:[{check_id, reason}]}] → {check_id: {count, names, sample_reasons}}.

    Group by check_id, NOT by reason string — reasons embed per-instrument
    numbers ("市值250亿 ≤ 300亿") so grouping on them would yield size-1 groups.
    Keep up to 3 sample reasons so the model can quote real numbers.
    """
    groups: dict[str, dict] = {}
    for rec in rejected:
        label = rec.get("name") or rec.get("code") or "?"
        for rr in rec.get("reasons", []):
            g = groups.setdefault(rr.get("check_id") or "?",
                                  {"count": 0, "names": [], "sample_reasons": []})
            g["count"] += 1
            if label not in g["names"]:
                g["names"].append(label)
            if len(g["sample_reasons"]) < 3:
                g["sample_reasons"].append(rr.get("reason"))
    return groups


def read_prescreen_tool(s: Session, args: dict) -> dict:
    m = read_api.read_prescreen(s, args["batch_id"])
    if not m:
        return {"error": "该批次还没有初筛结果——先跑「初筛」节点"}
    candidates = m.get("candidates") or []
    rejected = m.get("rejected") or []
    code = args.get("code")
    if code:
        return {"candidates": _filter_by_code(candidates, code),
                "rejected": _filter_by_code(rejected, code)}
    return {"summary": (m.get("report") or {}).get("summary"),
            "candidates": candidates,
            "rejected_by_check": group_rejected(rejected)}


def read_b_filter_tool(s: Session, args: dict) -> dict:
    data = read_api.read_b_filter(s, args["batch_id"])
    mj = data.get("manifest_json") or {}
    if not mj:
        return {"error": "该批次还没有 B 筛结果——先跑「B筛」节点"}
    white = mj.get("white_list") or []
    watch = mj.get("watch_list") or []
    rejected = mj.get("rejected") or []
    code = args.get("code")
    if code:
        return {"white_list": _filter_by_code(white, code),
                "watch_list": _filter_by_code(watch, code),
                "rejected": _filter_by_code(rejected, code)}
    return {"white_list": white, "watch_list": watch,
            "rejected_by_check": group_rejected(rejected)}


def read_exit_tool(s: Session, args: dict) -> dict:
    data = read_api.read_exit_check(s, args["batch_id"])
    if not data.get("items"):
        return {"error": "该批次没有出局建议——要么还没跑「出局」节点，要么持仓全部安全"}
    return data   # items + overview; exit lists are position-sized, no compaction needed


def read_positions_tool(s: Session, args: dict) -> dict:
    positions = read_api.read_positions(s, args["batch_id"])
    if not positions:
        return {"error": "该批次没有持仓数据——先在「持仓」节点上传券商截图"}
    temps = read_api.read_holding_temps(s, args["batch_id"])
    by_code = {norm_code(t["code"]): t for t in temps if t.get("code")}
    by_name = {t["name"]: t for t in temps if t.get("name")}
    out = []
    for p in positions:
        t = by_code.get(norm_code(p.get("code"))) or by_name.get(p.get("name")) or {}
        out.append({**p,
                    "temperature_status": t.get("temperature_status"),
                    "strength": t.get("strength"),
                    "right_side_days": t.get("right_side_days"),
                    "right_side_gain_pct": t.get("right_side_gain_pct"),
                    "jieqi": t.get("jieqi"),
                    "tags": t.get("tags") or []})
    code = args.get("code")
    if code:
        out = _filter_by_code(out, code)
    return {"positions": out}


def read_trend_brief_tool(s: Session, args: dict) -> dict:
    b = s.exec(select(TrendBrief).where(TrendBrief.batch_id == args["batch_id"])
               .order_by(TrendBrief.id.desc())).first()
    if b is None:
        return {"error": "该批次还没有趋势研判——跑一次「日报」节点即可生成"}
    return {"markdown": b.markdown, "model": b.model, "created_at": str(b.created_at)}
