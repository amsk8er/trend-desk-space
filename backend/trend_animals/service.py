"""趋势动物同步的共享日期、账单和审计辅助。"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Session

from backend.db import TrendApiSync
from backend.trend_animals.errors import TrendAnimalsError, redact_secret


def status_by_asset(rows: list[dict]) -> dict[str, dict]:
    return {str(row.get("asset")): row for row in rows if row.get("asset")}


def require_asset_date(status_rows: list[dict], asset: str, expected_date: str | None) -> dict:
    row = status_by_asset(status_rows).get(asset)
    if row is None:
        raise TrendAnimalsError("api_contract_error", f"更新状态缺少资产类别：{asset}")
    actual = row.get("asOfDate")
    if expected_date and actual != expected_date:
        raise TrendAnimalsError(
            "data_stale", f"{asset} 数据日期 {actual or '未知'}，期望 {expected_date}")
    return row


def new_audit(s: Session, *, scope: str, batch_id: str | None = None) -> TrendApiSync:
    audit = TrendApiSync(batch_id=batch_id, scope=scope, status="running")
    s.add(audit); s.commit(); s.refresh(audit)
    return audit


def finish_audit(s: Session, audit: TrendApiSync, *, status: str,
                 as_of_date: str | None = None, tm_count: int | None = None,
                 requested_fields: list[str] | None = None,
                 estimated_cost: float | None = None, actual_cost: float | None = None,
                 incomplete_rows: list | None = None, details: dict | None = None,
                 error: TrendAnimalsError | None = None) -> None:
    audit.status = status
    if as_of_date is not None:
        audit.as_of_date = as_of_date
    if tm_count is not None:
        audit.tm_count = tm_count
    if requested_fields is not None:
        audit.requested_fields = requested_fields
    if estimated_cost is not None:
        audit.estimated_cost = estimated_cost
    if actual_cost is not None:
        audit.actual_cost = actual_cost
    if incomplete_rows is not None:
        audit.incomplete_rows = incomplete_rows
    if details is not None:
        audit.details = details
    if error is not None:
        audit.error_code = error.code
        audit.error_message = redact_secret(error.message)
    audit.finished_at = datetime.utcnow()
    s.add(audit); s.commit()


def ledger_signature(row: dict) -> tuple:
    return (row.get("insDt"), row.get("ApiName"), row.get("params"),
            row.get("apiCost"), row.get("balanceBefore"), row.get("balanceAfter"))


def ledger_mark(client) -> set[tuple]:
    try:
        rows = client.get_account_ledger()
    except Exception:  # 费用明细核对失败不能伪装主数据失败；actual_cost 留空
        return set()
    if not isinstance(rows, list):
        return set()
    return {ledger_signature(row) for row in rows if isinstance(row, dict)}


def ledger_delta(client, before: set[tuple]) -> float | None:
    if not before:
        return None
    try:
        rows = client.get_account_ledger()
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    total = 0.0
    found = False
    for row in rows:
        if not isinstance(row, dict) or ledger_signature(row) in before:
            continue
        cost = row.get("apiCost")
        if isinstance(cost, (int, float)):
            total += float(cost); found = True
    return round(total, 6) if found else 0.0
