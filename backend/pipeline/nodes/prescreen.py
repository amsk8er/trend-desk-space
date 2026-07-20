from sqlmodel import Session, select
from backend.db import Manifest, OcrRow, OcrJob
from backend.ocr.review import clean_approved_rows
from backend.ocr.sectors import resolve_sectors
from backend.filters.checks import Instrument, detect_etf, is_us_market
from backend.filters.chain import run_main_filter, fails
from backend.pipeline.state import transition, NodeStatus


def _sector_status_map(rows) -> dict:
    """板块温度查表：板块行(name=板块名) → temperature_status。M1 据此回查。"""
    return {r.name: r.temperature_status
            for r in rows if r.row_type == "sector" and r.name}


def _to_instrument(row, sector_map: dict | None = None, *,
                   resolved_sector: str | None = None,
                   etf_min_aum_yi: float | None = None,
                   etf_min_turnover_yi: float | None = None,
                   min_market_cap_yi: float | None = None,
                   min_turnover_yi: float | None = None) -> Instrument:
    rf = row.raw_fields or {}
    is_etf = detect_etf(row.name, row.code, row.sector)
    sector = resolved_sector if resolved_sector is not None else row.sector
    return Instrument(
        code=row.code or "", name=row.name or "",
        sector=sector,
        sector_status=(sector_map or {}).get(sector),
        market_cap_yi=rf.get("market_cap_yi"),
        turnover_yi=rf.get("turnover_yi"),
        right_side_days=row.right_side_days,
        right_side_gain_pct=row.right_side_gain_pct,
        jieqi=row.jieqi,
        daily_change_pct=rf.get("daily_change_pct"),
        strength=row.strength,
        temperature_status=row.temperature_status,
        stop_loss=rf.get("stop_loss"),
        is_etf=is_etf,
        etf_min_aum_yi=etf_min_aum_yi if is_etf else None,
        etf_min_turnover_yi=etf_min_turnover_yi if is_etf else None,
        min_market_cap_yi=min_market_cap_yi if not is_etf else None,
        min_turnover_yi=min_turnover_yi if not is_etf else None,
        tags=rf.get("tags") or [],
    )


def _market_summary(rows) -> dict | None:
    """捞大盘行做大盘卡。真实 OCR 用 row_type=="overview"(也兼容 "market")。无则 None。
    本版只做 A 股：优先取名为「A股」的总览行（大类资产榜第一条常是美股，不能直接取首行）。"""
    market_rows = [r for r in rows if r.row_type in ("overview", "market")]
    if not market_rows:
        return None
    market_row = next((r for r in market_rows if r.name == "A股"), market_rows[0])
    rf = market_row.raw_fields or {}
    return {"name": market_row.name or "大盘", "strength": market_row.strength,
            "status": market_row.temperature_status, "regime": rf.get("regime")}


def _build_report(candidates: list[dict], rejected: list[dict]) -> dict:
    """初筛报告 = 拒绝原因清单(用户选定形态)。"""
    detail = [
        {"name": r["name"], "code": r["code"], "status": r.get("status"),
         "strength": r.get("strength"),
         "reasons": [rr["reason"] for rr in r.get("reasons", [])]}
        for r in rejected
    ]
    return {"summary": f"{len(candidates)} 只进初筛, {len(rejected)} 只被拒",
            "rejected_detail": detail}


def run_prescreen(s: Session, *, batch_id: str,
                  etf_min_aum_yi: float | None = None,
                  etf_min_turnover_yi: float | None = None,
                  min_market_cap_yi: float | None = None,
                  min_turnover_yi: float | None = None) -> dict:
    transition(s, batch_id=batch_id, node="prescreen", to=NodeStatus.RUNNING)
    rows = clean_approved_rows(s, batch_id=batch_id)
    sector_map = _sector_status_map(rows)
    # 板块解析跑「全批次原始行」（含 rejected/截断/重复），不是 clean_approved_rows——
    # 因为被剔的行在屏序里仍可能承接板块继承链，跑全集才不断链。
    pairs = s.exec(
        select(OcrRow, OcrJob).join(OcrJob, OcrRow.job_id == OcrJob.job_id)
        .where(OcrJob.batch_id == batch_id)
    ).all()
    resolved = resolve_sectors(pairs)
    # 入池门：温度档位「温(=旧库误读的暖)/热/沸」之一，或强度 ≥ 60 兜底。
    # 「温」与「暖」是同一档（旧 prompt 枚举把「温」错写成「暖」），两者都收。
    POOL_STATUSES = ("温", "暖", "热", "沸")
    instruments = [_to_instrument(r, sector_map,
                                  resolved_sector=resolved.get(r.row_id),
                                  etf_min_aum_yi=etf_min_aum_yi,
                                  etf_min_turnover_yi=etf_min_turnover_yi,
                                  min_market_cap_yi=min_market_cap_yi,
                                  min_turnover_yi=min_turnover_yi)
                   for r in rows if r.row_type == "instrument"
                   and not is_us_market(r.name, r.code, r.sector)
                   and (r.temperature_status in POOL_STATUSES or (r.strength or 0) >= 60)]
    candidates, rejected = [], []
    for i in instruments:
        failed = fails(run_main_filter(i))
        record = {"code": i.code, "name": i.name, "status": i.temperature_status,
                  "strength": i.strength, "right_side_days": i.right_side_days,
                  "sector": i.sector, "sector_status": i.sector_status, "jieqi": i.jieqi,
                  "market_cap_yi": i.market_cap_yi, "turnover_yi": i.turnover_yi,
                  "right_side_gain_pct": i.right_side_gain_pct, "is_etf": i.is_etf,
                  "daily_change_pct": i.daily_change_pct, "tags": i.tags}
        if failed:
            record["reasons"] = [{"check_id": r.check_id, "reason": r.reason} for r in failed]
            rejected.append(record)
        else:
            candidates.append(record)
    manifest = {"stage": "prescreen", "candidates": candidates, "rejected": rejected,
                "market": _market_summary(rows), "report": _build_report(candidates, rejected),
                # 本次初筛所用的可调阈值——持久化，供 B 筛回读沿用，否则 B 筛重算 M1-M4
                # 时落回 config 默认，会把只因放宽阈值才过初筛的票静默丢掉（小由 2026-06-24）。
                "thresholds": {"etf_min_aum_yi": etf_min_aum_yi,
                               "etf_min_turnover_yi": etf_min_turnover_yi,
                               "min_market_cap_yi": min_market_cap_yi,
                               "min_turnover_yi": min_turnover_yi}}
    s.add(Manifest(batch_id=batch_id, stage="prescreen", manifest_json=manifest,
                   white_list=candidates, rejected=rejected))
    s.commit()
    transition(s, batch_id=batch_id, node="prescreen", to=NodeStatus.DONE)
    return manifest
