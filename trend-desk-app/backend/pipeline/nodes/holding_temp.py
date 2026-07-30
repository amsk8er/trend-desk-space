"""趋势动物「收藏夹 > 持仓」温度页：持仓的温度 + 真实代码权威来源。

券商持仓截图没有股票代码（只有名称+市场标记），OCR 禁止脑补 → Position.code=None。
趋势动物「持仓」分组温度页有真实代码（带 .SH/.SZ/.OF 后缀）+ 温度/右侧天数/强度/tags，
且覆盖 ETF/LOF 基金温度。本节点把它写入 HoldingTemp 表，并按**名称**把真实代码
回填到 Position.code（只填空，不覆盖券商页之外已有的可信代码）。

实盘背景（batch_20260612_2222）：claude 凭名称脑补 ETF/LOF 代码几乎全错
（红土创新精选LOF 脑补 161005、实际 168401.SZ…），脑补代码绝不可信。
"""
import asyncio
import logging
from pathlib import Path
from sqlmodel import Session, select, delete
from backend.db import HoldingTemp, Position
from backend.pipeline.state import transition, NodeStatus
from backend.ocr.worker import _coerce_float

log = logging.getLogger(__name__)

# 温度页扫目录的内存进度（一次性操作，不建表）。单 worker(--reload)下读写同进程。
_HT_PROGRESS: dict[str, dict] = {}
_HT_RUNNING: dict[str, "asyncio.Task"] = {}


def norm_code(code: str | None) -> str:
    """"168401.SZ" / "159507.OF" → "168401" / "159507"（去交易所后缀，便于与主库纯数字代码匹配）。"""
    return (code or "").split(".")[0].strip()


def norm_name(name: str | None) -> str:
    """名称归一：去首尾空白、大小写无关、去全角空格。

    券商页与趋势动物页同一标的名称可能有细微差异（空格/大小写），按归一名关联。
    """
    if not name:
        return ""
    return "".join(name.split()).replace("　", "").lower()


def _coerce_int(v):
    f = _coerce_float(v)
    return int(f) if f is not None else None


def upsert_holding_temps(s: Session, *, batch_id: str, rows: list[dict], commit: bool = True) -> None:
    """替换式写入本批趋势动物持仓温度行（重跑覆盖，不累加）。"""
    s.exec(delete(HoldingTemp).where(HoldingTemp.batch_id == batch_id))
    for r in rows:
        raw = dict(r.get("raw_fields") or {})
        tags = r.get("tags")
        if tags is not None:
            raw["tags"] = tags
        s.add(HoldingTemp(
            batch_id=batch_id,
            tm_id=r.get("tm_id"),
            code=r.get("code"),
            name=r.get("name") or "",
            market=r.get("market"),
            temperature_status=r.get("temperature_status"),
            strength=_coerce_int(r.get("strength")),
            right_side_days=_coerce_int(r.get("right_side_days")),
            right_side_gain_pct=_coerce_float(r.get("right_side_gain_pct")),
            jieqi=r.get("jieqi"),
            sector=r.get("sector"),
            raw_fields=raw,
            source_image=r.get("source_image"),
            as_of_date=r.get("as_of_date"),
            update_dt=r.get("update_dt"),
            data_source=r.get("data_source") or "ocr",
        ))
    if commit:
        s.commit()


def backfill_position_codes(s: Session, *, batch_id: str) -> int:
    """用趋势动物持仓页的真实代码，按名称回填空缺的 Position.code。

    只填 code 为空的持仓（券商页脑补禁用后的常态），不覆盖已有代码。
    返回回填条数。调用方负责 commit。
    """
    temps = s.exec(select(HoldingTemp).where(HoldingTemp.batch_id == batch_id)).all()
    coded = [t for t in temps if t.code and norm_name(t.name)]
    by_name: dict[str, HoldingTemp] = {}
    for t in coded:
        by_name.setdefault(norm_name(t.name), t)
    n = 0
    positions = s.exec(select(Position).where(Position.batch_id == batch_id)).all()
    for p in positions:
        if p.code:
            continue
        pkey = norm_name(p.name)
        if not pkey:
            continue
        match = by_name.get(pkey)
        source = "trend_api" if match is not None and match.data_source == "trend_api" else "holding_temp"
        if match is None:
            # 包含匹配兜底：券商简称 ⊂ 温度页全称（如 G60创新 ⊂ G60创新ETF申万菱信），
            # 唯一命中才用 —— 防止短名误配多个候选。标 fuzzy 供人工复核。
            # 连子串都不是的（科创材基 vs 科创新材料ETF汇添富）留给人工配对。
            cands = [t for t in coded if pkey in norm_name(t.name)]
            if len(cands) == 1:
                match = cands[0]
                source = ("trend_api_fuzzy" if match.data_source == "trend_api"
                          else "holding_temp_fuzzy")
        if match is None:
            continue
        p.code = match.code
        p.code_source = source
        s.add(p)
        n += 1
    return n


def set_position_code(s: Session, *, batch_id: str, position_id: int, code: str) -> dict:
    """人工配对：把券商持仓手动配到温度页真实代码（自动匹配救不了的简称重组，
    如 科创材基 ↔ 科创新材料ETF汇添富）。调用方负责 commit。"""
    p = s.get(Position, position_id)
    if p is None or p.batch_id != batch_id:
        return {"ok": False, "note": "持仓不存在或不属于该批次"}
    p.code = code
    p.code_source = "manual"
    s.add(p)
    return {"ok": True, "position_id": position_id, "code": code}


async def run_holding_temp_node(*, engine, batch_id: str, image_paths: list[str],
                                client, prompt: str, progress=None,
                                max_retries: int = 1, timeout_s: int = 180) -> dict:
    """解析趋势动物持仓温度页 → 写 HoldingTemp → 回填 Position.code。

    逐张容错(对齐 OCR worker)：单张 LLM 超时/失败重试 max_retries 次，仍失败则
    跳过该图、记入 failed，不挂整批、不抛(避免并发瞬时超时直接 500)。
    progress(current, total, image, ok, failed) 每张回调一次，供前端显示进度。
    """
    from backend.ocr.parser import parse_ocr_json
    from backend.llm.base import LLMRequest

    with Session(engine) as s:
        transition(s, batch_id=batch_id, node="holding_temp", to=NodeStatus.RUNNING)
    rows: list[dict] = []
    failed: list[dict] = []
    total = len(image_paths)
    for i, img in enumerate(image_paths):
        resp = None
        for attempt in range(max_retries + 1):
            try:
                resp = await client.complete(
                    LLMRequest(prompt=prompt, images=[img], timeout_s=timeout_s))
                break
            except Exception as e:  # noqa: BLE001 — 单张失败不该挂整批；瞬时超时常见，重试
                if attempt >= max_retries:
                    failed.append({"image": img, "error": f"{type(e).__name__}: {e}"})
        if resp is not None:
            payload = parse_ocr_json(resp.text)
            for r in payload.get("rows", []):
                r = dict(r)
                r["source_image"] = img
                rows.append(r)
        if progress:
            progress(current=i + 1, total=total, image=img, ok=len(rows), failed=len(failed))
    with Session(engine) as s:
        upsert_holding_temps(s, batch_id=batch_id, rows=rows)
        backfilled = backfill_position_codes(s, batch_id=batch_id)
        s.commit()
        transition(s, batch_id=batch_id, node="holding_temp", to=NodeStatus.DONE)
    return {"rows": len(rows), "backfilled": backfilled, "failed": failed}


def get_holding_temp_status(batch_id: str) -> dict:
    """前端轮询用：返回本批温度页扫目录的进度，无则 idle。"""
    return _HT_PROGRESS.get(batch_id, {"status": "idle"})


def schedule_holding_temp_run(*, engine, client, batch_id: str, image_paths: list[str],
                              prompt: str, archive_src: str | None = None) -> dict:
    """异步后台跑温度页识别(逐张容错+进度)，立即返回 {ok, total}。
    进度写 _HT_PROGRESS，前端轮询 get_holding_temp_status。MUST 在事件循环内调用。"""
    cur = _HT_PROGRESS.get(batch_id)
    if cur and cur.get("status") == "running":
        return {"ok": False, "total": cur.get("total", 0), "reason": "已在识别中"}
    _HT_PROGRESS[batch_id] = {"status": "running", "current": 0, "total": len(image_paths),
                              "image": None, "ok": 0, "failed": 0, "rows": 0,
                              "backfilled": 0, "failed_items": []}

    def _progress(*, current, total, image, ok, failed):
        _HT_PROGRESS[batch_id].update(current=current, total=total,
                                      image=Path(image).name, ok=ok, failed=failed)

    async def _run():
        from backend import config
        from backend.pipeline.archive_originals import archive_source_images
        try:
            result = await run_holding_temp_node(
                engine=engine, batch_id=batch_id, image_paths=image_paths,
                client=client, prompt=prompt, progress=_progress)
            _HT_PROGRESS[batch_id].update(
                status="done", rows=result["rows"], backfilled=result["backfilled"],
                failed_items=result["failed"])
            if archive_src:
                archive_source_images(source_dir=Path(archive_src),
                                      archive_root=config.SCREENSHOTS_ARCHIVE,
                                      batch_id=batch_id,
                                      filenames=[Path(p).name for p in image_paths])
        except Exception as e:  # noqa: BLE001 — 后台任务异常落进度，不沉默
            log.exception("holding_temp background run failed")
            _HT_PROGRESS[batch_id] = {**_HT_PROGRESS.get(batch_id, {}),
                                      "status": "error", "error": str(e)}

    task = asyncio.get_running_loop().create_task(_run())
    _HT_RUNNING[batch_id] = task
    task.add_done_callback(lambda t: _HT_RUNNING.pop(batch_id, None))
    return {"ok": True, "total": len(image_paths)}
