"""券商成交 CSV/Excel 的预览、确认和计划对账。"""
from __future__ import annotations

import hashlib
from io import BytesIO, StringIO
from pathlib import Path

import pandas as pd
from sqlmodel import Session, select

from backend.db import BrokerImport, Execution, PositionLot, TradePlan, TradePlanItem

ALIASES = {
    "trade_date": ["trade_date", "成交日期", "日期"],
    "executed_at": ["executed_at", "成交时间", "时间"],
    "code": ["code", "证券代码", "股票代码", "基金代码"],
    "name": ["name", "证券名称", "股票名称", "基金名称"],
    "side": ["side", "买卖方向", "业务名称", "操作"],
    "price": ["price", "成交价格", "成交均价"],
    "shares": ["shares", "成交数量", "数量"],
    "fees": ["fees", "手续费", "费用"],
}


def _norm_code(v) -> str:
    text = str(v or "").strip().upper()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit() and len(text) < 6:
        text = text.zfill(6)
    return text


def _norm_side(v) -> str | None:
    text = str(v or "").strip().lower()
    if text in {"buy", "买", "买入", "证券买入"} or "买入" in text:
        return "buy"
    if text in {"sell", "卖", "卖出", "证券卖出"} or "卖出" in text:
        return "sell"
    return None


def _read_frame(filename: str, content: bytes) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(BytesIO(content), dtype=str)
    last = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return pd.read_csv(StringIO(content.decode(encoding)), dtype=str)
        except (UnicodeDecodeError, pd.errors.ParserError) as exc:
            last = exc
    raise ValueError(f"unsupported_csv_encoding:{type(last).__name__}")


def _mapping(columns) -> dict[str, str]:
    by_norm = {str(c).strip().lower(): str(c) for c in columns}
    out = {}
    for target, aliases in ALIASES.items():
        for alias in aliases:
            hit = by_norm.get(alias.lower())
            if hit:
                out[target] = hit
                break
    return out


def preview_import(s: Session, *, plan_id: str, filename: str, content: bytes) -> dict:
    if s.get(TradePlan, plan_id) is None:
        raise KeyError(plan_id)
    digest = hashlib.sha256(content).hexdigest()
    existing = s.exec(select(BrokerImport).where(
        BrokerImport.plan_id == plan_id, BrokerImport.file_hash == digest,
        BrokerImport.import_type == "executions")).first()
    if existing is not None:
        return existing.model_dump()
    frame = _read_frame(filename, content)
    mapping = _mapping(frame.columns)
    required = {"trade_date", "code", "side", "price", "shares"}
    missing = sorted(required - set(mapping))
    if missing:
        raise ValueError("missing_columns:" + ",".join(missing))
    parsed, anomalies = [], []
    for idx, raw in frame.fillna("").iterrows():
        try:
            side = _norm_side(raw[mapping["side"]])
            row = {
                "row_number": int(idx) + 2,
                "trade_date": str(raw[mapping["trade_date"]]).replace("/", "-")[:10],
                "executed_at": str(raw[mapping.get("executed_at", mapping["trade_date"])]),
                "code": _norm_code(raw[mapping["code"]]),
                "name": str(raw[mapping["name"]]) if "name" in mapping else "",
                "side": side,
                "price": float(str(raw[mapping["price"]]).replace(",", "")),
                "shares": int(float(str(raw[mapping["shares"]]).replace(",", ""))),
                "fees": float(str(raw[mapping["fees"]]).replace(",", "")) if "fees" in mapping and str(raw[mapping["fees"]]).strip() else 0.0,
            }
            problems = []
            if not row["code"]:
                problems.append("missing_code")
            if side is None:
                problems.append("unknown_side")
            if row["price"] <= 0:
                problems.append("invalid_price")
            if row["shares"] <= 0:
                problems.append("invalid_shares")
            if problems:
                anomalies.append({**row, "problems": problems})
            else:
                parsed.append(row)
        except (ValueError, TypeError) as exc:
            anomalies.append({"row_number": int(idx) + 2, "problems": [f"parse_error:{type(exc).__name__}"]})
    audit = BrokerImport(
        plan_id=plan_id, filename=Path(filename).name, file_hash=digest,
        status="preview", field_mapping=mapping, parsed_rows=parsed, anomaly_rows=anomalies,
    )
    s.add(audit); s.commit(); s.refresh(audit)
    return audit.model_dump()


def confirm_import(s: Session, import_id: int) -> dict:
    audit = s.get(BrokerImport, import_id)
    if audit is None:
        raise KeyError(import_id)
    if audit.status == "confirmed":
        executions = s.exec(select(Execution).where(Execution.import_id == import_id)).all()
        return {"import": audit.model_dump(), "executions": [e.model_dump() for e in executions]}
    items = s.exec(select(TradePlanItem).where(TradePlanItem.plan_id == audit.plan_id)).all()
    by_key: dict[tuple[str, str], list[TradePlanItem]] = {}
    for item in items:
        expected = "buy" if item.side == "buy" else "sell"
        by_key.setdefault((_norm_code(item.instrument_id), expected), []).append(item)
    made = []
    for row in audit.parsed_rows:
        matches = by_key.get((_norm_code(row["code"]), row["side"])) or []
        item = matches[0] if len(matches) == 1 else None
        deviation = None if item else "unplanned_execution"
        execution = Execution(
            plan_item_id=item.item_id if item else None, import_id=audit.import_id,
            trade_date=row["trade_date"], instrument_id=row["code"], side=row["side"],
            executed_at=row["executed_at"], price=row["price"], shares=row["shares"],
            fees=row["fees"], deviation_type=deviation,
        )
        s.add(execution); s.flush(); made.append(execution)
        if row["side"] == "buy":
            s.add(PositionLot(
                instrument_id=row["code"], name=(item.name if item else row.get("name") or row["code"]),
                asset_type=(item.asset_type if item else "stock"),
                opened_by_execution=execution.execution_id, opened_on=row["trade_date"],
                initial_shares=row["shares"], remaining_shares=row["shares"],
                avg_cost=row["price"], source="broker_import", as_of_date=row["trade_date"],
            ))
        else:
            remaining = row["shares"]
            lots = s.exec(select(PositionLot).where(
                PositionLot.instrument_id == row["code"], PositionLot.remaining_shares > 0,
            ).order_by(PositionLot.opened_on, PositionLot.lot_id)).all()
            for lot in lots:
                take = min(lot.remaining_shares, remaining)
                lot.remaining_shares -= take
                remaining -= take
                s.add(lot)
                if remaining == 0:
                    break
            if remaining > 0 and execution.deviation_type is None:
                execution.deviation_type = "position_lot_shortfall"
                execution.deviation_reason = f"正式持仓台账缺少 {remaining} 股"
                s.add(execution)
    for item in items:
        linked = [e for e in made if e.plan_item_id == item.item_id]
        done = sum(e.shares for e in linked)
        target = int(item.target_shares or 0)
        if target == 0 and item.side == "hold":
            item.status = "completed"
        elif done >= target > 0:
            item.status = "completed"
        elif done > 0:
            item.status = "partially_executed"
        else:
            item.status = "missed"
        s.add(item)
    audit.status = "confirmed"
    from datetime import datetime
    audit.confirmed_at = datetime.utcnow()
    s.add(audit)
    plan = s.get(TradePlan, audit.plan_id)
    if plan:
        statuses = {x.status for x in items}
        plan.status = "completed" if statuses <= {"completed"} else "partially_executed"
        s.add(plan)
    s.commit()
    return {"import": audit.model_dump(), "executions": [e.model_dump() for e in made]}
