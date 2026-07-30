import { type CSSProperties, type KeyboardEvent } from "react";

// 股票代码超链接：有 code → 可点击（点后切到「重要低点」页并自动查询该代码）；
// 无 code → 退化为纯文本（fallback，如持仓/出局的「待回填」）。
export function CodeLink({ code, className = "mono", style, fallback = "—", tags }: {
  code?: string | null;
  className?: string;
  style?: CSSProperties;
  fallback?: string;
  tags?: string[] | null;
}) {
  if (!code) return <span className={className} style={style}>{fallback}</span>;
  const open = () =>
    window.dispatchEvent(new CustomEvent("trenddesk:open-swing", { detail: { code, tags: tags ?? [] } }));
  return (
    <span
      className={className}
      role="button"
      tabIndex={0}
      onClick={open}
      onKeyDown={(e: KeyboardEvent<HTMLSpanElement>) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      }}
      style={{ cursor: "pointer", textDecoration: "underline", textDecorationStyle: "dotted", ...style }}
      title="查看重要低点"
    >
      {code}
    </span>
  );
}
