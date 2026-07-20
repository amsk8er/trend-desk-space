"""趋势动物“持仓”收藏夹 → HoldingTemp 的 API 主通道。"""
from __future__ import annotations

from sqlmodel import Session, select

from backend.db import Batch, HoldingTemp, Position, TrendApiSync
from backend.pipeline.nodes.holding_temp import (
    upsert_holding_temps, backfill_position_codes, norm_code, norm_name,
)
from backend.trend_animals.billing import estimate_snapshot_cost, ensure_budget
from backend.trend_animals.errors import TrendAnimalsError
from backend.trend_animals.mapper import holding_row
from backend.trend_animals.service import (
    finish_audit, ledger_delta, ledger_mark, new_audit, require_asset_date,
)


HOLDING_FIELDS = [
    "tmId", "tickerName", "tickerSymbol", "asset", "asOfDate", "industryName",
    "trendTemperatureCurr", "trendTemperaturePrev", "daysSinceTrendEntry",
    "trendPhaseCurr", "trendStrengthLocalCurr", "trendStrengthLocalChange",
    "stopwinFlagByDangerSignal", "stopwinFlagByBoilingTemperature",
    "stopwinFlagByPopChampagne",
]
IDENTITY_FIELDS = ("tmId", "tickerName", "tickerSymbol", "asOfDate")


def _unmatched_positions(s: Session, batch_id: str, mapped: list[dict]) -> list[dict]:
    by_code = {norm_code(row.get("code")) for row in mapped if row.get("code")}
    named = [(norm_name(row.get("name")), row) for row in mapped if norm_name(row.get("name"))]
    missing: list[dict] = []
    for position in s.exec(select(Position).where(Position.batch_id == batch_id)).all():
        if position.code and norm_code(position.code) in by_code:
            continue
        key = norm_name(position.name)
        exact = [row for name, row in named if name == key]
        fuzzy = [row for name, row in named if key and key in name]
        if len(exact) == 1 or (not exact and len(fuzzy) == 1):
            continue
        missing.append({"position_id": position.position_id, "name": position.name,
                        "reason": "ambiguous" if len(exact) > 1 or len(fuzzy) > 1 else "missing"})
    return missing


def _cached_holding_rows(s: Session, *, batch_id: str, date: str,
                         tm_ids: list[int]) -> list[HoldingTemp] | None:
    """相同批次、日期、字段集和 tmId 集合已有成功结果时复用本地行。"""
    audits = s.exec(select(TrendApiSync).where(
        TrendApiSync.batch_id == batch_id,
        TrendApiSync.scope == "holding",
        TrendApiSync.status == "done",
        TrendApiSync.as_of_date == date,
    )).all()
    if not any(list(row.requested_fields or []) == HOLDING_FIELDS for row in audits):
        return None
    rows = s.exec(select(HoldingTemp).where(
        HoldingTemp.batch_id == batch_id,
        HoldingTemp.data_source == "trend_api",
        HoldingTemp.as_of_date == date,
    )).all()
    returned = [row.tm_id for row in rows if row.tm_id is not None]
    if len(returned) != len(rows) or sorted(returned) != sorted(tm_ids):
        return None
    return rows


def estimate_holding_sync(client, *, expected_date: str | None = None) -> dict:
    docs = client.get_api_doc_intro()
    try:
        client.get_change_log()
    except TrendAnimalsError:
        pass
    statuses = client.get_update_status()
    favorites = client.get_favorites_ticker("持仓")
    billing = client.get_snapshot_billing()
    assets = sorted({row.get("asset") for row in favorites if row.get("asset")})
    status_dates = {}
    for asset in assets:
        status = require_asset_date(statuses, asset, expected_date)
        status_dates[asset] = status.get("asOfDate")
    for row in favorites:
        if expected_date and row.get("asOfDate") != expected_date:
            raise TrendAnimalsError(
                "data_stale", f"收藏夹 {row.get('tickername') or row.get('tickerName')} "
                f"日期 {row.get('asOfDate')}，期望 {expected_date}")
    tm_ids = [int(row["tmId"]) for row in favorites if row.get("tmId") is not None]
    if len(tm_ids) != len(favorites):
        raise TrendAnimalsError("api_contract_error", "持仓收藏夹存在缺少 tmId 的行")
    estimated = estimate_snapshot_cost(HOLDING_FIELDS, len(tm_ids), billing)
    return {
        "docs": docs, "statuses": statuses, "favorites": favorites, "billing": billing,
        "tm_ids": tm_ids, "status_dates": status_dates, "estimated_cost": estimated,
        "fields": list(HOLDING_FIELDS),
    }


def run_holding_sync(s: Session, *, client, batch_id: str,
                     approved_budget: float | None = None) -> dict:
    batch = s.get(Batch, batch_id)
    if batch is None:
        raise TrendAnimalsError("unknown_batch", f"批次不存在：{batch_id}")
    audit = new_audit(s, scope="holding", batch_id=batch_id)
    try:
        estimate = estimate_holding_sync(client, expected_date=batch.date)
        cached = _cached_holding_rows(
            s, batch_id=batch_id, date=batch.date, tm_ids=estimate["tm_ids"])
        if cached is not None:
            mapped = [row.model_dump(exclude={"holding_id", "batch_id"}) for row in cached]
            unmatched = _unmatched_positions(s, batch_id, mapped)
            if unmatched:
                raise TrendAnimalsError(
                    "missing_required_fields", f"API 收藏夹未唯一覆盖券商持仓：{unmatched}")
            backfilled = backfill_position_codes(s, batch_id=batch_id)
            s.commit()
            finish_audit(
                s, audit, status="done", as_of_date=batch.date, tm_count=len(cached),
                requested_fields=HOLDING_FIELDS, estimated_cost=estimate["estimated_cost"],
                actual_cost=0.0,
                details={"backfilled": backfilled, "source": "trend_api", "cached": True},
            )
            return {
                "ok": True, "source": "trend_api", "as_of_date": batch.date,
                "rows": len(cached), "backfilled": backfilled, "incomplete_rows": [],
                "estimated_cost": estimate["estimated_cost"], "actual_cost": 0.0,
                "cached": True,
            }
        ensure_budget(estimate["estimated_cost"], approved_budget)
        before = ledger_mark(client)
        snapshots = client.get_snapshot(estimate["tm_ids"], HOLDING_FIELDS)
        if not isinstance(snapshots, list):
            raise TrendAnimalsError("api_contract_error", "持仓快照 data 不是数组")
        by_tm = {int(row["tmId"]): row for row in snapshots if row.get("tmId") is not None}
        missing_tm = sorted(set(estimate["tm_ids"]) - set(by_tm))
        if missing_tm:
            raise TrendAnimalsError("missing_required_fields", f"持仓快照缺少 tmId：{missing_tm}")
        fav_by_tm = {int(row["tmId"]): row for row in estimate["favorites"]}
        incomplete: list[dict] = []
        mapped: list[dict] = []
        dates: set[str] = set()
        for tm_id in estimate["tm_ids"]:
            row = by_tm[tm_id]
            missing = [field for field in IDENTITY_FIELDS if row.get(field) is None]
            if missing:
                incomplete.append({"tmId": tm_id, "missing": missing})
                continue
            dates.add(str(row.get("asOfDate")))
            fav = fav_by_tm[tm_id]
            mapped.append(holding_row(row, update_dt=fav.get("updateDt")))
        if incomplete:
            raise TrendAnimalsError(
                "missing_required_fields", f"持仓快照关键字段缺失：{incomplete}")
        if dates != {batch.date}:
            raise TrendAnimalsError("data_stale", f"持仓快照出现非目标日期：{sorted(dates)}")
        unmatched = _unmatched_positions(s, batch_id, mapped)
        if unmatched:
            raise TrendAnimalsError(
                "missing_required_fields", f"API 收藏夹未唯一覆盖券商持仓：{unmatched}")
        # 替换 + 回填在同一事务内；API/校验失败时不会先删旧数据。
        upsert_holding_temps(s, batch_id=batch_id, rows=mapped, commit=False)
        backfilled = backfill_position_codes(s, batch_id=batch_id)
        s.commit()
        actual = ledger_delta(client, before)
        finish_audit(
            s, audit, status="done", as_of_date=batch.date, tm_count=len(mapped),
            requested_fields=HOLDING_FIELDS, estimated_cost=estimate["estimated_cost"],
            actual_cost=actual, incomplete_rows=incomplete,
            details={"backfilled": backfilled, "source": "trend_api"},
        )
        return {
            "ok": True, "source": "trend_api", "as_of_date": batch.date,
            "rows": len(mapped), "backfilled": backfilled, "incomplete_rows": incomplete,
            "estimated_cost": estimate["estimated_cost"], "actual_cost": actual,
            "cached": False,
        }
    except TrendAnimalsError as error:
        s.rollback()
        finish_audit(s, audit, status="blocked" if error.code in {
            "data_stale", "confirmation_required", "not_configured"
        } else "failed", error=error)
        raise
