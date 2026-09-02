"""美股右侧资产研究页 HTTP API。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from sqlmodel import Session

from backend.engine import engine
from backend.trend_animals.errors import TrendAnimalsError
from backend.us_right_side.contracts import UsRightSideError
from backend.us_right_side.service import UsRightSideService


router = APIRouter(prefix="/api/us-right-side", tags=["us-right-side"])


def _raise(exc: Exception) -> None:
    if isinstance(exc, UsRightSideError):
        raise HTTPException(status_code=exc.status_code, detail=exc.as_payload()) from exc
    if isinstance(exc, TrendAnimalsError):
        status = exc.status_code if exc.status_code and exc.status_code >= 400 else 502
        raise HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": exc.message, "context": {}},
        ) from exc
    raise exc


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail={
            "code": "payload_invalid", "message": "请求体必须是对象", "context": {},
        })
    return value


def _budget(payload: dict) -> float:
    value = payload.get("approved_budget_cny")
    if isinstance(value, bool):
        value = None
    try:
        budget = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail={
            "code": "payload_invalid", "message": "approved_budget_cny 必须是正数", "context": {},
        })
    if budget <= 0:
        raise HTTPException(status_code=422, detail={
            "code": "payload_invalid", "message": "approved_budget_cny 必须是正数", "context": {},
        })
    return budget


def _preflight_hash(payload: dict) -> str:
    value = payload.get("preflight_hash")
    if not isinstance(value, str) or len(value) != 64:
        raise HTTPException(status_code=422, detail={
            "code": "payload_invalid", "message": "缺少有效的 preflight_hash", "context": {},
        })
    return value


@router.get("/capabilities")
def capabilities():
    return UsRightSideService().capabilities()


@router.get("/overview")
def overview():
    try:
        with Session(engine) as session:
            return UsRightSideService().overview(session)
    except Exception as exc:
        _raise(exc)


@router.post("/universe/preflight")
def universe_preflight():
    try:
        return UsRightSideService().universe_preflight()
    except Exception as exc:
        _raise(exc)


@router.post("/universe/refresh")
def universe_refresh(payload: dict = Body(default_factory=dict)):
    try:
        body = _object(payload)
        confirmation = body.get("confirm_unbounded_all_basic")
        if not isinstance(confirmation, bool):
            raise UsRightSideError(
                "payload_invalid", "confirm_unbounded_all_basic 必须是布尔值", status_code=422)
        approved = body.get("approved_budget_cny")
        budget = None if approved is None else _budget(body)
        return UsRightSideService().refresh_universe(
            confirm_unbounded_all_basic=confirmation, approved_budget_cny=budget)
    except Exception as exc:
        _raise(exc)


@router.post("/scans/preflight")
def scan_preflight():
    try:
        with Session(engine) as session:
            return UsRightSideService().scan_preflight(session)
    except Exception as exc:
        _raise(exc)


@router.post("/scans")
def scan(payload: dict = Body(...)):
    try:
        body = _object(payload)
        run_id = str(body.get("run_id") or "").strip()
        if not run_id:
            raise UsRightSideError("payload_invalid", "缺少 run_id", status_code=422)
        with Session(engine) as session:
            return UsRightSideService().run_scan(
                session, run_id, _budget(body), _preflight_hash(body))
    except Exception as exc:
        _raise(exc)


@router.get("/scans/{run_id}")
def scan_status(run_id: str):
    try:
        with Session(engine) as session:
            return UsRightSideService().run_status(session, run_id)
    except Exception as exc:
        _raise(exc)


@router.post("/enrichment/preflight")
def enrichment_preflight(payload: dict = Body(...)):
    try:
        body = _object(payload)
        run_id = str(body.get("run_id") or "").strip()
        stage = str(body.get("stage") or "standard")
        with Session(engine) as session:
            service = UsRightSideService()
            if stage == "age":
                return service.age_preflight(session, run_id)
            if stage == "strength":
                raw_max_days = body.get("max_days", 30)
                if isinstance(raw_max_days, bool):
                    raise UsRightSideError("payload_invalid", "max_days 必须是 30", status_code=422)
                try:
                    max_days = int(raw_max_days)
                except (TypeError, ValueError):
                    raise UsRightSideError("payload_invalid", "max_days 必须是 30", status_code=422)
                return service.strength_preflight(session, run_id, max_days=max_days)
            if stage == "standard":
                raw_top_n = body.get("top_n", 100)
                if isinstance(raw_top_n, bool):
                    raise UsRightSideError("payload_invalid", "top_n 必须是 100", status_code=422)
                try:
                    top_n = int(raw_top_n)
                except (TypeError, ValueError):
                    raise UsRightSideError("payload_invalid", "top_n 必须是 100", status_code=422)
                return service.standard_preflight(session, run_id, top_n=top_n)
            if stage == "industry":
                return service.industry_preflight(session, run_id)
            if stage == "deep":
                return service.deep_preflight(session, run_id)
            raise UsRightSideError("payload_invalid", "stage 必须是 age、strength、standard、industry 或 deep", status_code=422)
    except Exception as exc:
        _raise(exc)


@router.post("/enrichment")
def enrichment(payload: dict = Body(...)):
    try:
        body = _object(payload)
        run_id = str(body.get("run_id") or "").strip()
        stage = str(body.get("stage") or "standard")
        budget = _budget(body)
        with Session(engine) as session:
            service = UsRightSideService()
            if stage == "age":
                return service.run_age(session, run_id, budget, _preflight_hash(body))
            if stage == "strength":
                return service.run_strength(session, run_id, budget, _preflight_hash(body))
            if stage == "standard":
                return service.run_standard(session, run_id, budget, _preflight_hash(body))
            if stage == "industry":
                return service.run_industry(session, run_id, budget, _preflight_hash(body))
            if stage == "deep":
                return service.run_deep(session, run_id, budget, _preflight_hash(body))
            raise UsRightSideError("payload_invalid", "stage 必须是 age、strength、standard、industry 或 deep", status_code=422)
    except Exception as exc:
        _raise(exc)


@router.get("/assets")
def assets(
    run_id: str | None = None,
    query: str | None = None,
    temperature: str | None = None,
    phase: str | None = None,
    industry: str | None = None,
    days_min: int | None = None,
    days_max: int | None = None,
    strength_min: float | None = None,
    risk_only: bool = False,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    cursor: str | None = None,
    page_size: int = Query(default=40, ge=1, le=100),
):
    try:
        with Session(engine) as session:
            return UsRightSideService().list_assets(
                session, run_id=run_id, query=query, temperature=temperature, phase=phase,
                industry=industry, days_min=days_min, days_max=days_max,
                strength_min=strength_min, risk_only=risk_only,
                sort_by=sort_by, sort_dir=sort_dir, cursor=cursor, page_size=page_size,
            )
    except Exception as exc:
        _raise(exc)


@router.get("/assets/{tm_id}")
def asset_detail(tm_id: int, run_id: str = Query(...)):
    try:
        with Session(engine) as session:
            return UsRightSideService().asset_detail(session, run_id=run_id, tm_id=tm_id)
    except Exception as exc:
        _raise(exc)


@router.post("/assets/{tm_id}/plot")
def asset_plot(tm_id: int, payload: dict = Body(...)):
    try:
        body = _object(payload)
        if _budget(body) + 1e-9 < 0.1:
            raise UsRightSideError(
                "budget_confirmation_required", "趋势图预计 0.10 元，请批准足额预算",
                context={"estimated_cost_cny": 0.1},
            )
        with Session(engine) as session:
            return UsRightSideService().trend_plot(
                session, str(body.get("run_id") or ""), tm_id)
    except Exception as exc:
        _raise(exc)
