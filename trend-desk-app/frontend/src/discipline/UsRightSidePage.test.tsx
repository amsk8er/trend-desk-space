import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getDisciplinePlans, getDisciplineTodayData, getDisciplineVersion, getUsRightSideAssets,
  getUsRightSideOverview, preflightUsRightSideEnrichment, preflightUsRightSideScan,
  preflightUsRightSideUniverse,
  refreshUsRightSideUniverse,
  type UsRightSideSortCapability,
} from "../api";
import DisciplineDesk from "./DisciplineDesk";
import UsRightSidePage from "./UsRightSidePage";

const run = {
  run_id: "run-1", as_of_date: "2026-08-11", membership_as_of_date: "2026-08-11",
  upstream_update_dt: "2026-08-12T01:00:00Z",
  scope: "trend_animals_us_right_side_v1", rules_version: "us-right-side-v1", status: "ready",
  counts: { universe: 8000, scanned: 8000, right_side: 52, unknown: 0, standard_covered: 52,
    industries: 17, industry_covered: 52, deep_covered: 0 },
  readiness: { standard: true, industry: true, deep: false },
  cost: { estimated_screen_cny: "1.00", approved_screen_cny: "1.00", actual_screen_cny: "1.00",
    estimated_standard_cny: "1.00", approved_standard_cny: "1.00", actual_standard_cny: "1.00",
    estimated_industry_cny: "0.10", approved_industry_cny: "0.10", actual_industry_cny: "0.10",
    estimated_deep_cny: null, approved_deep_cny: null, actual_deep_cny: null,
    actual_total_cny: "2.10", breakdown: {} },
  progress: {}, cache_hit: false, error: null, created_at: null, updated_at: null, completed_at: null,
  audit: {
    cache_key: { as_of_date: "2026-08-11", scope: "trend_animals_us_right_side_v1",
      universe_sha256: "a".repeat(64), screen_fields_hash: "b".repeat(64) },
    field_sets: {
      screen: { fields: ["isTrendRightSide"], sha256: "b".repeat(64) },
      standard: { fields: ["priceIndex"], sha256: "c".repeat(64) },
      industry: { fields: ["trendTemperatureCurr"], sha256: "d".repeat(64) },
      deep: { fields: ["gainSinceTrendEntry"], sha256: "e".repeat(64) },
    },
    manifest: { available: true, sha256: "f".repeat(64) }, lease_active: false,
  },
};

const asset = {
  rank: 1, tm_id: 3001, ticker_symbol: "ADP", ticker_name: "自动数据处理", asset: "美股",
  currency_default: "USD", as_of_date: "2026-08-11", is_right_side: true, tradable_flag: true,
  price_index: "273.67", market_cap: "1087", amount_1d: "14", temperature_prev: "热",
  temperature_curr: "热", days_since_trend_entry: 14, gain_since_trend_entry: "0.149",
  phase_curr: "立夏", strength_local_curr: "94.9", strength_local_change: "↑",
  industry_tm_id: 901, industry_name: "工业设备", industry_temperature_curr: "温",
  industry_strength_local_curr: "86.2", industry_phase_curr: "立夏", industry_is_right_side: true,
  industry_as_of_date: "2026-08-11",
  danger_flag: false, boiling_flag: false, champagne_flag: false, risk_flag_count: 0,
  ticker_labels: ["立夏"], heat_score_7d: null, return_1m: null,
  field_states: Object.fromEntries(["price_index", "market_cap", "amount_1d", "temperature_curr",
    "phase_curr", "days_since_trend_entry", "gain_since_trend_entry", "strength_local_curr",
    "risk_flag_count", "industry_name"].map(key => [key, "available"])),
  industry_field_states: { temperature_curr: "available", strength_local_curr: "available", phase_curr: "available" },
  industry_source_method: "trend_animals_stock_industry",
};

const capabilities: Record<string, UsRightSideSortCapability> = Object.fromEntries([
  "ticker_name", "ticker_symbol", "price_index", "temperature_curr", "phase_curr", "industry_name",
  "industry_temperature_curr", "industry_strength_local_curr", "industry_phase_curr",
  "days_since_trend_entry", "gain_since_trend_entry", "market_cap", "amount_1d",
  "strength_local_curr", "risk_flag_count",
].map(field => [field, { enabled: true, default_direction: field.includes("name") || field.includes("symbol") ? "asc" : "desc", blocked_reason: null }]));

vi.mock("../api", async importOriginal => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getUsRightSideOverview: vi.fn(async () => ({
      capabilities: { enabled: true, scope: "trend_animals_us_right_side_v1", rules_version: "us-right-side-v1",
        research_only: true, automated_trading: false, automatic_paid_refresh: false, default_page_size: 40, notice: "研究用途" },
      universe: { available: true, membership_as_of_date: "2026-08-11", stock_count: 8000 }, run,
      sort_capabilities: capabilities,
    })),
    getUsRightSideAssets: vi.fn(async () => ({ run, items: [asset], total: 1, page_size: 40,
      offset: 0, next_cursor: null, sort: { sort_by: "strength_local_curr", sort_dir: "desc" },
      sort_capabilities: capabilities, filters: {}, facets: { industries: ["工业设备"] } })),
    getUsManualCapabilities: vi.fn(async () => ({ enabled: true })),
    getUsRightSideCapabilities: vi.fn(async () => ({ enabled: true })),
    getDisciplineTodayData: vi.fn(async () => ({})),
    getDisciplinePlans: vi.fn(async () => []),
    getDisciplineVersion: vi.fn(async () => ({ version: "test" })),
    getUsRightSideAsset: vi.fn(async () => ({ run, asset })),
    getUsRightSidePlot: vi.fn(),
    preflightUsRightSideUniverse: vi.fn(async () => ({
      current: { available: true, membership_as_of_date: "2026-08-11", stock_count: 8000 },
      upstream: { tm_id: 1, as_of_date: "2026-08-11", update_dt: "2026-08-12T01:00:00Z" },
      billing_model: "per_row", estimated_cost_cny: null, requires_unbounded_confirmation: true,
      note: "展开行数未知", free_evidence: { api_doc_sha256: "a", change_log_sha256: "b",
        billing_sha256: "c", update_status_sha256: "d", api_count: 10, billing_field_count: 30,
        latest_change: null, missing_required_fields: [] },
    })),
    refreshUsRightSideUniverse: vi.fn(),
    preflightUsRightSideEnrichment: vi.fn(), preflightUsRightSideScan: vi.fn(),
    runUsRightSideEnrichment: vi.fn(), runUsRightSideScan: vi.fn(),
  };
});

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
  vi.mocked(getUsRightSideAssets).mockClear();
  vi.mocked(getDisciplineTodayData).mockClear();
  vi.mocked(getDisciplinePlans).mockClear();
  vi.mocked(getDisciplineVersion).mockClear();
  vi.mocked(getUsRightSideOverview).mockClear();
  vi.mocked(preflightUsRightSideUniverse).mockClear();
  vi.mocked(preflightUsRightSideScan).mockClear();
  vi.mocked(preflightUsRightSideEnrichment).mockClear();
  vi.mocked(refreshUsRightSideUniverse).mockClear();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><UsRightSidePage /></QueryClientProvider>);
}

function lastListParams() {
  const calls = vi.mocked(getUsRightSideAssets).mock.calls;
  return calls[calls.length - 1]?.[0];
}

describe("UsRightSidePage", () => {
  it("shows stock and industry evidence in the same sortable ledger", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "美股右侧资产" })).toBeInTheDocument();
    expect(screen.getByText("所属行业环境")).toBeInTheDocument();
    expect(screen.getAllByText("工业设备").length).toBeGreaterThan(0);
    expect(screen.getByText("86.2")).toBeInTheDocument();
    fireEvent.click(screen.getByText(/数据与费用审计/));
    expect(screen.getAllByText("getTickerSnapshot")).toHaveLength(6);

    await waitFor(() => expect(lastListParams()).toMatchObject({
      sortBy: "strength_local_curr", sortDir: "desc",
    }));
    fireEvent.click(screen.getByRole("button", { name: /本地强度/ }));
    await waitFor(() => expect(lastListParams()).toMatchObject({
      sortBy: "strength_local_curr", sortDir: "asc",
    }));

    expect(screen.getByRole("columnheader", { name: /本地强度/ })).toHaveAttribute("aria-sort", "ascending");
  });

  it("opens an evidence drawer that compares the stock with its industry", async () => {
    renderPage();
    fireEvent.click(await screen.findByText("自动数据处理"));

    const dialog = await screen.findByRole("dialog", { name: "美股右侧证据" });
    await screen.findByRole("heading", { name: "行业背景" });
    expect(dialog).toHaveTextContent("行业背景");
    expect(dialog).toHaveTextContent("86.2");
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("restores the independent page and sort without mounting A-share queries", async () => {
    window.history.replaceState(null, "", "/?page=us-right-side&sort_by=days_since_trend_entry&sort_dir=desc");
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><DisciplineDesk onOpenPipeline={() => {}} onOpenSwing={() => {}} /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "美股右侧资产" })).toBeInTheDocument();
    await waitFor(() => expect(lastListParams()).toMatchObject({ sortBy: "days_since_trend_entry", sortDir: "desc" }));
    expect(getDisciplineTodayData).not.toHaveBeenCalled();
    expect(getDisciplinePlans).not.toHaveBeenCalled();
    expect(getDisciplineVersion).not.toHaveBeenCalled();
    expect(window.location.search).toContain("page=us-right-side");
    expect(window.location.search).toContain("sort_by=days_since_trend_entry");
  });

  it("automatically reads the latest free upstream date and blocks a stale membership scan", async () => {
    vi.mocked(getUsRightSideOverview).mockResolvedValueOnce({
      capabilities: { enabled: true, scope: "trend_animals_us_right_side_v1", rules_version: "us-right-side-v1",
        research_only: true, automated_trading: false, automatic_paid_refresh: false, default_page_size: 40, notice: "研究用途" },
      universe: { available: true, membership_as_of_date: "2026-07-22", stock_count: 5181 },
      run: null, sort_capabilities: capabilities,
    });
    vi.mocked(preflightUsRightSideUniverse).mockResolvedValueOnce({
      current: { available: true, membership_as_of_date: "2026-07-22", stock_count: 5181 },
      upstream: { tm_id: 1, as_of_date: "2026-08-12", update_dt: "2026-08-13T01:00:00Z" },
      billing_model: "per_row", estimated_cost_cny: null, requires_unbounded_confirmation: true,
      note: "展开行数未知", free_evidence: { api_doc_sha256: "a", change_log_sha256: "b",
        billing_sha256: "c", update_status_sha256: "d", api_count: 10, billing_field_count: 30,
        latest_change: null, missing_required_fields: [] },
    });

    renderPage();

    expect((await screen.findAllByText("2026-08-12")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("2026-07-22").length).toBeGreaterThan(0);
    expect(screen.getByText("成员范围已过期")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新成员范围" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "生成右侧名单" })).not.toBeInTheDocument();
    expect(preflightUsRightSideUniverse).toHaveBeenCalledTimes(1);
    expect(refreshUsRightSideUniverse).not.toHaveBeenCalled();
  });

  it("shows real batch and member progress while the right-side list is being generated", async () => {
    const screeningRun = {
      ...run,
      status: "screening",
      counts: { ...run.counts, scanned: 3000, right_side: 41, unknown: 6 },
      progress: { screen: { completed_batches: 3, total_batches: 8 } },
    };
    vi.mocked(getUsRightSideOverview).mockResolvedValueOnce({
      capabilities: { enabled: true, scope: "trend_animals_us_right_side_v1", rules_version: "us-right-side-v1",
        research_only: true, automated_trading: false, automatic_paid_refresh: false, default_page_size: 40, notice: "研究用途" },
      universe: { available: true, membership_as_of_date: "2026-08-11", stock_count: 8000 },
      run: screeningRun, sort_capabilities: capabilities,
    });

    renderPage();

    expect(await screen.findByText("正在生成右侧名单")).toBeInTheDocument();
    const progress = screen.getByRole("progressbar", { name: "名单生成进度" });
    expect(progress).toHaveAttribute("aria-valuenow", "38");
    expect(screen.getByText("3 / 8")).toBeInTheDocument();
    expect(screen.getByText("3,000 / 8,000")).toBeInTheDocument();
    expect(screen.getByText("正在按批次写入，可离开页面后再回来查看")).toBeInTheDocument();
  });

  it("rounds the approved scan budget up so execution is immediately available", async () => {
    const awaitingRun = {
      ...run,
      status: "awaiting_screen_budget",
      counts: { ...run.counts, scanned: 0, right_side: 0, unknown: 0 },
      progress: { screen: { completed_batches: 0, total_batches: 18 } },
    };
    vi.mocked(getUsRightSideOverview).mockResolvedValueOnce({
      capabilities: { enabled: true, scope: "trend_animals_us_right_side_v1", rules_version: "us-right-side-v1",
        research_only: true, automated_trading: false, automatic_paid_refresh: false, default_page_size: 40, notice: "研究用途" },
      universe: { available: true, membership_as_of_date: "2026-08-11", stock_count: 8000 },
      run: awaitingRun, sort_capabilities: capabilities,
    });
    vi.mocked(preflightUsRightSideScan).mockResolvedValueOnce({
      ...awaitingRun,
      preflight: { row_count: 5181, batch_count: 18, fields: ["isTrendRightSide"],
        estimated_cost_cny: 7.0736, preflight_hash: "f".repeat(64), batches: [] },
    });

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "生成右侧名单" }));

    const budgetInput = await screen.findByRole("spinbutton", { name: "批准预算（元）" });
    expect(budgetInput).toHaveValue(7.08);
    expect(screen.getByRole("button", { name: "批准并执行" })).toBeEnabled();
    expect(screen.getByText("扫描计划已就绪，等待预算确认")).toBeInTheDocument();
  });

  it("replaces the old all-field enrichment with an age-first candidate funnel", async () => {
    const screenReadyRun = {
      ...run,
      status: "awaiting_standard_budget",
      counts: { ...run.counts, age_covered: 0, standard_covered: 0,
        standard_target: 0, standard_max_days: null, industries: 0, industry_covered: 0 },
      readiness: { age: false, standard: false, industry: false, deep: false },
    };
    vi.mocked(getUsRightSideOverview).mockResolvedValueOnce({
      capabilities: { enabled: true, scope: "trend_animals_us_right_side_v1", rules_version: "us-right-side-v1",
        research_only: true, automated_trading: false, automatic_paid_refresh: false, default_page_size: 40, notice: "研究用途" },
      universe: { available: true, membership_as_of_date: "2026-08-11", stock_count: 8000 },
      run: screenReadyRun, sort_capabilities: capabilities,
    });
    vi.mocked(preflightUsRightSideEnrichment).mockResolvedValueOnce({
      ...screenReadyRun,
      status: "awaiting_age_budget",
      cost: { ...screenReadyRun.cost, estimated_age_cny: "3.12" },
      preflight: { row_count: 52, batch_count: 1, fields: ["daysSinceTrendEntry"],
        estimated_cost_cny: 3.12, preflight_hash: "a".repeat(64), batches: [] },
    });

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "① 补齐右侧天数" }));

    await waitFor(() => expect(preflightUsRightSideEnrichment)
      .toHaveBeenCalledWith("run-1", "age"));
    expect((await screen.findAllByText("右侧天数初筛")).length).toBeGreaterThan(0);
    expect(screen.getByText("预计 ¥3.1200")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "② 获取 30 天候选强度" })).not.toBeInTheDocument();
  });

  it("uses a fixed 30-day strength gate before buying Top 100 details", async () => {
    const ageReadyRun = {
      ...run,
      status: "awaiting_strength_budget",
      counts: { ...run.counts, age_covered: 52, strength_covered: 0, strength_target: 52,
        strength_max_days: 30, standard_covered: 0, standard_target: 0, standard_top_n: 100 },
      readiness: { age: true, strength: false, standard: false, industry: false, deep: false },
    };
    vi.mocked(getUsRightSideOverview).mockResolvedValueOnce({
      capabilities: { enabled: true, scope: "trend_animals_us_right_side_v1", rules_version: "us-right-side-v1",
        research_only: true, automated_trading: false, automatic_paid_refresh: false, default_page_size: 40, notice: "研究用途" },
      universe: { available: true, membership_as_of_date: "2026-08-11", stock_count: 8000 },
      run: ageReadyRun, sort_capabilities: capabilities,
    });
    vi.mocked(preflightUsRightSideEnrichment).mockResolvedValueOnce({
      ...ageReadyRun,
      cost: { ...ageReadyRun.cost, estimated_strength_cny: "2.3712" },
      preflight: { row_count: 52, batch_count: 1, fields: ["trendStrengthLocalCurr"],
        estimated_cost_cny: 2.3712, preflight_hash: "s".repeat(64), batches: [] },
    });

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "② 获取 30 天候选强度" }));

    await waitFor(() => expect(preflightUsRightSideEnrichment).toHaveBeenCalledWith("run-1", "strength"));
    expect(await screen.findByText("预计 ¥2.3712")).toBeInTheDocument();
    expect(screen.getByText("30 天强度预筛，再为 Top 100 购买详情")).toBeInTheDocument();
  });
});
