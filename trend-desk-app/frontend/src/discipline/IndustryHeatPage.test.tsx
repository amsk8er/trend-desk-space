import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import IndustryHeatPage from "./IndustryHeatPage";

vi.mock("../api", async importOriginal => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getIndustryHeat: vi.fn(async () => ({
      trade_date: "2026-08-03", dataset_id: "d1",
      headline: { title: "半导体", summary: "趋势与广度共同验证。", confidence: "confirmed" },
      coverage: { industry_count: 76, trend_complete: 76, wind_requested: 8, wind_verified: 6 },
      methodology: { trend_score: "温度与强度评分", mainline_score: "Wind最多30%", wind_fields: [] },
      rows: [{
        dataset_id: "d1", trade_date: "2026-08-03", industry_tm_id: 1000, industry_name: "半导体",
        temperature_curr: "热", strength_curr: 92, strength_change: "↑", phase_curr: "立夏",
        warming_streak: 2, hot_duration_days: 3, strength_slope_5d: 4.2, warm_to_hot_count: 7,
        trend_score: 88, trend_rank: 1, trend_state: "leading", trend_evidence: {},
        wind_status: "verified", wind_score: 74, wind_coverage: 1,
        wind_metrics: { advancers: 30, decliners: 10, main_inflow_ratio_pct: 3 },
        wind_error: null, mainline_score: 83.8, mainline_state: "confirmed_mainline",
      }],
    })),
    refreshIndustryWind: vi.fn(),
  };
});

afterEach(cleanup);

describe("IndustryHeatPage", () => {
  it("keeps Trend Animals and Wind evidence visibly separate", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><IndustryHeatPage /></QueryClientProvider>);

    expect(
      await screen.findByRole("heading", { level: 1, name: "半导体" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("趋势热度").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Wind验证").length).toBeGreaterThan(0);
    expect(screen.getAllByText("主线确认").length).toBeGreaterThan(0);
    expect(screen.getByText("涨 30 / 跌 10 · 主力 +3.00%")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "行业节气分布" })).toBeInTheDocument();
    expect(screen.getByText(/主导节气 立夏：1 个行业/)).toBeInTheDocument();
    expect(screen.getByText("右侧一阶段 · 萌芽")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /立夏.*1.*100%/ }));
    expect(screen.getByText("正在查看：立夏")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Wind已验证" }));
    expect(screen.getAllByText("半导体").length).toBeGreaterThan(0);
  });
});
