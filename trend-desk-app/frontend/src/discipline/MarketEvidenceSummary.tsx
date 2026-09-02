import { useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  getMarketEvidence,
  type MarketEvidenceBucket,
  type MarketEvidenceEffect,
  type MarketEvidenceItem,
  type MarketEvidenceStatus,
} from "../api";
import "./market-evidence.css";


const bucketMeta: Array<{ id: MarketEvidenceBucket; label: string }> = [
  { id: "support", label: "支持" },
  { id: "challenge", label: "反证" },
  { id: "watch", label: "待确认" },
];

const effectLabel: Record<MarketEvidenceEffect, string> = {
  active: "现行纪律",
  gate: "数据闸门",
  display_only: "展示证据",
};

const statusLabel: Record<MarketEvidenceStatus, string> = {
  ready: "完整",
  partial: "部分",
  missing: "缺失",
  stale: "日期不一致",
};

function EvidenceItem({ item }: { item: MarketEvidenceItem }) {
  return <article className={`market-evidence-item status-${item.status}`}>
    <div>
      <b>{item.label}</b>
      <span className={`evidence-effect effect-${item.discipline_effect}`}>{effectLabel[item.discipline_effect]}</span>
      <span className={`evidence-status evidence-status-${item.status}`}>{statusLabel[item.status]}</span>
    </div>
    <strong>{item.display_value}</strong>
    <p>{item.detail}</p>
    <footer>
      <span>来源 <code>{item.source}</code></span>
      <span>截至 <time>{item.as_of ?? "未知"}</time></span>
    </footer>
  </article>;
}

export default function MarketEvidenceSummary({ tradeDate }: { tradeDate?: string }) {
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();
  const evidenceQ = useQuery({
    queryKey: ["market-evidence", tradeDate ?? "latest"],
    queryFn: () => getMarketEvidence(tradeDate),
    retry: false,
  });

  if (evidenceQ.isLoading) {
    return <section className="market-evidence-summary is-loading" aria-label="市场证据摘要">
      <b>市场证据摘要</b><span>读取已落库事实…</span>
    </section>;
  }
  if (evidenceQ.isError || !evidenceQ.data) {
    return <section className="market-evidence-summary is-error" aria-label="市场证据摘要">
      <div><b>市场证据暂不可用</b><span>不影响现有纪律结论与行动清单</span></div>
      <small>{(evidenceQ.error as Error | null)?.message ?? "读取失败"}</small>
    </section>;
  }

  const report = evidenceQ.data;
  const headline = report.headline;
  const headlineText = headline
    ? `${headline.market_temperature ?? "未知"} · 系数 ${headline.environment_factor.toFixed(2)}`
    : "尚无计划环境快照";

  return <section className="market-evidence-summary" aria-label="市场证据摘要">
    <header>
      <div className="market-evidence-title">
        <small>MARKET EVIDENCE · 只解释，不改规则</small>
        <div><h2>市场证据摘要</h2><strong>{headlineText}</strong></div>
        <p>现行纪律是容量与开仓许可的唯一真相源。</p>
      </div>
      <div className="market-evidence-counts" aria-label="证据分组计数">
        {bucketMeta.map(bucket => <span key={bucket.id} className={`bucket-${bucket.id}`}>
          {bucket.label} <b>{report.summary[bucket.id] ?? 0}</b>
        </span>)}
      </div>
      <div className="market-evidence-controls">
        <span>事实日 <time>{report.trade_date}</time></span>
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={contentId}
          onClick={() => setExpanded(value => !value)}
        >{expanded ? "收起证据" : "展开证据"}</button>
      </div>
    </header>

    {expanded && <div id={contentId} className="market-evidence-groups">
      {bucketMeta.map(bucket => {
        const items = report.items.filter(item => item.bucket === bucket.id);
        return <section key={bucket.id} className={`market-evidence-group bucket-${bucket.id}`}>
          <h3>{bucket.label}<span>{items.length}</span></h3>
          {items.length
            ? items.map(item => <EvidenceItem key={item.id} item={item} />)
            : <p className="market-evidence-empty">本组没有已落库证据。</p>}
        </section>;
      })}
    </div>}
  </section>;
}
