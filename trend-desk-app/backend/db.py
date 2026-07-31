from datetime import datetime
from decimal import Decimal
from typing import Optional
from sqlalchemy import Numeric
from sqlmodel import SQLModel, Field, Column, JSON, UniqueConstraint

class Batch(SQLModel, table=True):
    batch_id: str = Field(primary_key=True)
    date: str
    status: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    pipeline_state: dict = Field(default_factory=dict, sa_column=Column(JSON))
    source_dir: Optional[str] = None   # 导入源目录（iCloud inbox），供 OCR 后归档原图

class OcrJob(SQLModel, table=True):
    job_id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: str = Field(foreign_key="batch.batch_id", index=True)
    image_path: str
    image_index: int = 0
    model: str = "claude-sonnet-4-6"
    # LLM backend that actually served this job (claude_cli/anthropic_api/codex_cli).
    # With FallbackClient this is the backend that SUCCEEDED, not the one requested.
    # NB: `model` is only meaningful for anthropic backends; codex 的模型由 CODEX_MODEL 决定。
    backend: Optional[str] = None
    status: str = "todo"
    category: Optional[str] = None       # 聚合类别（面包屑中段：A股/A股组合/ETF基金…）
    partial_reason: Optional[str] = None
    raw_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    elapsed_ms: int = 0
    attempts: int = 0

class OcrRow(SQLModel, table=True):
    row_id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="ocrjob.job_id", index=True)
    row_type: str
    market: str
    code: Optional[str] = None
    name: Optional[str] = None
    sector: Optional[str] = None        # 所属板块（板块页=页板块；分组清单页=所在分组）
    # `temperature` (0-100 int) is a LEGACY/phantom field: the App's 温度 is a status
    # word (寒/凉/平/温/热/沸), not a number. OCR used to mirror the 强度 number into it.
    # Kept nullable for back-compat; new ingest leaves it None. Heat = temperature_status,
    # the 0-100 number = strength. See docs/architecture.md D15.
    temperature: Optional[int] = None
    temperature_status: Optional[str] = None
    strength: Optional[int] = None      # 趋势相对强度 0-100（真正的那个数字，原先被错填进 temperature）
    right_side_days: Optional[int] = None
    right_side_gain_pct: Optional[float] = None  # 右侧涨幅%（主筛选 M5 唯一依据）
    jieqi: Optional[str] = None                  # 节气标签（M6 与止盈「大暑后」依据）
    first_hot_date: Optional[str] = None
    last_cool_date: Optional[str] = None
    raw_fields: dict = Field(default_factory=dict, sa_column=Column(JSON))
    review_status: str = "pending"
    review_reason: Optional[str] = None
    reviewed_at: Optional[datetime] = None

class Position(SQLModel, table=True):
    position_id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: str = Field(foreign_key="batch.batch_id", index=True)
    # 券商持仓截图没有股票代码（只有名称+市场标记），OCR 禁止脑补 → code 可为 None。
    # 真实代码由趋势动物「持仓」温度页（HoldingTemp）按名称关联后回填（带 .SH/.SZ/.OF 后缀）。
    code: Optional[str] = None
    name: str
    shares: int
    avg_cost: float
    current_price: float
    pnl_pct: float
    stop_loss: Optional[float] = None
    entered_date: Optional[str] = None
    source_image: Optional[str] = None
    # 回填来源标记：None=未回填 / "holding_temp"=趋势动物持仓页关联到的真实代码。
    code_source: Optional[str] = None
    confirmed: bool = False
    confirmed_at: Optional[datetime] = None


class HoldingTemp(SQLModel, table=True):
    """趋势动物 App「收藏夹 > 持仓」分组温度页解析行。

    作为持仓标的的**温度 + 真实代码权威来源**：覆盖 ETF/LOF 基金温度（主截图
    「A股个股」页匹配不上的那批），code 带 .SH/.SZ/.OF 后缀。出局检查（⑧）按
    归一代码或名称匹配持仓，回填 Position.code、提供温度/右侧天数/强度/tags。
    """
    holding_id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: str = Field(foreign_key="batch.batch_id", index=True)
    tm_id: Optional[int] = Field(default=None, index=True)  # 趋势动物稳定品种 ID；OCR 历史行为 null
    code: Optional[str] = None              # 带交易所后缀，如 168401.SZ / 159507.OF
    name: str
    market: Optional[str] = None            # 页内分组：A股个股 / ETF基金
    temperature_status: Optional[str] = None
    strength: Optional[int] = None
    right_side_days: Optional[int] = None
    right_side_gain_pct: Optional[float] = None
    jieqi: Optional[str] = None
    sector: Optional[str] = None
    raw_fields: dict = Field(default_factory=dict, sa_column=Column(JSON))
    source_image: Optional[str] = None
    as_of_date: Optional[str] = None
    update_dt: Optional[str] = None
    data_source: str = "ocr"               # ocr | trend_api

class Manifest(SQLModel, table=True):
    manifest_id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: str = Field(foreign_key="batch.batch_id", index=True)
    stage: str
    manifest_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    white_list: list = Field(default_factory=list, sa_column=Column(JSON))
    rejected: list = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)

class ExitListItem(SQLModel, table=True):
    exit_id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: str = Field(foreign_key="batch.batch_id", index=True)
    position_id: int = Field(foreign_key="position.position_id")
    trigger: str
    action: str
    reason: str
    detail: dict = Field(default_factory=dict, sa_column=Column(JSON))  # 提醒要素（温度/盈亏/板块等）

class ChatMessage(SQLModel, table=True):
    msg_id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: str = Field(foreign_key="batch.batch_id", index=True)
    role: str
    content: str = ""
    tool_name: Optional[str] = None
    tool_args: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)

class DailyBar(SQLModel, table=True):
    """swing 取数层本地缓存：存**不复权原始价 + adj_factor**，读时合成前复权。

    根治前复权一致性——复权价随除权事件变动，缓存原始价+因子才不会随窗口漂移。
    tushare daily(原始 OHLCV) + adj_factor(绝对因子) 按 (ts_code,trade_date) 合并落库。
    东财兜底返回的是已复权 qfq、无因子，不进此表（降级、不缓存）。
    """
    __table_args__ = (UniqueConstraint("ts_code", "trade_date"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    ts_code: str = Field(index=True)        # "600519.SH"
    trade_date: str = Field(index=True)     # "YYYY-MM-DD"
    open: float                              # 不复权原始价
    high: float
    low: float
    close: float
    vol: float
    amount: float
    adj_factor: float                        # 绝对复权因子（tushare 真因子；东财兜底存 1.0）
    # 数据来源：'tushare'=原始价+真因子；'eastmoney'=已复权 qfq 当 raw、factor=1。
    # 同标的不混源（upsert 换源先清旧），否则 rows_to_qfq_df 按 max 因子缩放会错乱。
    source: str = Field(default="tushare")


class TrendBrief(SQLModel, table=True):
    """节点⑨日报的 LLM 趋势研判缓存：facts_hash 命中则复用、不重复调 LLM。

    缓存按 (batch, backend, facts_hash) 三元组判定：相同事实用同一后端不重复调 LLM；
    切换后端（如 claude_cli ↔ codex_cli）则视为不同 key，自然失效旧 brief。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: str = Field(foreign_key="batch.batch_id", index=True)
    facts_hash: str
    markdown: str
    model: str
    backend: Optional[str] = None   # 生成该 brief 的 LLM 后端（claude_cli/codex_cli/anthropic_api）
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TrendApiSync(SQLModel, table=True):
    """趋势动物 API 每次持仓/选股同步的费用、日期和结果审计（绝不保存 API Key）。"""
    sync_id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: Optional[str] = Field(default=None, index=True)
    dataset_id: Optional[str] = Field(default=None, index=True)
    scope: str = Field(index=True)            # holding | selection
    status: str = Field(index=True)           # running | done | blocked | failed
    as_of_date: Optional[str] = None
    tm_count: int = 0
    requested_fields: list = Field(default_factory=list, sa_column=Column(JSON))
    estimated_cost: float = 0.0
    actual_cost: Optional[float] = None
    incomplete_rows: list = Field(default_factory=list, sa_column=Column(JSON))
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    details: dict = Field(default_factory=dict, sa_column=Column(JSON))
    trigger: Optional[str] = None             # scheduled | startup_catchup | manual | legacy
    attempt_no: int = 0
    scheduled_for: Optional[datetime] = None
    next_retry_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None


class DailyDataset(SQLModel, table=True):
    """纪律交易台的每日不可变事实包；同一交易日只发布一个正式数据集。"""
    __table_args__ = (UniqueConstraint("trade_date"),)
    dataset_id: str = Field(primary_key=True)
    trade_date: str = Field(index=True)
    status: str = Field(default="pending", index=True)
    source_mode: str = "trend_api"           # trend_api | ocr_fallback | mixed
    source_status: dict = Field(default_factory=dict, sa_column=Column(JSON))
    source_dates: dict = Field(default_factory=dict, sa_column=Column(JSON))
    attempt_count: int = 0
    next_retry_at: Optional[datetime] = None
    estimated_cost: float = 0.0
    actual_cost: Optional[float] = None
    approved_budget: float = 3.5
    dataset_hash: Optional[str] = Field(default=None, index=True)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    capability_flags: dict = Field(default_factory=dict, sa_column=Column(JSON))
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    ready_at: Optional[datetime] = None


class DailySchedulerState(SQLModel, table=True):
    """服务内日终采集调度器的持久心跳。

    DailyDataset 负责一次采集的幂等和数据库租约；这个单例只记录进程是否仍在
    按预期唤醒，供公网状态页与 GitHub watchdog 判定“主调度是否真正工作”。
    """
    scheduler_key: str = Field(primary_key=True)
    enabled: bool = False
    boot_id: Optional[str] = Field(default=None, index=True)
    process_started_at: Optional[datetime] = None
    last_tick_at: Optional[datetime] = Field(default=None, index=True)
    last_window_tick_at: Optional[datetime] = None
    last_trade_date: Optional[str] = Field(default=None, index=True)
    last_result: Optional[str] = Field(default=None, index=True)
    last_reason: Optional[str] = None
    last_dataset_status: Optional[str] = Field(default=None, index=True)
    last_attempt_at: Optional[datetime] = None
    next_due_at: Optional[datetime] = None
    last_trigger: Optional[str] = Field(default=None, index=True)
    last_error: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TrendDailySnapshot(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("dataset_id", "tm_id"),)
    snapshot_id: Optional[int] = Field(default=None, primary_key=True)
    dataset_id: str = Field(foreign_key="dailydataset.dataset_id", index=True)
    tm_id: int = Field(index=True)
    code: Optional[str] = Field(default=None, index=True)
    name: str
    asset: Optional[str] = None
    industry_tm_id: Optional[int] = None
    industry_name: Optional[str] = None
    temperature_prev: Optional[str] = None
    temperature_curr: Optional[str] = None
    strength: Optional[float] = None
    strength_change: Optional[str] = None
    right_side_days: Optional[int] = None
    phase: Optional[str] = None
    danger: Optional[bool] = None
    boiling: Optional[bool] = None
    champagne: Optional[bool] = None
    volatility_up: Optional[bool] = None
    market_cap_yi: Optional[float] = None
    amount_yi: Optional[float] = None
    as_of_date: str = Field(index=True)
    source: str = "trend_api"
    raw_payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    payload_hash: str = Field(index=True)
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


class TrendDailyMembership(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("dataset_id", "membership_type", "tm_id"),)
    membership_id: Optional[int] = Field(default=None, primary_key=True)
    dataset_id: str = Field(foreign_key="dailydataset.dataset_id", index=True)
    membership_type: str = Field(index=True)  # warm_to_hot_stock | warm_to_hot_etf | holding | market | sector
    tm_id: int = Field(index=True)
    metadata_json: dict = Field(default_factory=dict, sa_column=Column(JSON))


class TushareDailyFact(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("dataset_id", "ts_code"),)
    fact_id: Optional[int] = Field(default=None, primary_key=True)
    dataset_id: str = Field(foreign_key="dailydataset.dataset_id", index=True)
    ts_code: str = Field(index=True)
    trade_date: str = Field(index=True)
    close: Optional[float] = None
    amount_yi: Optional[float] = None
    float_market_cap_yi: Optional[float] = None
    fund_size_yi: Optional[float] = None
    suspended: Optional[bool] = None
    up_limit: Optional[float] = None
    down_limit: Optional[float] = None
    source_dates: dict = Field(default_factory=dict, sa_column=Column(JSON))
    raw_payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


class VolatilitySupplement(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("dataset_id", "instrument_id"),)
    supplement_id: Optional[int] = Field(default=None, primary_key=True)
    dataset_id: str = Field(foreign_key="dailydataset.dataset_id", index=True)
    instrument_id: str = Field(index=True)
    signal_date: str = Field(index=True)
    volatility_up: bool
    source: str = "manual"                  # manual | ocr
    evidence: dict = Field(default_factory=dict, sa_column=Column(JSON))
    confirmed_at: datetime = Field(default_factory=datetime.utcnow)


# ── 纪律交易闭环（v1.1，2026-07-12）──────────────────────────────────────

class DisciplineVersion(SQLModel, table=True):
    """不可变纪律快照；历史计划永远引用生成时的版本和哈希。"""
    version: str = Field(primary_key=True)
    effective_from: str
    status: str = Field(default="draft", index=True)  # draft | active | retired
    source_path: str
    rules_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    rules_hash: str = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PortfolioSnapshot(SQLModel, table=True):
    snapshot_id: Optional[int] = Field(default=None, primary_key=True)
    trade_date: str = Field(index=True)
    nav: float
    cash: float
    market_value: float
    source: str = "broker_ocr"  # broker_ocr | broker_import | manual | mock_strategy_ledger
    confirmed: bool = False
    as_of_date: str
    prior_snapshot_id: Optional[int] = Field(
        default=None, foreign_key="portfoliosnapshot.snapshot_id", index=True)
    price_date: Optional[str] = Field(default=None, index=True)
    reconciliation_status: str = Field(default="confirmed", index=True)
    derivation: dict = Field(default_factory=dict, sa_column=Column(JSON))
    synced_at: datetime = Field(default_factory=datetime.utcnow)


class TradePlan(SQLModel, table=True):
    plan_id: str = Field(primary_key=True)
    signal_date: str = Field(index=True)
    execute_date: str = Field(index=True)
    discipline_version: str = Field(foreign_key="disciplineversion.version")
    rules_hash: str
    status: str = Field(default="draft", index=True)  # draft | locked | partially_executed | completed | expired
    dataset_id: Optional[str] = Field(default=None, foreign_key="dailydataset.dataset_id", index=True)
    portfolio_snapshot_id: Optional[int] = Field(default=None, foreign_key="portfoliosnapshot.snapshot_id", index=True)
    plan_stage: str = Field(default="executable", index=True)  # signal | executable
    input_hash: Optional[str] = Field(default=None, index=True)
    supersedes_plan_id: Optional[str] = Field(default=None, foreign_key="tradeplan.plan_id")
    market_mode: str = "normal"
    environment_factor: float = 1.0
    capacity_snapshot: dict = Field(default_factory=dict, sa_column=Column(JSON))
    data_health: dict = Field(default_factory=dict, sa_column=Column(JSON))
    selection_snapshot: dict = Field(default_factory=dict, sa_column=Column(JSON))
    change_notice: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    locked_at: Optional[datetime] = None


class TradePlanItem(SQLModel, table=True):
    item_id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: str = Field(foreign_key="tradeplan.plan_id", index=True)
    instrument_id: str = Field(index=True)
    name: str
    asset_type: str  # stock | etf
    side: str  # buy | sell_all | reduce | hold
    target_weight: Optional[float] = None
    target_shares: Optional[int] = None
    reduce_fraction: Optional[float] = None
    priority: int = 99
    rule_evidence: dict = Field(default_factory=dict, sa_column=Column(JSON))
    source_dates: dict = Field(default_factory=dict, sa_column=Column(JSON))
    data_sources: dict = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = Field(default="pending", index=True)


class BrokerImport(SQLModel, table=True):
    import_id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: Optional[str] = Field(default=None, foreign_key="tradeplan.plan_id", index=True)
    import_type: str = "executions"  # positions | executions
    filename: str
    file_hash: str = Field(index=True)
    batch_id: Optional[str] = Field(default=None, index=True)
    source: str = "broker_file"  # broker_file | broker_ocr
    status: str = Field(default="preview", index=True)  # preview | confirmed | rejected
    field_mapping: dict = Field(default_factory=dict, sa_column=Column(JSON))
    parsed_rows: list = Field(default_factory=list, sa_column=Column(JSON))
    anomaly_rows: list = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    confirmed_at: Optional[datetime] = None


class Execution(SQLModel, table=True):
    execution_id: Optional[int] = Field(default=None, primary_key=True)
    plan_item_id: Optional[int] = Field(default=None, foreign_key="tradeplanitem.item_id", index=True)
    import_id: Optional[int] = Field(default=None, foreign_key="brokerimport.import_id", index=True)
    trade_date: str = Field(index=True)
    instrument_id: str = Field(index=True)
    side: str
    executed_at: str
    price: float
    shares: int
    fees: float = 0.0
    source: str = "broker_import"
    fingerprint: Optional[str] = Field(default=None, unique=True, index=True)
    gross_amount: Optional[float] = None
    net_amount: Optional[float] = None
    fee_source: str = "actual"  # actual | derived_from_net | conservative_estimate
    confirmed: bool = True
    deviation_type: Optional[str] = None
    deviation_reason: Optional[str] = None


class PositionLot(SQLModel, table=True):
    lot_id: Optional[int] = Field(default=None, primary_key=True)
    instrument_id: str = Field(index=True)
    name: str
    asset_type: str
    opened_by_execution: Optional[int] = Field(default=None, foreign_key="execution.execution_id")
    opened_on: str
    initial_shares: int
    remaining_shares: int
    avg_cost: float = 0.0
    source: str = "broker_confirmed"
    as_of_date: str
    synced_at: datetime = Field(default_factory=datetime.utcnow)


class TradingDayConfirmation(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("trade_date"),)
    confirmation_id: Optional[int] = Field(default=None, primary_key=True)
    trade_date: str = Field(index=True)
    status: str = Field(index=True)  # executions_confirmed | no_execution
    source: str = "manual"  # broker_ocr | broker_file | manual
    import_id: Optional[int] = Field(default=None, foreign_key="brokerimport.import_id")
    note: Optional[str] = None
    confirmed_at: datetime = Field(default_factory=datetime.utcnow)


class LedgerAdjustment(SQLModel, table=True):
    adjustment_id: Optional[int] = Field(default=None, primary_key=True)
    trade_date: str = Field(index=True)
    adjustment_type: str = Field(index=True)
    instrument_id: Optional[str] = Field(default=None, index=True)
    cash_amount: float = 0.0
    share_delta: int = 0
    note: str
    confirmed: bool = True
    applied_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FeeSchedule(SQLModel, table=True):
    schedule_id: str = Field(default="default", primary_key=True)
    # A 股/普通场内基金佣金。保留原字段名，兼容已落库的历史费率。
    commission_rate: float = 0.0
    minimum_commission: float = 0.0
    # ETF/LOF 的佣金可能与 A 股不同。NULL 表示历史配置尚未拆分，
    # 估算时向后兼容地沿用上面的 A 股费率，直到用户完成一次确认。
    etf_commission_rate: Optional[float] = Field(default=None)
    etf_minimum_commission: Optional[float] = Field(default=None)
    transfer_fee_rate: float = 0.0
    stamp_duty_rate: float = 0.0
    safety_multiplier: float = 1.2
    configured: bool = False
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AutomationRun(SQLModel, table=True):
    run_id: str = Field(primary_key=True)
    trade_date: str = Field(index=True)
    stage: str = Field(index=True)  # reminder | finalize | late_finalize | manual
    status: str = Field(index=True)  # running | done | skipped | blocked | failed
    trigger: str = "scheduled"
    details: dict = Field(default_factory=dict, sa_column=Column(JSON))
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None


class EmailDelivery(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("idempotency_key"),)
    delivery_id: Optional[int] = Field(default=None, primary_key=True)
    trade_date: str = Field(index=True)
    plan_id: Optional[str] = Field(default=None, foreign_key="tradeplan.plan_id", index=True)
    recipient: str
    kind: str = Field(index=True)  # reminder | action_list | blocked | test
    template_version: str = "v1"
    idempotency_key: str = Field(index=True)
    status: str = Field(default="pending", index=True)
    attempts: int = 0
    message_id: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    sent_at: Optional[datetime] = None


class SignalSnapshot(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("instrument_id", "as_of_date"),)
    snapshot_id: Optional[int] = Field(default=None, primary_key=True)
    instrument_id: str = Field(index=True)
    as_of_date: str = Field(index=True)
    temperature_prev: Optional[str] = None
    temperature_curr: Optional[str] = None
    strength: Optional[float] = None
    right_side_days: Optional[int] = None
    phase: Optional[str] = None
    danger: Optional[bool] = None
    boiling: Optional[bool] = None
    champagne: Optional[bool] = None
    volatility_up: Optional[bool] = None
    source: str = "trend_animals"
    raw_payload_hash: Optional[str] = None
    synced_at: datetime = Field(default_factory=datetime.utcnow)


class DailyExitSignal(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("plan_id", "instrument_id", "signal_date"),)
    signal_id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: str = Field(foreign_key="tradeplan.plan_id", index=True)
    instrument_id: str = Field(index=True)
    signal_date: str = Field(index=True)
    execute_date: str
    danger: bool = False
    temp_flat_or_below: bool = False
    champagne: Optional[bool] = None
    boiling: Optional[bool] = None
    volatility_up: Optional[bool] = None
    profit_signal_count: int = 0
    planned_reduce_fraction: float = 0.0
    consecutive_days_by_signal: dict = Field(default_factory=dict, sa_column=Column(JSON))
    action_generated: str
    target_shares: int = 0
    valid_until: str
    action_execution_id: Optional[int] = Field(default=None, foreign_key="execution.execution_id")
    violation: Optional[str] = None


class DailyReview(SQLModel, table=True):
    review_id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: str = Field(foreign_key="tradeplan.plan_id", index=True)
    trade_date: str = Field(index=True)
    plan_completion_rate: float
    discipline_score: float
    trade_result: str
    discipline_result: str
    violations: list = Field(default_factory=list, sa_column=Column(JSON))
    data_issues: list = Field(default_factory=list, sa_column=Column(JSON))
    notes: Optional[str] = None
    metrics: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ── 美股手工执行台（us-micro-live-h1，完全独立于 A 股纪律台账）───────────────

class UsUniverseArchive(SQLModel, table=True):
    """一份获准归档的趋势动物成员种子与 Bitget 公开交集。"""
    __tablename__ = "us_universe_archive"

    archive_id: str = Field(primary_key=True)
    trend_animals_as_of_date: str = Field(index=True)
    venue_retrieved_at: Optional[datetime] = None
    coverage_manifest_path: Optional[str] = None
    venue_manifest_path: Optional[str] = None
    seed_sha256: str = Field(index=True)
    intersection_sha256: str = Field(index=True)
    stock_count: int = 0
    etf_observation_count: int = 0
    metadata_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = Field(default="ready", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsDailyRun(SQLModel, table=True):
    """一日美股信号扫描的费用、缓存和状态机记录。"""
    __tablename__ = "us_daily_run"
    __table_args__ = (
        UniqueConstraint("as_of_date", "scope", "base_fields_hash", name="ux_us_daily_run_cache"),
    )

    run_id: str = Field(primary_key=True)
    as_of_date: str = Field(index=True)
    scope: str = Field(default="bitget_us_manual", index=True)
    status: str = Field(default="pending", index=True)
    rules_version: str = Field(index=True)
    universe_archive_id: Optional[str] = Field(
        default=None, foreign_key="us_universe_archive.archive_id", index=True)
    base_fields: list = Field(default_factory=list, sa_column=Column(JSON))
    base_fields_hash: str = Field(index=True)
    enrichment_fields: list = Field(default_factory=list, sa_column=Column(JSON))
    enrichment_fields_hash: Optional[str] = Field(default=None, index=True)
    estimated_base_cost_cny: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(20, 8)))
    approved_base_budget_cny: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(20, 8)))
    actual_base_cost_cny: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(20, 8)))
    estimated_enrichment_cost_cny: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(20, 8)))
    approved_enrichment_budget_cny: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(20, 8)))
    actual_enrichment_cost_cny: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(20, 8)))
    estimated_total_cost_cny: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(20, 8)))
    actual_total_cost_cny: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(20, 8)))
    daily_cost_cap_cny: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(20, 8)))
    cost_breakdown_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    trigger: Optional[str] = Field(default=None, index=True)
    attempt_count: int = 0
    next_retry_at: Optional[datetime] = Field(default=None, index=True)
    lease_owner: Optional[str] = Field(default=None, index=True)
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    cache_hit: bool = False
    universe_count: int = 0
    returned_count: int = 0
    funnel_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    source_dates_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    exit_status: Optional[str] = Field(default=None, index=True)
    environment_status: Optional[str] = Field(default=None, index=True)
    etf_mapping_status: Optional[str] = Field(default=None, index=True)
    raw_archive_path: Optional[str] = None
    raw_sha256: Optional[str] = Field(default=None, index=True)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None


class UsCandidateSnapshot(SQLModel, table=True):
    """每日候选及其完整筛选证据；缺字段也保留，而不是静默删除。"""
    __tablename__ = "us_candidate_snapshot"
    __table_args__ = (UniqueConstraint("run_id", "tm_id", name="ux_us_candidate_run_tm"),)

    candidate_id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(foreign_key="us_daily_run.run_id", index=True)
    tm_id: int = Field(index=True)
    ticker_symbol: str = Field(index=True)
    ticker_name: Optional[str] = None
    asset_type: str = Field(default="stock", index=True)  # stock | etf | legacy etf_observation
    venue_instrument: Optional[str] = Field(default=None, index=True)
    venue_metadata_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    temperature_prev: Optional[str] = None
    temperature_curr: Optional[str] = None
    right_side_calendar_days: Optional[int] = None
    right_side_age_bucket: Optional[str] = None
    warm_to_hot: bool = False
    gate_passed: bool = Field(default=False, index=True)
    strength_local: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 8)))
    industry_tm_id: Optional[int] = Field(default=None, index=True)
    industry_name: Optional[str] = None
    industry_temperature_curr: Optional[str] = None
    industry_strength_local: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(20, 8)))
    industry_source_method: Optional[str] = None
    ticker_labels: list = Field(default_factory=list, sa_column=Column(JSON))
    trend_phase_curr: Optional[str] = None
    market_cap: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    amount_1d: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    price_index: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    reference_price_usdt: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(24, 8)))
    reference_price_at: Optional[datetime] = None
    reference_price_source: Optional[str] = None
    quote_status: str = Field(default="not_requested", index=True)
    screen_status: str = Field(default="observe", index=True)
    primary_reason: Optional[str] = None
    all_reasons: list = Field(default_factory=list, sa_column=Column(JSON))
    rank: Optional[int] = Field(default=None, index=True)
    observation_rank: Optional[int] = Field(default=None, index=True)
    raw_fields: dict = Field(default_factory=dict, sa_column=Column(JSON))
    raw_sha256: Optional[str] = Field(default=None, index=True)
    environment_id: Optional[str] = Field(
        default=None, foreign_key="us_market_environment_snapshot.environment_id", index=True)
    etf_identity_id: Optional[str] = Field(
        default=None, foreign_key="us_etf_identity.identity_id", index=True)
    etf_benchmark_evidence_id: Optional[str] = Field(
        default=None, foreign_key="us_etf_benchmark_evidence.evidence_id", index=True)
    benchmark_family_id: Optional[str] = Field(default=None, index=True)
    exposure_key: Optional[str] = Field(default=None, index=True)
    benchmark_status: str = Field(default="not_applicable", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsMarketEnvironmentSnapshot(SQLModel, table=True):
    """H6 美股根资产温度及其当日新增买入容量系数。"""
    __tablename__ = "us_market_environment_snapshot"
    __table_args__ = (UniqueConstraint("run_id", name="ux_us_environment_run"),)

    environment_id: str = Field(primary_key=True)
    run_id: str = Field(foreign_key="us_daily_run.run_id", index=True)
    as_of_date: str = Field(index=True)
    market_tm_id: int = Field(index=True)
    market_temperature: str = Field(index=True)
    environment_factor: Decimal = Field(sa_column=Column(Numeric(8, 4)))
    status: str = Field(default="ready", index=True)
    contract_hash: str = Field(index=True)
    raw_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    raw_sha256: str = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsEtfIdentity(SQLModel, table=True):
    """SEC investment-company ticker/series/class 的版本化身份锚点。"""
    __tablename__ = "us_etf_identity"
    __table_args__ = (UniqueConstraint("ticker_symbol", "version", name="ux_us_etf_identity_version"),)

    identity_id: str = Field(primary_key=True)
    ticker_symbol: str = Field(index=True)
    version: int = 1
    status: str = Field(index=True)
    cik: Optional[str] = Field(default=None, index=True)
    series_id: Optional[str] = Field(default=None, index=True)
    class_id: Optional[str] = Field(default=None, index=True)
    class_ticker: Optional[str] = Field(default=None, index=True)
    series_name: Optional[str] = None
    class_name: Optional[str] = None
    legal_fund_name: Optional[str] = None
    source_url: str
    content_sha256: str = Field(index=True)
    evidence_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    is_active: bool = Field(default=True, index=True)
    retrieved_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    expires_at: Optional[datetime] = Field(default=None, index=True)


class UsEtfBenchmarkEvidence(SQLModel, table=True):
    """发行商/SEC 支持的 ETF 基准和执行暴露指纹；版本不可改写。"""
    __tablename__ = "us_etf_benchmark_evidence"
    __table_args__ = (UniqueConstraint("identity_id", "version", name="ux_us_etf_benchmark_version"),)

    evidence_id: str = Field(primary_key=True)
    identity_id: str = Field(foreign_key="us_etf_identity.identity_id", index=True)
    ticker_symbol: str = Field(index=True)
    version: int = 1
    status: str = Field(index=True)
    benchmark_family_id: Optional[str] = Field(default=None, index=True)
    benchmark_name_raw: Optional[str] = None
    benchmark_canonical_name: Optional[str] = None
    benchmark_provider: Optional[str] = None
    benchmark_ticker: Optional[str] = None
    strategy_type: Optional[str] = None
    exposure_direction: Optional[str] = None
    leverage_multiplier: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(12, 6)))
    currency_hedge: Optional[str] = None
    exposure_key: Optional[str] = Field(default=None, index=True)
    source_type: str
    source_url: str
    filing_accession_no: Optional[str] = None
    source_effective_date: Optional[str] = None
    content_sha256: str = Field(index=True)
    parser_version: str
    evidence_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    supersedes_evidence_id: Optional[str] = Field(
        default=None, foreign_key="us_etf_benchmark_evidence.evidence_id", index=True)
    is_active: bool = Field(default=True, index=True)
    retrieved_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    expires_at: Optional[datetime] = Field(default=None, index=True)


class UsEtfBenchmarkReview(SQLModel, table=True):
    """ETF 权威来源冲突的人工复核留痕；不能用自由文本伪造 verified。"""
    __tablename__ = "us_etf_benchmark_review"
    __table_args__ = (UniqueConstraint("idempotency_key", name="ux_us_etf_review_idempotency"),)

    review_id: str = Field(primary_key=True)
    evidence_id: str = Field(foreign_key="us_etf_benchmark_evidence.evidence_id", index=True)
    idempotency_key: str = Field(index=True)
    resolution: str
    reason: str
    evidence_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsRiskAnchor(SQLModel, table=True):
    """H6 Bitget rToken EP3 前期重要低点；只反推仓位，不是卖出止损。"""
    __tablename__ = "us_risk_anchor"
    __table_args__ = (UniqueConstraint("candidate_id", "input_hash", name="ux_us_risk_anchor_input"),)

    anchor_id: str = Field(primary_key=True)
    candidate_id: int = Field(foreign_key="us_candidate_snapshot.candidate_id", index=True)
    run_id: str = Field(foreign_key="us_daily_run.run_id", index=True)
    status: str = Field(index=True)
    signal_date: str = Field(index=True)
    algorithm_version: str = Field(index=True)
    algorithm_sha256: str = Field(index=True)
    contract_hash: str = Field(index=True)
    input_hash: str = Field(index=True)
    bitget_symbol: str = Field(index=True)
    quote_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    quote_at: Optional[datetime] = None
    anchor_date: Optional[str] = Field(default=None, index=True)
    anchor_price_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    anchor_distance: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 12)))
    price_tick_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 12)))
    daily_sha256: Optional[str] = Field(default=None, index=True)
    hourly_sha256: Optional[str] = Field(default=None, index=True)
    archive_path: Optional[str] = None
    evidence_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsAllocationPreview(SQLModel, table=True):
    """按固定排名和容量生成的不可变 H6 多候选分配预览。"""
    __tablename__ = "us_allocation_preview"
    __table_args__ = (UniqueConstraint("input_hash", name="ux_us_allocation_input"),)

    allocation_preview_id: str = Field(primary_key=True)
    run_id: str = Field(foreign_key="us_daily_run.run_id", index=True)
    environment_id: str = Field(foreign_key="us_market_environment_snapshot.environment_id", index=True)
    intended_execution_date: str = Field(index=True)
    status: str = Field(default="ready", index=True)
    input_hash: str = Field(index=True)
    daily_limit_usdt: Decimal = Field(sa_column=Column(Numeric(20, 8)))
    daily_remaining_usdt: Decimal = Field(sa_column=Column(Numeric(20, 8)))
    portfolio_remaining_usdt: Decimal = Field(sa_column=Column(Numeric(20, 8)))
    cash_remaining_usdt: Decimal = Field(sa_column=Column(Numeric(20, 8)))
    available_usdt: Decimal = Field(sa_column=Column(Numeric(20, 8)))
    allocated_usdt: Decimal = Field(sa_column=Column(Numeric(20, 8)))
    open_ticker_count: int = 0
    available_ticker_slots: int = 0
    exclusions_json: list = Field(default_factory=list, sa_column=Column(JSON))
    backfill_requested: bool = False
    snapshot_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsAllocationPreviewItem(SQLModel, table=True):
    __tablename__ = "us_allocation_preview_item"
    __table_args__ = (UniqueConstraint("allocation_preview_id", "candidate_id", name="ux_us_allocation_candidate"),)

    allocation_item_id: Optional[int] = Field(default=None, primary_key=True)
    allocation_preview_id: str = Field(foreign_key="us_allocation_preview.allocation_preview_id", index=True)
    candidate_id: int = Field(foreign_key="us_candidate_snapshot.candidate_id", index=True)
    risk_anchor_id: Optional[str] = Field(default=None, foreign_key="us_risk_anchor.anchor_id", index=True)
    priority: int
    status: str = Field(index=True)
    reason_code: Optional[str] = None
    reference_price_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    anchor_price_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    anchor_distance: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 12)))
    risk_ceiling_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 8)))
    target_notional_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 8)))
    allocated_notional_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 8)))
    target_quantity: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(28, 12)))
    anchor_loss_estimate_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 8)))
    minimum_notional_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 8)))
    evidence_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsWindDataCache(SQLModel, table=True):
    """H5 Wind 身份与日线缓存；同一契约键成功后不重复调用。"""
    __tablename__ = "us_wind_data_cache"

    cache_key: str = Field(primary_key=True)
    asset_type: str = Field(index=True)
    ticker_symbol: str = Field(index=True)
    signal_date: str = Field(index=True)
    begin_date: str
    end_date: str
    contract_hash: str = Field(index=True)
    wind_symbol: str = Field(index=True)
    payload_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    response_sha256: str = Field(index=True)
    archive_path: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsWindRequestAudit(SQLModel, table=True):
    """Wind 调用/缓存命中单独审计，不计入趋势动物 ¥5 上限。"""
    __tablename__ = "us_wind_request_audit"

    audit_id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="us_candidate_snapshot.candidate_id", index=True)
    cache_key: str = Field(index=True)
    server_type: str
    tool_name: str
    request_sha256: str = Field(index=True)
    cache_hit: bool = False
    status: str = Field(index=True)  # success | error
    response_sha256: Optional[str] = Field(default=None, index=True)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsStopSuggestion(SQLModel, table=True):
    """不可变的 Wind + Bitget 双源止损证据。"""
    __tablename__ = "us_stop_suggestion"
    __table_args__ = (
        UniqueConstraint("candidate_id", "input_hash", name="ux_us_stop_candidate_input"),
    )

    suggestion_id: str = Field(primary_key=True)
    candidate_id: int = Field(foreign_key="us_candidate_snapshot.candidate_id", index=True)
    run_id: str = Field(foreign_key="us_daily_run.run_id", index=True)
    status: str = Field(index=True)  # auto_ready | review_required | blocked
    source_mode: str = Field(index=True)  # shadow | active
    signal_date: str = Field(index=True)
    asset_type: str
    algorithm_version: str = Field(index=True)
    algorithm_sha256: str = Field(index=True)
    contract_hash: str = Field(index=True)
    input_hash: str = Field(index=True)
    wind_symbol: Optional[str] = Field(default=None, index=True)
    bitget_symbol: str = Field(index=True)
    anchor_type: Optional[str] = None
    anchor_date: Optional[str] = Field(default=None, index=True)
    wind_anchor_low_usd: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    wind_signal_close_usd: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    wind_ratio: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 12)))
    wind_mapped_stop_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    bitget_anchor_low_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    bitget_quote_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    bitget_quote_at: Optional[datetime] = None
    deviation_ratio: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 12)))
    deviation_threshold: Decimal = Field(sa_column=Column(Numeric(20, 12)))
    price_tick_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 12)))
    suggested_stop_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    wind_response_sha256: Optional[str] = Field(default=None, index=True)
    bitget_daily_sha256: Optional[str] = Field(default=None, index=True)
    bitget_hourly_sha256: Optional[str] = Field(default=None, index=True)
    archive_path: Optional[str] = None
    evidence_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsStopReview(SQLModel, table=True):
    """偏差超限后的不可变人工定稿；不改写原始建议。"""
    __tablename__ = "us_stop_review"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="ux_us_stop_review_idempotency"),
        UniqueConstraint("suggestion_id", name="ux_us_stop_review_suggestion"),
    )

    review_id: str = Field(primary_key=True)
    suggestion_id: str = Field(foreign_key="us_stop_suggestion.suggestion_id", index=True)
    idempotency_key: str = Field(index=True)
    resolution: str  # wind_mapped | bitget_anchor | custom
    custom_price_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    final_stop_usdt: Decimal = Field(sa_column=Column(Numeric(24, 8)))
    final_source: str
    reason: str
    evidence_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsSizingPreviewSnapshot(SQLModel, table=True):
    """不可变仓位预览；绑定当时的候选、止损建议和报价。"""
    __tablename__ = "us_sizing_preview_snapshot"

    sizing_preview_id: str = Field(primary_key=True)
    candidate_id: int = Field(foreign_key="us_candidate_snapshot.candidate_id", index=True)
    stop_suggestion_id: str = Field(foreign_key="us_stop_suggestion.suggestion_id", index=True)
    stop_review_id: Optional[str] = Field(default=None, foreign_key="us_stop_review.review_id", index=True)
    input_hash: str = Field(index=True)
    entry_reference_price_usdt: Decimal = Field(sa_column=Column(Numeric(24, 8)))
    stop_price_usdt: Decimal = Field(sa_column=Column(Numeric(24, 8)))
    stop_source: str
    snapshot_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsManualAccountSnapshot(SQLModel, table=True):
    """实验子账户快照；来源区分人工成交台账与已确认的账户截图。"""
    __tablename__ = "us_manual_account_snapshot"

    snapshot_id: Optional[int] = Field(default=None, primary_key=True)
    as_of_date: str = Field(index=True)
    starting_equity_usdt: Decimal = Field(sa_column=Column(Numeric(20, 8)))
    cash_usdt: Decimal = Field(sa_column=Column(Numeric(20, 8)))
    open_cost_usdt: Decimal = Field(sa_column=Column(Numeric(20, 8)))
    open_risk_usdt: Decimal = Field(sa_column=Column(Numeric(20, 8)))
    position_count: int = 0
    source: str = "manual_ledger"
    derivation_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    confirmed_at: datetime = Field(default_factory=datetime.utcnow)


class UsAccountOcrBatch(SQLModel, table=True):
    """Bitget 持仓截图的暂存批次；确认前不得影响账户或交易纪律。"""
    __tablename__ = "us_account_ocr_batch"
    __table_args__ = (
        UniqueConstraint("confirmation_key", name="ux_us_account_ocr_confirmation_key"),
    )

    batch_id: str = Field(primary_key=True)
    capture_date: str = Field(index=True)
    provider: Optional[str] = None
    status: str = Field(default="running", index=True)  # running | ready | failed | confirmed
    image_count: int = 0
    processed_image_count: int = 0
    failed_image_count: int = 0
    equity_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 8)))
    cash_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 8)))
    currency: Optional[str] = None
    source_images_json: list = Field(default_factory=list, sa_column=Column(JSON))
    conflicts_json: list = Field(default_factory=list, sa_column=Column(JSON))
    raw_responses_json: list = Field(default_factory=list, sa_column=Column(JSON))
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    confirmation_key: Optional[str] = Field(default=None, index=True)
    confirmed_snapshot_id: Optional[int] = Field(
        default=None, foreign_key="us_manual_account_snapshot.snapshot_id", index=True,
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    confirmed_at: Optional[datetime] = None


class UsAccountOcrPosition(SQLModel, table=True):
    """OCR 逐项预览；允许散股，但没有明确代码时必须失败关闭。"""
    __tablename__ = "us_account_ocr_position"

    row_id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: str = Field(foreign_key="us_account_ocr_batch.batch_id", index=True)
    ticker_symbol: Optional[str] = Field(default=None, index=True)
    ticker_name: Optional[str] = None
    venue_instrument: Optional[str] = Field(default=None, index=True)
    quantity: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(28, 12)))
    average_cost_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    current_price_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    market_value_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    unrealized_pnl_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    source_image: str
    status: str = Field(default="review_required", index=True)  # ready | review_required
    errors_json: list = Field(default_factory=list, sa_column=Column(JSON))
    raw_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsManualPlan(SQLModel, table=True):
    """不可变的美股人工执行清单。"""
    __tablename__ = "us_manual_plan"

    plan_id: str = Field(primary_key=True)
    signal_date: str = Field(index=True)
    intended_execution_date: str = Field(index=True)
    run_id: str = Field(foreign_key="us_daily_run.run_id", index=True)
    status: str = Field(default="draft", index=True)
    rules_version: str = Field(index=True)
    rules_sha256: str = Field(index=True)
    account_snapshot_id: Optional[int] = Field(
        default=None, foreign_key="us_manual_account_snapshot.snapshot_id", index=True)
    account_snapshot_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    data_health_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    input_hash: Optional[str] = Field(default=None, index=True)
    allocation_preview_id: Optional[str] = Field(
        default=None, foreign_key="us_allocation_preview.allocation_preview_id", index=True)
    supersedes_plan_id: Optional[str] = Field(
        default=None, foreign_key="us_manual_plan.plan_id", index=True)
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    locked_at: Optional[datetime] = None


class UsManualPlanItem(SQLModel, table=True):
    """计划项可持有 Decimal 散股，但永远不代表已向交易所下单。"""
    __tablename__ = "us_manual_plan_item"

    item_id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: str = Field(foreign_key="us_manual_plan.plan_id", index=True)
    candidate_id: Optional[int] = Field(
        default=None, foreign_key="us_candidate_snapshot.candidate_id", index=True)
    side: str = Field(default="buy", index=True)  # buy | sell | hold
    priority: int = 99
    ticker_symbol: str = Field(index=True)
    ticker_name: Optional[str] = None
    asset_type: str = "stock"
    venue: str = "bitget"
    venue_instrument: str = Field(index=True)
    venue_metadata_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    entry_reference_price: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    entry_reference_at: Optional[datetime] = None
    entry_reference_source: Optional[str] = None  # bitget_public_quote | manual
    stop_price: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    stop_source: Optional[str] = None
    stop_suggestion_id: Optional[str] = Field(
        default=None, foreign_key="us_stop_suggestion.suggestion_id", index=True)
    stop_review_id: Optional[str] = Field(
        default=None, foreign_key="us_stop_review.review_id", index=True)
    sizing_preview_id: Optional[str] = Field(
        default=None, foreign_key="us_sizing_preview_snapshot.sizing_preview_id", index=True)
    allocation_preview_item_id: Optional[int] = Field(
        default=None, foreign_key="us_allocation_preview_item.allocation_item_id", index=True)
    risk_anchor_id: Optional[str] = Field(default=None, foreign_key="us_risk_anchor.anchor_id", index=True)
    environment_id: Optional[str] = Field(
        default=None, foreign_key="us_market_environment_snapshot.environment_id", index=True)
    exit_decision_id: Optional[str] = Field(default=None, foreign_key="us_exit_decision.decision_id", index=True)
    risk_anchor_price: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    risk_anchor_date: Optional[str] = None
    anchor_loss_estimate_usdt: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(20, 8)))
    stop_anchor_type: Optional[str] = None
    stop_anchor_date: Optional[str] = None
    stop_evidence_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    stop_distance: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 10)))
    risk_budget_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 8)))
    target_notional_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 8)))
    target_quantity: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(28, 12)))
    estimated_max_loss_usdt: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(20, 8)))
    reason_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = Field(default="pending", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsManualExecution(SQLModel, table=True):
    """用户确认后写入的人工成交；冲正通过新行表达。"""
    __tablename__ = "us_manual_execution"
    __table_args__ = (UniqueConstraint("idempotency_key", name="ux_us_execution_idempotency"),)

    execution_id: Optional[int] = Field(default=None, primary_key=True)
    plan_item_id: Optional[int] = Field(
        default=None, foreign_key="us_manual_plan_item.item_id", index=True)
    correction_of_id: Optional[int] = Field(
        default=None, foreign_key="us_manual_execution.execution_id", index=True)
    idempotency_key: str = Field(index=True)
    trade_date: str = Field(index=True)
    executed_at: str
    side: str = Field(index=True)  # buy | sell | correction | reversal
    ticker_symbol: str = Field(index=True)
    venue_instrument: str = Field(index=True)
    price_usdt: Decimal = Field(sa_column=Column(Numeric(24, 8)))
    quantity: Decimal = Field(sa_column=Column(Numeric(28, 12)))
    fee_usdt: Decimal = Field(default=Decimal("0"), sa_column=Column(Numeric(20, 8)))
    gross_usdt: Decimal = Field(default=Decimal("0"), sa_column=Column(Numeric(24, 8)))
    source: str = "manual"
    note: Optional[str] = None
    confirmed: bool = False
    confirmed_at: Optional[datetime] = None


class UsPositionLot(SQLModel, table=True):
    """按买入成交创建的散股持仓批次，卖出采用 FIFO 减少。"""
    __tablename__ = "us_position_lot"

    lot_id: Optional[int] = Field(default=None, primary_key=True)
    ticker_symbol: str = Field(index=True)
    ticker_name: Optional[str] = None
    asset_type: str = "stock"
    venue_instrument: str = Field(index=True)
    opened_by_execution_id: Optional[int] = Field(
        default=None, foreign_key="us_manual_execution.execution_id", index=True)
    opened_on_data_date: str = Field(index=True)
    initial_quantity: Decimal = Field(sa_column=Column(Numeric(28, 12)))
    remaining_quantity: Decimal = Field(sa_column=Column(Numeric(28, 12)))
    average_cost_usdt: Decimal = Field(sa_column=Column(Numeric(24, 8)))
    stop_price: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(24, 8)))
    stop_source: Optional[str] = None
    stop_suggestion_id: Optional[str] = Field(
        default=None, foreign_key="us_stop_suggestion.suggestion_id", index=True)
    stop_review_id: Optional[str] = Field(
        default=None, foreign_key="us_stop_review.review_id", index=True)
    stop_anchor_type: Optional[str] = None
    stop_anchor_date: Optional[str] = None
    stop_evidence_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    tm_id: Optional[int] = Field(default=None, index=True)
    rules_version: Optional[str] = Field(default=None, index=True)
    risk_anchor_id: Optional[str] = Field(default=None, foreign_key="us_risk_anchor.anchor_id", index=True)
    environment_id: Optional[str] = Field(
        default=None, foreign_key="us_market_environment_snapshot.environment_id", index=True)
    holding_trading_days: int = 0
    stop_triggered: bool = False
    status: str = Field(default="open", index=True)
    closed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class UsDailyReview(SQLModel, table=True):
    """美股实验日复盘；交易结果和纪律执行分开保留。"""
    __tablename__ = "us_daily_review"
    __table_args__ = (UniqueConstraint("as_of_date", "plan_id", name="ux_us_review_date_plan"),)

    review_id: Optional[int] = Field(default=None, primary_key=True)
    as_of_date: str = Field(index=True)
    run_id: Optional[str] = Field(default=None, foreign_key="us_daily_run.run_id", index=True)
    plan_id: Optional[str] = Field(default=None, foreign_key="us_manual_plan.plan_id", index=True)
    funnel_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    execution_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    compliance_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    metrics_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsHoldingSignalSnapshot(SQLModel, table=True):
    """H6 开放持仓的趋势动物最小退出字段快照。"""
    __tablename__ = "us_holding_signal_snapshot"
    __table_args__ = (
        UniqueConstraint("as_of_date", "lot_id", "contract_hash", name="ux_us_holding_signal_contract"),
    )

    snapshot_id: str = Field(primary_key=True)
    run_id: Optional[str] = Field(default=None, foreign_key="us_daily_run.run_id", index=True)
    as_of_date: str = Field(index=True)
    lot_id: int = Field(foreign_key="us_position_lot.lot_id", index=True)
    tm_id: int = Field(index=True)
    ticker_symbol: str = Field(index=True)
    temperature_curr: Optional[str] = None
    danger: Optional[bool] = None
    boiling: Optional[bool] = None
    champagne: Optional[bool] = None
    status: str = Field(index=True)
    contract_hash: str = Field(index=True)
    raw_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    raw_sha256: str = Field(index=True)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsExitDecision(SQLModel, table=True):
    """由趋势温度/危险/沸/开香槟生成的不可变手工卖出动作。"""
    __tablename__ = "us_exit_decision"
    __table_args__ = (
        UniqueConstraint("lot_id", "as_of_date", "signal_hash", name="ux_us_exit_lot_signal"),
        UniqueConstraint("snapshot_id", name="ux_us_exit_snapshot"),
    )

    decision_id: str = Field(primary_key=True)
    snapshot_id: str = Field(foreign_key="us_holding_signal_snapshot.snapshot_id", index=True)
    lot_id: int = Field(foreign_key="us_position_lot.lot_id", index=True)
    as_of_date: str = Field(index=True)
    action: str = Field(index=True)
    priority: int
    sell_ratio: Decimal = Field(sa_column=Column(Numeric(8, 4)))
    remaining_quantity_before: Decimal = Field(sa_column=Column(Numeric(28, 12)))
    planned_quantity: Decimal = Field(sa_column=Column(Numeric(28, 12)))
    reason_codes: list = Field(default_factory=list, sa_column=Column(JSON))
    signal_hash: str = Field(index=True)
    status: str = Field(default="pending", index=True)
    intended_execution_date: str = Field(index=True)
    evidence_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)
