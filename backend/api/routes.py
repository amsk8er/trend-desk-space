# backend/api/routes.py
from pathlib import Path
from fastapi import APIRouter, HTTPException, Body, File, Form, UploadFile
from sqlmodel import Session
from backend.engine import engine
from backend.db import Batch
from backend import config, backup
from backend.llm import get_client
from backend.pipeline.nodes.import_ import import_batch
from backend.ocr.runner import schedule_ocr_run, cancel_ocr_run
from backend.pipeline.nodes.positions import (
    schedule_positions_run, get_positions_status, load_position_prompt,
)
from backend.pipeline.nodes.holding_temp import (
    set_position_code,
    schedule_holding_temp_run, get_holding_temp_status,
)
from backend.pipeline.nodes.review import review_summary, run_all_can_proceed
from backend.pipeline.nodes.aggregate import run_aggregate
from backend.pipeline.nodes.prescreen import run_prescreen
from backend.pipeline.nodes.b_filter import run_b_filter
from backend.pipeline.nodes.exit_check import run_exit_check
from backend.pipeline.nodes.report import compose_report_with_brief
from backend.pipeline.nodes.push import push_node
from backend.pipeline.orchestrator import run_auto
from backend.chat.tools import run_tool, REGISTRY, ToolForbidden
from backend.chat import conversation
from backend.trend_animals.client import TrendAnimalsClient
from backend.trend_animals.errors import TrendAnimalsError
from backend.trend_animals.holding_sync import estimate_holding_sync, run_holding_sync
from backend.trend_animals.selection import estimate_selection, run_selection_pipeline

_PROMPTS = Path(__file__).parent.parent.parent / "prompts"


def _save_uploads(files: list[UploadFile], batch_id: str, sub: str) -> list[str]:
    dest = config.DATA / "batches" / batch_id / sub
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for f in files:
        p = dest / Path(f.filename or "upload").name
        p.write_bytes(f.file.read())
        saved.append(str(p))
    return saved


def _resolve_inputs(files, source, batch_id: str, sub: str) -> tuple[list[str], str | None]:
    """返回 (待 OCR 的图片路径列表, 用于成功后归档的源目录或 None)。

    source 给定 → 扫该目录 *.png，归档源目录=source；
    files 给定 → 存盘走上传，无源目录(None)；都没有 → 400。
    用 isinstance 守卫：直接 await 路由（测试）时未传的参数会是 File(None)/Form(None)
    的 FieldInfo 哨兵（truthy），只认真正的 str / list。
    """
    if isinstance(source, str) and source:
        if not Path(source).is_dir():
            raise HTTPException(400, f"源目录不存在: {source}")
        paths = sorted(str(p) for p in Path(source).glob("*.png"))
        if not paths:
            raise HTTPException(400, f"源目录无 png: {source}")
        return paths, source
    if isinstance(files, list) and files:
        return _save_uploads(files, batch_id, sub), None
    raise HTTPException(400, "需要 files 或 source 之一")

router = APIRouter(prefix="/api")


@router.get("/config/paths")
def api_config_paths():
    # 前端预填用：三个 iCloud 默认目录（机器相关，env 可覆盖）。
    return {
        "import_dir": str(config.IMPORT_DIR),
        "pos_dir": str(config.POS_DIR),
        "archive_dir": str(config.SCREENSHOTS_ARCHIVE),
    }


@router.get("/config/llm")
def api_config_llm():
    import os
    import shutil
    from backend.llm import BACKEND_CHOICES, default_backend
    from backend.llm.codex_cli import resolve_codex_bin
    from backend.llm.minimax_coding_plan import minimax_cli_available
    from backend.llm.openai_compatible import (
        ai_builder_space_configured, openai_compatible_configured,
    )
    # 现读 env：config 模块值在 secrets.env 加载前就冻结了。
    space_vision = ai_builder_space_configured()
    return {"backend": default_backend(),
            "choices": list(BACKEND_CHOICES),
            "providers": {
                "minimax_coding_plan": {
                    "label": "MiniMax Coding Plan（视觉）",
                    "vision": True,
                    "configured": minimax_cli_available(),
                    "detail": "需安装 mmx-cli 并完成 Token Plan 登录；识别时通过 mmx CLI 调用视觉能力",
                },
                "codex_cli": {
                    "label": "Codex CLI",
                    "vision": True,
                    "configured": bool(shutil.which(resolve_codex_bin()) or os.path.isfile(resolve_codex_bin())),
                    "detail": "通过本机 Codex CLI 识图",
                },
                "anthropic_api": {
                    "label": "Anthropic API",
                    "vision": True,
                    "configured": bool(os.getenv("ANTHROPIC_API_KEY")),
                    "detail": "Anthropic 原生视觉消息",
                },
                "openai_compatible": {
                    "label": (
                        "AI Builder Space 视觉（Kimi K2.5）"
                        if space_vision else "OpenAI 兼容视觉 API"
                    ),
                    "vision": True,
                    "configured": openai_compatible_configured(),
                    "detail": (
                        "使用平台自动注入的服务端 Token；密钥不会进入浏览器"
                        if space_vision
                        else "使用 OpenAI chat/completions 的 image_url 协议"
                    ),
                },
                "claude_cli": {
                    "label": "Claude CLI",
                    "vision": True,
                    "configured": bool(shutil.which("claude")),
                    "detail": "通过本机 Claude CLI 识图",
                },
            }}


def _trend_animals_enabled() -> bool:
    import os
    return os.getenv(
        "TREND_ANIMALS_ENABLED", str(config.TREND_ANIMALS_ENABLED)).lower() == "true"


def _trend_client() -> TrendAnimalsClient:
    return TrendAnimalsClient()


def _trend_http_error(error: TrendAnimalsError):
    status = 502
    if error.code in {"unknown_batch"}:
        status = 404
    elif error.code in {"confirmation_required", "data_stale", "component_count_mismatch"}:
        status = 409
    elif error.code in {"not_configured", "disabled"}:
        status = 503
    elif error.code in {"missing_required_fields", "unsupported_row_count"}:
        status = 422
    raise HTTPException(status, {"code": error.code, "message": error.message})


def _require_trend_animals() -> None:
    if not _trend_animals_enabled():
        raise TrendAnimalsError("disabled", "趋势动物 API 功能未启用")


@router.get("/config/trend-animals")
def api_config_trend_animals():
    import os
    return {
        "enabled": _trend_animals_enabled(),
        "configured": bool(os.getenv("TREND_ANIMALS_API_KEY")),
        "default_budget": float(os.getenv(
            "TREND_ANIMALS_DEFAULT_BUDGET", str(config.TREND_ANIMALS_DEFAULT_BUDGET))),
        "selection_budget": float(os.getenv(
            "TREND_ANIMALS_SELECTION_BUDGET", str(config.TREND_ANIMALS_SELECTION_BUDGET))),
        "ocr_fallback_available": True,
    }


@router.post("/trend-animals/holding/estimate")
def api_trend_holding_estimate(batch_id: str = Body(..., embed=True)):
    try:
        _require_trend_animals()
        with Session(engine) as s:
            batch = s.get(Batch, batch_id)
            if batch is None:
                raise TrendAnimalsError("unknown_batch", f"批次不存在：{batch_id}")
            with _trend_client() as client:
                out = estimate_holding_sync(client, expected_date=batch.date)
        return {
            "ok": True, "as_of_dates": out["status_dates"],
            "tm_count": len(out["tm_ids"]), "fields": out["fields"],
            "estimated_cost": out["estimated_cost"],
        }
    except TrendAnimalsError as error:
        _trend_http_error(error)


@router.post("/trend-animals/holding/sync")
def api_trend_holding_sync(batch_id: str = Body(..., embed=True),
                           approved_budget: float | None = Body(None, embed=True)):
    try:
        _require_trend_animals()
        with _trend_client() as client, Session(engine) as s:
            return run_holding_sync(
                s, client=client, batch_id=batch_id, approved_budget=approved_budget)
    except TrendAnimalsError as error:
        _trend_http_error(error)


@router.post("/trend-animals/selection/estimate")
def api_trend_selection_estimate(date: str = Body(..., embed=True)):
    try:
        _require_trend_animals()
        with _trend_client() as client:
            out = estimate_selection(client, expected_date=date)
        return {
            "ok": True, "as_of_date": out["as_of_date"], "counts": out["counts"],
            "estimated_cost": out["estimated_cost"],
            "estimate_breakdown": out["estimate_breakdown"],
            "note": "估算会调用 searchTicker 和 constituentCount 低价快照，预计产生少量费用",
        }
    except TrendAnimalsError as error:
        _trend_http_error(error)


@router.post("/trend-animals/selection/run")
def api_trend_selection_run(
        date: str = Body(..., embed=True),
        batch_id: str | None = Body(None, embed=True),
        approved_budget: float | None = Body(None, embed=True),
        etf_min_aum_yi: float | None = Body(None, embed=True),
        etf_min_turnover_yi: float | None = Body(None, embed=True),
        min_market_cap_yi: float | None = Body(None, embed=True),
        min_turnover_yi: float | None = Body(None, embed=True)):
    try:
        _require_trend_animals()
        with _trend_client() as client, Session(engine) as s:
            return run_selection_pipeline(
                s, client=client, date=date, batch_id=batch_id,
                approved_budget=approved_budget,
                etf_min_aum_yi=etf_min_aum_yi,
                etf_min_turnover_yi=etf_min_turnover_yi,
                min_market_cap_yi=min_market_cap_yi,
                min_turnover_yi=min_turnover_yi,
            )
    except TrendAnimalsError as error:
        _trend_http_error(error)


@router.post("/import")
def api_import(source: str = Body(...), date: str = Body(...)):
    with Session(engine) as s:
        bid = import_batch(s, source_dir=Path(source), data_root=config.DATA, date=date)
    return {"batch_id": bid}


@router.post("/run/ocr")
async def api_run_ocr(batch_id: str = Body(..., embed=True),
                     indices: list[int] | None = Body(None, embed=True),
                     backend: str | None = Body(None, embed=True)):
    # Non-blocking: reset targets + schedule a background run, return immediately.
    # indices given → rerun those screenshots; omitted → first run / rerun all 未成功.
    # backend: 前端 OCR 页选择器的值；None → config.LLM_BACKEND 默认。
    # async def so we're on the event loop (schedule_ocr_run uses create_task).
    queued = schedule_ocr_run(batch_id, indices, backend=backend)
    return {"ok": True, "queued": queued}


@router.post("/run/ocr/cancel")
async def api_cancel_ocr(batch_id: str = Body(..., embed=True)):
    # Force-stop a stuck run: cancel the background task + unstick running jobs +
    # unlock the node. async def → runs on the event loop (task.cancel is loop-bound).
    return cancel_ocr_run(batch_id)


@router.post("/run/positions")
async def api_run_positions(files: list[UploadFile] = File(None), batch_id: str = Form(...),
                            source: str = Form(None),
                            backend: str | None = Form(None)):
    # 券商持仓页：股数/成本/现价/盈亏，**没有股票代码**。OCR 禁止脑补代码 → code 多为 null，
    # 仍按名称落库；真实代码由趋势动物「持仓」温度页(/run/holding_temp)回填。
    # 用名称去重（无 code 不能按 code 去重），同名后出现的覆盖。
    # 异步后台跑(逐张容错+进度)，立即返回；前端轮询 /run/positions/status 看进度。
    # backend: 持仓页选择器 → 与节点②OCR 同一选择（claude_cli/codex_cli/anthropic_api），
    # None → 走 config.LLM_BACKEND 默认；前端会写 localStorage 跨页面保持一致。
    image_paths, archive_src = _resolve_inputs(files, source, batch_id, "positions")
    client = get_client(backend)
    prompt = load_position_prompt(_PROMPTS, client)
    return schedule_positions_run(
        engine=engine, client=client, batch_id=batch_id,
        image_paths=image_paths, prompt=prompt, archive_src=archive_src,
    )


@router.get("/run/positions/status")
def api_positions_status(batch_id: str):
    return get_positions_status(batch_id)


@router.post("/run/holding_temp")
async def api_run_holding_temp(files: list[UploadFile] = File(None), batch_id: str = Form(...),
                               source: str = Form(None),
                               backend: str | None = Form(None)):
    # 趋势动物「持仓」温度页：真实代码(带后缀)+温度+右侧天数+强度+tags，覆盖 ETF/LOF。
    # 作为持仓的温度+代码权威源；写 HoldingTemp 并按名称回填 Position.code。
    # backend: 持仓页选择器 → 与节点②OCR 同一选择（claude_cli/codex_cli/anthropic_api），
    # None → 走 config.LLM_BACKEND 默认；前端会写 localStorage 跨页面保持一致。
    image_paths, archive_src = _resolve_inputs(files, source, batch_id, "holding_temp")
    prompt = (_PROMPTS / "ocr_holding_temp.md").read_text()
    # 异步后台跑(逐张容错+进度)，立即返回；前端轮询 /run/holding_temp/status 看进度。
    # 归档移到后台任务成功后(见 schedule_holding_temp_run)。
    return schedule_holding_temp_run(
        engine=engine, client=get_client(backend), batch_id=batch_id,
        image_paths=image_paths, prompt=prompt, archive_src=archive_src,
    )


@router.get("/run/holding_temp/status")
def api_holding_temp_status(batch_id: str):
    return get_holding_temp_status(batch_id)


@router.post("/positions/pair")
def api_pair_position(batch_id: str = Body(..., embed=True),
                      position_id: int = Body(..., embed=True),
                      code: str = Body(..., embed=True)):
    # 人工配对：自动匹配救不了的简称重组（科创材基 ↔ 科创新材料ETF汇添富），用户从
    # 温度页下拉手动指定。写 Position.code + code_source="manual"。
    with Session(engine) as s:
        out = set_position_code(s, batch_id=batch_id, position_id=position_id, code=code)
        s.commit()
        return out


@router.get("/review/{batch_id}")
def api_review(batch_id: str):
    with Session(engine) as s:
        ok, msg = run_all_can_proceed(s, batch_id)
        return {"summary": review_summary(s, batch_id), "can_proceed": ok, "message": msg}


@router.post("/run/aggregate")
def api_run_aggregate(batch_id: str = Body(..., embed=True)):
    with Session(engine) as s:
        return run_aggregate(s, batch_id=batch_id)


@router.post("/run/prescreen")
def api_prescreen(batch_id: str = Body(..., embed=True),
                  etf_min_aum_yi: float | None = Body(None, embed=True),
                  etf_min_turnover_yi: float | None = Body(None, embed=True),
                  min_market_cap_yi: float | None = Body(None, embed=True),
                  min_turnover_yi: float | None = Body(None, embed=True)):
    # ETF 线全局参数（规模门 + ETF 成交额门，亿）；个股门可调参数（市值门 + 成交额门）；
    # 缺省落回 config 默认，不破原有行为。
    with Session(engine) as s:
        return run_prescreen(s, batch_id=batch_id,
                             etf_min_aum_yi=etf_min_aum_yi,
                             etf_min_turnover_yi=etf_min_turnover_yi,
                             min_market_cap_yi=min_market_cap_yi,
                             min_turnover_yi=min_turnover_yi)


@router.post("/run/b_filter")
def api_b_filter(batch_id: str = Body(..., embed=True),
                 risk_pct: float | None = Body(None, embed=True),
                 fixed_stop_pct: float | None = Body(None, embed=True)):
    # Q4：risk_pct/fixed_stop_pct 为前端全局参数（单笔风险% + 固定止损距离%），
    # 缺省时落回 risk/config 默认（1% 风险 + 结构止损参考），不破原有行为。
    with Session(engine) as s:
        return run_b_filter(s, batch_id=batch_id,
                            risk_pct=risk_pct, fixed_stop_pct=fixed_stop_pct)


@router.post("/run/exit_check")
def api_exit_check(batch_id: str = Body(..., embed=True)):
    # run_exit_check 现返回 {"items": [...], "overview": [...]}（总览含每只持仓的温度+建议）。
    with Session(engine) as s:
        return run_exit_check(s, batch_id=batch_id)


@router.post("/run/report")
async def api_report(batch_id: str = Body(..., embed=True),
                     backend: str | None = Body(None)):
    # backend: 日报 LLM 趋势研判用的后端（claude_cli/codex_cli/anthropic_api），
    # None → config.LLM_BACKEND 默认。缓存按 (batch, backend, facts_hash) 隔离，
    # 切换后端会强制重生成（避免不同后端的 brief 互相复用）。
    # 工厂用 default-arg 捕获 backend，避免 lambda 闭包变量被改。
    factory = (lambda b=backend: get_client(b)) if backend else get_client
    return await compose_report_with_brief(engine, batch_id=batch_id,
                                           client_factory=factory, backend=backend)


@router.post("/run/auto")
def api_run_auto(batch_id: str = Body(..., embed=True)):
    # RUN ALL：一键顺序跑机械节点（聚合/初筛/B筛/出局/日报）。人工节点（导入/OCR/
    # 校对/持仓/温度页/推送）仍由用户驱动。基于现有数据跑。
    with Session(engine) as s:
        return run_auto(s, batch_id=batch_id)


@router.post("/run/push")
async def api_push(batch_id: str = Body(..., embed=True)):
    report = await compose_report_with_brief(engine, batch_id=batch_id)
    # F2: push 前先做备份快照（session 已关闭，避免占着连接跑 VACUUM）
    backup.snapshot(config.DB_PATH, config.BACKUPS, batch_id=batch_id)
    backup.rotate(config.BACKUPS, keep=7)
    url = await push_node(batch_id=batch_id, report=report, data_root=config.DATA)
    return {"url": url}


@router.post("/chat/tool")
def api_chat_tool(name: str = Body(...), args: dict = Body(...)):
    with Session(engine) as s:
        try:
            return {"result": run_tool(s, name, args)}
        except ToolForbidden as e:
            raise HTTPException(403, str(e))


@router.get("/chat/tools")
def api_chat_tools_meta():
    return {name: {"needs_confirm": t.needs_confirm} for name, t in REGISTRY.items()}


@router.get("/chat/history/{batch_id}")
def api_chat_history(batch_id: str):
    with Session(engine) as s:
        msgs = conversation.load_messages(s, batch_id)
        return [
            {"msg_id": m.msg_id, "role": m.role, "content": m.content,
             "tool_name": m.tool_name, "tool_args": m.tool_args}
            for m in msgs
        ]


@router.post("/chat/message")
async def api_chat_message(
    batch_id: str = Body(...),
    content: str = Body(...),
    model: str = Body(config.CHAT_MODEL_DEFAULT),
    current_node: str = Body(""),
):
    with Session(engine) as s:
        conversation.persist_user(s, batch_id, content)
        return await conversation.run_turn(
            s, batch_id=batch_id, client=get_client(), model=model, current_node=current_node,
        )


@router.post("/chat/confirm")
async def api_chat_confirm(
    batch_id: str = Body(...),
    name: str = Body(...),
    args: dict = Body(...),
    confirmed: bool = Body(...),
    model: str = Body(config.CHAT_MODEL_DEFAULT),
    current_node: str = Body(""),
):
    with Session(engine) as s:
        return await conversation.confirm_and_continue(
            s, batch_id=batch_id, name=name, args=args, confirmed=confirmed,
            client=get_client(), model=model, current_node=current_node,
        )
