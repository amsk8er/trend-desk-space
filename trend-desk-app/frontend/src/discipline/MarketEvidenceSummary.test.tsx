import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getMarketEvidence, type MarketEvidenceReport } from "../api";
import MarketEvidenceSummary from "./MarketEvidenceSummary";


vi.mock("../api", async importOriginal => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, getMarketEvidence: vi.fn() };
});

const report: MarketEvidenceReport = {
  trade_date: "2026-08-13",
  dataset_id: "dataset-1",
  discipline_version: "v1.5",
  headline: {
    market_temperature: "温",
    environment_factor: 1,
    per_position_weight: 0.05,
    opening_allowed: true,
    one_liner: "环境温，系数 1.00，单票仓位 5%",
  },
  items: [
    {
      id: "discipline.environment", bucket: "support", label: "纪律环境",
      value: { temperature: "温" }, display_value: "温 · 系数 1.00 · 单票 5.00%",
      source: "discipline_day_card", as_of: "2026-08-13", status: "ready",
      interpretation_rule: "active mapping", discipline_effect: "active",
      detail: "当前容量与开仓许可只以该纪律快照为准。",
    },
    {
      id: "industry.heat", bucket: "challenge", label: "行业领导力",
      value: {}, display_value: "仍未形成清晰主线",
      source: "industry_heat_snapshot", as_of: "2026-08-13", status: "ready",
      interpretation_rule: "explicit lookup", discipline_effect: "display_only",
      detail: "行业领导力偏弱，与当前允许开仓环境形成反证，但不改写纪律。",
    },
    {
      id: "plan.data_health", bucket: "watch", label: "计划数据闸门",
      value: { warnings: ["source_partial"] }, display_value: "1 项提醒",
      source: "trade_plan.data_health", as_of: "2026-08-13", status: "partial",
      interpretation_rule: "warnings", discipline_effect: "gate",
      detail: "存在非阻断提醒，需保留原始代码并人工理解。",
    },
  ],
  summary: {
    support: 1, challenge: 1, watch: 1,
    ready: 2, partial: 1, missing: 0, stale: 0,
  },
  generated_at: "2026-08-13T09:30:00Z",
  network_calls: 0,
  llm_required: false,
};

function renderSummary() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MarketEvidenceSummary tradeDate="2026-08-13" />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("MarketEvidenceSummary", () => {
  it("shows the authoritative headline and expands all evidence groups", async () => {
    vi.mocked(getMarketEvidence).mockResolvedValue(report);
    renderSummary();

    expect(await screen.findByText("温 · 系数 1.00")).toBeInTheDocument();
    expect(screen.getByText("只解释，不改规则", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("2026-08-13")).toBeInTheDocument();
    const disclosure = screen.getByRole("button", { name: "展开证据" });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(disclosure);

    expect(disclosure).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("纪律环境")).toBeInTheDocument();
    expect(screen.getByText("行业领导力")).toBeInTheDocument();
    expect(screen.getByText("计划数据闸门")).toBeInTheDocument();
    expect(screen.getByText("现行纪律")).toBeInTheDocument();
    expect(screen.getByText("展示证据")).toBeInTheDocument();
    expect(screen.getByText("部分")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /应用|交易|刷新/ })).not.toBeInTheDocument();
  });

  it("fails visibly without hiding the existing discipline workflow", async () => {
    vi.mocked(getMarketEvidence).mockRejectedValue(new Error("network unavailable"));
    renderSummary();

    expect(await screen.findByText("市场证据暂不可用")).toBeInTheDocument();
    expect(screen.getByText("不影响现有纪律结论与行动清单")).toBeInTheDocument();
    expect(screen.getByText("network unavailable")).toBeInTheDocument();
  });
});
