// frontend/src/api.ts
// Typed fetch helpers for every backend endpoint. All calls go through here;
// stages never fetch() directly. Vite proxies /api -> backend in dev.

// ── shared types (mirror backend/api/read.py + routes.py shapes) ──

export type NodeStatus =
  | "todo"
  | "running"
  | "done"
  | "failed"
  | "skipped"
  | string;

export interface BatchSummary {
  batch_id: string;
  date: string;
  status: string;
}

export interface PipelineState {
  // per-node status keyed by node id (import/ocr/.../push). Free-form because
  // the backend writes whatever node keys it currently tracks.
  [nodeId: string]: unknown;
}

export interface State {
  batch_id: string;
  date: string;
  status: string;
  pipeline_state: PipelineState;
}

export interface OcrStats {
  total: number;
  todo: number;
  running: number;
  done: number;
  failed: number;
  skipped: number;
}

export interface OcrJob {
  job_id: number;
  image: string | null;
  image_index: number | null;
  status: NodeStatus;
  model: string | null;
  backend?: string | null;
  partial_reason: string | null;
  reason_friendly: string | null;
  rows: number;
}

export interface OcrData {
  stats: OcrStats;
  jobs: OcrJob[];
}

// Per-job OCR result (raw_json) for the detail panel.
export interface OcrResult {
  job_id: number;
  status: NodeStatus;
  partial_reason: string | null;
  reason_friendly: string | null;
  raw_json: Record<string, unknown>;
}

export interface Row {
  row_id: number;
  job_id: number;
  row_type: string | null;
  market: string | null;
  code: string | null;
  name: string | null;
  sector: string | null;
  sector_status?: string | null;
  temperature: number | null;
  temperature_status: string | null;
  strength: number | null;
  is_etf?: boolean;
  right_side_days: number | null;
  right_side_gain_pct: number | null;
  jieqi: string | null;
  first_hot_date: string | null;
  last_cool_date: string | null;
  review_status: string | null;
  review_reason: string | null;
  raw_fields: Record<string, unknown> | null;
  is_truncated?: boolean;
}

export interface Position {
  position_id: number;
  batch_id: string;
  // 券商持仓页无代码（OCR 禁脑补）→ 可为 null；真实代码由趋势动物持仓温度页按名称回填。
  code: string | null;
  name: string;
  shares: number;
  avg_cost: number;
  current_price: number;
  pnl_pct: number;
  stop_loss: number | null;
  entered_date: string | null;
  source_image: string | null;
  code_source: string | null; // trend_api / trend_api_fuzzy / holding_temp / manual
  confirmed: boolean;
  confirmed_at: string | null;
}

export interface BFilterData {
  white_list: unknown[];
  watch_list: unknown[];
  rejected: unknown[];
  manifest_json: Record<string, unknown>;
}

export interface ExitListItem {
  exit_id: number;
  batch_id: string;
  position_id: number;
  trigger: string;
  action: string;
  reason: string;
  detail?: Record<string, unknown>;
}

// 持仓状态总览：每只持仓一行（含热而正常持有的），让用户区分「判它持有」与「没数据」。
export interface ExitOverviewItem {
  position_id: number;
  code: string | null;
  name: string;
  temperature_status: string | null;
  temp_source: "trend_api" | "holding_temp" | "ocr_row" | null;
  right_side_days: number | null;
  right_side_gain_pct: number | null;
  jieqi: string | null;
  pnl_pct: number;
  shares: number;
  tags?: string[] | null;
  signal_unavailable?: string[] | null;
  suggest: string;
}

// POST /run/exit_check 的返回（逐条提醒 + 全量总览）。
export interface ExitCheckResult {
  items: ExitListItem[];
  overview: ExitOverviewItem[];
}

export interface ReviewData {
  summary: unknown;
  can_proceed: boolean;
  message: string;
}

export interface ChatToolsMeta {
  [name: string]: { needs_confirm: boolean };
}

// Prescreen returns the raw manifest_json object (free-form).
export type PrescreenData = Record<string, unknown>;

// ── swing（重要低点标注页）──
// time 用 "YYYY-MM-DD"，贴 lightweight-charts 的格式。

export interface SwingBar {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface SwingPoint {
  time: string;
  price: number;
}

export interface SwingStop {
  time: string;
  stop: number;
}

export interface SwingData {
  code: string;
  name: string;
  start: string;
  end: string;
  ohlc: SwingBar[];
  important_lows: SwingPoint[];
  minor_lows: SwingPoint[];
  stop_ladder: SwingStop[];
}

export interface SwingParams {
  code: string;
  start?: string;
  end?: string;
  k?: number;
  breakout_pct?: number;
}

// ── 纪律交易闭环 ──
export interface DisciplineEvidence { rule?: string; value?: unknown; passed?: boolean; source?: string; [k: string]: unknown }
export interface DisciplinePlanItem {
  item_id: number; plan_id: string; instrument_id: string; name: string;
  asset_type: "stock" | "etf"; side: "buy" | "sell_all" | "reduce" | "hold" | "manual_review";
  target_weight: number | null; target_shares: number | null; reduce_fraction: number | null;
  priority: number; rule_evidence: Record<string, unknown>; source_dates: Record<string, string>;
  data_sources: Record<string, string>; status: string;
}
export interface DisciplineCandidate {
  code: string; name: string; asset_type: string; eligible: boolean; shadow: boolean;
  price?: number; temperature_prev?: string; temperature_curr?: string; phase?: string;
  strength?: number; strength_change?: string | null; amount_yi?: number;
  tags?: string[] | null;
  float_market_cap_yi?: number; aum_yi?: number;
  right_side_days?: number; capacity_reason?: string;
  selection_rank?: number; selected_rank?: number; replaced_by?: string;
  capacity_limit?: number; allocation_budget?: number; theoretical_lots?: number;
  executable_lots?: number; executable_shares?: number; one_lot_cost?: number;
  budget_shortfall_to_one_lot?: number; estimated_gross?: number;
  estimated_fee?: number | null; estimated_cash_required?: number | null;
  fee_configured?: boolean;
  sector?: string | null; sector_temperature?: string | null;
  evidence?: DisciplineEvidence[]; failed_rules?: DisciplineEvidence[];
}
export interface DisciplinePlanAccount {
  nav: number; cash: number; market_value: number; confirmed: boolean;
  as_of_date?: string; source?: string;
}
export interface DisciplinePlan {
  plan_id: string; signal_date: string; execute_date: string; discipline_version: string;
  rules_hash: string; status: string; market_mode: string; environment_factor: number;
  dataset_id?: string | null; portfolio_snapshot_id?: number | null;
  plan_stage?: "signal" | "executable"; supersedes_plan_id?: string | null;
  change_notice?: string | null;
  account?: DisciplinePlanAccount | null;
  capacity_snapshot: Record<string, unknown>; data_health: {
    lockable: boolean; errors: string[]; warnings: string[]; source_modes: Record<string, string>;
  };
  selection_snapshot: {
    white_list?: DisciplineCandidate[]; watch_list?: DisciplineCandidate[];
    shadow_pool?: DisciplineCandidate[]; rejected?: DisciplineCandidate[];
  };
  items: DisciplinePlanItem[];
}
export interface DailyDatasetStatus {
  dataset_id: string; trade_date: string;
  status: "pending" | "checking" | "waiting_retry" | "fetching" | "ready" |
    "ready_degraded" | "awaiting_budget" | "manual_required" | "failed";
  source_mode: "trend_api" | "ocr_fallback" | "mixed";
  source_status: Record<string, { status?: string; as_of_date?: string; rows?: number; [k: string]: unknown }>;
  source_dates: Record<string, string>; attempt_count: number;
  next_retry_at: string | null; estimated_cost: number; actual_cost: number | null;
  approved_budget: number; error_code: string | null; error_message: string | null;
  capability_flags: { volatility_supported?: boolean; volatility_field?: string | null; [k: string]: unknown };
  cached: boolean; network_calls: number | null; trend_rows: number; market_rows: number;
  can_generate_plan: boolean; before_collection_window?: boolean; server_time_china?: string;
}
export interface DisciplineReview {
  review_id: number; plan_id: string; trade_date: string; plan_completion_rate: number;
  discipline_score: number; trade_result: string; discipline_result: string;
  violations: Record<string, unknown>[]; data_issues: string[]; metrics: Record<string, number>;
}
export interface BrokerImportPreview {
  import_id: number; plan_id: string | null; batch_id?: string | null; filename: string; status: string;
  field_mapping: Record<string, string>; parsed_rows: Record<string, unknown>[];
  anomaly_rows: Record<string, unknown>[];
}

export interface ExecutionOcrJob {
  job_id: string;
  status: "running" | "done" | "error";
  total: number;
  result?: BrokerImportPreview;
  error_code?: string;
  error?: string;
}

export interface LedgerStatus {
  trade_date: string | null;
  snapshot: null | {
    snapshot_id: number; trade_date: string; nav: number; cash: number;
    market_value: number; source: string; reconciliation_status: string;
  };
  confirmation: null | {
    confirmation_id: number; trade_date: string; status: string; source: string;
  };
  positions: { code: string; name: string; asset_type: string; shares: number; avg_cost: number; as_of_date: string }[];
  fee_schedule: {
    commission_rate: number; minimum_commission: number; transfer_fee_rate: number;
    etf_commission_rate: number | null; etf_minimum_commission: number | null;
    stamp_duty_rate: number; safety_multiplier: number; configured: boolean;
  };
  ready_for_roll_forward: boolean;
}

export interface AutomationStatus {
  enabled: boolean; shadow_mode: boolean; timezone: string;
  reminder_time: string; finalize_time: string; late_deadline: string;
  shadow_verified_days: number; shadow_ready_for_live: boolean;
  database: { backend: string; persistent: boolean; revision: string | null };
  email: { configured: boolean; sender: string; recipient: string; provider: string };
  readiness?: {
    trade_date: string; ready: boolean; human_action_required: boolean;
    blockers: { code: string; message: string; action: string; human_required: boolean }[];
    collection_blockers?: { code: string; message: string; action: string; human_required: boolean }[];
    human_blockers?: { code: string; message: string; action: string; human_required: boolean }[];
    collection_summary: {
      status: string; source_mode?: string; warm_to_hot_stock: number;
      warm_to_hot_etf: number; warm_to_hot_total: number;
    };
    account_snapshot_id: number | null; fee_configured: boolean;
  };
  collection?: {
    dataset_id?: string | null; status: string; source_mode?: string;
    warm_to_hot_stock?: number; warm_to_hot_etf?: number; warm_to_hot_total?: number;
    attempt_count: number; next_retry_at: string | null;
    error_code: string | null; error_message: string | null; source_status: Record<string, unknown>;
  };
  scheduler?: {
    scheduler_key: string; enabled: boolean; recorded_enabled: boolean | null;
    window_state: string; heartbeat_threshold_seconds: number;
    heartbeat_age_seconds: number | null; boot_age_seconds: number | null;
    heartbeat_fresh: boolean; fresh_boot: boolean; primary_healthy: boolean;
    last_tick_at: string | null; last_trade_date: string | null;
    last_result: string | null; last_reason: string | null;
    last_dataset_status: string | null; last_attempt_at: string | null;
    next_due_at: string | null; last_trigger: string | null; last_error: string | null;
  };
  latest_run: Record<string, unknown> | null;
  latest_email: Record<string, unknown> | null;
}

export interface DisciplineRules {
  version?: string;
  effective_from?: string;
  source_hash?: string | null;
  selection?: {
    max_entry_phase_exclusive?: string;
    stock?: {
      min_float_market_cap_yi?: number; min_amount_yi?: number;
      max_right_side_days?: number; min_sector_temperature?: string;
      requires_warm_to_hot?: boolean; exclude_warm_to_boiling?: boolean;
    };
    etf?: {
      min_aum_yi?: number; min_amount_yi?: number; min_strength?: number;
      requires_warm_to_hot?: boolean; exclude_warm_to_boiling?: boolean;
      deduplicate_by_benchmark?: boolean; benchmark_tiebreakers?: string[];
    };
  };
  observation?: {
    strength_change?: {
      field?: string; applies_to?: string[]; decision_effect?: string;
      documented_values?: Record<string, string>; unknown_value_policy?: string;
    };
  };
  capacity?: {
    base_new_position_pct?: number;
    environment_factors?: Record<string, number>;
    normal?: { max_new_tools?: number; max_added_weight?: number };
    resonance?: { max_new_tools?: number; max_added_weight?: number };
    max_total_weight?: number; max_tools?: number;
  };
  exit?: {
    full_exit_temperatures?: string[]; profit_signals?: string[];
    fraction_per_signal?: number; round_lot?: number;
    full_exit_priority?: number; reduce_priority?: number; hold_priority?: number;
  };
}

export interface DisciplineVersion {
  version: string; effective_from: string; status: string;
  source_path: string; rules_json: DisciplineRules; rules_hash: string;
  created_at: string;
}

// ── core request helpers ──

const APP_BASE = import.meta.env.BASE_URL.replace(/\/$/, "");
const appUrl = (path: string) => `${APP_BASE}${path}`;

const H6_NEXT_STEPS: Record<string, string> = {
  historical_candidate_read_only: "请返回当前 H6 观察清单重新选择候选",
  historical_plan_read_only: "请只查看历史记录，并从当前 H6 推荐分配生成新清单",
  historical_run_read_only: "请采集当前 H6 数据日后再记录今日不交易",
  legacy_read_only: "H1–H5 只读；请使用当前 H6 运行",
  bitget_quote_stale: "请重新生成 Bitget 风险锚点后再计算推荐分配",
  bitget_candles_unavailable: "请稍后重新生成风险锚点；缺失证据不能绕过",
  bitget_daily_incomplete: "请等待 Bitget 1D 连续性证据完整后重试",
  bitget_hourly_session_missing: "请等待 1H 常规交易时段数据可用后重试",
  bitget_hourly_session_incomplete: "请等待完整的美股常规交易时段后重试",
  risk_anchor_not_confirmed: "该标的暂无已确认 EP3 前期重要低点，保持观察",
  risk_anchor_stale: "请重新刷新 Bitget 报价与风险锚点后再计算分配",
  risk_anchor_not_ready: "先补齐 Bitget 1D、1H 与公开报价证据",
  etf_benchmark_not_verified: "请先补齐 SEC/发行商权威基准证据",
  etf_benchmark_blocked: "请复核或刷新 ETF 基准证据；不得用自由文本代替",
  market_environment_blocked: "等待美股整体温度可用；本日不开新仓",
  exit_data_blocked: "先补齐开放持仓的退出字段，再计算新买入",
  exit_ready_buy_blocked_budget: "退出已检查；买入阶段超过当日费用上限，本日不开新仓",
  allocation_capacity_changed: "当前现金/敷口/日限额已变化，请重新生成推荐分配",
  allocation_preview_stale: "报价、锚点或容量已变化，请重新生成推荐分配",
  h6_shadow_read_only: "完成三个真实数据日 shadow 核验后再切换 active",
  h6_off: "先启用 H6 shadow 采集证据；完成三个真实数据日核验后再切 active",
  structure_stop_retired: "前期重要低点只反推仓位；真实卖出由趋势温度纪律决定",
};

function explicitNextStep(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (!value || typeof value !== "object") return null;
  const record = value as { title?: unknown; detail?: unknown };
  if (typeof record.detail === "string" && record.detail.trim()) return record.detail.trim();
  if (typeof record.title === "string" && record.title.trim()) return record.title.trim();
  return null;
}

export function apiErrorMessage(detail: unknown, status: number, statusText: string): string {
  const envelope = detail && typeof detail === "object" && "detail" in detail
    ? (detail as { detail?: unknown }).detail : detail;
  const reason = envelope && typeof envelope === "object"
    ? (envelope as { message?: unknown }).message : null;
  const rawCode = envelope && typeof envelope === "object"
    ? (envelope as { code?: unknown }).code : null;
  const code = typeof rawCode === "string" ? rawCode : rawCode == null ? null : String(rawCode);
  const rawNext = envelope && typeof envelope === "object"
    ? (envelope as { next_step?: unknown }).next_step : null;
  const rawNextAction = envelope && typeof envelope === "object"
    ? (envelope as { next_action?: unknown }).next_action : null;
  const next = explicitNextStep(rawNextAction) ?? explicitNextStep(rawNext)
    ?? (code ? H6_NEXT_STEPS[code] : null);
  const friendly = typeof reason === "string" ? reason : `${status} ${statusText}`;
  return `${friendly}${code ? `（${code}）` : ""}${next ? `；下一步：${next}` : ""}`;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(appUrl(path), init);
  if (!res.ok) {
    let detail: unknown = null;
    try {
      detail = await res.json();
    } catch {
      try { detail = await res.text(); } catch { /* ignore */ }
    }
    throw new Error(apiErrorMessage(detail, res.status, res.statusText));
  }
  // 204 / empty body guard
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function jsonPost<T>(path: string, body: unknown): Promise<T> {
  return req<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ── GET endpoints ──

export interface AuthStatus {
  required: boolean;
  authenticated: boolean;
}
export const getAuthStatus = () => req<AuthStatus>("/api/auth/status");
export const loginWithAccessKey = (accessKey: string) =>
  jsonPost<{ ok: boolean }>("/api/auth/login", { access_key: accessKey });
export const logout = () => jsonPost<{ ok: boolean }>("/api/auth/logout", {});

export const getState = (batchId: string) => req<State>(`/api/state/${batchId}`);

export const getDisciplineVersion = () => req<DisciplineVersion>(`/api/discipline/version`);
export const getDisciplinePlans = () => req<DisciplinePlan[]>(`/api/discipline/plans`);
export const getDisciplinePlan = (planId: string) => req<DisciplinePlan>(`/api/discipline/plan/${planId}`);
export const lockDisciplinePlan = (planId: string) => jsonPost<DisciplinePlan>(`/api/discipline/plan/${planId}/lock`, {});
export const getDisciplineReview = (planId: string) => req<DisciplineReview>(`/api/discipline/review/${planId}`);
export const generateDisciplineReview = (planId: string) => jsonPost<DisciplineReview>(`/api/discipline/review/${planId}`, {});
export const getDisciplineDataProbe = () => req<Record<string, unknown>>(`/api/discipline/data/probe`);
export const getDisciplineTodayData = () => req<DailyDatasetStatus>(`/api/discipline/data/today`);
export const checkDisciplineTodayData = () => jsonPost<DailyDatasetStatus>(`/api/discipline/data/today/check`, {});
export const approveDisciplineBudget = (tradeDate: string, amount: number) =>
  jsonPost<DailyDatasetStatus>(`/api/discipline/data/${tradeDate}/budget-approval`, { amount });
export const getDisciplineTodayPlan = () => req<DisciplinePlan>(`/api/discipline/plans/today`);

// ── 美股手工执行台（只读趋势/公开报价 + 本地人工成交台账） ──
// Decimal 金额和散股数量在 API 中始终是字符串，前端只展示或原样回传。
export interface UsManualCapabilities {
  enabled: boolean;
  manual_only: boolean;
  automated_trading: false;
  automated_collection: boolean;
  scheduler_enabled: boolean;
  private_bitget_access: false;
  bitget_public_market_only: true;
  order_api_enabled: false;
  etf_execution_enabled: boolean;
  etf_mode: "observation_only" | "trade_pool";
  notice: string;
  rules_version: string;
  h6_mode: "off" | "shadow" | "active";
  risk_anchor_source: "bitget_public_1d_1h_quote";
  risk_anchor_is_exit_stop: false;
  real_exit_source: "trend_animals_temperature_danger_boiling_champagne";
  wind_required: false;
  max_distinct_tickers: number;
}
export interface UsUniverseArchive {
  archive_id: string;
  trend_animals_as_of_date: string;
  stock_count: number;
  etf_observation_count: number;
  seed_sha256: string;
  intersection_sha256: string;
  metadata_json: Record<string, unknown>;
}
export interface UsUniverseStatus {
  archive: UsUniverseArchive;
  trend_animals: {
    membership_archive_date: string;
    seed_instrument_count: number;
    seed_sha256: string;
    coverage_manifest_path: string | null;
  };
  bitget_public_intersection: {
    stock_count: number;
    etf_observation_count: number;
    intersection_sha256: string;
    pool_path: string;
    retrieval_date: string;
  };
  etf_observation: { enabled: boolean; execution_enabled: boolean; count: number; notice: string };
  notice: string;
}
export interface UsCandidate {
  candidate_id: number;
  run_id: string;
  tm_id: number;
  ticker_symbol: string;
  ticker_name: string | null;
  asset_type: "stock" | "etf" | "etf_observation";
  venue_instrument: string | null;
  venue_metadata_json: Record<string, unknown>;
  temperature_prev: string | null;
  temperature_curr: string | null;
  right_side_calendar_days: number | null;
  right_side_age_bucket: string | null;
  warm_to_hot: boolean;
  gate_passed: boolean;
  strength_local: string | null;
  industry_tm_id: number | null;
  industry_name: string | null;
  industry_temperature_curr: string | null;
  industry_strength_local: string | null;
  ticker_labels: string[];
  trend_phase_curr: string | null;
  market_cap: string | null;
  amount_1d: string | null;
  price_index: string | null;
  reference_price_usdt: string | null;
  reference_price_at: string | null;
  reference_price_source: string | null;
  quote_status: "not_requested" | "available" | "quote_unavailable" | string;
  screen_status: string;
  primary_reason: string | null;
  all_reasons: string[];
  rank: number | null;
  observation_rank: number | null;
  raw_fields: Record<string, unknown>;
  environment_id: string | null;
  etf_identity_id: string | null;
  etf_benchmark_evidence_id: string | null;
  benchmark_family_id: string | null;
  exposure_key: string | null;
  benchmark_status: string;
  etf_benchmark_evidence: UsEtfBenchmarkEvidence | null;
  risk_anchor_status: UsRiskAnchorStatus;
  risk_anchor: UsRiskAnchor | null;
  legacy_read_only: boolean;
  /** H5 archive fields: current H6 code never mutates them. */
  stop_status?: UsStopStatus;
  stop_suggestion?: UsStopSuggestion | null;
}
export type UsRiskAnchorStatus = "pending" | "ready" | "blocked";
export interface UsRiskAnchor {
  anchor_id: string;
  candidate_id: number;
  run_id: string;
  status: "ready" | "blocked";
  signal_date: string;
  algorithm_version: string;
  bitget_symbol: string;
  quote_usdt: string | null;
  quote_at: string | null;
  anchor_date: string | null;
  anchor_price_usdt: string | null;
  anchor_distance: string | null;
  price_tick_usdt: string | null;
  daily_sha256: string | null;
  hourly_sha256: string | null;
  error_code: string | null;
  error_message: string | null;
  semantic: string;
  planning_enabled: boolean;
  source_mode: "off" | "shadow" | "active";
  evidence_json?: Record<string, unknown>;
}
export type UsStopStatus = "pending" | "auto_ready" | "review_required" | "manual_resolved" | "blocked";
export interface UsStopReview {
  review_id: string;
  suggestion_id: string;
  resolution: "wind_mapped" | "bitget_anchor" | "custom";
  custom_price_usdt: string | null;
  final_stop_usdt: string;
  final_source: string;
  reason: string;
  created_at: string;
}
export interface UsStopSuggestion {
  suggestion_id: string;
  candidate_id: number;
  run_id: string;
  status: "auto_ready" | "review_required" | "blocked";
  effective_status: Exclude<UsStopStatus, "pending">;
  source_mode: "shadow" | "active";
  signal_date: string;
  algorithm_version: string;
  wind_symbol: string | null;
  bitget_symbol: string;
  anchor_type: "signal_day_low" | "confirmed_swing_low" | null;
  anchor_date: string | null;
  wind_anchor_low_usd: string | null;
  wind_signal_close_usd: string | null;
  wind_ratio: string | null;
  wind_mapped_stop_usdt: string | null;
  bitget_anchor_low_usdt: string | null;
  bitget_quote_usdt: string | null;
  bitget_quote_at: string | null;
  deviation_ratio: string | null;
  deviation_threshold: string;
  price_tick_usdt: string | null;
  suggested_stop_usdt: string | null;
  wind_response_sha256: string | null;
  bitget_daily_sha256: string | null;
  bitget_hourly_sha256: string | null;
  final_stop_usdt: string | null;
  final_source: string | null;
  error_code: string | null;
  error_message: string | null;
  planning_enabled: boolean;
  review: UsStopReview | null;
  evidence_json?: Record<string, unknown>;
}
export interface UsDailyRun {
  run_id: string;
  as_of_date: string;
  status: string;
  rules_version: string;
  universe_count: number;
  returned_count: number;
  estimated_base_cost_cny: string | null;
  approved_base_budget_cny: string | null;
  actual_base_cost_cny: string | null;
  estimated_enrichment_cost_cny: string | null;
  approved_enrichment_budget_cny: string | null;
  actual_enrichment_cost_cny: string | null;
  estimated_total_cost_cny: string | null;
  actual_total_cost_cny: string | null;
  daily_cost_cap_cny: string | null;
  cost_breakdown_json: Record<string, string>;
  base_fields?: string[];
  enrichment_fields?: string[];
  cache_hit: boolean;
  trigger?: string | null;
  attempt_count?: number;
  next_retry_at?: string | null;
  exit_status?: string | null;
  environment_status?: string | null;
  etf_mapping_status?: string | null;
  market_environment?: UsMarketEnvironment | null;
  legacy_read_only?: boolean;
  funnel: {
    warm_to_hot_stocks?: number;
    warm_to_hot_etfs?: number;
    bitget_stock_intersection?: number;
    bitget_etf_intersection?: number;
    right_side_within_window?: number | null;
    sector_temperature_warm_plus?: number | null;
    etf_signal_gate_passed?: number | null;
    quality_complete?: number | null;
    relative_strength_90_plus?: number | null;
    ready_for_plan?: number;
    etf_benchmark_verified?: number;
    etf_benchmark_blocked?: number;
    risk_anchor_pending?: number;
    risk_anchor_ready?: number;
    risk_anchor_blocked?: number;
    stop_pending?: number;
    stop_auto_ready?: number;
    stop_review_required?: number;
    stop_manual_resolved?: number;
    stop_blocked?: number;
    screen_status_counts?: Record<string, number>;
    right_side_age_buckets?: Record<string, number>;
    [key: string]: unknown;
  };
  candidates?: UsCandidate[];
  scan_plan?: {
    estimated_cost_cny: number;
    requested_fields: string[];
    batches: Array<{ row_count: number; estimated_cost_cny: number }>;
    membership_as_of_dates?: string[];
  };
  [key: string]: unknown;
}
export interface UsMarketEnvironment {
  environment_id: string;
  run_id: string;
  as_of_date: string;
  market_tm_id: number;
  market_temperature: string;
  environment_factor: string;
  status: string;
  contract_hash: string;
  created_at: string;
  market_strength_local?: string | number | null;
  market_phase?: string | null;
  market_labels?: string[];
  days_since_trend_entry?: number | null;
}
export interface UsAccountState {
  starting_equity_usdt: string;
  reported_equity_usdt?: string | null;
  cash_usdt: string;
  ledger_cash_usdt?: string;
  open_cost_usdt: string;
  open_risk_usdt?: string;
  position_count: number;
  execution_count?: number;
  account_source?: string;
  ocr_snapshot_id?: number | null;
  ocr_snapshot_as_of_date?: string | null;
  ocr_snapshot_stale?: boolean;
  reconciliation_status?: string;
  reconciliations?: Array<Record<string, unknown>>;
  open_positions: Array<{
    ticker_symbol: string;
    ticker_name?: string | null;
    quantity: string;
    observed_quantity?: string | null;
    cost_usdt?: string | null;
    source?: string;
    reconciliation_status?: string;
  }>;
}
export interface UsCapacity {
  daily_limit_usdt: string;
  daily_remaining_usdt: string;
  portfolio_remaining_usdt: string;
  cash_remaining_usdt: string;
  available_usdt: string;
  open_ticker_count: number;
  occupied_ticker_count: number;
  available_ticker_slots: number;
  reservations: {
    all_locked_unfilled_usdt: string;
    session_locked_unfilled_usdt: string;
    session_confirmed_usdt: string;
    locked_buy_tickers: string[];
  };
  account: UsAccountState;
}
export interface UsAccountOcrRow {
  row_id: number;
  ticker_symbol: string | null;
  ticker_name: string | null;
  venue_instrument: string | null;
  quantity: string | null;
  average_cost_usdt: string | null;
  current_price_usdt: string | null;
  market_value_usdt: string | null;
  unrealized_pnl_usdt: string | null;
  source_image: string;
  status: "ready" | "review_required";
  errors: string[];
}
export interface UsAccountOcrBatch {
  batch_id: string;
  capture_date: string;
  provider: string | null;
  status: "running" | "ready" | "failed" | "confirmed";
  image_count: number;
  processed_image_count: number;
  failed_image_count: number;
  account: { equity_usdt: string | null; cash_usdt: string | null; currency: string | null };
  source_images: string[];
  conflicts: Array<Record<string, unknown>>;
  error: { code: string; message: string } | null;
  confirmed_snapshot_id: number | null;
  confirmed_at: string | null;
  created_at: string;
  updated_at: string;
  rows: UsAccountOcrRow[];
  notice: string;
}
export interface UsPositionAction {
  lot: {
    lot_id: number;
    ticker_symbol: string;
    ticker_name: string | null;
    remaining_quantity: string;
    average_cost_usdt: string;
    asset_type?: string;
    rules_version?: string | null;
    holding_trading_days: number;
  };
  decision?: UsExitDecision;
  as_of_date: string | null;
  action: "exit_all" | "reduce_25" | "reduce_50" | "manual_review" | "hold" | "legacy_read_only";
  reason: string;
  priority: number;
  legacy_read_only?: boolean;
  notice: string;
}
export interface UsExecutionSchedule {
  calendar: "XNYS";
  signal_date: string;
  session_date: string;
  generated_at_utc: string | null;
  generated_at_beijing: string | null;
  open_utc: string;
  open_new_york: string;
  open_beijing: string;
}
export interface UsExitDecision {
  decision_id: string;
  snapshot_id: string;
  lot_id: number;
  as_of_date: string;
  action: "exit_all" | "reduce_25" | "reduce_50" | "manual_review" | "hold";
  priority: number;
  sell_ratio: string;
  remaining_quantity_before: string;
  planned_quantity: string;
  reason_codes: string[];
  status: string;
  intended_execution_date: string;
  execution_schedule?: UsExecutionSchedule;
  evidence_json: Record<string, unknown>;
}
export interface UsManualPlanItem {
  item_id: number;
  candidate_id: number | null;
  side: "buy" | "sell" | "hold";
  ticker_symbol: string;
  ticker_name: string | null;
  asset_type: string;
  venue_instrument: string;
  entry_reference_price: string | null;
  entry_reference_at?: string | null;
  entry_reference_source?: string | null;
  allocation_preview_item_id: number | null;
  risk_anchor_id: string | null;
  environment_id: string | null;
  exit_decision_id: string | null;
  risk_anchor_price: string | null;
  risk_anchor_date: string | null;
  anchor_loss_estimate_usdt: string | null;
  stop_evidence_json: Record<string, unknown>;
  target_notional_usdt: string | null;
  target_quantity: string | null;
  reason_json: Record<string, unknown>;
  status: string;
  /** H1-H5 archive fields, never used as H6 exit authority. */
  stop_price?: string | null;
  stop_source?: string | null;
  stop_suggestion_id?: string | null;
  stop_review_id?: string | null;
  sizing_preview_id?: string | null;
  stop_anchor_type?: string | null;
  stop_anchor_date?: string | null;
  estimated_max_loss_usdt?: string | null;
}
export interface UsManualPlan {
  plan_id: string;
  signal_date: string;
  intended_execution_date: string;
  execution_schedule?: UsExecutionSchedule;
  run_id: string;
  status: string;
  rules_version: string;
  allocation_preview_id: string | null;
  legacy_read_only: boolean;
  reservation_active: boolean;
  data_health_json: { lockable?: boolean; [key: string]: unknown };
  notes: string | null;
  items: UsManualPlanItem[];
  manual_only_notice: string;
}
export interface UsSizingPreview {
  sizing_preview_id: string;
  candidate_id: number;
  stop_suggestion_id: string;
  stop_review_id: string | null;
  stop_source: string;
  verdict: "manual_review" | "watch";
  reason: string;
  message?: string;
  entry_price: string;
  stop_price: string;
  stop_distance?: string;
  stop_distance_pct?: string;
  risk_budget_usdt?: string;
  planned_notional_usdt?: string;
  planned_quantity?: string;
  estimated_max_loss_usdt?: string;
  [key: string]: unknown;
}
export interface UsAllocationPreviewItem {
  allocation_item_id: number;
  allocation_preview_id: string;
  candidate_id: number;
  risk_anchor_id: string | null;
  priority: number;
  status: "allocated" | "skipped" | "excluded";
  reason_code: string | null;
  reference_price_usdt: string | null;
  anchor_price_usdt: string | null;
  anchor_distance: string | null;
  risk_ceiling_usdt: string | null;
  target_notional_usdt: string | null;
  allocated_notional_usdt: string | null;
  target_quantity: string | null;
  anchor_loss_estimate_usdt: string | null;
  minimum_notional_usdt: string | null;
  evidence_json: Record<string, unknown>;
  candidate: UsCandidate;
}
export interface UsAllocationPreview {
  allocation_preview_id: string;
  run_id: string;
  environment_id: string;
  intended_execution_date: string;
  execution_schedule?: UsExecutionSchedule;
  status: string;
  daily_limit_usdt: string;
  daily_remaining_usdt: string;
  portfolio_remaining_usdt: string;
  cash_remaining_usdt: string;
  available_usdt: string;
  allocated_usdt: string;
  open_ticker_count: number;
  available_ticker_slots: number;
  exclusions_json: Array<{ candidate_id: number; reason: string }>;
  backfill_requested: boolean;
  snapshot_json: Record<string, unknown>;
  items: UsAllocationPreviewItem[];
  notice: string;
  created_at: string;
}
export interface UsManualOverview {
  capabilities: UsManualCapabilities;
  notice: string;
  state: string;
  universe?: UsUniverseStatus | null;
  run: UsDailyRun | null;
  candidates: UsCandidate[];
  positions: UsPositionAction[];
  exit_actions: { actionable: number; manual_review: number; legacy_read_only: number };
  market_environment: UsMarketEnvironment | null;
  capacity: UsCapacity | null;
  account: UsAccountState;
  latest_account_ocr: UsAccountOcrBatch | null;
  policy: {
    account_usdt: string;
    risk_budget_usdt_per_trade: string;
    min_single_notional_usdt: string;
    target_single_notional_usdt: string;
    max_open_positions: number;
    environment_factors: Record<string, string>;
    [key: string]: unknown;
  };
  next_step: { code: string; title: string; detail: string };
  last_attempt?: {
    run_id: string; trigger: string | null; attempt_count: number;
    created_at: string | null; completed_at: string | null; next_retry_at: string | null;
  } | null;
  data_update?: {
    as_of_date: string; us_as_of_date?: string | null; us_etf_as_of_date?: string | null;
  } | null;
  schedule: {
    timezone: string; manual_full_collection_after: string; automatic_slots: string[];
    automatic_cutoff: string; scheduler_enabled: boolean; now_local: string;
  };
  cost: {
    daily_cap_cny: string | null; estimated_total_cny: string | null;
    actual_total_cny: string | null; current_run_estimated_cny?: string | null;
    current_run_actual_cny?: string | null; breakdown: Record<string, string>;
  };
  combos: {
    warm_to_hot_stocks: number; warm_to_hot_etfs: number;
    bitget_stock_intersection: number; bitget_etf_intersection: number;
  };
  quote_status: { available: number; unavailable: number };
  risk_anchor_status: Record<UsRiskAnchorStatus, number>;
  etf_benchmark_status: { verified: number; blocked: number };
  legacy: Record<string, unknown> & { read_only: true };
  error?: { code: string; message: string };
}
export type UsCollectResult = UsDailyRun | {
  run_id: null; status: string; state: string; as_of_date: string | null;
  next_retry_at?: string | null; paid_calls?: number;
};
export const getUsManualCapabilities = () => req<UsManualCapabilities>("/api/us-manual/capabilities");
export const getUsManualOverview = () => req<UsManualOverview>("/api/us-manual/overview");
export async function importUsAccountScreenshots(
  captureDate: string, files: File[], backend?: string,
): Promise<UsAccountOcrBatch> {
  const body = new FormData();
  body.append("capture_date", captureDate);
  if (backend) body.append("backend", backend);
  files.forEach(file => body.append("files", file));
  return req<UsAccountOcrBatch>("/api/us-manual/account/ocr", { method: "POST", body });
}
export const getUsAccountOcr = (batchId: string) =>
  req<UsAccountOcrBatch>(`/api/us-manual/account/ocr/${encodeURIComponent(batchId)}`);
export const getLatestUsAccountOcr = () =>
  req<UsAccountOcrBatch | null>("/api/us-manual/account/ocr/latest");
export const confirmUsAccountOcr = (batchId: string, payload: {
  equity_usdt: string;
  cash_usdt: string;
  currency: string;
  full_snapshot_confirmed: boolean;
  confirmed_no_positions?: boolean;
  idempotency_key: string;
}) => jsonPost<UsAccountOcrBatch>(
  `/api/us-manual/account/ocr/${encodeURIComponent(batchId)}/confirm`, payload,
);
export const getUsManualUniverse = () => req<UsUniverseStatus>("/api/us-manual/universe/latest");
export const collectUsManualRun = () => jsonPost<UsCollectResult>("/api/us-manual/runs/collect", {});
export const preflightUsManualRun = () => jsonPost<UsDailyRun>("/api/us-manual/runs/preflight", {});
export const scanUsManualRun = (runId: string, approvedBudgetCny: string) =>
  jsonPost<UsDailyRun>(`/api/us-manual/runs/${encodeURIComponent(runId)}/scan`, { approved_budget_cny: approvedBudgetCny });
export const preflightUsManualEnrichment = (runId: string) =>
  jsonPost<UsDailyRun>(`/api/us-manual/runs/${encodeURIComponent(runId)}/enrichment/preflight`, {});
export const enrichUsManualRun = (runId: string, approvedBudgetCny: string) =>
  jsonPost<UsDailyRun>(`/api/us-manual/runs/${encodeURIComponent(runId)}/enrichment`, { approved_budget_cny: approvedBudgetCny });
/** @deprecated H5 endpoint kept only so archived callers receive the backend 410. */
export const previewUsSizing = (payload: {
  candidate_id: number; stop_suggestion_id: string;
}) => jsonPost<UsSizingPreview>("/api/us-manual/sizing/preview", payload);
export const getUsPublicQuote = (venueInstrument: string) =>
  req<{ reference_price: string; quoted_at: string; source: string }>(`/api/us-manual/quotes/${encodeURIComponent(venueInstrument)}`);
export const createUsManualPlan = (payload: {
  allocation_preview_id: string; notes?: string;
}) => jsonPost<UsManualPlan>("/api/us-manual/plans", payload);
export const createUsAllocationPreview = (payload: {
  run_id: string;
  exclusions?: Array<{ candidate_id: number; reason: string }>;
  backfill?: boolean;
  base_preview_id?: string;
}) => jsonPost<UsAllocationPreview>("/api/us-manual/allocation-previews", payload);
export const getUsAllocationPreview = (previewId: string) =>
  req<UsAllocationPreview>(`/api/us-manual/allocation-previews/${encodeURIComponent(previewId)}`);
export const refreshUsRiskAnchor = (candidateId: number) =>
  jsonPost<UsRiskAnchor>(`/api/us-manual/candidates/${candidateId}/risk-anchor/refresh`, {});
export const getUsRiskAnchor = (anchorId: string) =>
  req<UsRiskAnchor>(`/api/us-manual/risk-anchors/${encodeURIComponent(anchorId)}`);
/** @deprecated H5 stop evidence is read-only. */
export const refreshUsStopSuggestion = (candidateId: number) =>
  jsonPost<UsStopSuggestion>(`/api/us-manual/candidates/${candidateId}/stop-suggestion/refresh`, {});
export const getUsStopSuggestion = (suggestionId: string) =>
  req<UsStopSuggestion>(`/api/us-manual/stop-suggestions/${encodeURIComponent(suggestionId)}`);
export const reviewUsStopSuggestion = (suggestionId: string, payload: {
  resolution: "wind_mapped" | "bitget_anchor" | "custom";
  custom_price_usdt?: string;
  reason: string;
  idempotency_key: string;
}) => jsonPost<UsStopSuggestion>(
  `/api/us-manual/stop-suggestions/${encodeURIComponent(suggestionId)}/review`, payload,
);
export const getUsManualPlans = () => req<UsManualPlan[]>("/api/us-manual/plans");
export const lockUsManualPlan = (planId: string) => jsonPost<UsManualPlan>(`/api/us-manual/plans/${encodeURIComponent(planId)}/lock`, {});
export const markUsPlanNoExecution = (planId: string, note?: string) =>
  jsonPost<UsManualPlan>(`/api/us-manual/plans/${encodeURIComponent(planId)}/no-execution`, { note });
export const markUsRunNoExecution = (runId: string, note?: string) =>
  jsonPost<UsManualPlan>(`/api/us-manual/runs/${encodeURIComponent(runId)}/no-execution`, { note });
export const previewUsExecution = (itemId: number, payload: { price_usdt: string; quantity: string; fee_usdt?: string; trade_date: string; executed_at: string }) =>
  jsonPost<Record<string, unknown>>(`/api/us-manual/plan-items/${itemId}/executions/preview`, payload);
export const confirmUsExecution = (itemId: number, payload: { price_usdt: string; quantity: string; fee_usdt?: string; trade_date: string; executed_at: string; idempotency_key: string; note?: string }) =>
  jsonPost<Record<string, unknown>>(`/api/us-manual/plan-items/${itemId}/executions/confirm`, payload);
export const getUsPositions = () => req<UsPositionAction[]>("/api/us-manual/positions");
export const getUsExitDecisions = () => req<UsExitDecision[]>("/api/us-manual/exit-decisions");
export const createUsExitDecisionPlan = (decisionId: string) =>
  jsonPost<UsManualPlan>(`/api/us-manual/exit-decisions/${encodeURIComponent(decisionId)}/plan`, {});
export const markUsStopTriggered = (lotId: number) => jsonPost<Record<string, unknown>>(`/api/us-manual/positions/${lotId}/stop-triggered`, {});
export const createUsExitPlan = (lotId: number, asOfDate?: string | null) =>
  jsonPost<UsManualPlan>(`/api/us-manual/positions/${lotId}/exit-plan`, { as_of_date: asOfDate ?? null });
export interface UsEtfBenchmarkEvidence {
  evidence_id: string;
  ticker_symbol: string;
  status: string;
  benchmark_family_id: string | null;
  benchmark_name_raw: string | null;
  benchmark_canonical_name?: string | null;
  benchmark_provider?: string | null;
  benchmark_ticker?: string | null;
  strategy_type: string | null;
  exposure_direction: string | null;
  leverage_multiplier: string | null;
  currency_hedge: string | null;
  exposure_key: string | null;
  source_type: string;
  source_url: string;
  filing_accession_no?: string | null;
  source_effective_date?: string | null;
  retrieved_at: string;
  expires_at: string | null;
  fingerprint: Record<string, string | null>;
  identity: Record<string, unknown>;
  evidence_json?: Record<string, unknown>;
}
export const getUsEtfBenchmarks = () => req<UsEtfBenchmarkEvidence[]>("/api/us-manual/etf-benchmarks");
export const refreshUsEtfBenchmark = (tickerSymbol: string) =>
  jsonPost<UsEtfBenchmarkEvidence>(`/api/us-manual/etf-benchmarks/${encodeURIComponent(tickerSymbol)}/refresh`, {});
export async function importDisciplinePositions(
  tradeDate: string, files: File[], backend?: string,
): Promise<ScanTrigger> {
  const body = new FormData(); body.append("trade_date", tradeDate);
  if (backend) body.append("backend", backend);
  files.forEach(file => body.append("files", file));
  return req<ScanTrigger>(`/api/discipline/positions/import`, { method: "POST", body });
}
export const getDisciplinePositionImportStatus = (batchId: string) =>
  req<ScanStatus>(`/api/discipline/positions/import/status?batch_id=${encodeURIComponent(batchId)}`);
export interface ConfirmPositionsResult {
  batch_id: string;
  confirmed: number;
  position_lots_created?: number;
  portfolio_snapshot_id?: number;
  plan?: DisciplinePlan | null;
  message?: string | null;
  batch_trade_date?: string;
  signal_trade_date?: string;
  confirmed_at?: string;
}
export const confirmDisciplinePositions = (
  batchId: string, positionIds: number[] | null, nav: number, cash: number,
) => jsonPost<ConfirmPositionsResult>(
  `/api/discipline/positions/${batchId}/confirm`,
  { position_ids: positionIds, nav, cash },
);
export const previewDisciplineOcrFallback = (tradeDate: string, batchId: string) =>
  jsonPost<Record<string, unknown>>(`/api/discipline/data/${tradeDate}/ocr-fallback/preview`, { batch_id: batchId });
export const confirmDisciplineOcrFallback = (tradeDate: string, batchId: string) =>
  jsonPost<DailyDatasetStatus>(`/api/discipline/data/${tradeDate}/ocr-fallback/confirm`, { batch_id: batchId });
export async function previewBrokerImport(planId: string, file: File): Promise<BrokerImportPreview> {
  const body = new FormData(); body.append("file", file);
  return req<BrokerImportPreview>(`/api/discipline/broker/import/preview?plan_id=${encodeURIComponent(planId)}`, { method: "POST", body });
}
export const confirmBrokerImport = (importId: number) =>
  jsonPost<{ import: BrokerImportPreview; executions: Record<string, unknown>[] }>(`/api/discipline/broker/import/${importId}/confirm`, {});
export async function previewExecutionScreenshots(
  tradeDate: string, files: File[], backend?: string,
): Promise<ExecutionOcrJob> {
  const body = new FormData(); body.append("trade_date", tradeDate);
  if (backend) body.append("backend", backend);
  files.forEach(file => body.append("files", file));
  return req<ExecutionOcrJob>("/api/discipline/executions/ocr/preview", { method: "POST", body });
}
export const getExecutionOcrStatus = (jobId: string) =>
  req<ExecutionOcrJob>(`/api/discipline/executions/ocr/status?job_id=${encodeURIComponent(jobId)}`);
export const confirmExecutionOcr = (batchId: string, acceptValidRowsOnly: boolean) =>
  jsonPost<{ import: BrokerImportPreview; executions: Record<string, unknown>[] }>(
    `/api/discipline/executions/ocr/${batchId}/confirm`,
    { confirmed: true, accept_valid_rows_only: acceptValidRowsOnly },
  );
export const confirmNoExecution = (tradeDate: string) =>
  jsonPost<Record<string, unknown>>(`/api/discipline/trading-days/${tradeDate}/no-execution`, { confirmed: true });
export const getLedgerStatus = (tradeDate?: string) =>
  req<LedgerStatus>(`/api/discipline/ledger/status${tradeDate ? `?trade_date=${encodeURIComponent(tradeDate)}` : ""}`);
export const rollForwardLedger = (tradeDate: string) =>
  jsonPost<Record<string, unknown>>(`/api/discipline/ledger/${tradeDate}/roll-forward`, {});
export const updateFeeSchedule = (payload: LedgerStatus["fee_schedule"]) =>
  jsonPost<LedgerStatus["fee_schedule"]>("/api/discipline/ledger/fee-schedule", payload);
export const getAutomationStatus = () => req<AutomationStatus>("/api/automation/status");
export const runAutomationNow = (stage = "finalize") =>
  jsonPost<Record<string, unknown>>("/api/automation/run-now", { stage });
export const sendAutomationTestEmail = () =>
  jsonPost<Record<string, unknown>>("/api/automation/send-test", {});

export const getBatches = () => req<BatchSummary[]>(`/api/batches`);

export const getOcr = (batchId: string) => req<OcrData>(`/api/ocr/${batchId}`);

// Per-job raw OCR result (for the detail panel: view / download JSON).
export const getOcrResult = (jobId: number) =>
  req<OcrResult>(`/api/ocr/result/${jobId}`);

// Direct URL for the screenshot file (used as <img src> + download link).
export const ocrImageUrl = (jobId: number) => `/api/ocr/image/${jobId}`;

export const getRows = (batchId: string) => req<Row[]>(`/api/rows/${batchId}`);

export interface AggregateRow {
  row_id: number;
  code: string | null;
  name: string | null;
  sector: string | null;
  market: string | null;
  row_type: string | null;
  temperature_status: string | null;
  strength_a_share: number | null;   // A股内排名
  strength_intraday: number | null;  // 温转热页内排名
  right_side_days: number | null;
  right_side_gain_pct: number | null;
  jieqi: string | null;
  tags: string[] | null;
  price: number | null;
  market_cap_yi: number | null;
  turnover_yi: number | null;
  review_status: string | null;
  raw_fields: Record<string, unknown> | null;
}
export interface AggregateCategory {
  category: string;
  row_count_before_dedup: number;
  row_count_after_dedup: number;
  dropped_truncated: number;
  rows: AggregateRow[];
}
export interface AggregateData {
  batch_id: string;
  categories: AggregateCategory[];
}
export const runAggregate = (batchId: string) =>
  jsonPost<AggregateData>(`/api/run/aggregate`, { batch_id: batchId });

// 按类别聚合 + 按 code 去重（trend-desk 版 by_market）
export const getAggregate = (batchId: string) =>
  req<AggregateData>(`/api/aggregate/${batchId}`);

export const getPrescreen = (batchId: string) =>
  req<PrescreenData>(`/api/prescreen/${batchId}`);

export const getPositions = (batchId: string) =>
  req<Position[]>(`/api/positions/${batchId}`);
export const confirmOcrPositions = (batchId: string, positionIds?: number[]) =>
  jsonPost<{ batch_id: string; confirmed: number; position_lots_created: number }>(
    `/api/discipline/positions/${batchId}/confirm`, { position_ids: positionIds ?? null },
  );

export interface HoldingTempLite {
  holding_id: number;
  code: string | null;
  name: string;
  temperature_status: string | null;
  market: string | null;
  right_side_days?: number | null;
  right_side_gain_pct?: number | null;
  jieqi?: string | null;
  strength?: number | null;
  tags?: string[] | null;
  signal_unavailable?: string[] | null;
  data_source?: "ocr" | "trend_api" | null;
  as_of_date?: string | null;
  [k: string]: unknown;
}
export const getHoldingTemps = (batchId: string) =>
  req<HoldingTempLite[]>(`/api/holding_temp/${batchId}`);
export const pairPosition = (batchId: string, positionId: number, code: string) =>
  jsonPost<{ ok: boolean; code?: string; note?: string }>(`/api/positions/pair`, {
    batch_id: batchId,
    position_id: positionId,
    code,
  });

export const getBFilter = (batchId: string) =>
  req<BFilterData>(`/api/b_filter/${batchId}`);

export const getExitCheck = (batchId: string) =>
  req<ExitCheckResult>(`/api/exit_check/${batchId}`);

// 重要低点：输代码+区间 → 东财前复权日线 → detect() → 标注 JSON。
export const getSwing = (p: SwingParams) => {
  const q = new URLSearchParams({ code: p.code });
  if (p.start) q.set("start", p.start);
  if (p.end) q.set("end", p.end);
  if (p.k != null) q.set("k", String(p.k));
  if (p.breakout_pct != null) q.set("breakout_pct", String(p.breakout_pct));
  return req<SwingData>(`/api/swing?${q.toString()}`);
};

export const getReview = (batchId: string) =>
  req<ReviewData>(`/api/review/${batchId}`);

export const getChatTools = () => req<ChatToolsMeta>(`/api/chat/tools`);

export interface ConfigPaths {
  import_dir: string;
  pos_dir: string;
  archive_dir: string;
}
export const getConfigPaths = () => req<ConfigPaths>(`/api/config/paths`);

export interface LlmProviderInfo {
  label: string;
  vision: boolean;
  configured: boolean;
  detail: string;
}

// Which LLM backend OCR runs on: {backend} = server default; {choices} = selectable set.
export const getLlmConfig = () =>
  req<{ backend: string; choices: string[]; providers: Record<string, LlmProviderInfo> }>(`/api/config/llm`);

// ── 趋势动物官方 API：持仓同步 + 温转热选股流水线 ──
export interface TrendAnimalsConfig {
  enabled: boolean;
  configured: boolean;
  default_budget: number;
  selection_budget: number;
  ocr_fallback_available: boolean;
}
export interface TrendHoldingEstimate {
  ok: boolean;
  as_of_dates: Record<string, string>;
  tm_count: number;
  fields: string[];
  estimated_cost: number;
}
export interface TrendHoldingSyncResult {
  ok: boolean;
  source: "trend_api";
  as_of_date: string;
  rows: number;
  backfilled: number;
  incomplete_rows: unknown[];
  estimated_cost: number;
  actual_cost: number | null;
  cached: boolean;
}
export interface TrendSelectionEstimate {
  ok: boolean;
  as_of_date: string;
  counts: Record<string, number>;
  estimated_cost: number;
  estimate_breakdown: Record<string, number>;
  note: string;
}
export interface TrendSelectionResult {
  ok: boolean;
  batch_id: string;
  as_of_date: string;
  estimated_cost: number;
  actual_cost: number | null;
  component_counts: Record<string, number>;
  basic_component_counts: Record<string, number>;
  component_count_warnings: Array<{
    combo: string;
    constituent_count: number;
    returned_basic_count: number;
    note: string;
  }>;
  unique_components: number;
  sector_count: number;
  candidates: Array<Record<string, unknown>>;
  rejected: Array<Record<string, unknown>>;
  market: Record<string, unknown> | null;
  enrichment_warning: string | null;
}

export const getTrendAnimalsConfig = () =>
  req<TrendAnimalsConfig>(`/api/config/trend-animals`);
export const estimateTrendHolding = (batchId: string) =>
  jsonPost<TrendHoldingEstimate>(`/api/trend-animals/holding/estimate`, { batch_id: batchId });
export const syncTrendHolding = (batchId: string, approvedBudget: number) =>
  jsonPost<TrendHoldingSyncResult>(`/api/trend-animals/holding/sync`, {
    batch_id: batchId, approved_budget: approvedBudget,
  });
export const estimateTrendSelection = (date: string) =>
  jsonPost<TrendSelectionEstimate>(`/api/trend-animals/selection/estimate`, { date });
export const runTrendSelection = (params: {
  date: string;
  batch_id?: string | null;
  approved_budget: number;
  etf_min_aum_yi?: number | null;
  etf_min_turnover_yi?: number | null;
  min_market_cap_yi?: number | null;
  min_turnover_yi?: number | null;
}) => jsonPost<TrendSelectionResult>(`/api/trend-animals/selection/run`, params);

// ── POST endpoints (JSON body) ──

export const postImport = (source: string, date: string) =>
  jsonPost<{ batch_id: string }>(`/api/import`, { source, date });

// Rerun OCR. indices given → those screenshots (0-based image_index);
// omitted → first run / rerun all 未成功(skip+failed). Returns immediately
// (backend runs in background); `queued` = how many jobs were scheduled.
export const rerunOcr = (batchId: string, indices?: number[], backend?: string) =>
  jsonPost<{ ok: boolean; queued: number }>(`/api/run/ocr`, {
    batch_id: batchId,
    ...(indices ? { indices } : {}),
    ...(backend ? { backend } : {}),
  });

export const runOcr = (batchId: string, backend?: string) => rerunOcr(batchId, undefined, backend);

// Force-stop a stuck OCR run: cancels the background task, unsticks running jobs
// (→ todo), unlocks the node. `reset` = how many wedged jobs were reset.
export const cancelOcr = (batchId: string) =>
  jsonPost<{ cancelled: boolean; reset: number }>(`/api/run/ocr/cancel`, { batch_id: batchId });

// ETF 线全局参数（规模门 / ETF 成交额门，单位亿）。不传 → 后端用 config 默认。
// 个股线参数（市值门 / 成交额门，单位亿）同理。
export const runPrescreen = (
  batchId: string,
  opts?: {
    etfMinAumYi?: number | null;
    etfMinTurnoverYi?: number | null;
    minMarketCapYi?: number | null;
    minTurnoverYi?: number | null;
  },
) =>
  jsonPost<unknown>(`/api/run/prescreen`, {
    batch_id: batchId,
    etf_min_aum_yi: opts?.etfMinAumYi ?? null,
    etf_min_turnover_yi: opts?.etfMinTurnoverYi ?? null,
    min_market_cap_yi: opts?.minMarketCapYi ?? null,
    min_turnover_yi: opts?.minTurnoverYi ?? null,
  });

// Q4：riskPct/fixedStopPct 为可选全局参数（单笔风险% + 固定止损距离%，小数比例）。
// 不传 → 后端落回默认（1% 风险 + 结构止损参考）。
export const runBFilter = (
  batchId: string,
  opts?: { riskPct?: number | null; fixedStopPct?: number | null },
) =>
  jsonPost<unknown>(`/api/run/b_filter`, {
    batch_id: batchId,
    risk_pct: opts?.riskPct ?? null,
    fixed_stop_pct: opts?.fixedStopPct ?? null,
  });

export const runExitCheck = (batchId: string) =>
  jsonPost<ExitCheckResult>(`/api/run/exit_check`, { batch_id: batchId });

// 节点⑨ 日报：触发机械段 + 顶部 LLM 趋势研判（自动+缓存）。
// backend: claude_cli/codex_cli/anthropic_api；省略 → 服务端默认（config.LLM_BACKEND）。
// 缓存按 (batch, backend, facts_hash) 隔离，切换后端会强制重生成。
export const runReport = (batchId: string, backend?: string) =>
  jsonPost<unknown>(`/api/run/report`, { batch_id: batchId, ...(backend ? { backend } : {}) });

export const runPush = (batchId: string) =>
  jsonPost<{ url: string }>(`/api/run/push`, { batch_id: batchId });

export const chatTool = (name: string, args: Record<string, unknown>) =>
  jsonPost<{ result: unknown }>(`/api/chat/tool`, { name, args });

// ── chatbox conversation (Task 9.12) ──

export type ChatRole = "user" | "assistant" | "tool_call" | "tool_result";

export interface ChatHistoryMsg {
  msg_id: number;
  role: ChatRole;
  content: string;
  tool_name: string | null;
  tool_args: Record<string, unknown> | null;
}

export interface PendingTool {
  name: string;
  args: Record<string, unknown>;
}

// One driven turn: "done" = plain reply; "needs_confirm" = a write tool is
// waiting on the user; "max_rounds" = the tool loop hit its cap.
export interface ChatTurn {
  status: "done" | "needs_confirm" | "max_rounds";
  assistant: string;
  tool?: PendingTool;
}

interface ChatOpts {
  model?: string;
  currentNode?: string;
}

function chatBody(base: Record<string, unknown>, opts?: ChatOpts) {
  return {
    ...base,
    ...(opts?.model ? { model: opts.model } : {}),
    ...(opts?.currentNode ? { current_node: opts.currentNode } : {}),
  };
}

export const getChatHistory = (batchId: string) =>
  req<ChatHistoryMsg[]>(`/api/chat/history/${batchId}`);

export const sendChatMessage = (batchId: string, content: string, opts?: ChatOpts) =>
  jsonPost<ChatTurn>(`/api/chat/message`, chatBody({ batch_id: batchId, content }, opts));

export const confirmChatTool = (
  batchId: string,
  name: string,
  args: Record<string, unknown>,
  confirmed: boolean,
  opts?: ChatOpts,
) =>
  jsonPost<ChatTurn>(`/api/chat/confirm`, chatBody({ batch_id: batchId, name, args, confirmed }, opts));

// ── POST endpoints (multipart: files + batch_id) ──

function filesPost<T>(path: string, batchId: string, files: File[], backend?: string): Promise<T> {
  const fd = new FormData();
  fd.append("batch_id", batchId);
  if (backend) fd.append("backend", backend);
  for (const f of files) fd.append("files", f);
  return req<T>(path, { method: "POST", body: fd });
}

export const runPositions = (batchId: string, files: File[], backend?: string) =>
  filesPost<ScanTrigger>(`/api/run/positions`, batchId, files, backend);

function dirPost<T>(path: string, batchId: string, source: string, backend?: string): Promise<T> {
  const fd = new FormData();
  fd.append("batch_id", batchId);
  fd.append("source", source);
  if (backend) fd.append("backend", backend);
  return req<T>(path, { method: "POST", body: fd });
}

// 与上面的 run* 命中同一后端路由：带 source(目录) 走扫目录模式，带 files 走上传模式。
export const scanPositions = (batchId: string, source: string, backend?: string) =>
  dirPost<ScanTrigger>(`/api/run/positions`, batchId, source, backend);
export const scanHoldingTemp = (batchId: string, source: string, backend?: string) =>
  dirPost<ScanTrigger>(`/api/run/holding_temp`, batchId, source, backend);

// 温度页/券商持仓页识别现为异步后台：触发立即返回 {ok,total}，进度走 status 轮询。
export interface ScanTrigger { ok: boolean; total: number; reason?: string }
export interface ScanStatus {
  status: "idle" | "running" | "done" | "error";
  current?: number; total?: number; image?: string | null;
  ok?: number; failed?: number;
  rows?: number; backfilled?: number;  // 温度页
  count?: number;                       // 券商持仓页
  account?: {
    nav: number | null;
    cash: number | null;
    currency: string | null;
    source_images: string[];
    conflicts: {
      field: "nav" | "cash"; kept: number; other: number; image: string;
    }[];
    complete: boolean;
  };
  failed_items?: { image: string; error: string }[]; error?: string;
}
export const getHoldingTempStatus = (batchId: string) =>
  req<ScanStatus>(`/api/run/holding_temp/status?batch_id=${encodeURIComponent(batchId)}`);
export const getPositionsStatus = (batchId: string) =>
  req<ScanStatus>(`/api/run/positions/status?batch_id=${encodeURIComponent(batchId)}`);

export interface RunAutoResult {
  ran: string[];
  failed: { node: string; error: string }[];
}
export const runAuto = (batchId: string) =>
  jsonPost<RunAutoResult>(`/api/run/auto`, { batch_id: batchId });

// 趋势动物「持仓」温度页：写 HoldingTemp + 按名称回填 Position.code（真实代码权威源）。
export const runHoldingTemp = (batchId: string, files: File[], backend?: string) =>
  filesPost<ScanTrigger>(`/api/run/holding_temp`, batchId, files, backend);

// ── SSE ──

// Open an EventSource on the pipeline channel. Caller attaches listeners for
// "state" (JSON pipeline_state) and "error" events, and is responsible for
// calling .close().
export function openPipelineSse(batchId: string): EventSource {
  return new EventSource(appUrl(`/api/sse/pipeline/${batchId}`));
}
