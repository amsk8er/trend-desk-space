"""Bitget 账户截图 OCR：暂存、人工确认和保守账户补充。

截图永远不是成交凭证。确认后的快照只用于补充当前账户总额、现金和外部持仓占用，
不会创建 ``UsManualExecution`` 或 ``UsPositionLot``，也不会产生订单。
"""
from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from sqlmodel import Session

from backend import config
from backend.db import UsAccountOcrBatch, UsAccountOcrPosition, UsManualAccountSnapshot
from backend.llm.base import LLMRequest
from backend.ocr.parser import parse_ocr_json
from backend.trend_animals.us_xstock_pool import _bitget_stock_rows
from backend.us_manual import repository
from backend.us_manual.bitget_public import fetch_public_instruments
from backend.us_manual.contracts import (
    UsManualError,
    canonical_json,
    decimal_text,
    serialize,
    sha256,
    utc_now,
)


log = logging.getLogger(__name__)
MAX_OCR_IMAGES = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"}
_RUNNING: dict[str, asyncio.Task] = {}


def load_prompt() -> str:
    return (config.ROOT / "prompts" / "ocr_us_account.md").read_text(encoding="utf-8")


def _decimal(value: Any, *, non_negative: bool = True) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "").replace("，", "")
    text = re.sub(r"(?:USDT|USD|\$|≈|约|\s)", "", text, flags=re.I)
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or (non_negative and parsed < 0):
        return None
    return parsed


def _first(mapping: dict[str, Any], names: tuple[str, ...]) -> Any:
    return next((mapping.get(name) for name in names if mapping.get(name) is not None), None)


def _identity(row: dict[str, Any]) -> tuple[str | None, str | None, list[str]]:
    errors: list[str] = []
    explicit_instrument = str(_first(row, ("venue_instrument", "instrument", "symbol")) or "").strip().upper()
    explicit_ticker = str(_first(row, ("ticker_symbol", "ticker", "code")) or "").strip().upper()
    instrument: str | None = None
    ticker: str | None = None

    if explicit_instrument:
        if re.fullmatch(r"R[A-Z0-9]{1,12}USDT", explicit_instrument):
            instrument = explicit_instrument
            ticker = explicit_instrument[1:-4]
        elif re.fullmatch(r"R[A-Z0-9]{1,12}", explicit_instrument):
            ticker = explicit_instrument[1:]
            instrument = f"R{ticker}USDT"
        elif re.fullmatch(r"[A-Z][A-Z0-9]{0,11}", explicit_instrument):
            ticker = explicit_instrument
            instrument = f"R{ticker}USDT"
        else:
            errors.append("截图产品代码格式无法严格映射")

    if explicit_ticker:
        normalized = explicit_ticker
        if re.fullmatch(r"R[A-Z0-9]{1,12}USDT", normalized):
            normalized = normalized[1:-4]
        elif re.fullmatch(r"R[A-Z0-9]{1,12}", normalized):
            normalized = normalized[1:]
        if not re.fullmatch(r"[A-Z][A-Z0-9]{0,11}", normalized):
            errors.append("截图股票代码格式不明确")
        elif ticker is not None and ticker != normalized:
            errors.append("截图股票代码与产品代码冲突")
        else:
            ticker = normalized
            instrument = instrument or f"R{ticker}USDT"

    if ticker is None:
        errors.append("截图未明确显示股票或产品代码，禁止按名称猜测")
    return ticker, instrument, errors


def parse_us_account_payload(payload: dict[str, Any], *, source_image: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    account_raw = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    account = {
        "equity_usdt": _decimal(_first(account_raw, (
            "equity_usdt", "equity", "nav", "net_asset", "total_asset", "account_amount",
        ))),
        "cash_usdt": _decimal(_first(account_raw, (
            "cash_usdt", "cash", "available_cash", "available_amount", "available",
        ))),
        "currency": str(account_raw.get("currency") or "").strip().upper() or None,
        "raw_fields": account_raw.get("raw_fields") if isinstance(account_raw.get("raw_fields"), dict) else {},
    }
    raw_rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        ticker, instrument, errors = _identity(raw)
        quantity = _decimal(_first(raw, ("quantity", "shares", "qty")))
        average_cost = _decimal(_first(raw, ("average_cost_usdt", "avg_cost", "average_cost", "cost_price")))
        if quantity is None or quantity <= 0:
            errors.append("持仓数量缺失或不是正数")
        if average_cost is None or average_cost <= 0:
            errors.append("平均成本缺失或不是正数")
        rows.append({
            "ticker_symbol": ticker,
            "ticker_name": str(_first(raw, ("ticker_name", "name")) or "").strip() or None,
            "venue_instrument": instrument,
            "quantity": quantity,
            "average_cost_usdt": average_cost,
            "current_price_usdt": _decimal(_first(raw, ("current_price_usdt", "current_price", "last_price"))),
            "market_value_usdt": _decimal(_first(raw, ("market_value_usdt", "market_value"))),
            "unrealized_pnl_usdt": _decimal(
                _first(raw, ("unrealized_pnl_usdt", "unrealized_pnl")), non_negative=False,
            ),
            "source_image": source_image,
            "status": "ready" if not errors else "review_required",
            "errors_json": errors,
            "raw_json": raw,
        })
    return account, rows


def _merge_account(current: dict[str, Any], incoming: dict[str, Any], image: str) -> None:
    for field in ("equity_usdt", "cash_usdt"):
        value = incoming.get(field)
        if value is None:
            continue
        if current[field] is None:
            current[field] = value
        elif current[field] != value:
            current["conflicts"].append({
                "field": field,
                "kept": decimal_text(current[field]),
                "other": decimal_text(value),
                "image": Path(image).name,
            })
    currency = incoming.get("currency")
    if currency and current["currency"] is None:
        current["currency"] = currency
    elif currency and current["currency"] != currency:
        current["conflicts"].append({
            "field": "currency", "kept": current["currency"],
            "other": currency, "image": Path(image).name,
        })


def _deduplicate(rows: list[dict[str, Any]], conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    comparable = ("ticker_symbol", "quantity", "average_cost_usdt", "current_price_usdt", "market_value_usdt")
    for row in rows:
        key = str(row.get("venue_instrument") or "")
        if not key:
            unkeyed.append(row)
            continue
        previous = output.get(key)
        if previous is None:
            output[key] = row
            continue
        if all(previous.get(field) == row.get(field) for field in comparable):
            continue
        reason = f"多张截图中的 {key} 持仓数据不一致"
        previous["status"] = "review_required"
        previous["errors_json"] = [*previous["errors_json"], reason]
        row["status"] = "review_required"
        row["errors_json"] = [*row["errors_json"], reason]
        conflicts.append({
            "field": "position", "venue_instrument": key,
            "images": [Path(previous["source_image"]).name, Path(row["source_image"]).name],
        })
        unkeyed.append(row)
    return [*output.values(), *unkeyed]


def batch_payload(session: Session, batch: UsAccountOcrBatch) -> dict[str, Any]:
    rows = repository.account_ocr_rows(session, batch.batch_id)
    return {
        "batch_id": batch.batch_id,
        "capture_date": batch.capture_date,
        "provider": batch.provider,
        "status": batch.status,
        "image_count": batch.image_count,
        "processed_image_count": batch.processed_image_count,
        "failed_image_count": batch.failed_image_count,
        "account": {
            "equity_usdt": decimal_text(batch.equity_usdt),
            "cash_usdt": decimal_text(batch.cash_usdt),
            "currency": batch.currency,
        },
        "source_images": list(batch.source_images_json or []),
        "conflicts": serialize(batch.conflicts_json or []),
        "error": ({"code": batch.error_code, "message": batch.error_message}
                  if batch.error_code else None),
        "confirmed_snapshot_id": batch.confirmed_snapshot_id,
        "confirmed_at": serialize(batch.confirmed_at),
        "created_at": serialize(batch.created_at),
        "updated_at": serialize(batch.updated_at),
        "rows": [
            {
                "row_id": row.row_id,
                "ticker_symbol": row.ticker_symbol,
                "ticker_name": row.ticker_name,
                "venue_instrument": row.venue_instrument,
                "quantity": decimal_text(row.quantity),
                "average_cost_usdt": decimal_text(row.average_cost_usdt),
                "current_price_usdt": decimal_text(row.current_price_usdt),
                "market_value_usdt": decimal_text(row.market_value_usdt),
                "unrealized_pnl_usdt": decimal_text(row.unrealized_pnl_usdt),
                "source_image": Path(row.source_image).name,
                "status": row.status,
                "errors": list(row.errors_json or []),
            }
            for row in rows
        ],
        "notice": "OCR 结果仅为待确认账户快照；不会创建成交、纪律持仓或 Bitget 订单。",
    }


def create_batch(
    session: Session, *, capture_date: str, provider: str | None, image_paths: list[str],
) -> UsAccountOcrBatch:
    batch = UsAccountOcrBatch(
        batch_id=f"us-account-{capture_date.replace('-', '')}-{uuid4().hex[:12]}",
        capture_date=capture_date,
        provider=provider,
        status="running",
        image_count=len(image_paths),
        source_images_json=[Path(path).name for path in image_paths],
    )
    return repository.save_account_ocr_batch(session, batch)


def _progress(engine: Any, batch_id: str, *, processed: int, failed: int) -> None:
    with Session(engine) as session:
        batch = repository.get_account_ocr_batch(session, batch_id)
        batch.processed_image_count = processed
        batch.failed_image_count = failed
        batch.updated_at = utc_now()
        repository.save_account_ocr_batch(session, batch)


async def run_account_ocr(
    *, engine: Any, batch_id: str, image_paths: list[str], client: Any, prompt: str,
) -> dict[str, Any]:
    merged_account: dict[str, Any] = {
        "equity_usdt": None, "cash_usdt": None, "currency": None, "conflicts": [],
    }
    parsed_rows: list[dict[str, Any]] = []
    response_audit: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    archive_root = config.DATA / "research" / "bitget" / "us_account_ocr" / batch_id
    archive_root.mkdir(parents=True, exist_ok=True)
    for index, image_path in enumerate(image_paths):
        try:
            response = await client.complete(LLMRequest(
                prompt=prompt, images=[image_path], timeout_s=180,
            ))
            payload = parse_ocr_json(response.text)
            account, rows = parse_us_account_payload(
                payload, source_image=image_path,
            )
            _merge_account(merged_account, account, image_path)
            parsed_rows.extend(rows)
            archive_payload = {
                "source_image": Path(image_path).name,
                "response_sha256": sha256(response.text),
                "parsed": serialize(payload),
            }
            archive_path = archive_root / f"response-{index + 1}.json"
            archive_path.write_text(canonical_json(archive_payload) + "\n", encoding="utf-8")
            response_audit.append({
                "source_image": Path(image_path).name,
                "response_sha256": archive_payload["response_sha256"],
                "archive_file": archive_path.name,
            })
        except Exception as exc:  # one bad image must not discard valid siblings
            log.exception("US account OCR image failed")
            failed.append({"image": Path(image_path).name, "error": f"{type(exc).__name__}: {exc}"})
        _progress(engine, batch_id, processed=index + 1, failed=len(failed))

    parsed_rows = _deduplicate(parsed_rows, merged_account["conflicts"])
    with Session(engine) as session:
        batch = repository.get_account_ocr_batch(session, batch_id)
        models = [UsAccountOcrPosition(batch_id=batch_id, **row) for row in parsed_rows]
        repository.replace_account_ocr_rows(session, batch_id=batch_id, rows=models)
        batch.equity_usdt = merged_account["equity_usdt"]
        batch.cash_usdt = merged_account["cash_usdt"]
        batch.currency = merged_account["currency"]
        batch.conflicts_json = [*merged_account["conflicts"], *failed]
        batch.raw_responses_json = response_audit
        batch.processed_image_count = len(image_paths)
        batch.failed_image_count = len(failed)
        batch.updated_at = utc_now()
        if not parsed_rows and batch.equity_usdt is None and batch.cash_usdt is None:
            batch.status = "failed"
            batch.error_code = "account_ocr_empty"
            batch.error_message = "截图未识别到账户金额或美股持仓"
        else:
            batch.status = "ready"
            batch.error_code = None
            batch.error_message = None
        repository.save_account_ocr_batch(session, batch)
        return batch_payload(session, batch)


def schedule_account_ocr(
    *, engine: Any, batch_id: str, image_paths: list[str], client: Any, prompt: str,
) -> dict[str, Any]:
    current = _RUNNING.get(batch_id)
    if current is not None and not current.done():
        return {"batch_id": batch_id, "status": "running", "accepted": False}

    async def runner() -> None:
        try:
            await run_account_ocr(
                engine=engine, batch_id=batch_id, image_paths=image_paths,
                client=client, prompt=prompt,
            )
        except Exception as exc:
            log.exception("US account OCR batch failed")
            with Session(engine) as session:
                batch = repository.get_account_ocr_batch(session, batch_id)
                batch.status = "failed"
                batch.error_code = "account_ocr_failed"
                batch.error_message = str(exc)
                batch.updated_at = utc_now()
                repository.save_account_ocr_batch(session, batch)

    task = asyncio.get_running_loop().create_task(runner())
    _RUNNING[batch_id] = task
    task.add_done_callback(lambda _task: _RUNNING.pop(batch_id, None))
    return {"batch_id": batch_id, "status": "running", "accepted": True}


def _available_products(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    parsed = [row for row in _bitget_stock_rows(rows) if row.get("quote_coin") == "USDT"]
    result: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for row in parsed:
        symbol = str(row.get("venue_instrument") or "").upper()
        if not symbol:
            continue
        if symbol in result:
            duplicates.add(symbol)
        result[symbol] = row
    for symbol in duplicates:
        result.pop(symbol, None)
    return result


def confirm_account_ocr(
    session: Session, *, batch_id: str, payload: dict[str, Any],
    instruments_fetcher: Callable[[], list[dict[str, Any]]] = fetch_public_instruments,
) -> dict[str, Any]:
    batch = repository.get_account_ocr_batch(session, batch_id)
    key = str(payload.get("idempotency_key") or "").strip()
    if not key or len(key) > 200:
        raise UsManualError("idempotency_key_required", "确认账户截图必须提供幂等键", 422)
    if batch.status == "confirmed":
        if batch.confirmation_key == key:
            return batch_payload(session, batch)
        raise UsManualError("account_ocr_already_confirmed", "该截图批次已经确认，不能覆盖", 409)
    if batch.status != "ready":
        raise UsManualError("account_ocr_not_ready", "截图尚未识别完成，不能确认", 409)
    if payload.get("full_snapshot_confirmed") is not True:
        raise UsManualError(
            "account_snapshot_scope_required",
            "请确认截图覆盖完整账户，而不是局部持仓列表",
            422,
        )
    currency = str(payload.get("currency") or batch.currency or "").strip().upper()
    if currency != "USDT":
        raise UsManualError("account_currency_invalid", "美股实验账户只接受明确的 USDT 口径", 422)
    equity = _decimal(payload.get("equity_usdt", batch.equity_usdt))
    cash = _decimal(payload.get("cash_usdt", batch.cash_usdt))
    if equity is None or equity <= 0 or cash is None or cash < 0 or cash > equity:
        raise UsManualError(
            "account_totals_invalid", "账户总额和可用现金必须有效，且 0 ≤ 现金 ≤ 总额", 422,
        )
    rows = repository.account_ocr_rows(session, batch_id)
    if not rows and payload.get("confirmed_no_positions") is not True:
        raise UsManualError(
            "account_positions_empty", "未识别到持仓；如账户确为空仓，请明确确认空仓", 422,
        )
    blocked = [row for row in rows if row.status != "ready" or row.errors_json]
    if blocked or batch.conflicts_json:
        raise UsManualError(
            "account_ocr_review_required",
            "截图存在代码缺失、持仓冲突或单图失败；请补充清晰完整截图后重试",
            422,
            {"row_ids": [row.row_id for row in blocked], "conflicts": batch.conflicts_json},
        )
    products = _available_products(instruments_fetcher())
    missing = [row.venue_instrument for row in rows if str(row.venue_instrument or "").upper() not in products]
    if missing:
        raise UsManualError(
            "account_instrument_not_tradeable",
            "截图持仓无法在当前 Bitget 公开 Reality 产品清单中唯一确认",
            422,
            {"venue_instruments": sorted(str(value) for value in missing)},
        )
    positions: list[dict[str, Any]] = []
    open_cost = Decimal("0")
    for row in rows:
        assert row.quantity is not None and row.average_cost_usdt is not None
        cost = row.quantity * row.average_cost_usdt
        open_cost += cost
        positions.append({
            "ticker_symbol": row.ticker_symbol,
            "ticker_name": row.ticker_name,
            "venue_instrument": row.venue_instrument,
            "quantity": decimal_text(row.quantity),
            "average_cost_usdt": decimal_text(row.average_cost_usdt),
            "cost_usdt": decimal_text(cost),
            "current_price_usdt": decimal_text(row.current_price_usdt),
            "market_value_usdt": decimal_text(row.market_value_usdt),
            "unrealized_pnl_usdt": decimal_text(row.unrealized_pnl_usdt),
            "source": "bitget_account_ocr",
        })
    snapshot = repository.save_account_snapshot(session, UsManualAccountSnapshot(
        as_of_date=batch.capture_date,
        starting_equity_usdt=equity,
        cash_usdt=cash,
        open_cost_usdt=open_cost,
        open_risk_usdt=Decimal("0"),
        position_count=len({row["ticker_symbol"] for row in positions}),
        source="bitget_ocr_confirmed",
        derivation_json={
            "source_batch_id": batch.batch_id,
            "scope": "full_account_snapshot",
            "currency": currency,
            "observed_equity_usdt": decimal_text(equity),
            "observed_cash_usdt": decimal_text(cash),
            "positions": positions,
            "raw_response_audit": batch.raw_responses_json,
            "snapshot_sha256": sha256({
                "capture_date": batch.capture_date, "equity": equity,
                "cash": cash, "positions": positions,
            }),
            "notice": "截图未创建成交或纪律持仓；账户容量按台账与截图中更保守的一侧计算。",
        },
    ))
    batch.status = "confirmed"
    batch.confirmation_key = key
    batch.confirmed_snapshot_id = snapshot.snapshot_id
    batch.confirmed_at = utc_now()
    batch.updated_at = utc_now()
    repository.save_account_ocr_batch(session, batch)
    return batch_payload(session, batch)
