# backend/api/read.py
# GET endpoints so the React frontend stages can read pipeline state and each
# node's persisted data. Read-only: never re-runs a node, only reads rows.
import os
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, select, func
from backend.engine import engine
from backend.db import Batch, OcrJob, OcrRow, Position, Manifest, ExitListItem, HoldingTemp
from backend.ocr.aggregate import aggregate_by_category
from backend.pipeline.nodes.exit_check import build_exit_overview

router = APIRouter(prefix="/api")


# --- read functions (session-injected, so they're unit-testable) ---

def read_state(s: Session, batch_id: str) -> dict:
    b = s.get(Batch, batch_id)
    if b is None:
        raise HTTPException(404, "unknown batch")
    return {
        "batch_id": b.batch_id,
        "date": b.date,
        "status": b.status,
        "pipeline_state": b.pipeline_state or {},
    }


def read_batches(s: Session) -> list:
    rows = s.exec(select(Batch).order_by(Batch.created_at.desc())).all()
    return [{"batch_id": b.batch_id, "date": b.date, "status": b.status} for b in rows]


def friendly_skip_reason(reason):
    """Turn an OcrJob.partial_reason code into a human sentence for the UI.

    Sources: parser.is_bad_image (market_missing_or_wrong / rows_empty /
    confidence_low=*) and worker._process_one (exception* on hard failure).
    Unknown codes fall back to the raw string so nothing ever shows blank.
    """
    if not reason:
        return None
    if reason == "market_missing_or_wrong":
        return "非 A股页面，已跳过"
    if reason == "rows_empty":
        return "未识别到任何行（疑似错图）"
    if reason.startswith("confidence_low"):
        return "识别置信度过低，已跳过"
    if reason.startswith("exception"):
        return "OCR 调用失败（可重跑）"
    return reason


def read_ocr(s: Session, batch_id: str) -> dict:
    jobs = s.exec(select(OcrJob).where(OcrJob.batch_id == batch_id)).all()
    stats = {"total": 0, "todo": 0, "running": 0, "done": 0, "failed": 0, "skipped": 0}
    # job status word "skip" maps to the "skipped" stat bucket
    _STAT_KEY = {"skip": "skipped"}
    for j in jobs:
        stats["total"] += 1
        key = _STAT_KEY.get(j.status, j.status)
        if key in stats:
            stats[key] += 1
    out_jobs = []
    for j in jobs:
        n_rows = s.exec(
            select(func.count()).select_from(OcrRow).where(OcrRow.job_id == j.job_id)
        ).one()
        out_jobs.append({
            "job_id": j.job_id,
            "image": os.path.basename(j.image_path) if j.image_path else None,
            "image_index": j.image_index,
            "status": j.status,
            "model": j.model,
            "backend": j.backend,
            "partial_reason": j.partial_reason,
            "reason_friendly": friendly_skip_reason(j.partial_reason),
            "rows": n_rows,
        })
    return {"stats": stats, "jobs": out_jobs}


def ocr_image_path(s: Session, job_id: int) -> str:
    """Resolve a job's screenshot file on disk. image_path is the source of truth
    and is kept updated as the file moves inbox→archive/failed (ocr.py)."""
    j = s.get(OcrJob, job_id)
    if j is None:
        raise HTTPException(404, "unknown job")
    if not j.image_path or not Path(j.image_path).is_file():
        raise HTTPException(404, "image file not found")
    return j.image_path


def read_ocr_result(s: Session, job_id: int) -> dict:
    j = s.get(OcrJob, job_id)
    if j is None:
        raise HTTPException(404, "unknown job")
    return {
        "job_id": j.job_id,
        "status": j.status,
        "partial_reason": j.partial_reason,
        "reason_friendly": friendly_skip_reason(j.partial_reason),
        "raw_json": j.raw_json or {},
    }


_ROW_FIELDS = (
    "row_id", "job_id", "row_type", "market", "code", "name", "sector", "temperature",
    "temperature_status", "strength", "right_side_days", "right_side_gain_pct", "jieqi",
    "first_hot_date", "last_cool_date",
    "review_status", "review_reason", "raw_fields",
)


def read_rows(s: Session, batch_id: str) -> list:
    from backend.ocr.review import is_truncated
    from backend.ocr.sectors import resolve_sectors
    from backend.filters.checks import detect_etf
    pairs = s.exec(
        select(OcrRow, OcrJob).join(OcrJob, OcrJob.job_id == OcrRow.job_id)
        .where(OcrJob.batch_id == batch_id)
    ).all()
    resolved = resolve_sectors(pairs)
    # sector_status_map：按板块名索引温度（板块行的 temperature_status）
    sector_status_map = {
        r.name: r.temperature_status
        for r, _job in pairs if r.row_type == "sector" and r.name
    }
    # is_truncated（Q3）：截断/无身份行标记。sector 用 resolve_sectors 解析（跨截图继承/按代码回填）。
    out = []
    for r, _job in pairs:
        d = {f: getattr(r, f) for f in _ROW_FIELDS}
        d["sector"] = resolved.get(r.row_id, r.sector)
        d["is_truncated"] = is_truncated(r)
        d["sector_status"] = sector_status_map.get(d["sector"])
        d["is_etf"] = detect_etf(r.name, r.code, r.sector)
        out.append(d)
    return out


def read_prescreen(s: Session, batch_id: str) -> dict:
    # latest-wins: a node re-run appends a new Manifest, so order by recency
    m = s.exec(
        select(Manifest).where(Manifest.batch_id == batch_id, Manifest.stage == "prescreen")
        .order_by(Manifest.created_at.desc())
    ).first()
    return (m.manifest_json or {}) if m else {}


def read_positions(s: Session, batch_id: str) -> list:
    rows = s.exec(select(Position).where(Position.batch_id == batch_id)).all()
    return [p.model_dump() for p in rows]


def read_holding_temps(s: Session, batch_id: str) -> list:
    """趋势动物持仓温度页行——供前端人工配对的下拉用（name + 真实 code + 温度）。
    tags 投影到顶层（raw_fields["tags"]），持仓页据此显示 OCR 标签。"""
    rows = s.exec(select(HoldingTemp).where(HoldingTemp.batch_id == batch_id)).all()
    out = []
    for h in rows:
        d = h.model_dump()
        d["tags"] = (h.raw_fields or {}).get("tags") or []
        d["signal_unavailable"] = (h.raw_fields or {}).get("signal_unavailable") or []
        out.append(d)
    return out


def read_b_filter(s: Session, batch_id: str) -> dict:
    m = s.exec(
        select(Manifest).where(Manifest.batch_id == batch_id, Manifest.stage == "b_filter")
        .order_by(Manifest.created_at.desc())
    ).first()
    if m is None:
        return {"white_list": [], "watch_list": [], "rejected": [], "manifest_json": {}}
    return {
        "white_list": m.white_list or [],
        "watch_list": (m.manifest_json or {}).get("watch_list", []),
        "rejected": m.rejected or [],
        "manifest_json": m.manifest_json or {},
    }


def read_exit_check(s: Session, batch_id: str) -> dict:
    rows = s.exec(select(ExitListItem).where(ExitListItem.batch_id == batch_id)).all()
    items = [e.model_dump() for e in rows]
    overview = build_exit_overview(s, batch_id=batch_id)
    return {"items": items, "overview": overview}


# --- HTTP endpoints ---

@router.get("/state/{batch_id}")
def api_state(batch_id: str):
    with Session(engine) as s:
        return read_state(s, batch_id)


@router.get("/batches")
def api_batches():
    with Session(engine) as s:
        return read_batches(s)


@router.get("/ocr/{batch_id}")
def api_ocr(batch_id: str):
    with Session(engine) as s:
        return read_ocr(s, batch_id)


@router.get("/ocr/result/{job_id}")
def api_ocr_result(job_id: int):
    with Session(engine) as s:
        return read_ocr_result(s, job_id)


@router.get("/ocr/image/{job_id}")
def api_ocr_image(job_id: int):
    with Session(engine) as s:
        return FileResponse(ocr_image_path(s, job_id))


@router.get("/aggregate/{batch_id}")
def api_aggregate(batch_id: str):
    with Session(engine) as s:
        return aggregate_by_category(s, batch_id)


@router.get("/rows/{batch_id}")
def api_rows(batch_id: str):
    with Session(engine) as s:
        return read_rows(s, batch_id)


@router.get("/prescreen/{batch_id}")
def api_prescreen(batch_id: str):
    with Session(engine) as s:
        return read_prescreen(s, batch_id)


@router.get("/positions/{batch_id}")
def api_positions(batch_id: str):
    with Session(engine) as s:
        return read_positions(s, batch_id)


@router.get("/holding_temp/{batch_id}")
def api_holding_temp(batch_id: str):
    with Session(engine) as s:
        return read_holding_temps(s, batch_id)


@router.get("/b_filter/{batch_id}")
def api_b_filter(batch_id: str):
    with Session(engine) as s:
        return read_b_filter(s, batch_id)


@router.get("/exit_check/{batch_id}")
def api_exit_check(batch_id: str):
    with Session(engine) as s:
        return read_exit_check(s, batch_id)
