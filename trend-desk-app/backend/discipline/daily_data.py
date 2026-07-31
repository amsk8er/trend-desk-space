"""纪律交易台每日事实包：当日API只采一次，成功后所有下游只读数据库。"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, delete, select

from backend import config
from backend.db import (
    Batch, DailyDataset, HoldingTemp, Manifest, PositionLot, TrendApiSync,
    TrendDailyMembership, TrendDailySnapshot, TushareDailyFact,
)
from backend.discipline.data_sources import TushareProbeClient, normalize_tushare_code
from backend.trend_animals.billing import (
    component_pricing, endpoint_fixed_cost, ensure_budget,
    estimate_component_cost, estimate_snapshot_cost,
)
from backend.trend_animals.errors import (
    BudgetConfirmationRequired, TrendAnimalsError, redact_secret,
)
from backend.trend_animals.service import ledger_delta, ledger_mark, require_asset_date

CHINA_TZ = ZoneInfo(config.TREND_DAILY_TIMEZONE)
READY_STATES = {"ready", "ready_degraded"}
RETRY_STATES = {"pending", "checking", "waiting_retry", "fetching", "failed"}
COMBOS = {"温转热(A股)": "warm_to_hot_stock", "温转热(ETF基金个股)": "warm_to_hot_etf"}
COUNT_FIELDS = ["tmId", "tickerName", "asOfDate", "constituentCount"]
IDENTITY_FIELDS = ["tmId", "tickerName", "tickerSymbol", "asset", "asOfDate"]
CANDIDATE_TREND_FIELDS = [
    "trendTemperatureCurr", "trendTemperaturePrev",
    "daysSinceTrendEntry", "trendPhaseCurr", "trendStrengthLocalCurr",
    "trendStrengthLocalChange",
]
CANDIDATE_FIELDS = [*IDENTITY_FIELDS, *CANDIDATE_TREND_FIELDS]
STOCK_INDUSTRY_FIELDS = [
    *IDENTITY_FIELDS, "industryTmId", "industryName",
    "industryTrendTemperatureCurr",
]
HOLDING_EXIT_FIELDS = [
    *IDENTITY_FIELDS, "stopwinFlagByDangerSignal", "stopwinFlagByPopChampagne",
]
HOLDING_ONLY_FIELDS = [
    *CANDIDATE_FIELDS,
    "stopwinFlagByDangerSignal", "stopwinFlagByPopChampagne",
]
MARKET_FIELDS = [
    *IDENTITY_FIELDS, "trendTemperatureCurr", "stopwinFlagByDangerSignal",
]
_RUN_LOCK = threading.Lock()


def _required_tushare_codes(s: Session, dataset_id: str) -> list[str]:
    """Return the union of signal-universe codes and every open formal lot.

    The closing-price ledger must be able to value holdings that have dropped
    out of the Trend Animals watch/holding groups.  PositionLot is the durable
    account authority, so an open lot must never be omitted merely because the
    daily signal snapshot no longer contains it.
    """
    snapshots = s.exec(select(TrendDailySnapshot).where(
        TrendDailySnapshot.dataset_id == dataset_id,
        TrendDailySnapshot.code.is_not(None),
    )).all()
    lots = s.exec(select(PositionLot).where(PositionLot.remaining_shares > 0)).all()
    return sorted({
        normalize_tushare_code(str(code))
        for code in [*(row.code for row in snapshots), *(row.instrument_id for row in lots)]
        if code
    })


def china_now() -> datetime:
    return datetime.now(CHINA_TZ)


def china_trade_date(now: datetime | None = None) -> str:
    return (now or china_now()).astimezone(CHINA_TZ).date().isoformat()


def _parse_hhmm(value: str) -> time:
    hour, minute = (int(x) for x in value.split(":"))
    return time(hour, minute)


def collection_slots(trade_date: str) -> list[datetime]:
    day = date.fromisoformat(trade_date)
    start = datetime.combine(day, _parse_hhmm(config.TREND_DAILY_START), CHINA_TZ)
    cutoff = datetime.combine(day, _parse_hhmm(config.TREND_DAILY_CUTOFF), CHINA_TZ)
    slots: list[datetime] = []
    current = start
    while current <= cutoff:
        slots.append(current)
        current += timedelta(minutes=config.TREND_DAILY_RETRY_MINUTES)
    return slots


def before_collection_window(now: datetime | None = None) -> bool:
    current = (now or china_now()).astimezone(CHINA_TZ)
    return current < collection_slots(current.date().isoformat())[0]


def after_cutoff(now: datetime | None = None) -> bool:
    current = (now or china_now()).astimezone(CHINA_TZ)
    return current > collection_slots(current.date().isoformat())[-1]


def next_retry(now: datetime | None = None) -> datetime | None:
    current = (now or china_now()).astimezone(CHINA_TZ)
    for slot in collection_slots(current.date().isoformat()):
        if slot > current:
            return slot.astimezone(timezone.utc).replace(tzinfo=None)
    return None


def _dataset_id(trade_date: str) -> str:
    return f"dataset_{trade_date.replace('-', '')}"


def ensure_dataset(s: Session, trade_date: str) -> DailyDataset:
    row = s.exec(select(DailyDataset).where(DailyDataset.trade_date == trade_date)).first()
    if row is not None:
        return row
    row = DailyDataset(
        dataset_id=_dataset_id(trade_date), trade_date=trade_date,
        approved_budget=config.TREND_ANIMALS_DAILY_AUTO_BUDGET,
        source_status={"trend_animals": {"status": "pending"},
                       "tushare": {"status": "pending"}},
    )
    s.add(row)
    try:
        s.commit(); s.refresh(row)
        return row
    except IntegrityError:
        s.rollback()
        existing = s.exec(select(DailyDataset).where(DailyDataset.trade_date == trade_date)).first()
        if existing is None:
            raise
        return existing


def serialize_dataset(s: Session, row: DailyDataset, *, cached: bool = True) -> dict:
    # SQLAlchemy expires ORM attributes after commit.  Refresh before serialising so
    # SQLModel does not emit a deceptively empty model_dump for a persisted row.
    s.refresh(row)
    out = row.model_dump()
    out["cached"] = cached
    out["network_calls"] = 0 if cached else None
    out["trend_rows"] = len(s.exec(select(TrendDailySnapshot).where(
        TrendDailySnapshot.dataset_id == row.dataset_id)).all())
    out["market_rows"] = len(s.exec(select(TushareDailyFact).where(
        TushareDailyFact.dataset_id == row.dataset_id)).all())
    out["can_generate_plan"] = row.status in READY_STATES
    return out


def get_dataset_by_date(s: Session, trade_date: str) -> DailyDataset:
    row = s.exec(select(DailyDataset).where(DailyDataset.trade_date == trade_date)).first()
    if row is None:
        raise KeyError(trade_date)
    return row


def _hash_payload(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _find_combos(rows: list[dict], trade_date: str) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for name in COMBOS:
        matches = [row for row in rows if row.get("tickerName") == name]
        if len(matches) != 1:
            raise TrendAnimalsError("api_contract_error", f"组合 {name} 搜索结果应唯一，实际 {len(matches)}")
        if matches[0].get("asOfDate") != trade_date:
            raise TrendAnimalsError("data_stale", f"组合 {name} 尚未更新到 {trade_date}")
        found[name] = matches[0]
    return found


def _daily_cost_estimate(
    *, docs: list[dict], billing: list[dict],
    component_counts: dict[str, int],
    snapshot_batches: list[tuple[str, list[str], int]],
) -> dict:
    """按实际 API 请求逐批估价，避免跨请求错误共享阶梯折扣。"""
    fixed_search = endpoint_fixed_cost(docs, "searchTicker")
    count_snapshot = estimate_snapshot_cost(COUNT_FIELDS, len(COMBOS), billing)
    base, normal_row, combo_row = component_pricing(docs)
    component_cost = sum(
        estimate_component_cost(
            count, combo=True, base_cost=base, normal_row_cost=normal_row,
            combo_row_cost=combo_row,
        )
        for count in component_counts.values()
    )
    snapshot_costs = {
        label: estimate_snapshot_cost(fields, row_count, billing)
        for label, fields, row_count in snapshot_batches
        if row_count
    }
    breakdown = {
        "search": fixed_search,
        "count_snapshot": count_snapshot,
        "components": component_cost,
        **snapshot_costs,
    }
    return {
        "estimated_cost": round(sum(breakdown.values()), 6),
        "estimate_breakdown": breakdown,
    }


def _validate_direct_components(
    *, combo_name: str, rows: list[dict], trade_date: str,
) -> None:
    expected_asset = "A股" if COMBOS[combo_name] == "warm_to_hot_stock" else "ETF基金"
    invalid = [
        {
            "tmId": row.get("tmId"),
            "asset": row.get("asset"),
            "tickerSymbol": row.get("tickerSymbol"),
        }
        for row in rows
        if row.get("tmId") is None
        or row.get("tickerSymbol") is None
        or row.get("asset") != expected_asset
    ]
    if invalid:
        raise TrendAnimalsError(
            "nested_components_require_expansion",
            f"{combo_name} 直接成分含非基础品种，已停止以避免 all_basic=1 扩展计费：{invalid}",
        )
    if any(row.get("asOfDate") != trade_date for row in rows):
        raise TrendAnimalsError("data_stale", f"{combo_name} 成分包含非 {trade_date} 数据")
    tm_ids = [int(row["tmId"]) for row in rows]
    if len(tm_ids) != len(set(tm_ids)):
        raise TrendAnimalsError("api_contract_error", f"{combo_name} 成分返回重复 tmId")


def _fetch_snapshot_batch(
    client, *, tm_ids: list[int], fields: list[str],
    trade_date: str, label: str,
) -> list[dict]:
    if not tm_ids:
        return []
    rows = client.get_snapshot(tm_ids, fields)
    if not isinstance(rows, list):
        raise TrendAnimalsError("api_contract_error", f"{label}快照 data 不是数组")
    by_tm = {int(row["tmId"]): row for row in rows if row.get("tmId") is not None}
    missing = sorted(set(tm_ids) - set(by_tm))
    if missing:
        raise TrendAnimalsError("missing_required_fields", f"{label}快照缺少 tmId：{missing}")
    if len(by_tm) != len(rows):
        raise TrendAnimalsError("api_contract_error", f"{label}快照存在缺失或重复 tmId")
    if any(row.get("asOfDate") != trade_date for row in rows):
        raise TrendAnimalsError("data_stale", f"{label}快照包含非 {trade_date} 数据")
    return rows


def _collect_trend(client, *, trade_date: str, approved_budget: float) -> dict:
    before = ledger_mark(client)
    docs = client.get_api_doc_intro()
    change_log_available = True
    try:
        client.get_change_log()
    except TrendAnimalsError:
        change_log_available = False
    billing = client.get_snapshot_billing()
    statuses = client.get_update_status()
    market_status = require_asset_date(statuses, "A股", trade_date)
    require_asset_date(statuses, "ETF基金", trade_date)

    combos = _find_combos(client.search_ticker("温转热"), trade_date)
    combo_ids = [int(combos[name]["tmId"]) for name in COMBOS]
    count_rows = client.get_snapshot(combo_ids, COUNT_FIELDS)
    count_rows_by_tm = {
        int(row["tmId"]): row for row in count_rows if row.get("tmId") is not None
    }
    counts: dict[str, int] = {}
    for name, combo in combos.items():
        tm_id = int(combo["tmId"])
        count_row = count_rows_by_tm.get(tm_id)
        count = count_row.get("constituentCount") if count_row else None
        if not isinstance(count, (int, float)) or int(count) < 0:
            raise TrendAnimalsError(
                "api_contract_error", f"组合 {name} 缺少有效 constituentCount")
        counts[name] = int(count)
    eligible_favorites = [
        row for row in client.get_favorites_ticker("持仓")
        if row.get("asset") in {"A股", "ETF基金"}
    ]
    if any(row.get("tmId") is None for row in eligible_favorites):
        raise TrendAnimalsError("missing_required_fields", "持仓收藏夹存在缺少 tmId 的行")
    favorites_by_tm = {
        int(row["tmId"]): row
        for row in eligible_favorites
    }
    favorites = list(favorites_by_tm.values())
    if any(row.get("asOfDate") != trade_date for row in favorites):
        raise TrendAnimalsError("data_stale", "持仓收藏夹尚未全部更新到当日")

    preflight = _daily_cost_estimate(
        docs=docs,
        billing=billing,
        component_counts=counts,
        snapshot_batches=[
            ("candidate_core", CANDIDATE_FIELDS, sum(counts.values())),
            ("stock_industry", STOCK_INDUSTRY_FIELDS,
             counts.get("温转热(A股)", 0)),
            # 展开前按“持仓均不与候选重合”估算，宁可略高也不漏掉退出字段。
            ("holding_only", HOLDING_ONLY_FIELDS, len(favorites)),
            ("market", MARKET_FIELDS, 1),
        ],
    )
    ensure_budget(preflight["estimated_cost"], approved_budget)

    components: dict[str, list[dict]] = {}
    memberships: list[dict] = []
    component_count_warnings: list[dict] = []
    for name, combo in combos.items():
        # 温转热榜单预期直接返回基础品种；不用 all_basic=1，防止递归展开产生隐性行费。
        rows = client.get_components(int(combo["tmId"]), all_basic=False)
        if not isinstance(rows, list):
            raise TrendAnimalsError("api_contract_error", f"{name} 成分 data 不是数组")
        if counts[name] and not rows:
            raise TrendAnimalsError("missing_required_fields", f"{name} 返回空成分")
        _validate_direct_components(combo_name=name, rows=rows, trade_date=trade_date)
        if len(rows) != counts[name]:
            component_count_warnings.append({
                "combo": name,
                "constituent_count": counts[name],
                "returned_direct_count": len(rows),
            })
        components[name] = rows
        memberships.extend({"membership_type": COMBOS[name], "tm_id": int(row["tmId"]),
                            "metadata_json": {"combo": name}} for row in rows)
    for row in favorites:
        if row.get("tmId") is None:
            raise TrendAnimalsError("missing_required_fields", "持仓收藏夹存在缺少 tmId 的行")
        memberships.append({"membership_type": "holding", "tm_id": int(row["tmId"]),
                            "metadata_json": {"update_dt": row.get("updateDt")}})

    stock_ids = sorted({
        int(row["tmId"]) for row in components.get("温转热(A股)", [])
    })
    etf_ids = sorted({
        int(row["tmId"]) for row in components.get("温转热(ETF基金个股)", [])
    })
    if set(stock_ids) & set(etf_ids):
        raise TrendAnimalsError("api_contract_error", "A股与 ETF 温转热成分出现重复 tmId")
    candidate_ids = set(stock_ids) | set(etf_ids)
    holding_ids = set(favorites_by_tm)
    holding_overlap_ids = sorted(holding_ids & candidate_ids)
    holding_only_ids = sorted(holding_ids - candidate_ids)
    market_tm_id = int(market_status["tmId"])

    realized = _daily_cost_estimate(
        docs=docs,
        billing=billing,
        component_counts={name: len(rows) for name, rows in components.items()},
        snapshot_batches=[
            ("candidate_core", CANDIDATE_FIELDS, len(candidate_ids)),
            ("stock_industry", STOCK_INDUSTRY_FIELDS, len(stock_ids)),
            ("holding_exit_extras", HOLDING_EXIT_FIELDS, len(holding_overlap_ids)),
            ("holding_only", HOLDING_ONLY_FIELDS, len(holding_only_ids)),
            ("market", MARKET_FIELDS, 1),
        ],
    )
    # 成分调用后、较贵的快照调用前，按真实直接成分数和持仓重合关系再做一次闸门。
    ensure_budget(realized["estimated_cost"], approved_budget)

    batches = [
        ("候选核心", sorted(candidate_ids), CANDIDATE_FIELDS),
        ("A股候选行业", stock_ids, STOCK_INDUSTRY_FIELDS),
        ("候选持仓退出补充", holding_overlap_ids, HOLDING_EXIT_FIELDS),
        ("非候选持仓", holding_only_ids, HOLDING_ONLY_FIELDS),
        ("A股大盘", [market_tm_id], MARKET_FIELDS),
    ]
    merged: dict[int, dict] = {}
    for label, ids, fields in batches:
        for raw in _fetch_snapshot_batch(
            client, tm_ids=ids, fields=fields, trade_date=trade_date, label=label,
        ):
            tm_id = int(raw["tmId"])
            merged[tm_id] = {**merged.get(tm_id, {}), **raw}
    missing_symbols = sorted(
        tm_id for tm_id in candidate_ids | holding_ids
        if not merged.get(tm_id, {}).get("tickerSymbol")
    )
    if missing_symbols:
        raise TrendAnimalsError(
            "missing_required_fields", f"候选/持仓快照缺少 tickerSymbol：{missing_symbols}")
    market_row = merged.get(market_tm_id, {})
    if market_row.get("trendTemperatureCurr") is None or \
            market_row.get("stopwinFlagByDangerSignal") is None:
        raise TrendAnimalsError("missing_required_fields", "A股大盘快照缺少温度或危险信号")

    memberships.append({"membership_type": "market", "tm_id": int(market_status["tmId"]),
                        "metadata_json": {}})

    requested_fields = sorted({field for _, _, fields in batches for field in fields})
    return {"snapshots": list(merged.values()), "memberships": memberships,
            "estimated_cost": realized["estimated_cost"],
            "actual_cost": ledger_delta(client, before),
            "capabilities": {
                "volatility_field": None,
                "volatility_supported": False,
                "volatility_replaced_by_boiling": True,
                "industry_temperature_embedded": True,
                "change_log_available": change_log_available,
                "collection_plan_version": "minimum-fields-v2",
                "cost_breakdown": realized["estimate_breakdown"],
                "component_count_warnings": component_count_warnings,
            },
            "requested_fields": requested_fields}


def _persist_trend(s: Session, dataset: DailyDataset, payload: dict) -> None:
    s.exec(delete(TrendDailyMembership).where(TrendDailyMembership.dataset_id == dataset.dataset_id))
    s.exec(delete(TrendDailySnapshot).where(TrendDailySnapshot.dataset_id == dataset.dataset_id))
    seen: set[int] = set()
    for raw in payload["snapshots"]:
        tm_id = int(raw["tmId"])
        if tm_id in seen:
            continue
        seen.add(tm_id)
        strength = raw.get("trendStrengthLocalCurr")
        temperature_curr = raw.get("trendTemperatureCurr")
        boiling = raw.get("stopwinFlagByBoilingTemperature")
        if boiling is None and temperature_curr is not None:
            # 实时规范把“沸腾止盈”定义为当前温度为“沸”，无需再购买同义字段。
            boiling = temperature_curr == "沸"
        s.add(TrendDailySnapshot(
            dataset_id=dataset.dataset_id, tm_id=tm_id, code=raw.get("tickerSymbol"),
            name=raw.get("tickerName") or raw.get("industryName") or str(tm_id),
            asset=raw.get("asset"), industry_tm_id=raw.get("industryTmId"),
            industry_name=raw.get("industryName"),
            temperature_prev=raw.get("trendTemperaturePrev"),
            temperature_curr=temperature_curr,
            strength=float(strength) if isinstance(strength, (int, float)) else None,
            strength_change=raw.get("trendStrengthLocalChange"),
            right_side_days=int(raw["daysSinceTrendEntry"]) if isinstance(raw.get("daysSinceTrendEntry"), (int, float)) else None,
            phase=raw.get("trendPhaseCurr"), danger=raw.get("stopwinFlagByDangerSignal"),
            boiling=boiling,
            champagne=raw.get("stopwinFlagByPopChampagne"),
            volatility_up=None,
            market_cap_yi=float(raw["marketCap"]) if isinstance(raw.get("marketCap"), (int, float)) else None,
            amount_yi=float(raw["amount1d"]) if isinstance(raw.get("amount1d"), (int, float)) else None,
            as_of_date=dataset.trade_date, raw_payload=raw, payload_hash=_hash_payload(raw),
        ))
    dedup: set[tuple[str, int]] = set()
    for item in payload["memberships"]:
        key = (item["membership_type"], int(item["tm_id"]))
        if key in dedup:
            continue
        dedup.add(key)
        s.add(TrendDailyMembership(dataset_id=dataset.dataset_id, **item))
    s.flush()


def _persist_tushare(s: Session, dataset: DailyDataset, rows: list[dict]) -> None:
    s.exec(delete(TushareDailyFact).where(TushareDailyFact.dataset_id == dataset.dataset_id))
    for row in rows:
        s.add(TushareDailyFact(dataset_id=dataset.dataset_id, **row))
    s.flush()


def _bare(code: str | None) -> str:
    return str(code or "").upper().split(".", 1)[0]


def missing_selection_fact_fields(
    s: Session, dataset_id: str, rows: list[dict],
) -> dict[str, list[str]]:
    """检查选股硬门所需的Tushare字段，避免空壳事实被误标为ready。"""
    facts = {_bare(row.get("ts_code")): row for row in rows}
    snapshots = {
        row.tm_id: row for row in s.exec(select(TrendDailySnapshot).where(
            TrendDailySnapshot.dataset_id == dataset_id)).all()
    }
    memberships = s.exec(select(TrendDailyMembership).where(
        TrendDailyMembership.dataset_id == dataset_id,
        TrendDailyMembership.membership_type.in_(("warm_to_hot_stock", "warm_to_hot_etf")),
    )).all()
    missing: dict[str, list[str]] = {}
    for membership in memberships:
        snapshot = snapshots.get(membership.tm_id)
        if snapshot is None or not snapshot.code:
            continue
        code = _bare(snapshot.code)
        fact = facts.get(code) or {}
        required = (
            ("close", "amount_yi", "fund_size_yi")
            if membership.membership_type == "warm_to_hot_etf"
            else ("close", "amount_yi", "float_market_cap_yi")
        )
        absent = [field for field in required if fact.get(field) is None]
        if absent:
            missing[code] = absent
    return missing


def _ensure_complete_selection_facts(
    s: Session, dataset_id: str, rows: list[dict],
) -> None:
    missing = missing_selection_fact_fields(s, dataset_id, rows)
    if missing:
        details = json.dumps(missing, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        raise RuntimeError(f"tushare_missing_required_fields:{details}")


def _dataset_hash(s: Session, dataset_id: str) -> str:
    trend = s.exec(select(TrendDailySnapshot).where(
        TrendDailySnapshot.dataset_id == dataset_id).order_by(TrendDailySnapshot.tm_id)).all()
    facts = s.exec(select(TushareDailyFact).where(
        TushareDailyFact.dataset_id == dataset_id).order_by(TushareDailyFact.ts_code)).all()
    memberships = s.exec(select(TrendDailyMembership).where(
        TrendDailyMembership.dataset_id == dataset_id).order_by(
            TrendDailyMembership.membership_type, TrendDailyMembership.tm_id)).all()
    return _hash_payload({"trend": [x.payload_hash for x in trend],
                          "facts": [x.model_dump(exclude={"fact_id", "fetched_at"}) for x in facts],
                          "memberships": [(x.membership_type, x.tm_id) for x in memberships]})


def _audit(s: Session, dataset: DailyDataset, *, trigger: str,
           scheduled_for: datetime | None) -> TrendApiSync:
    row = TrendApiSync(
        dataset_id=dataset.dataset_id, scope="daily_dataset", status="running",
        trigger=trigger, attempt_no=dataset.attempt_count,
        scheduled_for=scheduled_for,
    )
    s.add(row); s.commit(); s.refresh(row)
    return row


def _claim_lease(s: Session, dataset: DailyDataset) -> str | None:
    """Atomically claim the dataset across processes before any external call."""
    owner = uuid4().hex
    now = datetime.utcnow()
    result = s.execute(sa_update(DailyDataset).where(
        DailyDataset.dataset_id == dataset.dataset_id,
        (DailyDataset.lease_owner.is_(None)) | (DailyDataset.lease_expires_at < now),
    ).values(lease_owner=owner, lease_expires_at=now + timedelta(minutes=15)))
    s.commit()
    return owner if result.rowcount == 1 else None


def _finish_audit(s: Session, audit: TrendApiSync, dataset: DailyDataset, *, status: str) -> None:
    audit.status = status
    audit.as_of_date = dataset.trade_date
    audit.estimated_cost = dataset.estimated_cost
    audit.actual_cost = dataset.actual_cost
    audit.next_retry_at = dataset.next_retry_at
    audit.error_code = dataset.error_code
    audit.error_message = redact_secret(dataset.error_message)
    audit.finished_at = datetime.utcnow()
    s.add(audit); s.commit()


def run_daily_collection(s: Session, *, trend_client, tushare_client: TushareProbeClient,
                         trade_date: str, trigger: str = "manual",
                         scheduled_for: datetime | None = None,
                         manual: bool = False, now: datetime | None = None) -> dict:
    """幂等执行一次；ready 后第一行即返回，不访问任何外部服务。"""
    dataset = ensure_dataset(s, trade_date)
    if dataset.status in READY_STATES:
        return serialize_dataset(s, dataset, cached=True)
    if not _RUN_LOCK.acquire(blocking=False):
        s.refresh(dataset)
        return serialize_dataset(s, dataset, cached=True)
    audit: TrendApiSync | None = None
    try:
        current = (now or china_now()).astimezone(CHINA_TZ)
        if not manual and current.date().isoformat() == trade_date and after_cutoff(current):
            dataset.status = "manual_required"; dataset.next_retry_at = None
            dataset.error_code = "automatic_window_closed"
            dataset.error_message = "已超过北京时间20:00，等待人工检查"
            dataset.updated_at = datetime.utcnow(); s.add(dataset); s.commit()
            return serialize_dataset(s, dataset, cached=True)

        lease_owner = _claim_lease(s, dataset)
        if lease_owner is None:
            s.refresh(dataset)
            return serialize_dataset(s, dataset, cached=True)
        if not tushare_client.is_trade_day(trade_date):
            dataset.status = "manual_required"; dataset.error_code = "not_trade_day"
            dataset.error_message = "非A股交易日"; dataset.next_retry_at = None
            dataset.lease_owner = None; dataset.lease_expires_at = None
            dataset.updated_at = datetime.utcnow(); s.add(dataset); s.commit()
            return {**serialize_dataset(s, dataset, cached=True), "is_trade_day": False}

        dataset.status = "checking"; dataset.attempt_count += 1
        dataset.lease_owner = lease_owner
        dataset.error_code = None; dataset.error_message = None; dataset.updated_at = datetime.utcnow()
        s.add(dataset); s.commit()
        audit = _audit(s, dataset, trigger=trigger, scheduled_for=scheduled_for)
        source_status = dict(dataset.source_status or {})

        if source_status.get("trend_animals", {}).get("status") != "ready":
            dataset.status = "fetching"; s.add(dataset); s.commit()
            trend = _collect_trend(
                trend_client, trade_date=trade_date, approved_budget=dataset.approved_budget)
            _persist_trend(s, dataset, trend)
            dataset.estimated_cost = trend["estimated_cost"]
            dataset.actual_cost = trend["actual_cost"]
            dataset.capability_flags = trend["capabilities"]
            source_status["trend_animals"] = {"status": "ready", "as_of_date": trade_date,
                                               "rows": len(trend["snapshots"])}
            dataset.source_dates = {**(dataset.source_dates or {}), "trend_animals": trade_date}
            dataset.source_status = source_status; s.add(dataset); s.commit()

        if source_status.get("tushare", {}).get("status") != "ready":
            codes = _required_tushare_codes(s, dataset.dataset_id)
            facts = tushare_client.fetch_daily_facts(trade_date=trade_date, codes=codes)
            if not facts and codes:
                raise RuntimeError("tushare_empty_facts")
            _ensure_complete_selection_facts(s, dataset.dataset_id, facts)
            _persist_tushare(s, dataset, facts)
            source_status["tushare"] = {"status": "ready", "as_of_date": trade_date,
                                         "rows": len(facts),
                                         "next_trade_date": tushare_client.next_trade_day(trade_date)}
            dataset.source_dates = {**(dataset.source_dates or {}), "tushare": trade_date}
            dataset.source_status = source_status; s.add(dataset); s.commit()

        dataset.status = "ready"
        dataset.dataset_hash = _dataset_hash(s, dataset.dataset_id)
        dataset.ready_at = datetime.utcnow(); dataset.next_retry_at = None
        dataset.lease_owner = None; dataset.lease_expires_at = None
        dataset.updated_at = datetime.utcnow(); s.add(dataset); s.commit()
        from backend.discipline.dataset_plan import ensure_signal_plan
        ensure_signal_plan(s, dataset.dataset_id)
        if audit:
            _finish_audit(s, audit, dataset, status="done")
        return serialize_dataset(s, dataset, cached=False)
    except TrendAnimalsError as exc:
        s.rollback(); dataset = get_dataset_by_date(s, trade_date)
        retry_at = next_retry(now)
        if exc.code == "confirmation_required":
            dataset.status = "awaiting_budget"; retry_at = None
            if isinstance(exc, BudgetConfirmationRequired):
                dataset.estimated_cost = float(exc.estimated_cost)
        elif retry_at is None and not manual:
            dataset.status = "manual_required"
        else:
            dataset.status = "waiting_retry"
        dataset.next_retry_at = retry_at; dataset.error_code = exc.code
        dataset.error_message = exc.message; dataset.lease_owner = None; dataset.lease_expires_at = None
        dataset.updated_at = datetime.utcnow(); s.add(dataset); s.commit()
        if audit:
            _finish_audit(s, audit, dataset, status="blocked")
        return serialize_dataset(s, dataset, cached=False)
    except Exception as exc:
        s.rollback(); dataset = get_dataset_by_date(s, trade_date)
        retry_at = next_retry(now)
        dataset.status = "waiting_retry" if retry_at is not None or manual else "manual_required"
        dataset.next_retry_at = retry_at; dataset.error_code = type(exc).__name__
        dataset.error_message = redact_secret(exc); dataset.lease_owner = None; dataset.lease_expires_at = None
        dataset.updated_at = datetime.utcnow(); s.add(dataset); s.commit()
        if audit:
            _finish_audit(s, audit, dataset, status="failed")
        return serialize_dataset(s, dataset, cached=False)
    finally:
        _RUN_LOCK.release()


def repair_tushare_facts(
    s: Session, *, tushare_client: TushareProbeClient, trade_date: str,
) -> dict:
    """只修复既有API数据集的Tushare事实，不访问趋势动物。"""
    dataset = get_dataset_by_date(s, trade_date)
    trend_status = (dataset.source_status or {}).get("trend_animals", {}).get("status")
    if trend_status != "ready":
        raise ValueError("trend_dataset_not_ready")
    codes = _required_tushare_codes(s, dataset.dataset_id)
    facts = tushare_client.fetch_daily_facts(trade_date=trade_date, codes=codes)
    if not facts and codes:
        raise RuntimeError("tushare_empty_facts")
    _ensure_complete_selection_facts(s, dataset.dataset_id, facts)
    _persist_tushare(s, dataset, facts)
    source_status = dict(dataset.source_status or {})
    source_status["tushare"] = {
        "status": "ready", "as_of_date": trade_date, "rows": len(facts),
        "next_trade_date": tushare_client.next_trade_day(trade_date),
        "repaired": True,
    }
    dataset.source_status = source_status
    dataset.source_dates = {**(dataset.source_dates or {}), "tushare": trade_date}
    dataset.status = "ready"
    dataset.dataset_hash = _dataset_hash(s, dataset.dataset_id)
    dataset.error_code = None
    dataset.error_message = None
    dataset.updated_at = datetime.utcnow()
    dataset.ready_at = dataset.ready_at or datetime.utcnow()
    s.add(dataset)
    s.commit()
    from backend.discipline.dataset_plan import ensure_signal_plan
    plan = ensure_signal_plan(s, dataset.dataset_id)
    return {**serialize_dataset(s, dataset, cached=False), "plan": plan,
            "repair_scope": "tushare_only", "trend_network_calls": 0}


def approve_budget(s: Session, *, trade_date: str, amount: float) -> dict:
    dataset = get_dataset_by_date(s, trade_date)
    if amount <= 0:
        raise ValueError("invalid_budget")
    if dataset.status == "awaiting_budget" and \
            dataset.estimated_cost > 0 and amount + 1e-9 < dataset.estimated_cost:
        raise ValueError(
            f"approved_budget_below_estimate:"
            f"需要至少 {dataset.estimated_cost:.3f} 元，提交 {amount:.3f} 元")
    dataset.approved_budget = float(amount)
    if dataset.status == "awaiting_budget":
        dataset.status = "pending"; dataset.error_code = None; dataset.error_message = None
    dataset.updated_at = datetime.utcnow(); s.add(dataset); s.commit()
    return serialize_dataset(s, dataset)


def _latest_fallback_manifest(s: Session, batch_id: str) -> Manifest | None:
    """Prefer the fully filtered list, but allow an older prescreen-only batch."""
    rows = s.exec(select(Manifest).where(Manifest.batch_id == batch_id).order_by(
        Manifest.created_at.desc())).all()
    return next((row for row in rows if row.stage == "b_filter"), None) or next(
        (row for row in rows if row.stage == "prescreen"), None)


def preview_ocr_fallback(s: Session, *, trade_date: str, batch_id: str) -> dict:
    batch = s.get(Batch, batch_id)
    if batch is None:
        raise KeyError(batch_id)
    if batch.date != trade_date:
        raise ValueError("ocr_batch_date_mismatch")
    holdings = s.exec(select(HoldingTemp).where(HoldingTemp.batch_id == batch_id)).all()
    manifest = _latest_fallback_manifest(s, batch_id)
    candidates = list((manifest.white_list if manifest else []) or [])
    missing_codes = [row.name for row in holdings if not row.code]
    missing_codes.extend(str(row.get("name") or "unknown") for row in candidates if not row.get("code"))
    return {
        "status": "preview", "trade_date": trade_date, "batch_id": batch_id,
        "manifest_stage": manifest.stage if manifest else None,
        "holding_rows": len(holdings), "candidate_rows": len(candidates),
        "blockers": ([{"code": "missing_instrument_code", "items": missing_codes}]
                     if missing_codes else []),
        "warnings": ["OCR只作为趋势数据备用；确认后数据集标记为ready_degraded"],
    }


def _ocr_tm_id(code: str) -> int:
    # Negative IDs cannot collide with Trend Animals' positive tmId values.
    digest = hashlib.sha256(code.upper().encode()).hexdigest()
    return -(int(digest[:12], 16) % 2_000_000_000 + 1)


def _candidate_value(row: dict, *names: str):
    for name in names:
        value = row.get(name)
        if value is not None:
            return value
    return None


def confirm_ocr_fallback(s: Session, *, trade_date: str, batch_id: str) -> dict:
    """Publish a manually confirmed legacy OCR batch as a degraded daily dataset."""
    preview = preview_ocr_fallback(s, trade_date=trade_date, batch_id=batch_id)
    if preview["blockers"]:
        raise ValueError("ocr_fallback_has_blockers")
    dataset = ensure_dataset(s, trade_date)
    if dataset.status in READY_STATES and dataset.source_mode == "trend_api":
        raise ValueError("api_dataset_already_ready")
    holdings = s.exec(select(HoldingTemp).where(HoldingTemp.batch_id == batch_id)).all()
    manifest = _latest_fallback_manifest(s, batch_id)
    candidates = list((manifest.white_list if manifest else []) or [])
    if not holdings and not candidates:
        raise ValueError("ocr_fallback_empty")

    s.exec(delete(TrendDailyMembership).where(TrendDailyMembership.dataset_id == dataset.dataset_id))
    s.exec(delete(TrendDailySnapshot).where(TrendDailySnapshot.dataset_id == dataset.dataset_id))
    s.exec(delete(TushareDailyFact).where(TushareDailyFact.dataset_id == dataset.dataset_id))
    snapshots: dict[str, dict] = {}
    memberships: set[tuple[str, str]] = set()
    for row in holdings:
        code = str(row.code)
        snapshots[code] = {
            "code": code, "name": row.name, "asset": row.market,
            "temperature_curr": row.temperature_status, "strength": row.strength,
            "strength_change": (row.raw_fields or {}).get("trend_strength_change_raw"),
            "right_side_days": row.right_side_days, "phase": row.jieqi,
            "raw": row.raw_fields or {},
        }
        memberships.add(("holding", code))
    for row in candidates:
        code = str(row["code"])
        asset_type = str(row.get("asset_type") or row.get("market") or "stock").lower()
        existing = snapshots.setdefault(code, {
            "code": code, "name": str(row.get("name") or code),
            "asset": "ETF基金" if "etf" in asset_type else "A股",
            "temperature_curr": _candidate_value(row, "temperature_curr", "temperature_status"),
            "temperature_prev": _candidate_value(row, "temperature_prev"),
            "strength": _candidate_value(row, "strength"),
            "strength_change": _candidate_value(
                row, "strength_change", "trend_strength_change_raw",
                "trendStrengthLocalChange",
            ),
            "right_side_days": _candidate_value(row, "right_side_days"),
            "phase": _candidate_value(row, "phase", "jieqi"), "raw": row,
        })
        membership_type = "warm_to_hot_etf" if "etf" in asset_type else "warm_to_hot_stock"
        memberships.add((membership_type, code))
        s.add(TushareDailyFact(
            dataset_id=dataset.dataset_id, ts_code=code, trade_date=trade_date,
            close=_candidate_value(row, "price", "close"),
            amount_yi=_candidate_value(row, "amount_yi", "turnover_yi"),
            float_market_cap_yi=_candidate_value(row, "float_market_cap_yi", "market_cap_yi"),
            fund_size_yi=_candidate_value(row, "aum_yi", "fund_size_yi"),
            source_dates={"ocr_fallback": trade_date}, raw_payload=row,
        ))
        existing["raw"] = {**(existing.get("raw") or {}), "candidate": row}
    for code, row in snapshots.items():
        raw = row.pop("raw")
        s.add(TrendDailySnapshot(
            dataset_id=dataset.dataset_id, tm_id=_ocr_tm_id(code),
            as_of_date=trade_date, source="ocr_fallback", payload_hash=_hash_payload(raw),
            raw_payload=raw, **row,
        ))
    for membership_type, code in memberships:
        s.add(TrendDailyMembership(
            dataset_id=dataset.dataset_id, membership_type=membership_type,
            tm_id=_ocr_tm_id(code), metadata_json={"batch_id": batch_id},
        ))
    dataset.status = "ready_degraded"; dataset.source_mode = "ocr_fallback"
    dataset.source_status = {
        "trend_animals": {"status": "fallback", "batch_id": batch_id,
                          "rows": len(snapshots)},
        "tushare": {"status": "fallback", "batch_id": batch_id,
                    "rows": len(candidates)},
    }
    dataset.source_dates = {"ocr_fallback": trade_date}
    dataset.capability_flags = {"volatility_supported": False, "ocr_fallback": True}
    dataset.error_code = None; dataset.error_message = None
    dataset.ready_at = datetime.utcnow(); dataset.updated_at = datetime.utcnow()
    dataset.next_retry_at = None; s.add(dataset); s.commit()
    dataset.dataset_hash = _dataset_hash(s, dataset.dataset_id); s.add(dataset); s.commit()
    from backend.discipline.dataset_plan import ensure_signal_plan
    plan = ensure_signal_plan(s, dataset.dataset_id)
    return {**serialize_dataset(s, dataset, cached=True), "fallback_preview": preview,
            "plan": plan}
