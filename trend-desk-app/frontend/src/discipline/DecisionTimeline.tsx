import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createDecisionEvent,
  getDecisionEvents,
  type DecisionEvent,
  type DecisionEventType,
  type DisciplinePlan,
} from "../api";


const labels: Record<DecisionEventType, string> = {
  plan_generated: "计划生成",
  no_trade_confirmed: "确认无成交",
  candidate_excluded: "候选排除",
  risk_blocked: "风险 / 数据阻断",
  execution_confirmed: "成交确认",
  execution_missed: "计划未执行",
  review_completed: "复盘完成",
  note: "决定备注",
};

const manualTypes: { value: DecisionEventType; label: string }[] = [
  { value: "note", label: "决定备注" },
  { value: "risk_blocked", label: "风险 / 数据阻断" },
  { value: "candidate_excluded", label: "候选排除" },
  { value: "execution_missed", label: "计划未执行" },
];

function eventDetail(event: DecisionEvent): string {
  if (event.note) return event.note;
  if (event.reason_code) return event.reason_code;
  if (event.event_type === "execution_confirmed") {
    const side = String(event.payload_json.side ?? "成交");
    const shares = event.payload_json.shares == null ? "" : ` ${event.payload_json.shares} 股`;
    return `${side}${shares}`;
  }
  return "系统事实已记入";
}

export default function DecisionTimeline({ plan }: { plan: DisciplinePlan | null }) {
  const qc = useQueryClient();
  const [eventType, setEventType] = useState<DecisionEventType>("note");
  const [instrumentId, setInstrumentId] = useState("");
  const [note, setNote] = useState("");
  const instrumentRequired = eventType === "candidate_excluded" || eventType === "execution_missed";
  const queryKey = ["decision-events", plan?.plan_id];
  const eventsQ = useQuery({
    queryKey,
    queryFn: () => getDecisionEvents({ planId: plan!.plan_id, limit: 100 }),
    enabled: !!plan,
  });
  const create = useMutation({
    mutationFn: () => createDecisionEvent({
      trade_date: plan!.execute_date,
      market: "a_share",
      event_type: eventType,
      instrument_id: instrumentId.trim() || undefined,
      plan_id: plan!.plan_id,
      reason_code: "manual_entry",
      note: note.trim(),
      discipline_version: plan!.discipline_version,
      dataset_id: plan!.dataset_id ?? undefined,
      payload_json: { source: "discipline_review" },
    }),
    onSuccess: () => {
      setNote("");
      setInstrumentId("");
      qc.invalidateQueries({ queryKey });
    },
  });

  return <section className="desk-panel decision-journal" data-testid="decision-timeline">
    <div className="panel-head"><div><small>DECISION JOURNAL</small><h2>决定时间线</h2></div><span>只追加 · 不改写</span></div>
    {!plan && <p className="decision-empty">选择一份计划后查看决定证据。</p>}
    {plan && <>
      <div className="decision-entry">
        <select aria-label="决定类型" className="desk-input" value={eventType} onChange={event => setEventType(event.target.value as DecisionEventType)}>
          {manualTypes.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>
        <input aria-label="标的代码" className="desk-input mono" value={instrumentId} onChange={event => setInstrumentId(event.target.value)} placeholder={instrumentRequired ? "标的代码（必填）" : "标的代码（可选）"} />
        <input aria-label="决定说明" className="desk-input" value={note} onChange={event => setNote(event.target.value)} placeholder="写下当时可见的事实与原因" />
        <button className="desk-button" disabled={!note.trim() || (instrumentRequired && !instrumentId.trim()) || create.isPending} onClick={() => create.mutate()}>{create.isPending ? "记录中…" : "记入日志"}</button>
      </div>
      <p className="decision-help">“今日无成交”请在成交确认区操作，系统会同步记入；此处用于补充阻断、排除和未执行原因。</p>
      {create.isError && <p className="decision-error">{(create.error as Error).message}</p>}
      {eventsQ.isLoading && <p className="decision-empty">读取决定事件…</p>}
      {eventsQ.isError && <p className="decision-error">{(eventsQ.error as Error).message}</p>}
      {eventsQ.data && eventsQ.data.length === 0 && <p className="decision-empty">这份计划尚无决定事件。</p>}
      {!!eventsQ.data?.length && <ol className="decision-events">{eventsQ.data.map(event => <li key={event.event_id}>
        <time>{event.trade_date}<small>{new Date(event.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</small></time>
        <span className={`decision-kind kind-${event.event_type}`}>{labels[event.event_type]}</span>
        <div><b>{event.instrument_id ?? "组合 / 系统"}</b><p>{eventDetail(event)}</p></div>
      </li>)}</ol>}
    </>}
  </section>;
}
