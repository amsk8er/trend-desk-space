"""今日—选股—持仓—复盘主流程 API。"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from sqlmodel import Session, select

from backend import config
from backend.db import Batch, DailyReview, TradePlan
from backend.discipline.accounts import confirm_ocr_positions
from backend.discipline.broker import confirm_import, preview_import
from backend.discipline.data_sources import TushareProbeClient, probe_trend_animals
from backend.discipline.daily_data import (
    approve_budget, before_collection_window, china_now, china_trade_date,
    confirm_ocr_fallback, ensure_dataset, get_dataset_by_date,
    preview_ocr_fallback, run_daily_collection, serialize_dataset,
)
from backend.discipline.dataset_plan import latest_plan_for_date
from backend.discipline.day_card import build_day_card
from backend.discipline.plan import ensure_active_version, generate_plan, lock_plan, serialize_plan
from backend.discipline.review import generate_review
from backend.engine import engine
from backend.trend_animals.client import TrendAnimalsClient
from backend.llm import get_client
from backend.pipeline.nodes.positions import (
    get_positions_status, load_position_prompt, schedule_positions_run,
)

router = APIRouter(prefix="/api/discipline", tags=["discipline"])


def _http_error(exc: Exception):
    if isinstance(exc, KeyError):
        raise HTTPException(404, "not_found")
    if isinstance(exc, ValueError):
        raise HTTPException(422, str(exc))
    raise exc


@router.get("/version")
def version():
    with Session(engine) as s:
        row = ensure_active_version(s)
        return row.model_dump()


@router.get("/plans")
def plans(limit: int = 20):
    with Session(engine) as s:
        rows = s.exec(select(TradePlan).order_by(TradePlan.created_at.desc()).limit(min(max(limit, 1), 100))).all()
        return [x.model_dump() for x in rows]


@router.get("/data/today")
def data_today():
    trade_date = china_trade_date()
    with Session(engine) as s:
        row = ensure_dataset(s, trade_date)
        return {**serialize_dataset(s, row), "is_trade_day": row.error_code != "not_trade_day",
                "before_collection_window": before_collection_window(),
                "server_time_china": china_now().isoformat()}


@router.post("/data/today/check")
def data_today_check():
    if before_collection_window():
        raise HTTPException(409, {"code": "before_collection_window",
                                  "message": "北京时间16:30前不查询当日趋势数据"})
    trade_date = china_trade_date()
    trend = TrendAnimalsClient(); tushare = TushareProbeClient()
    try:
        with Session(engine) as s:
            return run_daily_collection(
                s, trend_client=trend, tushare_client=tushare,
                trade_date=trade_date, trigger="manual", manual=True,
            )
    finally:
        trend.close(); tushare.close()


@router.post("/data/{trade_date}/budget-approval")
def data_budget_approval(trade_date: str, amount: float = Body(..., embed=True)):
    try:
        with Session(engine) as s:
            return approve_budget(s, trade_date=trade_date, amount=amount)
    except Exception as exc:
        _http_error(exc)


@router.get("/data/probe")
def data_probe():
    """显式探针，不返回 token、原始持仓或完整行情。"""
    tushare = TushareProbeClient()
    trend = TrendAnimalsClient()
    try:
        return {"tushare": tushare.probe(), "trend_animals": probe_trend_animals(trend),
                "credentials": {"tushare": bool(os.getenv("TUSHARE_TOKEN")),
                                "trend_animals": trend.configured}}
    finally:
        tushare.close(); trend.close()


@router.get("/data/{trade_date}")
def data_by_date(trade_date: str):
    try:
        with Session(engine) as s:
            return serialize_dataset(s, get_dataset_by_date(s, trade_date))
    except Exception as exc:
        _http_error(exc)


@router.get("/plans/today")
def plans_today():
    try:
        with Session(engine) as s:
            return latest_plan_for_date(s, china_trade_date())
    except Exception as exc:
        _http_error(exc)


@router.get("/day-card")
def day_card_today(verbose: bool = False):
    """今日纪律卡片 Q1–Q4（只读库，不付费）。见 docs/agent-api-contract.md。"""
    with Session(engine) as s:
        return build_day_card(s, verbose=verbose)


@router.get("/day-card/{trade_date}")
def day_card_by_date(trade_date: str, verbose: bool = False):
    with Session(engine) as s:
        return build_day_card(s, trade_date=trade_date, verbose=verbose)


@router.post("/plan/generate")
def plan_generate(payload: dict = Body(...)):
    try:
        with Session(engine) as s:
            return generate_plan(s, payload)
    except Exception as exc:
        _http_error(exc)


@router.post("/positions/import")
async def positions_import(
        trade_date: str = Form(...), files: list[UploadFile] = File(...),
        backend: str | None = Form(None)):
    batch_id = f"account_{trade_date.replace('-', '')}"
    dest = config.DATA / "batches" / batch_id / "positions"
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, upload in enumerate(files):
        name = Path(upload.filename or f"position-{index}.png").name
        path = dest / name
        path.write_bytes(await upload.read()); paths.append(str(path))
    with Session(engine) as s:
        if s.get(Batch, batch_id) is None:
            s.add(Batch(batch_id=batch_id, date=trade_date, status="running")); s.commit()
    client = get_client(backend)
    prompt = load_position_prompt(config.ROOT / "prompts", client)
    return schedule_positions_run(
        engine=engine, client=client, batch_id=batch_id,
        image_paths=paths, prompt=prompt, archive_src=None,
    )


@router.get("/positions/import/status")
def positions_import_status(batch_id: str):
    return get_positions_status(batch_id)


@router.post("/positions/{batch_id}/confirm")
def positions_confirm(batch_id: str, payload: dict = Body(default_factory=dict)):
    try:
        with Session(engine) as s:
            return confirm_ocr_positions(
                s, batch_id=batch_id, position_ids=payload.get("position_ids"),
                nav=payload.get("nav"), cash=payload.get("cash"))
    except Exception as exc:
        _http_error(exc)


@router.post("/signals/{trade_date}/volatility/preview")
async def volatility_preview(trade_date: str, request: Request):
    """已忽略：Nick 确认波动率放大已融入「沸」，不再单独处理。"""
    raise HTTPException(
        410,
        {"code": "volatility_retired",
         "message": "波动率放大已融入沸，无需 OCR 或手工补录"},
    )


@router.post("/signals/{trade_date}/volatility/confirm")
def volatility_confirm(trade_date: str, payload: dict = Body(...)):
    """已忽略：Nick 确认波动率放大已融入「沸」。"""
    raise HTTPException(
        410,
        {"code": "volatility_retired",
         "message": "波动率放大已融入沸，无需 OCR 或手工补录"},
    )


@router.post("/data/{trade_date}/ocr-fallback/preview")
def ocr_fallback_preview(trade_date: str, payload: dict = Body(...)):
    try:
        with Session(engine) as s:
            return preview_ocr_fallback(
                s, trade_date=trade_date, batch_id=str(payload["batch_id"]))
    except Exception as exc:
        _http_error(exc)


@router.post("/data/{trade_date}/ocr-fallback/confirm")
def ocr_fallback_confirm(trade_date: str, payload: dict = Body(...)):
    try:
        with Session(engine) as s:
            return confirm_ocr_fallback(
                s, trade_date=trade_date, batch_id=str(payload["batch_id"]))
    except Exception as exc:
        _http_error(exc)


@router.get("/plan/{plan_id}")
def plan_get(plan_id: str):
    try:
        with Session(engine) as s:
            return serialize_plan(s, plan_id)
    except Exception as exc:
        _http_error(exc)


@router.post("/plan/{plan_id}/lock")
def plan_lock(plan_id: str):
    try:
        with Session(engine) as s:
            return lock_plan(s, plan_id)
    except Exception as exc:
        _http_error(exc)


@router.post("/broker/import/preview")
async def broker_preview(plan_id: str, file: UploadFile = File(...)):
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(413, "file_too_large")
    try:
        with Session(engine) as s:
            return preview_import(s, plan_id=plan_id, filename=file.filename or "upload.csv", content=content)
    except Exception as exc:
        _http_error(exc)


@router.post("/broker/import/{import_id}/confirm")
def broker_confirm(import_id: int):
    try:
        with Session(engine) as s:
            return confirm_import(s, import_id)
    except Exception as exc:
        _http_error(exc)


@router.post("/review/{plan_id}")
def review(plan_id: str):
    try:
        with Session(engine) as s:
            return generate_review(s, plan_id)
    except Exception as exc:
        _http_error(exc)


@router.get("/review/{plan_id}")
def review_get(plan_id: str):
    with Session(engine) as s:
        row = s.exec(select(DailyReview).where(DailyReview.plan_id == plan_id)
                     .order_by(DailyReview.created_at.desc())).first()
        if row is None:
            raise HTTPException(404, "review_not_generated")
        return row.model_dump()
