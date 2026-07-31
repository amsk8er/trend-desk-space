"""美股手工执行台 HTTP 契约。

本 router 故意没有订单、凭证、签名、私有账户或撤单端点。所有交易动作都由
用户在 Bitget 手工完成后再回填本地台账。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile
from sqlmodel import Session

from backend import config
from backend.engine import engine
from backend.llm import get_client
from backend.us_manual import repository
from backend.us_manual.account_ocr import (
    ALLOWED_IMAGE_SUFFIXES,
    ALLOWED_IMAGE_TYPES,
    MAX_IMAGE_BYTES,
    MAX_OCR_IMAGES,
    batch_payload as account_ocr_payload,
    confirm_account_ocr,
    create_batch as create_account_ocr_batch,
    load_prompt as load_account_ocr_prompt,
    schedule_account_ocr,
)
from backend.us_manual.allocation import allocation_payload, create_allocation_preview
from backend.us_manual.contracts import UsManualError
from backend.us_manual.etf_benchmarks import EtfBenchmarkService
from backend.us_manual.exits import mark_stop_triggered, position_actions
from backend.us_manual.ledger import confirm_execution, correct_execution, preview_execution
from backend.us_manual.plans import (
    confirm_no_trade_for_run,
    create_exit_plan,
    create_draft,
    lock_plan,
    mark_no_execution,
    serialize_plan,
)
from backend.us_manual.reviews import generate_review, list_reviews
from backend.us_manual.risk_anchor import RiskAnchorService
from backend.us_manual.service import UsManualService


router = APIRouter(prefix="/api/us-manual", tags=["us-manual"])


def _enabled() -> None:
    if not config.US_MANUAL_DESK_ENABLED:
        raise HTTPException(status_code=404, detail={"code": "us_manual_disabled", "message": "美股手工执行台已关闭"})


def _payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail={"code": "payload_invalid", "message": "请求体必须是对象"})
    return value


def _strict_bool(payload: dict[str, Any], key: str, *, default: bool = False) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise HTTPException(
            status_code=422,
            detail={"code": "payload_invalid", "message": f"{key} 必须是布尔值"},
        )
    return value


def _raise(exc: Exception) -> None:
    if isinstance(exc, UsManualError):
        raise HTTPException(status_code=exc.status_code, detail=exc.as_payload()) from exc
    raise exc


@router.get("/capabilities")
def capabilities():
    service = UsManualService()
    return service.capabilities()


@router.get("/overview")
def overview(as_of: str | None = Query(default=None)):
    _enabled()
    try:
        with Session(engine) as session:
            return UsManualService().overview(session, as_of=as_of)
    except Exception as exc:
        _raise(exc)


@router.post("/account/ocr")
async def account_ocr_import(
    capture_date: str = Form(...),
    files: list[UploadFile] = File(...),
    backend: str | None = Form(default=None),
):
    """Stage Bitget account screenshots; OCR never writes executions or lots."""
    _enabled()
    try:
        try:
            date.fromisoformat(capture_date)
        except ValueError as exc:
            raise UsManualError("account_capture_date_invalid", "截图日期必须为 YYYY-MM-DD", 422) from exc
        if not files or len(files) > MAX_OCR_IMAGES:
            raise UsManualError(
                "account_ocr_image_count_invalid",
                f"每次请上传 1–{MAX_OCR_IMAGES} 张完整账户截图",
                422,
            )
        upload_key = f"upload-{uuid4().hex[:16]}"
        destination = config.DATA / "research" / "bitget" / "us_account_ocr" / upload_key
        destination.mkdir(parents=True, exist_ok=False)
        paths: list[str] = []
        for index, upload in enumerate(files):
            suffix = Path(upload.filename or "").suffix.lower()
            media_type = str(upload.content_type or "").lower()
            if suffix not in ALLOWED_IMAGE_SUFFIXES or media_type not in ALLOWED_IMAGE_TYPES:
                raise UsManualError(
                    "account_ocr_image_type_invalid",
                    "账户截图仅支持 PNG、JPEG、WebP 或 HEIC 图片",
                    422,
                )
            content = await upload.read(MAX_IMAGE_BYTES + 1)
            if not content or len(content) > MAX_IMAGE_BYTES:
                raise UsManualError(
                    "account_ocr_image_size_invalid",
                    "每张账户截图必须非空且不超过 10MB",
                    422,
                )
            path = destination / f"account-{index + 1}{suffix}"
            path.write_bytes(content)
            paths.append(str(path))
        llm_client = get_client(backend)
        with Session(engine) as session:
            batch = create_account_ocr_batch(
                session, capture_date=capture_date,
                provider=getattr(llm_client, "name", backend), image_paths=paths,
            )
            response = account_ocr_payload(session, batch)
        response["schedule"] = schedule_account_ocr(
            engine=engine, batch_id=batch.batch_id, image_paths=paths,
            client=llm_client, prompt=load_account_ocr_prompt(),
        )
        return response
    except Exception as exc:
        _raise(exc)


@router.get("/account/ocr/latest")
def account_ocr_latest():
    _enabled()
    try:
        with Session(engine) as session:
            batch = repository.latest_account_ocr_batch(session)
            return account_ocr_payload(session, batch) if batch is not None else None
    except Exception as exc:
        _raise(exc)


@router.get("/account/ocr/{batch_id}")
def account_ocr_get(batch_id: str):
    _enabled()
    try:
        with Session(engine) as session:
            return account_ocr_payload(session, repository.get_account_ocr_batch(session, batch_id))
    except Exception as exc:
        _raise(exc)


@router.post("/account/ocr/{batch_id}/confirm")
def account_ocr_confirm(batch_id: str, payload: dict = Body(...)):
    _enabled()
    try:
        with Session(engine) as session:
            return confirm_account_ocr(
                session, batch_id=batch_id, payload=_payload(payload),
            )
    except Exception as exc:
        _raise(exc)


@router.get("/universe/latest")
def universe_latest():
    _enabled()
    try:
        with Session(engine) as session:
            return UsManualService().universe_latest(session)
    except Exception as exc:
        _raise(exc)


@router.post("/universe/preflight")
def universe_preflight():
    _enabled()
    try:
        with Session(engine) as session:
            return UsManualService().universe_preflight(session)
    except Exception as exc:
        _raise(exc)


@router.post("/universe/refresh")
def universe_refresh(payload: dict = Body(default_factory=dict)):
    _enabled()
    try:
        body = _payload(payload)
        with Session(engine) as session:
            return UsManualService().universe_refresh(
                session,
                confirm_unbounded_all_basic=_strict_bool(body, "confirm_unbounded_all_basic"),
            )
    except Exception as exc:
        _raise(exc)


@router.post("/runs/preflight")
def runs_preflight():
    _enabled()
    raise HTTPException(
        status_code=410,
        detail={"code": "h1_endpoint_retired", "message": "H1 多步骤预检已退役；请调用 /runs/collect"},
    )


@router.post("/runs/collect")
def runs_collect():
    _enabled()
    try:
        with Session(engine) as session:
            return UsManualService().collect(session, trigger="manual")
    except Exception as exc:
        _raise(exc)


@router.get("/runs/{run_id}")
def run_get(run_id: str):
    _enabled()
    try:
        with Session(engine) as session:
            run = repository.get_run(session, run_id)
            return UsManualService().run_payload(session, run)
    except Exception as exc:
        _raise(exc)


@router.post("/runs/{run_id}/scan")
def run_scan(run_id: str, payload: dict = Body(...)):
    _enabled()
    raise HTTPException(
        status_code=410,
        detail={"code": "h1_endpoint_retired", "message": "H1 基础扫描已退役；请调用 /runs/collect"},
    )


@router.post("/runs/{run_id}/enrichment/preflight")
def enrichment_preflight(run_id: str):
    _enabled()
    raise HTTPException(
        status_code=410,
        detail={"code": "h1_endpoint_retired", "message": "H1 补充预检已退役；H6 采集会在 ¥5 上限内自动完成"},
    )


@router.post("/runs/{run_id}/enrichment")
def enrichment(run_id: str, payload: dict = Body(...)):
    _enabled()
    raise HTTPException(
        status_code=410,
        detail={"code": "h1_endpoint_retired", "message": "H1 补充接口已退役；请调用 /runs/collect"},
    )


@router.post("/sizing/preview")
def sizing(payload: dict = Body(...)):
    _enabled()
    raise HTTPException(
        status_code=410,
        detail={
            "code": "h5_sizing_retired",
            "message": "H5 单候选止损仓位预览已退役；H6 请调用 /allocation-previews",
        },
    )


@router.post("/allocation-previews")
def allocation_preview_create(payload: dict = Body(...)):
    _enabled()
    try:
        with Session(engine) as session:
            return create_allocation_preview(session, _payload(payload))
    except Exception as exc:
        _raise(exc)


@router.get("/allocation-previews/{preview_id}")
def allocation_preview_get(preview_id: str):
    _enabled()
    try:
        with Session(engine) as session:
            return allocation_payload(session, preview_id)
    except Exception as exc:
        _raise(exc)


@router.get("/quotes/{venue_instrument}")
def quote(venue_instrument: str):
    _enabled()
    try:
        with Session(engine) as session:
            return UsManualService().quote(session, venue_instrument=venue_instrument)
    except Exception as exc:
        _raise(exc)


@router.get("/candidates/{candidate_id}")
def candidate_get(candidate_id: int):
    _enabled()
    try:
        with Session(engine) as session:
            return UsManualService().candidate_detail(session, candidate_id=candidate_id)
    except Exception as exc:
        _raise(exc)


@router.post("/candidates/{candidate_id}/quote/refresh")
def candidate_quote_refresh(candidate_id: int):
    _enabled()
    raise HTTPException(
        status_code=410,
        detail={
            "code": "h4_quote_refresh_retired",
            "message": "单独报价刷新已退役；H6 必须统一刷新报价、1D/1H 和 EP3 风险锚点",
            "next_step": f"调用 /api/us-manual/candidates/{candidate_id}/risk-anchor/refresh",
        },
    )


@router.post("/candidates/{candidate_id}/risk-anchor/refresh")
def candidate_risk_anchor_refresh(candidate_id: int):
    _enabled()
    try:
        with Session(engine) as session:
            return RiskAnchorService().refresh(session, candidate_id=candidate_id)
    except Exception as exc:
        _raise(exc)


@router.get("/risk-anchors/{anchor_id}")
def risk_anchor_get(anchor_id: str):
    _enabled()
    try:
        with Session(engine) as session:
            return RiskAnchorService.payload(repository.get_risk_anchor(session, anchor_id))
    except Exception as exc:
        _raise(exc)


@router.post("/candidates/{candidate_id}/stop-suggestion/refresh")
def candidate_stop_suggestion_refresh(candidate_id: int):
    _enabled()
    raise HTTPException(
        status_code=410,
        detail={
            "code": "h5_stop_suggestion_retired",
            "message": "H5 Wind 双源止损已退役；历史证据只读，H6 请刷新 risk-anchor",
            "next_step": f"/api/us-manual/candidates/{candidate_id}/risk-anchor/refresh",
        },
    )


@router.get("/stop-suggestions/{suggestion_id}")
def stop_suggestion_get(suggestion_id: str):
    _enabled()
    try:
        with Session(engine) as session:
            suggestion = repository.get_stop_suggestion(session, suggestion_id)
            return {**repository.model_payload(suggestion), "legacy_read_only": True}
    except Exception as exc:
        _raise(exc)


@router.post("/stop-suggestions/{suggestion_id}/review")
def stop_suggestion_review(suggestion_id: str, payload: dict = Body(...)):
    _enabled()
    raise HTTPException(
        status_code=410,
        detail={"code": "h5_stop_review_retired", "message": "H5 止损建议只读，不再接受人工定稿"},
    )


@router.get("/etf-benchmarks")
def etf_benchmarks_list():
    _enabled()
    try:
        with Session(engine) as session:
            service = EtfBenchmarkService()
            return [service.payload(session, row) for row in repository.list_etf_benchmarks(session)]
    except Exception as exc:
        _raise(exc)


@router.post("/etf-benchmarks/census")
def etf_benchmarks_census(payload: dict = Body(default_factory=dict)):
    _enabled()
    try:
        body = _payload(payload)
        refresh_missing = _strict_bool(body, "refresh_missing")
        start_after = body.get("start_after")
        if start_after is not None and not isinstance(start_after, str):
            raise UsManualError("etf_census_cursor_invalid", "ETF 普查游标必须是 ticker 字符串", 422)
        batch_size = body.get("batch_size", 5)
        if isinstance(batch_size, bool):
            raise UsManualError("etf_census_batch_invalid", "ETF 普查每批必须为 1–10 只", 422)
        try:
            parsed_batch_size = int(batch_size)
        except (TypeError, ValueError) as exc:
            raise UsManualError("etf_census_batch_invalid", "ETF 普查批次不是整数", 422) from exc
        with Session(engine) as session:
            return UsManualService().etf_benchmark_census(
                session,
                refresh_missing=refresh_missing,
                batch_size=parsed_batch_size,
                start_after=start_after,
            )
    except Exception as exc:
        _raise(exc)


@router.post("/etf-benchmarks/{ticker_symbol}/refresh")
def etf_benchmark_refresh(ticker_symbol: str):
    _enabled()
    try:
        with Session(engine) as session:
            return EtfBenchmarkService().refresh(
                session, ticker_symbol=ticker_symbol, force=True,
            )
    except Exception as exc:
        _raise(exc)


@router.get("/etf-benchmark-evidence/{evidence_id}")
def etf_benchmark_evidence_get(evidence_id: str):
    _enabled()
    try:
        with Session(engine) as session:
            evidence = repository.get_etf_benchmark(session, evidence_id)
            return EtfBenchmarkService().payload(session, evidence)
    except Exception as exc:
        _raise(exc)


@router.post("/etf-benchmark-evidence/{evidence_id}/review")
def etf_benchmark_evidence_review(evidence_id: str, payload: dict = Body(...)):
    _enabled()
    try:
        with Session(engine) as session:
            return EtfBenchmarkService().review(
                session, evidence_id=evidence_id, payload=_payload(payload),
            )
    except Exception as exc:
        _raise(exc)


@router.post("/plans")
def plans_create(payload: dict = Body(...)):
    _enabled()
    try:
        with Session(engine) as session:
            return create_draft(session, _payload(payload))
    except Exception as exc:
        _raise(exc)


@router.get("/plans")
def plans_list(limit: int = Query(default=50, ge=1, le=100)):
    _enabled()
    try:
        with Session(engine) as session:
            return [serialize_plan(session, row) for row in repository.list_plans(session, limit=limit)]
    except Exception as exc:
        _raise(exc)


@router.get("/plans/{plan_id}")
def plan_get(plan_id: str):
    _enabled()
    try:
        with Session(engine) as session:
            return serialize_plan(session, repository.get_plan(session, plan_id))
    except Exception as exc:
        _raise(exc)


@router.post("/plans/{plan_id}/lock")
def plan_lock(plan_id: str):
    _enabled()
    try:
        with Session(engine) as session:
            return lock_plan(session, plan_id)
    except Exception as exc:
        _raise(exc)


@router.post("/plans/{plan_id}/no-execution")
def plan_no_execution(plan_id: str, payload: dict = Body(default_factory=dict)):
    _enabled()
    try:
        with Session(engine) as session:
            return mark_no_execution(session, plan_id, note=str(_payload(payload).get("note") or "") or None)
    except Exception as exc:
        _raise(exc)


@router.post("/runs/{run_id}/no-execution")
def run_no_execution(run_id: str, payload: dict = Body(default_factory=dict)):
    _enabled()
    try:
        with Session(engine) as session:
            return confirm_no_trade_for_run(session, run_id=run_id,
                                             note=str(_payload(payload).get("note") or "") or None)
    except Exception as exc:
        _raise(exc)


@router.post("/plan-items/{item_id}/executions/preview")
def execution_preview(item_id: int, payload: dict = Body(...)):
    _enabled()
    try:
        with Session(engine) as session:
            return preview_execution(session, item_id=item_id, payload=_payload(payload))
    except Exception as exc:
        _raise(exc)


@router.post("/plan-items/{item_id}/executions/confirm")
def execution_confirm(item_id: int, payload: dict = Body(...)):
    _enabled()
    try:
        with Session(engine) as session:
            return confirm_execution(session, item_id=item_id, payload=_payload(payload))
    except Exception as exc:
        _raise(exc)


@router.post("/executions/{execution_id}/corrections")
def execution_correction(execution_id: int, payload: dict = Body(...)):
    _enabled()
    try:
        with Session(engine) as session:
            return correct_execution(session, execution_id=execution_id, payload=_payload(payload))
    except Exception as exc:
        _raise(exc)


@router.get("/positions")
def positions(as_of: str | None = Query(default=None)):
    _enabled()
    try:
        with Session(engine) as session:
            return position_actions(session, as_of_date=as_of)
    except Exception as exc:
        _raise(exc)


@router.get("/positions/actions")
def position_action_list(as_of: str | None = Query(default=None)):
    """Explicit H6 alias kept separate from the legacy position collection name."""
    _enabled()
    try:
        with Session(engine) as session:
            return position_actions(session, as_of_date=as_of)
    except Exception as exc:
        _raise(exc)


@router.get("/exit-decisions")
def exit_decisions(as_of: str | None = Query(default=None)):
    _enabled()
    try:
        with Session(engine) as session:
            return [
                repository.model_payload(row)
                for row in repository.latest_exit_decisions(session, as_of_date=as_of)
            ]
    except Exception as exc:
        _raise(exc)


@router.get("/exit-decisions/{decision_id}")
def exit_decision_get(decision_id: str):
    _enabled()
    try:
        with Session(engine) as session:
            return repository.model_payload(repository.get_exit_decision(session, decision_id))
    except Exception as exc:
        _raise(exc)


@router.post("/exit-decisions/{decision_id}/plan")
def exit_decision_plan(decision_id: str):
    _enabled()
    try:
        with Session(engine) as session:
            decision = repository.get_exit_decision(session, decision_id)
            return create_exit_plan(
                session, lot_id=decision.lot_id, as_of_date=decision.as_of_date,
            )
    except Exception as exc:
        _raise(exc)


@router.post("/positions/{lot_id}/stop-triggered")
def position_stop_triggered(lot_id: int):
    _enabled()
    try:
        with Session(engine) as session:
            return mark_stop_triggered(session, lot_id=lot_id)
    except Exception as exc:
        _raise(exc)


@router.post("/positions/{lot_id}/exit-plan")
def position_exit_plan(lot_id: int, payload: dict = Body(default_factory=dict)):
    _enabled()
    try:
        with Session(engine) as session:
            return create_exit_plan(session, lot_id=lot_id, as_of_date=_payload(payload).get("as_of_date"))
    except Exception as exc:
        _raise(exc)


@router.get("/reviews")
def reviews(as_of: str | None = Query(default=None)):
    _enabled()
    try:
        with Session(engine) as session:
            return list_reviews(session, as_of_date=as_of)
    except Exception as exc:
        _raise(exc)


@router.post("/reviews")
def review_generate(payload: dict = Body(...)):
    _enabled()
    try:
        data = _payload(payload)
        as_of = str(data.get("as_of_date") or "")
        if not as_of:
            raise UsManualError("as_of_required", "生成复盘需要 as_of_date", 422)
        with Session(engine) as session:
            return generate_review(session, as_of_date=as_of, plan_id=data.get("plan_id"), notes=data.get("notes"))
    except Exception as exc:
        _raise(exc)
