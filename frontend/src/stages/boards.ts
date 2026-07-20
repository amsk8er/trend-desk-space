// A 股板块分类（初筛/B筛共用单一真相源）：688→科创板、300/301→创业板、其余→主板、ETF 独立。
export type Board = "A股·主板" | "A股·创业板" | "A股·科创板" | "ETF";

export const BOARD_ORDER: Board[] = ["A股·主板", "A股·创业板", "A股·科创板", "ETF"];

export function boardOf(c: { code?: string | null; is_etf?: boolean | null }): Board {
  if (c.is_etf) return "ETF";
  const code = c.code || "";
  if (code.startsWith("688")) return "A股·科创板";
  if (code.startsWith("300") || code.startsWith("301")) return "A股·创业板";
  return "A股·主板";
}
