"""今日—选股—持仓—复盘主流程 API。"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from sqlmodel import Session, select

from backend import config
from backend.db import Batch, BrokerImport, DailyReview, TradePlan
from backend.discipline.accounts import confirm_ocr_positions
from backend.discipline.broker import confirm_import, preview_import
from backend.discipline.execution_ocr import (
    cleanup_execution_images,
    execution_temp_path,
    get_execution_preview_status,
    schedule_execution_preview,
)
from backend.discipline.ledger import (
    add_adjustment,
    confirm_no_execution,
    ledger_status,
    roll_forward,
    update_fee_schedule,
)
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
from backend.analysis.industry_heat import report as industry_heat_report
from backend.analysis.industry_heat import refresh as refresh_industry_heat
from backend.decision_events import append_decision_event
from backend.engine import engine
from backend.trend_animals.client import TrendAnimalsClient
from backend.trend_animals.errors import TrendAnimalsError
from backend.llm import get_client
from backend.pipeline.nodes.positions import (
    get_positions_status, load_position_prompt, schedule_positions_run,
)

router = APIRouter(prefix="/api/discipline", tags=["discipline"])


def _record_execution_events(session: Session, result: dict) -> None:
    """Mirror confirmed broker facts into the cross-market audit journal."""
    audit = result.get("import") or {}
    for execution in result.get("executions") or []:
        execution_id = execution.get("execution_id")
        append_decision_event(session, {
            "idempotency_key": f"discipline:execution_confirmed:{execution_id}",
            "trade_date": execution.get("trade_date"),
            "market": "a_share",
            "event_type": "execution_confirmed",
            "instrument_id": execution.get("instrument_id"),
            "plan_id": audit.get("plan_id"),
            "reason_code": "broker_import_confirmed",
            "payload_json": {
                "execution_id": execution_id,
                "import_id": audit.get("import_id"),
                "side": execution.get("side"),
                "shares": execution.get("shares"),
                "price": execution.get("price"),
            },
        })


def _http_error(exc: Exception):
    if isinstance(exc, KeyError):
        raise HTTPException(404, "not_found")
    if isinstance(exc, ValueError):
        raise HTTPException(422, str(exc))
    if isinstance(exc, TrendAnimalsError):
        status = 409 if exc.code in {"confirmation_required", "data_stale"} else 422
        if exc.code in {"not_configured", "disabled"}:
            status = 503
        elif exc.code in {"upstream_error", "rate_limited"}:
            status = 502
        raise HTTPException(status, {"code": exc.code, "message": exc.message})
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
            approved = approve_budget(s, trade_date=trade_date, amount=amount)
        # “批准额度”就是对本次付费请求的明确授权；当日立即续跑，避免在
        # 20:00 后只改成 pending 却再也等不到调度。历史日期绝不拿当前数据补采。
        if trade_date != china_trade_date():
            return approved
        trend = TrendAnimalsClient(); tushare = TushareProbeClient()
        try:
            with Session(engine) as s:
                return run_daily_collection(
                    s, trend_client=trend, tushare_client=tushare,
                    trade_date=trade_date, trigger="budget_approval", manual=True,
                )
        finally:
            trend.close(); tushare.close()
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


@router.get("/industry-heat")
def industry_heat(trade_date: str | None = None, limit: int = 100):
    """行业主线雷达；只读本地快照，GET 不触发任何外部调用。"""
    with Session(engine) as s:
        return industry_heat_report(s, trade_date=trade_date, limit=limit)


@router.post("/industry-heat/{trade_date}/wind-refresh")
def industry_heat_wind_refresh(
    trade_date: str, top_n: int = Body(default=8, embed=True),
):
    """仅重跑派生热度与 Top-N Wind 验证，不重复请求趋势动物。"""
    try:
        with Session(engine) as s:
            dataset = get_dataset_by_date(s, trade_date)
            result = refresh_industry_heat(
                s, dataset.dataset_id, use_wind=True,
                top_n=max(5, min(int(top_n), 10)),
            )
            report = industry_heat_report(s, trade_date=trade_date, limit=100)
            return {**report, "refresh": result}
    except Exception as exc:
        _http_error(exc)


@router.post("/plan/generate")
def plan_generate(payload: dict = Body(...)):
    try:
        with Session(engine) as s:
            result = generate_plan(s, payload)
            append_decision_event(s, {
                "idempotency_key": f"discipline:plan_generated:{result['plan_id']}",
                "trade_date": result["signal_date"],
                "market": "a_share",
                "event_type": "plan_generated",
                "plan_id": result["plan_id"],
                "discipline_version": result.get("discipline_version"),
                "dataset_id": result.get("dataset_id"),
                "facts_hash": result.get("input_hash") or result.get("rules_hash"),
                "payload_json": {
                    "execute_date": result.get("execute_date"),
                    "plan_stage": result.get("plan_stage"),
                    "status": result.get("status"),
                },
            })
            selection = result.get("selection") or result.get("selection_snapshot") or {}
            for candidate in selection.get("rejected") or []:
                instrument_id = str(candidate.get("code") or candidate.get("instrument_id") or "").strip()
                if not instrument_id:
                    continue
                append_decision_event(s, {
                    "idempotency_key": (
                        f"discipline:candidate_excluded:{result['plan_id']}:{instrument_id}"
                    ),
                    "trade_date": result["signal_date"],
                    "market": "a_share",
                    "event_type": "candidate_excluded",
                    "instrument_id": instrument_id,
                    "plan_id": result["plan_id"],
                    "reason_code": "selection_rule_failed",
                    "discipline_version": result.get("discipline_version"),
                    "dataset_id": result.get("dataset_id"),
                    "payload_json": {
                        "failed_rules": candidate.get("failed_rules") or [],
                        "name": candidate.get("name"),
                    },
                })
            health_errors = (result.get("data_health") or {}).get("errors") or []
            if health_errors:
                append_decision_event(s, {
                    "idempotency_key": f"discipline:risk_blocked:{result['plan_id']}:data_health",
                    "trade_date": result["signal_date"],
                    "market": "a_share",
                    "event_type": "risk_blocked",
                    "plan_id": result["plan_id"],
                    "reason_code": "data_health_blocked",
                    "discipline_version": result.get("discipline_version"),
                    "dataset_id": result.get("dataset_id"),
                    "payload_json": {"errors": health_errors},
                })
            return result
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
        with TrendAnimalsClient() as trend_client, Session(engine) as s:
            result = confirm_ocr_positions(
                s, batch_id=batch_id, position_ids=payload.get("position_ids"),
                nav=payload.get("nav"), cash=payload.get("cash"),
                trend_client=trend_client)
            from backend.discipline.automation import maybe_finalize_after_input
            automation = maybe_finalize_after_input(
                s,
                trade_date=str(result.get("signal_trade_date") or ""),
                trigger="account_confirmation",
            )
            if automation is not None:
                result["automation"] = automation
            return result
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
            result = confirm_import(s, import_id)
            _record_execution_events(s, result)
            from backend.discipline.automation import maybe_finalize_after_input
            trade_dates = sorted({
                str(row.get("trade_date") or "") for row in result.get("executions", [])
                if row.get("trade_date")
            })
            if len(trade_dates) == 1:
                automation = maybe_finalize_after_input(
                    s, trade_date=trade_dates[0], trigger="execution_confirmation",
                )
                if automation is not None:
                    result["automation"] = automation
            return result
    except Exception as exc:
        _http_error(exc)


@router.post("/executions/ocr/preview")
async def executions_ocr_preview(
    trade_date: str = Form(...),
    files: list[UploadFile] = File(...),
    backend: str | None = Form(None),
):
    if not files or len(files) > 10:
        raise HTTPException(422, "execution_images_must_be_1_to_10")
    paths: list[str] = []
    filenames: list[str] = []
    total = 0
    scheduled = False
    try:
        for upload in files:
            content = await upload.read()
            total += len(content)
            if not content or total > 20 * 1024 * 1024:
                raise HTTPException(413, "execution_images_too_large")
            path = execution_temp_path(upload.filename or "execution.png")
            path.write_bytes(content)
            paths.append(str(path))
            filenames.append(upload.filename or path.name)
        result = schedule_execution_preview(
            engine=engine,
            trade_date=trade_date,
            filenames=filenames,
            image_paths=paths,
            client=get_client(backend),
        )
        scheduled = True
        return result
    except Exception as exc:
        _http_error(exc)
    finally:
        if not scheduled:
            cleanup_execution_images(paths)


@router.get("/executions/ocr/status")
def executions_ocr_status(job_id: str):
    try:
        return get_execution_preview_status(job_id)
    except Exception as exc:
        _http_error(exc)


@router.post("/executions/ocr/{batch_id}/confirm")
def executions_ocr_confirm(batch_id: str, payload: dict = Body(default_factory=dict)):
    if payload.get("confirmed") is not True:
        raise HTTPException(422, "explicit_confirmation_required")
    try:
        with Session(engine) as s:
            audit = s.exec(select(BrokerImport).where(
                BrokerImport.batch_id == batch_id,
                BrokerImport.source == "broker_ocr",
            )).first()
            if audit is None or audit.import_id is None:
                raise KeyError(batch_id)
            if not audit.parsed_rows:
                raise ValueError("no_valid_executions")
            if audit.anomaly_rows and payload.get("accept_valid_rows_only") is not True:
                raise ValueError("execution_anomalies_require_acknowledgement")
            result = confirm_import(s, audit.import_id)
            _record_execution_events(s, result)
            from backend.discipline.automation import maybe_finalize_after_input
            automation = maybe_finalize_after_input(
                s,
                trade_date=str(audit.field_mapping.get("trade_date")),
                trigger="execution_confirmation",
            )
            if automation is not None:
                result["automation"] = automation
            return result
    except Exception as exc:
        _http_error(exc)


@router.post("/trading-days/{trade_date}/no-execution")
def trading_day_no_execution(trade_date: str, payload: dict = Body(default_factory=dict)):
    if payload.get("confirmed") is not True:
        raise HTTPException(422, "explicit_confirmation_required")
    try:
        with Session(engine) as s:
            result = confirm_no_execution(s, trade_date, payload.get("note"))
            plan = s.exec(select(TradePlan).where(
                TradePlan.execute_date == trade_date,
            ).order_by(TradePlan.created_at.desc())).first()
            append_decision_event(s, {
                "idempotency_key": f"discipline:no_execution:{trade_date}",
                "trade_date": trade_date,
                "market": "a_share",
                "event_type": "no_trade_confirmed",
                "plan_id": plan.plan_id if plan else None,
                "reason_code": "manual_confirmation",
                "note": payload.get("note"),
                "discipline_version": plan.discipline_version if plan else None,
                "dataset_id": plan.dataset_id if plan else None,
                "payload_json": {"confirmation_id": result.get("confirmation_id")},
            })
            from backend.discipline.automation import maybe_finalize_after_input
            automation = maybe_finalize_after_input(
                s, trade_date=trade_date, trigger="no_execution_confirmation",
            )
            if automation is not None:
                result["automation"] = automation
            return result
    except Exception as exc:
        _http_error(exc)


@router.post("/ledger/adjustments")
def ledger_adjustment_create(payload: dict = Body(...)):
    try:
        with Session(engine) as s:
            return add_adjustment(s, payload).model_dump()
    except Exception as exc:
        _http_error(exc)


@router.post("/ledger/fee-schedule")
def ledger_fee_schedule(payload: dict = Body(...)):
    try:
        with Session(engine) as s:
            result = update_fee_schedule(s, payload).model_dump()
            from backend.discipline.automation import maybe_finalize_after_input
            maybe_finalize_after_input(
                s, trade_date=china_trade_date(), trigger="fee_schedule_confirmation",
            )
            return result
    except Exception as exc:
        _http_error(exc)


@router.post("/ledger/{trade_date}/roll-forward")
def ledger_roll_forward(trade_date: str):
    try:
        with Session(engine) as s:
            return roll_forward(s, trade_date).model_dump()
    except Exception as exc:
        _http_error(exc)


@router.get("/ledger/status")
def ledger_status_get(trade_date: str | None = None):
    with Session(engine) as s:
        return ledger_status(s, trade_date)


@router.post("/review/{plan_id}")
def review(plan_id: str):
    try:
        with Session(engine) as s:
            result = generate_review(s, plan_id)
            plan = s.get(TradePlan, plan_id)
            append_decision_event(s, {
                "idempotency_key": f"discipline:review_completed:{result['review_id']}",
                "trade_date": result["trade_date"],
                "market": "a_share",
                "event_type": "review_completed",
                "plan_id": plan_id,
                "discipline_version": plan.discipline_version if plan else None,
                "dataset_id": plan.dataset_id if plan else None,
                "reason_code": result.get("discipline_result"),
                "payload_json": {
                    "review_id": result.get("review_id"),
                    "discipline_score": result.get("discipline_score"),
                    "plan_completion_rate": result.get("plan_completion_rate"),
                    "violations": result.get("violations") or [],
                },
            })
            return result
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
