import asyncio
import logging
import re
from pathlib import Path
from sqlmodel import Session, delete
from backend.db import Position
from backend.pipeline.state import transition, NodeStatus

log = logging.getLogger(__name__)

# 券商持仓页扫目录的内存进度（对称 holding_temp，一次性操作不建表）。
_POS_PROGRESS: dict[str, dict] = {}
_POS_RUNNING: dict[str, "asyncio.Task"] = {}


def load_position_prompt(prompt_root: Path, client) -> str:
    """Use a neutral transcription prompt for providers with strict moderation."""
    filename = (
        "ocr_position_minimax.md"
        if getattr(client, "name", None) == "minimax_coding_plan"
        else "ocr_position.md"
    )
    return (prompt_root / filename).read_text(encoding="utf-8")


def _money_or_none(value) -> float | None:
    """Normalize model money values while keeping unknowns as unknown."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("，", "")
    text = re.sub(r"[¥￥元人民币\s]", "", text)
    factor = 1.0
    if text.endswith("亿"):
        factor, text = 100_000_000.0, text[:-1]
    elif text.endswith("万"):
        factor, text = 10_000.0, text[:-1]
    try:
        return float(text) * factor
    except ValueError:
        return None


def _account_summary() -> dict:
    return {
        "nav": None,
        "cash": None,
        "currency": None,
        "source_images": [],
        "conflicts": [],
        "complete": False,
    }


def _merge_account(summary: dict, account: dict | None, image: str) -> None:
    if not isinstance(account, dict):
        return
    found = False
    aliases = {
        "nav": ("nav", "net_asset", "total_asset", "account_amount"),
        "cash": ("cash", "available_cash", "available_amount"),
    }
    for field, keys in aliases.items():
        raw_value = next((account.get(key) for key in keys if account.get(key) is not None), None)
        value = _money_or_none(raw_value)
        if value is None:
            continue
        found = True
        previous = summary[field]
        if previous is None:
            summary[field] = value
        elif abs(previous - value) > max(0.01, abs(previous) * 0.000001):
            summary["conflicts"].append({
                "field": field,
                "kept": previous,
                "other": value,
                "image": Path(image).name,
            })
    currency = account.get("currency")
    if currency and not summary["currency"]:
        summary["currency"] = str(currency)
    if found and Path(image).name not in summary["source_images"]:
        summary["source_images"].append(Path(image).name)
    summary["complete"] = summary["nav"] is not None and summary["cash"] is not None


def _account_payload_has_value(account: dict | None) -> bool:
    """Whether this image produced at least one usable account amount."""
    if not isinstance(account, dict):
        return False
    aliases = (
        "nav", "net_asset", "total_asset", "account_amount",
        "cash", "available_cash", "available_amount",
    )
    return any(_money_or_none(account.get(key)) is not None for key in aliases)


def upsert_positions(s: Session, *, batch_id: str, rows: list[dict]) -> None:
    # 券商持仓截图没有股票代码（OCR 禁止脑补）→ code 缺省/None 仍落库，
    # 真实代码由趋势动物「持仓」温度页（HoldingTemp）按名称回填。
    s.exec(delete(Position).where(Position.batch_id == batch_id))
    for r in rows:
        s.add(Position(batch_id=batch_id, code=r.get("code"), name=r["name"],
                       shares=r["shares"], avg_cost=r["avg_cost"],
                       current_price=r["current_price"], pnl_pct=r["pnl_pct"],
                       stop_loss=r.get("stop_loss"), entered_date=r.get("entered_date"),
                       source_image=r.get("source_image")))
    s.commit()

def finalize_positions(s: Session, batch_id: str) -> None:
    transition(s, batch_id=batch_id, node="positions", to=NodeStatus.DONE)


async def run_positions_node(*, engine, batch_id: str, image_paths: list[str],
                             client, prompt: str, progress=None,
                             max_retries: int = 1, timeout_s: int = 180) -> dict:
    """券商持仓页 OCR → 写 Position(名称去重) → 按 HoldingTemp 回填代码。

    逐张容错(对齐 OCR worker / holding_temp)：单张 LLM 超时/失败重试 max_retries 次，
    仍失败则跳过该图、记 failed，不挂整批、不抛(避免并发瞬时超时直接 500)。
    progress(current, total, image, ok, failed) 每张回调一次。
    """
    from backend.ocr.parser import parse_ocr_json
    from backend.llm.base import LLMNonRetryableError, LLMRequest
    from backend.pipeline.nodes.holding_temp import backfill_position_codes

    with Session(engine) as s:
        transition(s, batch_id=batch_id, node="positions", to=NodeStatus.RUNNING)
    by_name: dict[str, dict] = {}
    account = _account_summary()
    failed: list[dict] = []
    total = len(image_paths)
    for i, img in enumerate(image_paths):
        resp = None
        for attempt in range(max_retries + 1):
            try:
                resp = await client.complete(
                    LLMRequest(prompt=prompt, images=[img], timeout_s=timeout_s))
                break
            except Exception as e:  # noqa: BLE001 — 单张失败不挂整批；瞬时超时常见，重试
                if attempt >= max_retries:
                    failed.append({"image": img, "error": f"{type(e).__name__}: {e}"})
                if isinstance(e, LLMNonRetryableError):
                    if attempt < max_retries:
                        failed.append({"image": img, "error": f"{type(e).__name__}: {e}"})
                    break
        if resp is not None:
            payload = parse_ocr_json(resp.text)
            account_payload = payload.get("account")
            parsed_rows = payload.get("rows")
            if not isinstance(parsed_rows, list):
                parsed_rows = []
            _merge_account(account, account_payload, img)
            if not parsed_rows and not _account_payload_has_value(account_payload):
                failed.append({
                    "image": img,
                    "error": "模型有返回，但未输出可解析的持仓或账户 JSON",
                })
            for r in parsed_rows:
                if not isinstance(r, dict):
                    continue
                name = r.get("name") or ""
                if not name:
                    continue
                by_name[name] = {
                    "code": r.get("code"),  # 禁脑补：无代码就 None，等 HoldingTemp 回填
                    "name": name,
                    "shares": r.get("shares") or 0,
                    "avg_cost": r.get("avg_cost") or 0.0,
                    "current_price": r.get("current_price") or 0.0,
                    "pnl_pct": r.get("pnl_pct") or 0.0,
                    "stop_loss": r.get("stop_loss"),
                    "entered_date": r.get("entered_date"),
                    "source_image": img,
                }
        if progress:
            progress(
                current=i + 1, total=total, image=img, ok=len(by_name),
                failed=len(failed), account=account.copy(),
            )
    rows = list(by_name.values())
    with Session(engine) as s:
        upsert_positions(s, batch_id=batch_id, rows=rows)
        backfill_position_codes(s, batch_id=batch_id)  # 温度页若已传，立即回填真实代码
        s.commit()
        finalize_positions(s, batch_id)
    return {"count": len(rows), "failed": failed, "account": account}


def get_positions_status(batch_id: str) -> dict:
    """前端轮询用：返回本批券商持仓页扫目录的进度，无则 idle。"""
    return _POS_PROGRESS.get(batch_id, {"status": "idle"})


def schedule_positions_run(*, engine, client, batch_id: str, image_paths: list[str],
                           prompt: str, archive_src: str | None = None) -> dict:
    """异步后台跑券商持仓页识别(逐张容错+进度)，立即返回 {ok, total}。
    进度写 _POS_PROGRESS，前端轮询 get_positions_status。MUST 在事件循环内调用。"""
    cur = _POS_PROGRESS.get(batch_id)
    if cur and cur.get("status") == "running":
        return {"ok": False, "total": cur.get("total", 0), "reason": "已在识别中"}
    _POS_PROGRESS[batch_id] = {"status": "running", "current": 0, "total": len(image_paths),
                               "image": None, "ok": 0, "failed": 0, "count": 0,
                               "failed_items": [], "account": _account_summary()}

    def _progress(*, current, total, image, ok, failed, account):
        _POS_PROGRESS[batch_id].update(current=current, total=total,
                                       image=Path(image).name, ok=ok, failed=failed,
                                       account=account)

    async def _run():
        from backend import config
        from backend.pipeline.archive_originals import archive_source_images
        try:
            result = await run_positions_node(
                engine=engine, batch_id=batch_id, image_paths=image_paths,
                client=client, prompt=prompt, progress=_progress)
            _POS_PROGRESS[batch_id].update(
                status="done", count=result["count"], failed_items=result["failed"],
                account=result["account"])
            if archive_src:
                archive_source_images(source_dir=Path(archive_src),
                                      archive_root=config.SCREENSHOTS_ARCHIVE,
                                      batch_id=batch_id,
                                      filenames=[Path(p).name for p in image_paths])
        except Exception as e:  # noqa: BLE001 — 后台任务异常落进度，不沉默
            log.exception("positions background run failed")
            _POS_PROGRESS[batch_id] = {**_POS_PROGRESS.get(batch_id, {}),
                                       "status": "error", "error": str(e)}

    task = asyncio.get_running_loop().create_task(_run())
    _POS_RUNNING[batch_id] = task
    task.add_done_callback(lambda t: _POS_RUNNING.pop(batch_id, None))
    return {"ok": True, "total": len(image_paths)}
