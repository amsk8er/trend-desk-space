import { describe, expect, it } from "vitest";

import { calculateRisk } from "./UsRiskCalculator";


describe("calculateRisk", () => {
  it("matches the observed KovaView NVDA example", () => {
    const result = calculateRisk({
      account: 100_000, allocationPct: 10, entry: 225.16, stop: 218.59, riskBudgetPct: 1,
    });
    expect(result.sharesByCapital).toBe(44);
    expect(result.actualPosition).toBeCloseTo(9907.04, 2);
    expect(result.riskAmount).toBeCloseTo(289.08, 2);
    expect(result.riskPctOfAccount).toBeCloseTo(0.28908, 5);
    expect(result.maxRiskShares).toBe(152);
    expect(result.recommendedShares).toBe(44);
    expect(result.withinBudget).toBe(true);
  });

  it("caps shares when the chosen stop exceeds the risk budget", () => {
    const result = calculateRisk({
      account: 100_000, allocationPct: 100, entry: 100, stop: 90, riskBudgetPct: 1,
    });
    expect(result.sharesByCapital).toBe(1000);
    expect(result.maxRiskShares).toBe(100);
    expect(result.recommendedShares).toBe(100);
    expect(result.withinBudget).toBe(false);
    expect(result.impliedStop).toBe(99);
  });

  it("rejects a stop at or above entry", () => {
    const result = calculateRisk({ account: 10_000, allocationPct: 10, entry: 50, stop: 51, riskBudgetPct: 1 });
    expect(result.validStop).toBe(false);
    expect(result.recommendedShares).toBe(0);
  });
});
