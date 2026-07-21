"""Vision transcription for same-day broker execution screenshots."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from sqlmodel import Session, select

from backend import config
from backend.db import BrokerImport, TradePlan
from backend.llm.base import LLMRequest
from backend.ocr.parser import parse_ocr_json


def _number(value, *, integer: bool = False):
    if value is None or isinstance(value, bool):
        return None
    text = re.sub(r"[,\s¥￥元股]", "", str(value))
    if not text:
        return None
    try:
        result = float(text)
        return int(result) if integer else result
    except ValueError:
        return None


def _code(value) -> str:
    text = str(value or "").strip().upper()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit() and len(text) < 6:
        text = text.zfill(6)
    return text


def _side(value) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"buy", "买", "买入", "证券买入"} or "买入" in text:
        return "buy"
    if text in {"sell", "卖", "卖出", "证券卖出"} or "卖出" in text:
        return "sell"
    return None


def _latest_execution_plan(session: Session, trade_date: str) -> TradePlan | None:
    return session.exec(select(TradePlan).where(
        TradePlan.execute_date == trade_date,
    ).order_by(TradePlan.created_at.desc())).first()


def normalize_rows(payload: dict, trade_date: str) -> tuple[list[dict], list[dict]]:
    parsed: list[dict] = []
    anomalies: list[dict] = []
    seen: set[str] = set()
    conflict_keys: dict[tuple, tuple] = {}
    for index, raw in enumerate(payload.get("rows") or [], start=1):
        if not isinstance(raw, dict):
            anomalies.append({"row_number": index, "problems": ["invalid_row"]})
            continue
        row = {
            "row_number": index,
            "trade_date": str(raw.get("trade_date") or "").replace("/", "-")[:10],
            "executed_at": str(raw.get("executed_at") or "").strip(),
            "code": _code(raw.get("code")),
            "name": str(raw.get("name") or "").strip(),
            "side": _side(raw.get("side")),
            "price": _number(raw.get("price")),
            "shares": _number(raw.get("shares"), integer=True),
            "gross_amount": _number(raw.get("gross_amount")),
            "net_amount": _number(raw.get("net_amount")),
            "fees": _number(raw.get("fees")),
            "raw_fields": raw.get("raw_fields") if isinstance(raw.get("raw_fields"), dict) else {},
        }
        problems = []
        if row["trade_date"] != trade_date:
            problems.append("wrong_or_missing_trade_date")
        if not row["code"]:
            problems.append("missing_code")
        if row["side"] is None:
            problems.append("unknown_side")
        if not row["price"] or row["price"] <= 0:
            problems.append("invalid_price")
        if not row["shares"] or row["shares"] <= 0:
            problems.append("invalid_shares")
        identity = json.dumps({
            key: row.get(key) for key in (
                "trade_date", "executed_at", "code", "side", "price", "shares",
                "gross_amount", "net_amount",
            )
        }, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(identity.encode()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        conflict_key = (row["trade_date"], row["executed_at"], row["code"], row["side"])
        conflict_value = (row["price"], row["shares"], row["gross_amount"], row["net_amount"])
        if conflict_key in conflict_keys and conflict_keys[conflict_key] != conflict_value:
            problems.append("cross_screenshot_conflict")
        conflict_keys[conflict_key] = conflict_value
        if problems:
            anomalies.append({**row, "problems": problems})
        else:
            parsed.append(row)
    return parsed, anomalies


async def preview_execution_screenshots(
    session: Session,
    *,
    trade_date: str,
    filenames: list[str],
    image_paths: list[str],
    client,
) -> dict:
    prompt = (config.ROOT / "prompts" / "ocr_execution.md").read_text(encoding="utf-8")
    response = await client.complete(LLMRequest(
        prompt=prompt,
        images=image_paths,
        timeout_s=180,
    ))
    payload = parse_ocr_json(response.text)
    parsed, anomalies = normalize_rows(payload, trade_date)
    combined_hash = hashlib.sha256()
    for path in image_paths:
        combined_hash.update(Path(path).read_bytes())
    digest = combined_hash.hexdigest()
    existing = session.exec(select(BrokerImport).where(
        BrokerImport.file_hash == digest,
        BrokerImport.import_type == "executions",
    )).first()
    if existing is not None:
        return existing.model_dump()
    plan = _latest_execution_plan(session, trade_date)
    batch_id = f"execution_{trade_date.replace('-', '')}_{uuid4().hex[:8]}"
    audit = BrokerImport(
        plan_id=plan.plan_id if plan else None,
        import_type="executions",
        filename=f"{len(filenames)} execution screenshot(s)",
        file_hash=digest,
        batch_id=batch_id,
        source="broker_ocr",
        status="preview",
        field_mapping={
            "provider": getattr(client, "name", "vision"),
            "model_elapsed_ms": response.elapsed_ms,
            "trade_date": trade_date,
        },
        parsed_rows=parsed,
        anomaly_rows=anomalies,
    )
    session.add(audit)
    session.commit()
    session.refresh(audit)
    return audit.model_dump()


def cleanup_execution_images(paths: list[str]) -> None:
    for raw in paths:
        try:
            Path(raw).unlink(missing_ok=True)
        except OSError:
            pass


def execution_temp_path(filename: str) -> Path:
    root = config.STATE_DIR / "execution-ocr"
    root.mkdir(parents=True, exist_ok=True)
    safe = Path(filename).name or "execution.png"
    return root / f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}-{safe}"
