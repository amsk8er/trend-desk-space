from datetime import datetime
from typing import Optional
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
    commission_rate: float = 0.0
    minimum_commission: float = 0.0
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
