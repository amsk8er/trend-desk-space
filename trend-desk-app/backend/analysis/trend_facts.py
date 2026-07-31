# backend/analysis/trend_facts.py
"""趋势事实包：从近 N 个 batch + 当日数据拼出结构化 JSON，喂给 LLM 写研判。

纯函数、零 LLM、可单测。只负责「取数 + 聚合」，叙事交给 trend_brief.generate_brief。
取最新 Manifest 沿用 manifest_id.desc()（与 report.py / read.py 的 latest-wins 一致）。
"""
from datetime import date

from sqlmodel import Session, select

from backend.db import Batch, ExitListItem, Manifest, Position
from backend.ocr.review import clean_approved_rows
from backend.pipeline.nodes.exit_check import EXIT_STATUSES, build_exit_overview
from backend.pipeline.nodes.holding_temp import norm_code


def _latest_manifest(s: Session, batch_id: str, stage: str) -> Manifest | None:
    return next((m for m in s.exec(
        select(Manifest)
        .where(Manifest.batch_id == batch_id, Manifest.stage == stage)
        .order_by(Manifest.manifest_id.desc())).all()), None)


def _sector_status_map(rows) -> dict:
    """板块行（row_type="sector"）→ 温度。复用 prescreen/_sector_status_map 写法。"""
    return {r.name: r.temperature_status for r in rows if r.row_type == "sector" and r.name}


def _days_between(d_from: str | None, d_to: str | None) -> int | None:
    try:
        return (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days
    except (TypeError, ValueError):
        return None


def _candidates(s: Session, batch_id: str) -> list[dict]:
    pre = _latest_manifest(s, batch_id, "prescreen")
    return pre.manifest_json.get("candidates", []) if pre else []


def build_trend_facts(s: Session, *, batch_id: str, lookback: int = 5) -> dict:
    b = s.get(Batch, batch_id)

    # 近 lookback 个 batch（date ≤ 当日，降序取后翻转成「旧→新」便于读趋势）。
    recent = list(reversed(list(s.exec(
        select(Batch).where(Batch.date <= b.date)
        .order_by(Batch.date.desc()).limit(lookback)).all())))

    # ── 市场节奏时间序列 + 板块温度逐批快照 ──
    market_rhythm, sector_maps = [], []
    for rb in recent:
        cands = _candidates(s, rb.batch_id)
        strengths = [c["strength"] for c in cands
                     if isinstance(c.get("strength"), (int, float))]
        exits_n = len(s.exec(select(ExitListItem)
                             .where(ExitListItem.batch_id == rb.batch_id)).all())
        pos_n = len(s.exec(select(Position)
                           .where(Position.batch_id == rb.batch_id)).all())
        market_rhythm.append({
            "date": rb.date,
            "hot_new": len(cands),
            "cooling": exits_n,
            "strength_avg": round(sum(strengths) / len(strengths), 1) if strengths else None,
            "positions": pos_n,
        })
        sector_maps.append(_sector_status_map(clean_approved_rows(s, rb.batch_id)))

    all_sectors = sorted({sec for m in sector_maps for sec in m})
    sector_rhythm = [{"sector": sec, "trend": [m.get(sec) for m in sector_maps]}
                     for sec in all_sectors]

    # ── 今日数据 ──
    today_cands = _candidates(s, batch_id)
    today_rows = clean_approved_rows(s, batch_id)
    first_hot_by_code = {norm_code(r.code): r.first_hot_date for r in today_rows if r.code}
    first_hot_by_name = {r.name: r.first_hot_date for r in today_rows if r.name}

    # 今日热板块：当日候选按 sector 聚合 + 成员数
    by_sec: dict = {}
    for c in today_cands:
        sec = c.get("sector") or "未分类"
        e = by_sec.setdefault(sec, {"sector": sec, "status": c.get("sector_status"),
                                    "stock_count": 0})
        e["stock_count"] += 1
    sector_today_hot = sorted(by_sec.values(), key=lambda x: -x["stock_count"])

    # 个股轨迹：今日新增温转热
    new_hot = []
    for c in today_cands:
        fh = first_hot_by_code.get(norm_code(c.get("code"))) or first_hot_by_name.get(c.get("name"))
        new_hot.append({
            "name": c.get("name"), "status": c.get("status"), "strength": c.get("strength"),
            "sector": c.get("sector"), "right_side_days": c.get("right_side_days"),
            "days_since_hot": _days_between(fh, b.date),
        })

    # ── 持仓分析：复用 build_exit_overview（已 join Position+HoldingTemp+OcrRow）──
    overview = build_exit_overview(s, batch_id=batch_id)
    pnls = [o["pnl_pct"] for o in overview if isinstance(o.get("pnl_pct"), (int, float))]
    profit = sum(1 for v in pnls if v > 0)
    loss = sum(1 for v in pnls if v < 0)
    temp_dist: dict = {}
    for o in overview:
        st = o.get("temperature_status") or "无温度"
        temp_dist[st] = temp_dist.get(st, 0) + 1
    cooling = [{"name": o.get("name"), "status": o.get("temperature_status"),
                "pnl_pct": o.get("pnl_pct"), "suggest": o.get("suggest")}
               for o in overview
               if o.get("temperature_status") in EXIT_STATUSES or (o.get("pnl_pct") or 0) < 0]
    holdings = {
        "count": len(overview),
        "pnl_dist": {"profit": profit, "loss": loss, "flat": len(overview) - profit - loss,
                     "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else None},
        "temp_dist": temp_dist,
        "cooling": cooling,
    }

    # ── 执行结论：复用 b_filter / ExitListItem latest-wins ──
    bfilter = _latest_manifest(s, batch_id, "b_filter")
    wl = (bfilter.white_list or []) if bfilter else []
    watch = (bfilter.manifest_json.get("watch_list", []) if bfilter else [])
    rj = (bfilter.rejected or []) if bfilter else []
    exits = [{"reason": e.reason, "action": e.action}
             for e in s.exec(select(ExitListItem)
                             .where(ExitListItem.batch_id == batch_id)).all()]

    return {
        "date": b.date,
        "market_rhythm": market_rhythm,
        "sector_heat": {"today_hot": sector_today_hot, "rhythm": sector_rhythm},
        "new_hot": new_hot,
        "holdings": holdings,
        "exits": exits,
        "whitelist": [w.get("name") for w in wl],
        "watch": [w.get("name") for w in watch],
        "rejected_count": len(rj),
    }
