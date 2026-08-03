import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getUsManualOverview, getUsManualPlans, type UsManualOverview } from "../api";
import UsManualDataAudit from "./UsManualDataAudit";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, getUsManualOverview: vi.fn(), getUsManualPlans: vi.fn() };
});

const auditOverview = {
  state: "ready",
  capabilities: { rules_version: "us-manual-h6", h6_mode: "active" },
  run: {
    run_id: "run-h6", as_of_date: "2026-07-29", status: "ready", funnel: {},
  },
  candidates: [{
    candidate_id: 9, ticker_symbol: "SPY", ticker_name: "SPDR S&P 500 ETF Trust",
    asset_type: "etf", tm_id: 9001, screen_status: "ready", risk_anchor_status: "ready",
    right_side_calendar_days: 1, strength_local: "94.88", industry_name: null,
    industry_temperature_curr: null, ticker_labels: ["温转热"], trend_phase_curr: "立夏",
    all_reasons: [], benchmark_family_id: "sp500", exposure_key: "sp500|long|1x|plain|unhedged",
    etf_benchmark_evidence: {
      benchmark_canonical_name: "S&P 500 Index", benchmark_name_raw: "S&P 500 Index",
      strategy_type: "plain", exposure_direction: "long", leverage_multiplier: "1",
      currency_hedge: "unhedged", source_url: "https://www.sec.gov/Archives/edgar/data/example/spy.htm",
      retrieved_at: "2026-07-29T12:00:00", identity: { series_id: "S-SPY", class_id: "C-SPY" },
    },
    risk_anchor: {
      quote_usdt: "100.25", anchor_price_usdt: "95.25", anchor_date: "2026-07-18",
      anchor_distance: "0.049875", daily_sha256: "daily-hash", hourly_sha256: "hourly-hash",
      algorithm_version: "ep3-decimal-v1", error_message: null,
      evidence_json: {
        aggregated_sessions: Array.from({ length: 60 }, (_, index) => ({ date: `session-${index + 1}` })),
        daily_validation: { bar_count: 60, largest_jump_ratio: "0.02" },
        anchor: { future_data_used: false, ep3: { reference_high: "101.5", reference_high_date: "2026-07-15", confirmed_on: "2026-07-24" } },
      },
    },
  }],
  data_update: { as_of_date: "2026-07-29", us_as_of_date: "2026-07-29", us_etf_as_of_date: "2026-07-29" },
  last_attempt: { trigger: "manual" },
  schedule: { scheduler_enabled: true, timezone: "Asia/Shanghai", automatic_slots: ["08:00", "08:30"], manual_full_collection_after: "07:00", automatic_cutoff: "10:00" },
  cost: { estimated_total_cny: "1.2", daily_cap_cny: "5", current_run_estimated_cny: "1.2", actual_total_cny: "1.1", breakdown: { membership: "0.2" } },
  etf_benchmark_status: { verified: 1, blocked: 0 },
  risk_anchor_status: { pending: 0, ready: 1, blocked: 0 },
} as unknown as UsManualOverview;

describe("UsManualDataAudit", () => {
  beforeEach(() => {
    vi.mocked(getUsManualOverview).mockResolvedValue(auditOverview);
    vi.mocked(getUsManualPlans).mockResolvedValue([]);
  });

  it("contains the technical evidence removed from the execution cockpit", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><UsManualDataAudit /></QueryClientProvider>);
    await screen.findByText("当日候选底层证据（1）");
    const audit = screen.getByTestId("us-manual-data-audit");
    expect(audit).toHaveTextContent("当日候选底层证据（1）");
    expect(audit).toHaveTextContent("60 个完整交易日");
    expect(audit).toHaveTextContent("参考摆动高点");
    expect(audit).toHaveTextContent("未使用");
    expect(audit).toHaveTextContent("ep3-decimal-v1");
    expect(screen.getByRole("link", { name: "打开原始资料" })).toHaveAttribute(
      "href", "https://www.sec.gov/Archives/edgar/data/example/spy.htm",
    );
  });
});
