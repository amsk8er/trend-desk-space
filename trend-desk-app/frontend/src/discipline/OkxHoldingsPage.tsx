import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getOkxMonitorCapabilities, getOkxMonitorOverview, getOkxPositionHistory, updateOkxPositionPolicy,
  type OkxMonitorEvent, type OkxPositionRow,
} from "../api";
import "./okx-holdings.css";

const productLabel: Record<string, string> = {
  us_stock_spot: "美股现货", us_stock_xperp: "美股合约",
  crypto_spot: "加密现货", crypto_derivative: "加密合约", cash: "现金",
};
const eventLabel: Record<string, string> = {
  missing_protection: "没有完整保护单", weak_protection: "保护单弱于纪律线",
  stop_line_breached: "已触及纪律线", liquidation_near: "接近强平价",
  manual_review: "需要人工核对", reentry_ready: "满足重新关注条件",
  worker_stale: "监控心跳中断",
};
const stateLabel: Record<string, string> = {
  active: "持仓中", closed: "已平仓", cooldown: "冷却中",
  awaiting_confirmation: "等待两根 5m 确认", reentry_ready: "可人工评估买回",
  daily_locked: "今日停止买回",
};

function number(value: string | null | undefined, digits = 2) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
}

function eventFor(row: OkxPositionRow, events: OkxMonitorEvent[]) {
  return events.filter(event => event.position_key === row.position_key);
}

function stopGap(row: OkxPositionRow) {
  const policy = Number(row.policy?.effective_stop);
  const triggers = row.protections.map(item => Number(item.trigger_price)).filter(Number.isFinite);
  if (!Number.isFinite(policy) || !triggers.length) return null;
  const stop = row.side === "long" ? Math.max(...triggers) : Math.min(...triggers);
  return row.side === "long" ? stop - policy : policy - stop;
}

export default function OkxHoldingsPage() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<OkxPositionRow | null>(null);
  const capabilities = useQuery({ queryKey: ["okx-monitor-capabilities"], queryFn: getOkxMonitorCapabilities });
  const overview = useQuery({
    queryKey: ["okx-monitor-overview"], queryFn: getOkxMonitorOverview,
    refetchInterval: 15_000, retry: 1,
  });
  const rows = overview.data?.positions ?? [];
  const events = overview.data?.events ?? [];
  const critical = events.filter(event => event.severity === "critical").length;
  const covered = rows.filter(row => row.protections.length > 0).length;
  const cashValue = useMemo(() => (overview.data?.cash ?? []).reduce(
    (sum, row) => sum + Number(row.quantity || 0), 0,
  ), [overview.data?.cash]);
  const mode = !capabilities.data?.monitor_enabled ? "disabled"
    : capabilities.data.shadow_mode ? "shadow" : overview.data?.status ?? "loading";

  return <section className="okx-page" data-testid="page-okx">
    <header className="okx-hero">
      <div>
        <span className="okx-kicker">READ-ONLY POSITION CONTROL</span>
        <h1>OKX 持仓哨塔</h1>
        <p>把保护单和纪律线摆在同一张桌上。这里只读取、核对和提醒，不替你交易。</p>
      </div>
      <div className={`okx-system-state ${mode}`}>
        <span>系统状态</span>
        <strong>{mode === "disabled" ? "等待启用" : mode === "shadow" ? "影子观察" : mode === "ready" ? "持续监控" : "连接异常"}</strong>
        <small>{overview.data?.heartbeat?.last_sync_at
          ? `最近对账 ${new Date(overview.data.heartbeat.last_sync_at).toLocaleString("zh-CN")}`
          : "还没有完成首次账户对账"}</small>
      </div>
    </header>

    <div className="okx-readonly-strip">
      <b>只读边界</b><span>无下单、改单、撤单、划转接口</span>
      <i aria-hidden="true">●</i><span>保护单继续由 OKX 24/7 托管</span>
      <i aria-hidden="true">●</i><span>邮件只提醒，最终操作由你确认</span>
    </div>

    {overview.isError && <div className="okx-empty error">无法读取监控数据：{String(overview.error)}</div>}
    <div className="okx-metrics" aria-label="账户监控摘要">
      <Metric label="风险持仓" value={String(rows.length)} note="现货与合约" tone="blue" />
      <Metric label="已有保护" value={`${covered}/${rows.length || 0}`} note="至少发现一张保护单" tone={covered === rows.length ? "green" : "yellow"} />
      <Metric label="紧急事项" value={String(critical)} note={critical ? "需要登录 OKX 核对" : "当前无紧急缺口"} tone={critical ? "red" : "green"} />
      <Metric label="现金余额" value={number(String(cashValue), 2)} note="稳定币合计，仅展示" tone="cream" />
    </div>

    <div className="okx-grid">
      <div className="okx-board">
        <div className="okx-section-title">
          <div><span>POSITION LEDGER</span><h2>持仓与保护差距</h2></div>
          <small>每 15 秒重新对账</small>
        </div>
        {!rows.length ? <div className="okx-empty">
          <strong>{overview.isLoading ? "正在读取账户…" : "等待首次持仓同步"}</strong>
          <span>启用服务器 worker 并配置 OKX 只读密钥后，这里会自动出现全部持仓。</span>
        </div> : <div className="okx-table-wrap"><table className="okx-table">
          <thead><tr><th>标的</th><th>方向 / 数量</th><th>参考价</th><th>纪律线</th><th>OKX 保护</th><th>差距</th><th>状态</th></tr></thead>
          <tbody>{rows.map(row => {
            const rowEvents = eventFor(row, events);
            const gap = stopGap(row);
            const trigger = row.protections[0]?.trigger_price;
            return <tr key={row.position_key} onClick={() => setSelected(row)} tabIndex={0}
              onKeyDown={event => { if (event.key === "Enter") setSelected(row); }}>
              <td><b>{row.underlying_symbol ?? row.inst_id}</b><small>{productLabel[row.product_kind] ?? row.product_kind}</small></td>
              <td><span className={`side ${row.side}`}>{row.side === "long" ? "多" : "空"}</span><b className="mono">{number(row.quantity, 6)}</b></td>
              <td className="mono">{number(row.mark_price ?? row.last_price, 4)}</td>
              <td><b className="mono">{number(row.policy?.effective_stop, 4)}</b><small>{row.policy?.mode === "auto_ema10" ? "已完成 RTH · EMA10" : "手工纪律线"}</small></td>
              <td><b className="mono">{number(trigger, 4)}</b><small>{row.protections[0]?.trigger_price_type ?? "未发现"}</small></td>
              <td><span className={`gap ${gap === null ? "unknown" : gap >= 0 ? "safe" : "weak"}`}>{gap === null ? "待核对" : `${gap >= 0 ? "+" : ""}${number(String(gap), 4)}`}</span></td>
              <td>{rowEvents.length ? <span className={`row-alert ${rowEvents.some(item => item.severity === "critical") ? "critical" : "warning"}`}>{rowEvents.length} 项待办</span>
                : <span className="row-alert healthy">保护正常</span>}<small>{stateLabel[row.state?.state ?? "active"] ?? row.state?.state}</small></td>
            </tr>;
          })}</tbody>
        </table></div>}
      </div>

      <aside className="okx-alerts">
        <div className="okx-section-title"><div><span>ACTION QUEUE</span><h2>现在要看什么</h2></div></div>
        {!events.length ? <div className="okx-clear"><b>✓</b><strong>没有活动告警</strong><span>继续让保护单留在交易所。</span></div>
          : events.map(event => <button key={event.event_id} className={event.severity}
            onClick={() => setSelected(rows.find(row => row.position_key === event.position_key) ?? null)}>
            <span>{event.severity === "critical" ? "立即" : event.severity === "warning" ? "核对" : "观察"}</span>
            <strong>{eventLabel[event.event_type] ?? event.event_type}</strong>
            <small>{String(event.details.inst_id ?? "账户")}</small>
          </button>)}
      </aside>
    </div>
    {selected && <PolicyDrawer row={selected} events={eventFor(selected, events)} onClose={() => setSelected(null)}
      onSaved={() => { setSelected(null); qc.invalidateQueries({ queryKey: ["okx-monitor-overview"] }); }} />}
  </section>;
}

function Metric({ label, value, note, tone }: { label: string; value: string; note: string; tone: string }) {
  return <article className={`okx-metric ${tone}`}><span>{label}</span><strong>{value}</strong><small>{note}</small></article>;
}

function PolicyDrawer({ row, events, onClose, onSaved }: {
  row: OkxPositionRow; events: OkxMonitorEvent[]; onClose: () => void; onSaved: () => void;
}) {
  const usProduct = ["us_stock_spot", "us_stock_xperp"].includes(row.product_kind);
  const [mode, setMode] = useState<"auto_ema10" | "manual">(row.policy?.mode ?? (usProduct ? "auto_ema10" : "manual"));
  const [stop, setStop] = useState(row.policy?.manual_stop ?? row.policy?.effective_stop ?? "");
  const [reason, setReason] = useState(row.policy?.manual_reason ?? "");
  const history = useQuery({
    queryKey: ["okx-position-history", row.position_key],
    queryFn: () => getOkxPositionHistory(row.position_key), retry: false,
  });
  const save = useMutation({
    mutationFn: () => updateOkxPositionPolicy(row.position_key, {
      mode, manual_stop: mode === "manual" ? Number(stop) : null, reason: mode === "manual" ? reason : null,
    }), onSuccess: onSaved,
  });
  return <div className="okx-drawer-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <aside className="okx-drawer" role="dialog" aria-modal="true" aria-label={`${row.inst_id} 纪律设置`}>
      <button className="okx-close" onClick={onClose} aria-label="关闭">×</button>
      <span className="okx-kicker">POSITION POLICY</span>
      <h2>{row.underlying_symbol ?? row.inst_id}</h2>
      <p>{productLabel[row.product_kind]} · {row.side === "long" ? "多头" : "空头"} · 数量 {number(row.quantity, 6)}</p>
      <div className="okx-drawer-facts">
        <div><span>当前纪律线</span><b>{number(row.policy?.effective_stop, 4)}</b></div>
        <div><span>交易所保护</span><b>{number(row.protections[0]?.trigger_price, 4)}</b></div>
        <div><span>重入状态</span><b>{stateLabel[row.state?.state ?? "active"] ?? row.state?.state}</b></div>
      </div>
      {!!events.length && <div className="okx-drawer-events">{events.map(item => <div key={item.event_id}><b>{eventLabel[item.event_type] ?? item.event_type}</b><small>{item.severity}</small></div>)}</div>}
      <details className="okx-history">
        <summary>最近记录 <span>{history.data?.snapshots.length ?? 0} 次对账</span></summary>
        <div>{history.isLoading ? <small>读取中…</small> : <>
          {(history.data?.events ?? []).slice(0, 5).map(item => <p key={item.event_id}>
            <b>{eventLabel[item.event_type] ?? item.event_type}</b>
            <span>{new Date(item.first_seen_at).toLocaleString("zh-CN")}</span>
          </p>)}
          {!(history.data?.events.length) && <small>暂无历史事件。</small>}
        </>}</div>
      </details>
      <fieldset>
        <legend>纪律线来源</legend>
        {usProduct && <label><input type="radio" checked={mode === "auto_ema10"} onChange={() => setMode("auto_ema10")} />
          <span><b>自动 10EMA</b><small>只用已完成的美股常规时段日线；多头只升不降，空头只降不升。</small></span></label>}
        <label><input type="radio" checked={mode === "manual"} onChange={() => setMode("manual")} />
          <span><b>手工纪律线</b><small>适合加密资产或你需要覆盖自动线的情况。</small></span></label>
      </fieldset>
      {mode === "manual" && <div className="okx-manual-fields">
        <label>止损价格<input inputMode="decimal" value={stop} onChange={event => setStop(event.target.value)} placeholder="必须大于 0" /></label>
        <label>修改原因<textarea value={reason} onChange={event => setReason(event.target.value)} placeholder="例如：结构低点上移；必须填写" /></label>
      </div>}
      {save.isError && <div className="okx-save-error">保存失败：{String(save.error)}</div>}
      <button className="okx-save" disabled={save.isPending || (mode === "manual" && (!Number(stop) || !reason.trim()))}
        onClick={() => save.mutate()}>{save.isPending ? "保存中…" : "保存纪律设置"}</button>
      <small className="okx-drawer-notice">保存只改变监控判断，不会修改 OKX 上的保护单。</small>
    </aside>
  </div>;
}
