"""Lightweight, idempotent schema migrations for the SQLite DB.

`SQLModel.metadata.create_all` only creates MISSING TABLES — it never adds new
columns to a table that already exists. So when we add a column to a model
(e.g. OcrRow.strength), the live data/trend-desk.db needs an explicit
`ALTER TABLE ... ADD COLUMN`. This module does that, safely re-runnable.

Call `ensure_columns(engine)` right after `SQLModel.metadata.create_all(engine)`
wherever the real DB is opened (app lifespan, run_full).
"""
from sqlalchemy import text
from sqlmodel import Session

# table -> {column: "SQL type"} we expect to exist. Add entries as the schema grows.
_EXPECTED = {
    "ocrrow": {"strength": "INTEGER", "sector": "VARCHAR",
               "right_side_gain_pct": "FLOAT", "jieqi": "VARCHAR"},
    "ocrjob": {"category": "VARCHAR", "backend": "VARCHAR"},
    "exitlistitem": {"detail": "JSON"},
    # 持仓温度权威源（D16+）：code_source 标记真实代码来自趋势动物持仓页回填。
    "position": {"code_source": "VARCHAR", "confirmed": "BOOLEAN", "confirmed_at": "DATETIME"},
    # 趋势动物 API 主通道：历史 OCR 行保持 tm_id/date 为空，data_source 回填 ocr。
    "holdingtemp": {"tm_id": "INTEGER", "as_of_date": "VARCHAR",
                    "update_dt": "VARCHAR", "data_source": "VARCHAR"},
    "batch": {"source_dir": "VARCHAR"},
    # swing 缓存：区分 tushare(原始价+真因子) / eastmoney(qfq 当 raw、factor=1)。
    "dailybar": {"source": "VARCHAR"},
    # 趋势研判缓存按后端隔离（claude_cli/codex_cli/anthropic_api）— 同事实不同后端分别缓存。
    "trendbrief": {"backend": "VARCHAR"},
    "tradeplan": {"selection_snapshot": "JSON", "dataset_id": "VARCHAR",
                  "portfolio_snapshot_id": "INTEGER", "plan_stage": "VARCHAR",
                  "input_hash": "VARCHAR", "supersedes_plan_id": "VARCHAR",
                  "change_notice": "VARCHAR"},
    "portfoliosnapshot": {
        "prior_snapshot_id": "INTEGER", "price_date": "VARCHAR",
        "reconciliation_status": "VARCHAR", "derivation": "JSON",
    },
    "brokerimport": {"batch_id": "VARCHAR"},
    "execution": {
        "fingerprint": "VARCHAR", "gross_amount": "FLOAT", "net_amount": "FLOAT",
        "fee_source": "VARCHAR", "confirmed": "BOOLEAN",
    },
    "ledgeradjustment": {"applied_at": "DATETIME"},
    # A 股/基金与 ETF/LOF 的券商佣金分开保存；NULL 的历史配置会在读时
    # 兼容回落到 A 股佣金，避免旧本地库升级后误算为零佣金。
    "feeschedule": {
        "etf_commission_rate": "FLOAT",
        "etf_minimum_commission": "FLOAT",
    },
    # 纪律计划新增离场信号归属；旧流水线历史行无法可靠回填 plan_id，
    # 因此迁移列允许 NULL，新纪律计划写入真实 plan_id。
    "dailyexitsignal": {"plan_id": "VARCHAR"},
    "trendapisync": {"dataset_id": "VARCHAR", "trigger": "VARCHAR",
                     "attempt_no": "INTEGER", "scheduled_for": "DATETIME",
                     "next_retry_at": "DATETIME"},
    # 强度周变化只作为候选与持仓观察项，不参与硬筛选、排序或离场。
    "trenddailysnapshot": {"strength_change": "VARCHAR"},
}


def _existing_columns(s: Session, table: str) -> set[str]:
    rows = s.exec(text(f"PRAGMA table_info({table})")).all()  # type: ignore[call-arg]
    return {r[1] for r in rows}  # r = (cid, name, type, notnull, dflt, pk)


def _col_is_not_null(s: Session, table: str, col: str) -> bool:
    rows = s.exec(text(f"PRAGMA table_info({table})")).all()  # type: ignore[call-arg]
    for r in rows:  # r = (cid, name, type, notnull, dflt, pk)
        if r[1] == col:
            return bool(r[3])
    return False


def _drop_position_code_not_null(s: Session) -> bool:
    """旧库 position.code 是 NOT NULL；券商无代码持仓需写 code=NULL（D16+）。

    SQLite 不能 ALTER COLUMN 去约束 → 重建表：建新表（code 可空）、拷数据、换名。
    幂等：仅当 code 仍带 NOT NULL 时执行。返回是否重建。
    """
    if not _existing_columns(s, "position"):
        return False  # 表不存在（全新库由 create_all 已建可空 code）
    if not _col_is_not_null(s, "position", "code"):
        return False
    s.exec(text("ALTER TABLE position RENAME TO position_old"))  # type: ignore[call-arg]
    s.exec(text(  # type: ignore[call-arg]
        "CREATE TABLE position ("
        "position_id INTEGER PRIMARY KEY, batch_id VARCHAR NOT NULL, "
        "code VARCHAR, name VARCHAR NOT NULL, shares INTEGER NOT NULL, "
        "avg_cost FLOAT NOT NULL, current_price FLOAT NOT NULL, pnl_pct FLOAT NOT NULL, "
        "stop_loss FLOAT, entered_date VARCHAR, source_image VARCHAR, code_source VARCHAR)"))
    old_cols = _existing_columns(s, "position_old")
    shared = [c for c in ("position_id", "batch_id", "code", "name", "shares",
                          "avg_cost", "current_price", "pnl_pct", "stop_loss",
                          "entered_date", "source_image", "code_source", "confirmed", "confirmed_at") if c in old_cols]
    collist = ", ".join(shared)
    s.exec(text(  # type: ignore[call-arg]
        f"INSERT INTO position ({collist}) SELECT {collist} FROM position_old"))
    s.exec(text("DROP TABLE position_old"))  # type: ignore[call-arg]
    return True


def _unique_index_columns(s: Session, table: str) -> set[tuple[str, ...]]:
    indexes = s.exec(text(f"PRAGMA index_list({table})")).all()  # type: ignore[call-arg]
    out: set[tuple[str, ...]] = set()
    for row in indexes:  # (seq, name, unique, origin, partial)
        if not bool(row[2]):
            continue
        columns = s.exec(text(f"PRAGMA index_info('{row[1]}')")).all()  # type: ignore[call-arg]
        out.add(tuple(str(column[2]) for column in columns))
    return out


def _rebuild_daily_exit_signal_unique(s: Session) -> bool:
    """把旧的“品种+日期唯一”升级为“计划+品种+日期唯一”。"""
    columns = _existing_columns(s, "dailyexitsignal")
    if not columns or "plan_id" not in columns:
        return False
    wanted = ("plan_id", "instrument_id", "signal_date")
    if wanted in _unique_index_columns(s, "dailyexitsignal"):
        return False
    s.exec(text("ALTER TABLE dailyexitsignal RENAME TO dailyexitsignal_old"))  # type: ignore[call-arg]
    s.exec(text(  # type: ignore[call-arg]
        "CREATE TABLE dailyexitsignal ("
        "signal_id INTEGER NOT NULL PRIMARY KEY, plan_id VARCHAR, "
        "instrument_id VARCHAR NOT NULL, signal_date VARCHAR NOT NULL, "
        "execute_date VARCHAR NOT NULL, danger BOOLEAN NOT NULL, "
        "temp_flat_or_below BOOLEAN NOT NULL, champagne BOOLEAN, boiling BOOLEAN, "
        "volatility_up BOOLEAN, profit_signal_count INTEGER NOT NULL, "
        "planned_reduce_fraction FLOAT NOT NULL, consecutive_days_by_signal JSON, "
        "action_generated VARCHAR NOT NULL, target_shares INTEGER NOT NULL, "
        "valid_until VARCHAR NOT NULL, action_execution_id INTEGER, violation VARCHAR, "
        "UNIQUE (plan_id, instrument_id, signal_date), "
        "FOREIGN KEY(plan_id) REFERENCES tradeplan (plan_id), "
        "FOREIGN KEY(action_execution_id) REFERENCES execution (execution_id))"))
    ordered = (
        "signal_id", "plan_id", "instrument_id", "signal_date", "execute_date",
        "danger", "temp_flat_or_below", "champagne", "boiling", "volatility_up",
        "profit_signal_count", "planned_reduce_fraction", "consecutive_days_by_signal",
        "action_generated", "target_shares", "valid_until", "action_execution_id",
        "violation",
    )
    shared = [column for column in ordered if column in columns]
    column_list = ", ".join(shared)
    s.exec(text(  # type: ignore[call-arg]
        f"INSERT INTO dailyexitsignal ({column_list}) "
        f"SELECT {column_list} FROM dailyexitsignal_old"))
    s.exec(text("DROP TABLE dailyexitsignal_old"))  # type: ignore[call-arg]
    s.exec(text(  # type: ignore[call-arg]
        "CREATE INDEX ix_dailyexitsignal_plan_id ON dailyexitsignal (plan_id)"))
    s.exec(text(  # type: ignore[call-arg]
        "CREATE INDEX ix_dailyexitsignal_instrument_id ON dailyexitsignal (instrument_id)"))
    s.exec(text(  # type: ignore[call-arg]
        "CREATE INDEX ix_dailyexitsignal_signal_date ON dailyexitsignal (signal_date)"))
    return True


def ensure_columns(engine) -> list[str]:
    """Add any missing columns; return the list of columns added (for logging)."""
    added: list[str] = []
    with Session(engine) as s:
        # 先处理 position.code 旧 NOT NULL → 可空（重建时一并带出 code_source 列）。
        if _drop_position_code_not_null(s):
            added.append("position.code(nullable rebuild)")
        for table, cols in _EXPECTED.items():
            have = _existing_columns(s, table)
            if not have:
                continue  # table absent (e.g. create_all hasn't run yet) → nothing to alter
            for col, sqltype in cols.items():
                if col not in have:
                    s.exec(text(f"ALTER TABLE {table} ADD COLUMN {col} {sqltype}"))  # type: ignore[call-arg]
                    added.append(f"{table}.{col}")
        if _rebuild_daily_exit_signal_unique(s):
            added.append("dailyexitsignal.unique(plan_id,instrument_id,signal_date) rebuild")
        if _existing_columns(s, "execution"):
            s.exec(text(  # type: ignore[call-arg]
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_execution_fingerprint "
                "ON execution (fingerprint) WHERE fingerprint IS NOT NULL"))
        # backfill strength from the legacy temperature mirror (they were identical:
        # OCR copied the 强度 number into both). Only where strength is still null.
        if "ocrrow.strength" in added:
            s.exec(text(  # type: ignore[call-arg]
                "UPDATE ocrrow SET strength = temperature "
                "WHERE strength IS NULL AND temperature IS NOT NULL"))
        # 旧 dailybar 行（加列前写入的，都是 tushare 主源）source 默认补 tushare。
        if "dailybar.source" in added:
            s.exec(text(  # type: ignore[call-arg]
                "UPDATE dailybar SET source = 'tushare' WHERE source IS NULL"))
        if "holdingtemp.data_source" in added:
            s.exec(text(  # type: ignore[call-arg]
                "UPDATE holdingtemp SET data_source = 'ocr' WHERE data_source IS NULL"))
        if "position.confirmed" in added:
            s.exec(text(  # type: ignore[call-arg]
                "UPDATE position SET confirmed = 0 WHERE confirmed IS NULL"))
        if "tradeplan.selection_snapshot" in added:
            s.exec(text(  # type: ignore[call-arg]
                "UPDATE tradeplan SET selection_snapshot = '{}' WHERE selection_snapshot IS NULL"))
        if "tradeplan.plan_stage" in added:
            s.exec(text(  # type: ignore[call-arg]
                "UPDATE tradeplan SET plan_stage = 'executable' WHERE plan_stage IS NULL"))
        if "portfoliosnapshot.reconciliation_status" in added:
            s.exec(text(  # type: ignore[call-arg]
                "UPDATE portfoliosnapshot SET reconciliation_status = "
                "CASE WHEN confirmed = 1 THEN 'confirmed' ELSE 'unconfirmed' END "
                "WHERE reconciliation_status IS NULL"))
        if "portfoliosnapshot.derivation" in added:
            s.exec(text(  # type: ignore[call-arg]
                "UPDATE portfoliosnapshot SET derivation = '{}' WHERE derivation IS NULL"))
        if "execution.fee_source" in added:
            s.exec(text(  # type: ignore[call-arg]
                "UPDATE execution SET fee_source = 'actual' WHERE fee_source IS NULL"))
        if "execution.confirmed" in added:
            s.exec(text(  # type: ignore[call-arg]
                "UPDATE execution SET confirmed = 1 WHERE confirmed IS NULL"))
        if "trendapisync.attempt_no" in added:
            s.exec(text(  # type: ignore[call-arg]
                "UPDATE trendapisync SET attempt_no = 0 WHERE attempt_no IS NULL"))
        if "trenddailysnapshot.strength_change" in added:
            # 该字段此前已包含在 raw_payload 中，升级时直接无损回填历史快照。
            s.exec(text(  # type: ignore[call-arg]
                "UPDATE trenddailysnapshot "
                "SET strength_change = json_extract(raw_payload, '$.trendStrengthLocalChange') "
                "WHERE strength_change IS NULL AND raw_payload IS NOT NULL"))
        s.commit()
    return added
