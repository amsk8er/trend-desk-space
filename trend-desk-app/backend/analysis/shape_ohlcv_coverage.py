"""Kova 形态研究的候选 × OHLCV 覆盖审计（只读、匿名聚合）。

该模块不计算 VCP / Pocket Pivot，不联网，也不写数据库。它只回答：
现有 A 股和美股历史候选，是否已经具备同口径、无前视的行情窗口。
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import sqlite3
from typing import Callable


CONTRACT = "kova_shape_ohlcv_coverage_v1"
REQUIRED_TABLES = {
    "dailydataset",
    "dailybar",
    "trenddailymembership",
    "trenddailysnapshot",
    "us_daily_run",
    "us_candidate_snapshot",
    "us_risk_anchor",
}


def _rows(conn: sqlite3.Connection, sql: str) -> list[dict]:
    return [dict(row) for row in conn.execute(sql).fetchall()]


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 1) if denominator else 0.0


def _required_tables(conn: sqlite3.Connection) -> None:
    actual = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing = sorted(REQUIRED_TABLES - actual)
    if missing:
        raise ValueError(f"coverage audit missing required tables: {', '.join(missing)}")


def _audit_a_share(conn: sqlite3.Connection) -> dict:
    dataset_status = _rows(conn, """
        SELECT status, COUNT(*) AS datasets,
               MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
        FROM dailydataset
        GROUP BY status
        ORDER BY status
    """)
    source_distribution = _rows(conn, """
        SELECT source, COUNT(*) AS bars, COUNT(DISTINCT ts_code) AS symbols
        FROM dailybar
        GROUP BY source
        ORDER BY source
    """)
    bar_inventory = dict(conn.execute("""
        SELECT COUNT(*) AS bars, COUNT(DISTINCT ts_code) AS symbols,
               MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
        FROM dailybar
    """).fetchone())
    observations = _rows(conn, """
        WITH candidate AS (
            SELECT d.trade_date AS as_of,
                   d.dataset_id,
                   m.tm_id,
                   m.membership_type,
                   s.code
            FROM trenddailymembership AS m
            JOIN dailydataset AS d ON d.dataset_id = m.dataset_id
            LEFT JOIN trenddailysnapshot AS s
              ON s.dataset_id = m.dataset_id AND s.tm_id = m.tm_id
            WHERE m.membership_type IN ('warm_to_hot_stock', 'warm_to_hot_etf')
        )
        SELECT c.as_of, c.membership_type, c.code,
               (SELECT COUNT(*) FROM dailybar AS b
                 WHERE b.ts_code = c.code
                   AND b.trade_date BETWEEN date(c.as_of, '-120 day') AND c.as_of
               ) AS bars_120d,
               (SELECT MAX(b.trade_date) FROM dailybar AS b
                 WHERE b.ts_code = c.code AND b.trade_date <= c.as_of
               ) AS latest_bar,
               (SELECT COUNT(DISTINCT b.source) FROM dailybar AS b
                 WHERE b.ts_code = c.code
                   AND b.trade_date BETWEEN date(c.as_of, '-120 day') AND c.as_of
               ) AS source_count
        FROM candidate AS c
        ORDER BY c.as_of, c.membership_type, c.tm_id
    """)

    per_date: dict[str, dict] = defaultdict(lambda: {
        "candidate_observations": 0,
        "with_code": 0,
        "any_ohlcv": 0,
        "window_60": 0,
        "exact_as_of_tail": 0,
        "mixed_source": 0,
        "complete": 0,
    })
    for row in observations:
        day = per_date[row["as_of"]]
        day["candidate_observations"] += 1
        day["with_code"] += int(row["code"] is not None)
        day["any_ohlcv"] += int(row["bars_120d"] > 0)
        day["window_60"] += int(row["bars_120d"] >= 60)
        day["exact_as_of_tail"] += int(row["latest_bar"] == row["as_of"])
        day["mixed_source"] += int(row["source_count"] > 1)
        day["complete"] += int(
            row["code"] is not None
            and row["bars_120d"] >= 60
            and row["latest_bar"] == row["as_of"]
            and row["source_count"] == 1
        )

    candidate_dates = [{"as_of": day, **per_date[day]} for day in sorted(per_date)]
    total = len(observations)
    complete = sum(row["complete"] for row in candidate_dates)
    pending = sum(row["datasets"] for row in dataset_status if row["status"] != "ready")
    blockers = []
    if pending:
        blockers.append("non_ready_daily_datasets_present")
    if total == 0:
        blockers.append("no_candidate_observations")
    if complete < total:
        blockers.append("candidate_ohlcv_coverage_incomplete")
    if any(row["mixed_source"] for row in candidate_dates):
        blockers.append("mixed_ohlcv_sources_in_window")

    return {
        "dataset_status": dataset_status,
        "bar_inventory": bar_inventory,
        "source_distribution": source_distribution,
        "candidate_dates": candidate_dates,
        "candidate_observations": total,
        "complete_observations": complete,
        "coverage_pct": _pct(complete, total),
        "ready_for_shadow_validation": not blockers,
        "blocking_reasons": blockers,
    }


def _audit_us(conn: sqlite3.Connection, path_exists: Callable[[str], bool]) -> dict:
    run_status = _rows(conn, """
        SELECT as_of_date AS as_of, rules_version, status,
               COUNT(*) AS runs
        FROM us_daily_run
        GROUP BY as_of_date, rules_version, status
        ORDER BY as_of_date, rules_version, status
    """)
    observations = _rows(conn, """
        SELECT r.as_of_date AS as_of,
               c.ticker_symbol,
               c.asset_type,
               CASE WHEN MAX(c.screen_status = 'ready') = 1
                    THEN 'ready' ELSE 'observe' END AS screen_status
        FROM us_candidate_snapshot AS c
        JOIN us_daily_run AS r ON r.run_id = c.run_id
        WHERE r.rules_version = 'us-manual-h6'
        GROUP BY r.as_of_date, c.ticker_symbol, c.asset_type
        ORDER BY r.as_of_date, c.asset_type, c.ticker_symbol
    """)
    anchor_rows = _rows(conn, """
        SELECT r.as_of_date AS as_of,
               c.ticker_symbol,
               c.asset_type,
               a.status,
               json_array_length(a.evidence_json, '$.aggregated_sessions') AS session_count,
               a.archive_path
        FROM us_risk_anchor AS a
        JOIN us_candidate_snapshot AS c ON c.candidate_id = a.candidate_id
        JOIN us_daily_run AS r ON r.run_id = c.run_id
        WHERE r.rules_version = 'us-manual-h6'
        ORDER BY r.as_of_date, c.asset_type, c.ticker_symbol, a.created_at
    """)

    anchor_by_observation: dict[tuple[str, str, str], dict] = defaultdict(lambda: {
        "has_anchor": False,
        "window_60": False,
        "archive_exists": False,
    })
    for row in anchor_rows:
        key = (row["as_of"], row["ticker_symbol"], row["asset_type"])
        state = anchor_by_observation[key]
        state["has_anchor"] = True
        state["window_60"] = state["window_60"] or (
            row["status"] == "ready" and (row["session_count"] or 0) >= 60
        )
        state["archive_exists"] = state["archive_exists"] or bool(
            row["archive_path"] and path_exists(row["archive_path"])
        )

    per_date: dict[str, dict] = defaultdict(lambda: {
        "candidate_observations": 0,
        "ready": 0,
        "observe": 0,
        "with_anchor": 0,
        "window_60": 0,
        "archive_exists": 0,
        "complete": 0,
    })
    for row in observations:
        day = per_date[row["as_of"]]
        day["candidate_observations"] += 1
        day[row["screen_status"]] += 1
        state = anchor_by_observation[(row["as_of"], row["ticker_symbol"], row["asset_type"])]
        day["with_anchor"] += int(state["has_anchor"])
        day["window_60"] += int(state["window_60"])
        day["archive_exists"] += int(state["archive_exists"])
        day["complete"] += int(state["window_60"] and state["archive_exists"])

    candidate_dates = [{"as_of": day, **per_date[day]} for day in sorted(per_date)]
    total = len(observations)
    complete = sum(row["complete"] for row in candidate_dates)
    blockers = []
    if total == 0:
        blockers.append("no_h6_candidate_observations")
    if complete < total:
        blockers.append("h6_candidate_ohlcv_coverage_incomplete")
    if any(row["observe"] > row["complete"] for row in candidate_dates):
        blockers.append("observe_candidates_not_archived")

    return {
        "run_status": run_status,
        "candidate_dates": candidate_dates,
        "candidate_observations": total,
        "complete_observations": complete,
        "coverage_pct": _pct(complete, total),
        "ready_for_shadow_validation": not blockers,
        "blocking_reasons": blockers,
        "source_notice": "Bitget rToken USDT proxy; not native US security OHLCV",
    }


def audit_connection(
    conn: sqlite3.Connection,
    *,
    path_exists: Callable[[str], bool] | None = None,
) -> dict:
    """审计一个已打开的 SQLite 连接；不执行任何写语句。"""
    conn.row_factory = sqlite3.Row
    _required_tables(conn)
    exists = path_exists or (lambda value: Path(value).is_file())
    a_share = _audit_a_share(conn)
    us = _audit_us(conn, exists)
    return {
        "contract": CONTRACT,
        "read_only": True,
        "a_share": a_share,
        "us": us,
        "ready_for_shape_shadow_validation": (
            a_share["ready_for_shadow_validation"]
            and us["ready_for_shadow_validation"]
        ),
    }


def audit_sqlite(path: Path) -> dict:
    """以 SQLite URI `mode=ro` 打开活动库并执行匿名覆盖审计。"""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"trend-desk database not found: {resolved}")
    uri = f"{resolved.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        return audit_connection(conn)
