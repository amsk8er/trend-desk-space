"""Kova 形态研究 OHLCV 归档的零网络样本总体与请求预算。

模块只读 SQLite 和既有不可变研究档案。它不调用行情 provider、不写活动库，
也不计算任何形态标签或交易动作。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Callable

import exchange_calendars as xcals
import pandas as pd

from backend.analysis.shape_ohlcv_archive import verified_observation_exists


CONTRACT = "kova_shape_ohlcv_archive_dry_run_v1"
ARCHIVE_CONTRACT = "kova_shape_ohlcv_archive_v1"
A_SHARE_COHORT = "a_share_tushare_raw_adj_v1"
US_H6_COHORT = "us_h6_bitget_rtoken_regular_session_v1"
READY_DATASET_STATUSES = {"ready", "ready_degraded"}
REQUIRED_TABLES = {
    "dailybar",
    "dailydataset",
    "trenddailymembership",
    "trenddailysnapshot",
    "us_daily_run",
    "us_candidate_snapshot",
    "us_risk_anchor",
}
XNYS = xcals.get_calendar("XNYS")

# 与 backend.us_manual.bitget_public.fetch_public_candles 的分段边界一致。
BITGET_DAILY_SEGMENT = timedelta(days=89)
BITGET_HOURLY_SEGMENT = timedelta(hours=95)
BITGET_MAX_PAGES_PER_INTERVAL = 64


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _hash(value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _rows(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql).fetchall()]


def _required_tables(conn: sqlite3.Connection) -> None:
    actual = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing = sorted(REQUIRED_TABLES - actual)
    if missing:
        raise ValueError(f"archive dry-run missing required tables: {', '.join(missing)}")


def _observation_hash(*, market: str, as_of: str, identity: dict[str, Any]) -> str:
    return _hash({
        "contract": ARCHIVE_CONTRACT,
        "market": market,
        "as_of": as_of,
        "observation_identity": identity,
    }).removeprefix("sha256:")


def _segments(start: datetime, end: datetime, span: timedelta) -> int:
    return math.ceil((end - start).total_seconds() / span.total_seconds())


def _bitget_request_count(as_of: str) -> dict[str, int]:
    signal = pd.Timestamp(as_of)
    session = signal if XNYS.is_session(signal) else XNYS.date_to_session(signal, direction="previous")
    start = datetime.combine(
        date.fromisoformat(as_of) - timedelta(days=120),
        time.min,
        tzinfo=timezone.utc,
    )
    end = XNYS.session_close(session).to_pydatetime()
    daily = _segments(start, end, BITGET_DAILY_SEGMENT)
    hourly = _segments(start, end, BITGET_HOURLY_SEGMENT)
    return {"daily_1d": daily, "hourly_1h": hourly, "total": daily + hourly}


def _a_share_plan(
    conn: sqlite3.Connection,
    *,
    archive_complete: Callable[[str, str, str], bool],
) -> dict[str, Any]:
    dataset_status = _rows(conn, """
        SELECT status, COUNT(*) AS datasets
        FROM dailydataset
        GROUP BY status
        ORDER BY status
    """)
    rows = _rows(conn, """
        SELECT d.dataset_id, d.trade_date AS as_of, d.status AS dataset_status,
               d.dataset_hash, m.tm_id, m.membership_type, s.code, s.asset,
               s.as_of_date AS snapshot_as_of, s.payload_hash
        FROM trenddailymembership AS m
        JOIN dailydataset AS d ON d.dataset_id = m.dataset_id
        LEFT JOIN trenddailysnapshot AS s
          ON s.dataset_id = m.dataset_id AND s.tm_id = m.tm_id
        WHERE d.status IN ('ready', 'ready_degraded')
          AND m.membership_type IN ('warm_to_hot_stock', 'warm_to_hot_etf')
        ORDER BY d.trade_date, d.dataset_id, m.membership_type, m.tm_id
    """)
    local_adapter_keys = {
        (row["as_of"], row["code"])
        for row in _rows(conn, """
            WITH candidate AS (
                SELECT d.trade_date AS as_of, s.code
                FROM trenddailymembership AS m
                JOIN dailydataset AS d ON d.dataset_id = m.dataset_id
                LEFT JOIN trenddailysnapshot AS s
                  ON s.dataset_id = m.dataset_id AND s.tm_id = m.tm_id
                WHERE d.status IN ('ready', 'ready_degraded')
                  AND m.membership_type IN ('warm_to_hot_stock', 'warm_to_hot_etf')
            )
            SELECT c.as_of, c.code
            FROM candidate AS c
            JOIN dailybar AS b
              ON b.ts_code = c.code
             AND b.trade_date BETWEEN date(c.as_of, '-120 day') AND c.as_of
            GROUP BY c.as_of, c.code
            HAVING COUNT(*) >= 60
               AND MAX(b.trade_date) = c.as_of
               AND COUNT(DISTINCT b.source) = 1
               AND MIN(CASE WHEN b.source = 'tushare'
                             AND b.adj_factor IS NOT NULL
                             AND b.adj_factor > 0
                        THEN 1 ELSE 0 END) = 1
        """)
    }

    frame: list[dict[str, Any]] = []
    invalid = Counter()
    reusable = 0
    unique_instruments: set[str] = set()
    network_queries: set[tuple[str, str, str]] = set()
    request_endpoints = Counter()
    membership_counts: Counter[str] = Counter()
    included_datasets: set[str] = set()
    for row in rows:
        membership_counts[row["membership_type"]] += 1
        included_datasets.add(row["dataset_id"])
        code = str(row["code"] or "").strip().upper()
        reason: str | None = None
        if not code:
            reason = "candidate_code_missing"
        elif row["snapshot_as_of"] != row["as_of"]:
            reason = "candidate_as_of_mismatch"
        elif not row["payload_hash"]:
            reason = "candidate_snapshot_hash_missing"
        if reason:
            invalid[reason] += 1

        identity = {
            "dataset_id": row["dataset_id"],
            "tm_id": row["tm_id"],
            "membership_type": row["membership_type"],
        }
        observation_hash = _observation_hash(
            market="a_share",
            as_of=row["as_of"],
            identity=identity,
        )
        complete = reason is None and archive_complete("a_share", row["as_of"], observation_hash)
        reusable += int(complete)
        asset_request = (
            "fund_daily+fund_adj"
            if row["membership_type"] == "warm_to_hot_etf"
            else "daily+adj_factor"
        )
        if code:
            unique_instruments.add(code)
        if reason is None and not complete:
            # Query boundaries are observation-date-specific. The same code on
            # two as-of dates is intentionally two no-future query units.
            network_queries.add((row["as_of"], code, asset_request))
        frame.append({
            "as_of": row["as_of"],
            "identity": identity,
            "candidate_snapshot_hash": row["payload_hash"],
            "dataset_hash": row["dataset_hash"],
            "code": code,
            "asset_request": asset_request,
            "preflight_failure": reason,
        })

    requests = len(network_queries) * 2
    for _as_of, _code, asset_request in network_queries:
        for endpoint in asset_request.split("+"):
            request_endpoints[endpoint] += 1
    excluded = sum(
        row["datasets"]
        for row in dataset_status
        if row["status"] not in READY_DATASET_STATUSES
    )
    return {
        "market": "a_share",
        "source_cohort": A_SHARE_COHORT,
        "sample_frame_hash": _hash(frame),
        "observation_count": len(frame),
        "included_dataset_count": len(included_datasets),
        "excluded_non_ready_dataset_count": excluded,
        "membership_counts": dict(sorted(membership_counts.items())),
        "unique_instrument_count": len(unique_instruments),
        "reusable_archive_count": reusable,
        "local_dailybar_adapter_candidates": len(local_adapter_keys),
        "preflight_failure_count": sum(invalid.values()),
        "preflight_failure_reasons": dict(sorted(invalid.items())),
        "network_required_observation_count": len(frame) - reusable - sum(invalid.values()),
        "unique_query_count": len(network_queries),
        "estimated_requests": {
            "minimum": requests,
            "failure_closed_maximum": requests,
            "by_endpoint": dict(sorted(request_endpoints.items())),
            "model": "two as-of-bounded Tushare calls per unique observation query; no retries",
        },
        "credential_class": "local_config_required",
        "network_enabled": False,
    }


def _us_h6_plan(
    conn: sqlite3.Connection,
    *,
    archive_complete: Callable[[str, str, str], bool],
) -> dict[str, Any]:
    occurrences = _rows(conn, """
        SELECT r.as_of_date AS as_of, r.run_id, r.status AS run_status,
               c.candidate_id, c.ticker_symbol, c.asset_type,
               c.venue_instrument, c.screen_status, c.warm_to_hot,
               c.gate_passed, c.benchmark_status, c.benchmark_family_id,
               c.exposure_key
        FROM us_candidate_snapshot AS c
        JOIN us_daily_run AS r ON r.run_id = c.run_id
        WHERE r.rules_version = 'us-manual-h6'
          AND r.status IN ('ready', 'ready_degraded', 'ready_cached')
        ORDER BY r.as_of_date, c.ticker_symbol, c.asset_type, r.run_id, c.candidate_id
    """)
    excluded_non_ready_runs = conn.execute("""
        SELECT COUNT(*)
        FROM us_daily_run
        WHERE rules_version = 'us-manual-h6'
          AND status NOT IN ('ready', 'ready_degraded', 'ready_cached')
    """).fetchone()[0]
    legacy_anchor_rows = _rows(conn, """
            SELECT r.as_of_date AS as_of, c.ticker_symbol, c.asset_type
                 , a.archive_path, a.evidence_json
            FROM us_risk_anchor AS a
            JOIN us_candidate_snapshot AS c ON c.candidate_id = a.candidate_id
            JOIN us_daily_run AS r ON r.run_id = c.run_id
            WHERE r.rules_version = 'us-manual-h6'
              AND r.status IN ('ready', 'ready_degraded', 'ready_cached')
              AND a.status = 'ready'
              AND json_array_length(a.evidence_json, '$.aggregated_sessions') >= 60
              AND a.archive_path IS NOT NULL
            ORDER BY r.as_of_date, c.ticker_symbol, c.asset_type, a.archive_path
        """)
    legacy_anchor_keys: set[tuple[str, str, str]] = set()
    legacy_strict_ready: set[tuple[str, str, str]] = set()
    legacy_failure_reasons = Counter()
    for row in legacy_anchor_rows:
        key = (row["as_of"], row["ticker_symbol"], row["asset_type"])
        legacy_anchor_keys.add(key)
        path = Path(str(row["archive_path"] or ""))
        try:
            evidence = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        except (OSError, ValueError):
            evidence = None
        if not isinstance(evidence, dict):
            legacy_failure_reasons["archive_missing_or_invalid"] += 1
        elif not isinstance(evidence.get("daily"), list):
            legacy_failure_reasons["raw_daily_1d_missing"] += 1
        elif not isinstance(evidence.get("hourly"), list):
            legacy_failure_reasons["raw_hourly_1h_missing"] += 1
        else:
            legacy_strict_ready.add(key)

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in occurrences:
        key = (row["as_of"], row["ticker_symbol"], row["asset_type"])
        grouped[key].append(row)

    frame: list[dict[str, Any]] = []
    invalid = Counter()
    screen_counts: Counter[str] = Counter()
    reusable = 0
    network_requests = 0
    maximum_requests = 0
    network_queries = 0
    request_breakdown = Counter()
    unique_instruments: set[str] = set()
    for key in sorted(grouped):
        as_of, ticker, asset_type = key
        rows = grouped[key]
        venues = {str(row["venue_instrument"] or "").strip().upper() for row in rows}
        critical = {
            (
                str(row["venue_instrument"] or "").strip().upper(),
                row["warm_to_hot"],
                row["gate_passed"],
                row["benchmark_status"],
                row["benchmark_family_id"],
                row["exposure_key"],
            )
            for row in rows
        }
        reason: str | None = None
        if len(critical) > 1:
            reason = "candidate_snapshot_conflict"
        elif venues == {""}:
            reason = "venue_instrument_missing"
        elif len(venues) != 1:
            reason = "venue_instrument_conflict"
        if reason:
            invalid[reason] += 1

        screen_status = "ready" if any(row["screen_status"] == "ready" for row in rows) else "observe"
        screen_counts[screen_status] += 1
        identity = {"ticker_symbol": ticker, "asset_type": asset_type}
        observation_hash = _observation_hash(
            market="us_h6",
            as_of=as_of,
            identity=identity,
        )
        complete = reason is None and archive_complete("us_h6", as_of, observation_hash)
        reusable += int(complete)
        venue = next(iter(venues)) if len(venues) == 1 else ""
        if venue:
            unique_instruments.add(venue)
        if reason is None and not complete:
            planned = _bitget_request_count(as_of)
            network_queries += 1
            network_requests += planned["total"]
            maximum_requests += BITGET_MAX_PAGES_PER_INTERVAL * 2
            request_breakdown["daily_1d"] += planned["daily_1d"]
            request_breakdown["hourly_1h"] += planned["hourly_1h"]
        frame.append({
            "as_of": as_of,
            "identity": identity,
            "venue_instrument": venue,
            "screen_status": screen_status,
            "critical_snapshot": sorted(critical, key=str),
            "occurrence_refs": sorted(
                (row["run_id"], row["candidate_id"]) for row in rows
            ),
            "preflight_failure": reason,
        })

    return {
        "market": "us_h6",
        "source_cohort": US_H6_COHORT,
        "source_notice": "Bitget rToken USDT proxy; not native US security OHLCV",
        "sample_frame_hash": _hash(frame),
        "observation_count": len(frame),
        "raw_occurrence_count": len(occurrences),
        "deduplicated_occurrence_count": len(occurrences) - len(frame),
        "excluded_non_ready_run_count": excluded_non_ready_runs,
        "screen_status_counts": dict(sorted(screen_counts.items())),
        "unique_instrument_count": len(unique_instruments),
        "reusable_archive_count": reusable,
        "legacy_risk_anchor_observations": len(legacy_anchor_keys),
        "legacy_strict_adapter_ready_count": len(legacy_strict_ready),
        "legacy_strict_adapter_failure_reasons": dict(sorted(legacy_failure_reasons.items())),
        "preflight_failure_count": sum(invalid.values()),
        "preflight_failure_reasons": dict(sorted(invalid.items())),
        "network_required_observation_count": len(frame) - reusable - sum(invalid.values()),
        "unique_query_count": network_queries,
        "estimated_requests": {
            "minimum": network_requests,
            "failure_closed_maximum": maximum_requests,
            "by_interval": dict(sorted(request_breakdown.items())),
            "model": (
                "minimum is one request per 89-day 1D or 95-hour 1H segment; "
                "maximum is the provider adapter's 64-page cap per interval; no retries"
            ),
        },
        "credential_class": "none",
        "network_enabled": False,
    }


def plan_connection(
    conn: sqlite3.Connection,
    *,
    archive_root: Path,
    archive_complete: Callable[[str, str, str], bool] | None = None,
) -> dict[str, Any]:
    """从已打开的 SQLite 连接生成匿名 dry-run；不执行任何写语句。"""
    conn.row_factory = sqlite3.Row
    _required_tables(conn)
    if archive_complete is None:
        check = lambda market, as_of, digest: verified_observation_exists(  # noqa: E731
            archive_root,
            market=market,
            as_of=as_of,
            observation_hash=digest,
        )
    else:
        check = archive_complete
    a_share = _a_share_plan(conn, archive_complete=check)
    us_h6 = _us_h6_plan(conn, archive_complete=check)
    blockers = []
    if a_share["preflight_failure_count"]:
        blockers.append("a_share_preflight_failures")
    if us_h6["preflight_failure_count"]:
        blockers.append("us_h6_preflight_failures")
    if not a_share["observation_count"]:
        blockers.append("a_share_sample_frame_empty")
    if not us_h6["observation_count"]:
        blockers.append("us_h6_sample_frame_empty")
    return {
        "contract": CONTRACT,
        "mode": "dry_run",
        "read_only": True,
        "network_calls": 0,
        "activity_db_writes": 0,
        "archive_root": str(archive_root.expanduser().resolve()),
        "sample_frame_hash": _hash({
            "a_share": a_share["sample_frame_hash"],
            "us_h6": us_h6["sample_frame_hash"],
        }),
        "markets": {"a_share": a_share, "us_h6": us_h6},
        "clean_for_budget_review": not blockers,
        "collection_authorized": False,
        "review_requirements": [
            "confirm Tushare stock and fund endpoint capability and points",
            "approve source cohort, sample-frame hash, request ceiling, and output root",
            "approve an opaque-hash small batch that covers each source route and H6 screen status",
        ],
        "blocking_reasons": blockers,
    }


def plan_sqlite(path: Path, *, archive_root: Path) -> dict[str, Any]:
    """以 SQLite URI ``mode=ro`` 打开活动库并生成 dry-run。"""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"trend-desk database not found: {resolved}")
    uri = f"{resolved.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        return plan_connection(conn, archive_root=archive_root)
