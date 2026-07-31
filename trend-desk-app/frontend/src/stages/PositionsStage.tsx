import { useRef, useState, useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getPositions, confirmOcrPositions, runPositions, runHoldingTemp, getHoldingTemps, pairPosition,
  scanPositions, scanHoldingTemp, getConfigPaths, getHoldingTempStatus, getPositionsStatus,
  getLlmConfig, getTrendAnimalsConfig, estimateTrendHolding, syncTrendHolding,
  type Position, type HoldingTempLite,
} from "../api";
import { CodeLink } from "../components/CodeLink";

// 视觉 OCR 后端选择：OCR 页②与持仓页⑦共用，写 localStorage 跨页面保持一致。
// 选过的视觉后端会被 OCR 页和持仓页互相读，
// 避免"我 OCR 选了 codex_cli，到持仓页又跑回默认 claude_cli"。
const VISION_BACKEND_KEY = "td-vision-backend";
const backendLabel: Record<string, string> = {
  claude_cli: "Claude CLI", anthropic_api: "Claude API", codex_cli: "Codex CLI",
  minimax_coding_plan: "MiniMax Coding Plan（视觉）",
  openai_compatible: "OpenAI 兼容视觉 API",
};
const readStoredBackend = (): string => {
  try { return localStorage.getItem(VISION_BACKEND_KEY) || ""; } catch { return ""; }
};
const writeStoredBackend = (v: string) => {
  try { if (v) localStorage.setItem(VISION_BACKEND_KEY, v); else localStorage.removeItem(VISION_BACKEND_KEY); } catch { /* private mode */ }
};

export default function PositionsStage({ batchId }: { batchId: string | null }) {
  const qc = useQueryClient();
  const brokerRef = useRef<HTMLInputElement>(null);
  const tempRef = useRef<HTMLInputElement>(null);
  const [brokerFiles, setBrokerFiles] = useState<File[]>([]);
  const [tempFiles, setTempFiles] = useState<File[]>([]);
  // 视觉后端选择（与节点②OCR 共享 localStorage td-vision-backend；默认跟服务端配置）。
  const [backend, setBackendState] = useState<string>(() => readStoredBackend());
  const setBackend = (v: string) => { setBackendState(v); writeStoredBackend(v); };
  const llmCfg = useQuery({ queryKey: ["llm-config"], queryFn: getLlmConfig });
  const effBackend = backend || llmCfg.data?.backend || undefined;
  const trendCfg = useQuery({ queryKey: ["trend-animals-config"], queryFn: getTrendAnimalsConfig });
  const [apiBudget, setApiBudget] = useState("0.50");
  useEffect(() => {
    if (trendCfg.data?.default_budget != null) {
      setApiBudget(trendCfg.data.default_budget.toFixed(2));
    }
  }, [trendCfg.data?.default_budget]);

  const estimateApi = useMutation({
    mutationFn: () => estimateTrendHolding(batchId as string),
  });
  const syncApi = useMutation({
    mutationFn: () => syncTrendHolding(batchId as string, Number(apiBudget)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["positions", batchId] });
      qc.invalidateQueries({ queryKey: ["holding_temp", batchId] });
      qc.invalidateQueries({ queryKey: ["exit_check", batchId] });
    },
  });

  const positionsQuery = useQuery({
    queryKey: ["positions", batchId],
    queryFn: () => getPositions(batchId as string),
    enabled: !!batchId,
  });

  // 券商持仓页识别现为异步后台：触发只排队，进度/结果走 posStatus 轮询。
  const recognize = useMutation({
    mutationFn: () => runPositions(batchId as string, brokerFiles, effBackend),
    onSuccess: () => {
      setBrokerFiles([]);
      if (brokerRef.current) brokerRef.current.value = "";
      qc.invalidateQueries({ queryKey: ["positions_status", batchId] });
    },
  });

  // 温度页识别现为异步后台：触发只是排队，进度/结果走 htStatus 轮询。
  const recognizeTemp = useMutation({
    mutationFn: () => runHoldingTemp(batchId as string, tempFiles, effBackend),
    onSuccess: () => {
      setTempFiles([]);
      if (tempRef.current) tempRef.current.value = "";
      qc.invalidateQueries({ queryKey: ["holding_temp_status", batchId] });
    },
  });

  // 温度页项 + 人工配对（自动匹配救不了的简称重组，用户下拉手动指定真实代码）
  const holdingTempsQuery = useQuery({
    queryKey: ["holding_temp", batchId],
    queryFn: () => getHoldingTemps(batchId as string),
    enabled: !!batchId,
  });
  const pair = useMutation({
    mutationFn: (v: { positionId: number; code: string }) =>
      pairPosition(batchId as string, v.positionId, v.code),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["positions", batchId] }),
  });
  const confirmPositions = useMutation({
    mutationFn: (ids: number[]) => confirmOcrPositions(batchId as string, ids),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["positions", batchId] }),
  });

  const pathsQ = useQuery({ queryKey: ["config-paths"], queryFn: getConfigPaths });
  const [brokerDir, setBrokerDir] = useState("");
  const [tempDir, setTempDir] = useState("");
  const [brokerTouched, setBrokerTouched] = useState(false);
  const [tempTouched, setTempTouched] = useState(false);
  useEffect(() => {
    if (pathsQ.data?.pos_dir) {
      if (!brokerTouched) setBrokerDir(pathsQ.data.pos_dir);
      if (!tempTouched) setTempDir(pathsQ.data.pos_dir);   // 温度页与 pos 共用
    }
  }, [pathsQ.data, brokerTouched, tempTouched]);
  const scanBroker = useMutation({
    mutationFn: () => scanPositions(batchId as string, brokerDir.trim(), effBackend),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["positions_status", batchId] }),
  });
  // 券商持仓页识别进度：running 时每秒轮询；done/error 自动停。完成后刷新持仓表。
  const posStatus = useQuery({
    queryKey: ["positions_status", batchId],
    queryFn: () => getPositionsStatus(batchId as string),
    enabled: !!batchId,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1000 : false),
  });
  const posRunning = posStatus.data?.status === "running";
  useEffect(() => {
    if (posStatus.data?.status === "done") {
      qc.invalidateQueries({ queryKey: ["positions", batchId] });
      qc.invalidateQueries({ queryKey: ["holding_temp", batchId] });
    }
  }, [posStatus.data?.status, batchId, qc]);
  const scanTemp = useMutation({
    mutationFn: () => scanHoldingTemp(batchId as string, tempDir.trim(), effBackend),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["holding_temp_status", batchId] }),
  });

  // 温度页识别进度：running 时每秒轮询；done/error 自动停。完成后刷新持仓 + 温度页关联。
  const htStatus = useQuery({
    queryKey: ["holding_temp_status", batchId],
    queryFn: () => getHoldingTempStatus(batchId as string),
    enabled: !!batchId,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1000 : false),
  });
  const htRunning = htStatus.data?.status === "running";
  useEffect(() => {
    if (htStatus.data?.status === "done") {
      qc.invalidateQueries({ queryKey: ["positions", batchId] });
      qc.invalidateQueries({ queryKey: ["holding_temp", batchId] });
    }
  }, [htStatus.data?.status, batchId, qc]);

  // ── empty state: no batch selected ──
  if (!batchId) {
    return (
      <div className="panel">
        <div className="section-h">⑦ 持仓</div>
        <div style={{ fontSize: 12, color: "#6b7280", padding: "18px 4px" }}>
          请先在上方选择一个批次。
        </div>
      </div>
    );
  }

  const positions = positionsQuery.data ?? [];
  const missingCode = positions.filter((p) => !p.code).length;
  const unconfirmed = positions.filter((p) => !p.confirmed).length;

  return (
    <div className="panel">
      <div className="section-h" style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span>
          ⑦ 持仓
          <span className="chip" style={{ background: "#fed7aa" }}>
            VISION
          </span>
        </span>
        <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <select
            value={backend || llmCfg.data?.backend || ""}
            onChange={(e) => setBackend(e.target.value)}
            className="rounded border border-zinc-600 bg-zinc-800 px-2 py-1 text-sm"
            title="本次持仓页 OCR 用哪个模型后端（默认跟随服务端配置；与节点②OCR 选择保持一致）"
          >
            {(llmCfg.data?.choices ?? []).map((c) => (
              <option key={c} value={c}>
                {llmCfg.data?.providers?.[c]?.label ?? backendLabel[c] ?? c}
                {llmCfg.data?.providers?.[c]?.configured === false ? " · 未配置" : ""}
              </option>
            ))}
          </select>
        </span>
      </div>

      {/* ── 上传区 A：券商持仓页（股数/成本/现价/盈亏，无代码）── */}
      <UploadZone
        label="① 券商持仓截图"
        hint="股数/成本/现价/盈亏（无代码，识别后由温度页回填）"
        inputRef={brokerRef}
        files={brokerFiles}
        setFiles={setBrokerFiles}
        pending={recognize.isPending || posRunning}
        onRun={() => recognize.mutate()}
        accent="#fffdf3"
        dir={brokerDir} setDir={(v) => { setBrokerDir(v); setBrokerTouched(true); }}
        onScan={() => scanBroker.mutate()} scanPending={scanBroker.isPending || posRunning}
      />
      {recognize.isError && (
        <ErrLine>识别失败：{(recognize.error as Error).message}</ErrLine>
      )}
      {scanBroker.isError && (
        <ErrLine>扫目录失败：{(scanBroker.error as Error).message}</ErrLine>
      )}
      {/* 异步识别进度 / 结果（逐张可见，单张失败不挂整批）*/}
      {posRunning && (
        <OkLine>
          识别中 {posStatus.data?.current ?? 0}/{posStatus.data?.total ?? 0}
          {posStatus.data?.image ? ` · ${posStatus.data.image}` : ""}
          {(posStatus.data?.failed ?? 0) > 0 ? `（已失败 ${posStatus.data?.failed} 张）` : ""}
        </OkLine>
      )}
      {posStatus.data?.status === "done" && (
        <OkLine>
          已写入 {posStatus.data.count ?? 0} 条持仓（代码待温度页回填）
          {posStatus.data.account?.complete
            ? `，账户净值 ${posStatus.data.account.nav}、可用现金 ${posStatus.data.account.cash} 已识别`
            : ""}
          {(posStatus.data.failed ?? 0) > 0 ? `，失败 ${posStatus.data.failed} 张（可重扫）` : ""}。
        </OkLine>
      )}
      {posStatus.data?.status === "error" && (
        <ErrLine>持仓识别出错：{posStatus.data.error ?? "未知错误"}</ErrLine>
      )}

      {/* ── API 主通道：不再要求趋势动物持仓温度截图 ── */}
      <section className="chunky" style={{ background: "#ecfccb", padding: 12, marginTop: 12 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <div>
            <div className="section-h" style={{ margin: 0 }}>② 趋势动物 API · 持仓温度</div>
            <div style={{ fontSize: 10.5, fontWeight: 800, marginTop: 2 }}>
              收藏夹「持仓」→ 标准代码 / 温度 / 右侧天数 / 节气 / 风险信号
            </div>
          </div>
          <span className="pill mono" style={{ background: trendCfg.data?.configured ? "#86efac" : "#fecaca" }}>
            {trendCfg.data?.enabled ? (trendCfg.data.configured ? "API PRIMARY" : "KEY MISSING") : "API OFF"}
          </span>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap", marginTop: 10 }}>
          <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <span className="mono" style={{ fontSize: 9, fontWeight: 900 }}>预算上限/元</span>
            <input
              className="chat-input mono" type="number" min="0" step="0.01"
              value={apiBudget} onChange={(e) => setApiBudget(e.target.value)}
              style={{ width: 100, padding: "6px 7px" }}
            />
          </label>
          <button
            className="cbtn"
            disabled={!trendCfg.data?.enabled || !trendCfg.data?.configured || estimateApi.isPending}
            onClick={() => estimateApi.mutate()}
          >
            {estimateApi.isPending ? "估算中…" : "估算费用"}
          </button>
          <button
            className="cbtn cbtn-primary"
            disabled={!trendCfg.data?.enabled || !trendCfg.data?.configured || syncApi.isPending || !(Number(apiBudget) > 0)}
            onClick={() => syncApi.mutate()}
          >
            {syncApi.isPending ? "同步中…" : "从 API 同步"}
          </button>
          {estimateApi.data && (
            <span className="pill mono" style={{ background: "#fef08a" }}>
              {estimateApi.data.tm_count} 只 · ¥{estimateApi.data.estimated_cost.toFixed(3)} · {Object.values(estimateApi.data.as_of_dates).join("/")}
            </span>
          )}
          {syncApi.data && (
            <span className="pill mono" style={{ background: "#86efac" }}>
              ✓ {syncApi.data.rows} 行 · 回填 {syncApi.data.backfilled} · {syncApi.data.as_of_date}
              {` · 缺字段 ${syncApi.data.incomplete_rows.length}`}
              {syncApi.data.cached ? " · 本地复用" : ""}
              {syncApi.data.actual_cost == null
                ? " · 实际费用待账单"
                : ` · 实际 ¥${syncApi.data.actual_cost.toFixed(3)}`}
            </span>
          )}
        </div>
        {(estimateApi.isError || syncApi.isError) && (
          <ErrLine>{(syncApi.error as Error | null)?.message ?? (estimateApi.error as Error | null)?.message}</ErrLine>
        )}
      </section>

      <div className="section-h" style={{ marginTop: 14 }}>截图降级通道</div>
      {/* ── 上传区 B：趋势动物「持仓」温度页 OCR fallback ── */}
      <UploadZone
        label="趋势动物「持仓」温度页 · OCR FALLBACK"
        hint="真实代码+温度+右侧天数+强度（覆盖 ETF/基金，回填代码 & 喂出局检查）"
        inputRef={tempRef}
        files={tempFiles}
        setFiles={setTempFiles}
        pending={recognizeTemp.isPending || htRunning}
        onRun={() => recognizeTemp.mutate()}
        accent="#f0fdf4"
        dir={tempDir} setDir={(v) => { setTempDir(v); setTempTouched(true); }}
        onScan={() => scanTemp.mutate()} scanPending={scanTemp.isPending || htRunning}
      />
      {recognizeTemp.isError && (
        <ErrLine>温度页识别失败：{(recognizeTemp.error as Error).message}</ErrLine>
      )}
      {scanTemp.isError && (
        <ErrLine>扫目录失败：{(scanTemp.error as Error).message}</ErrLine>
      )}
      {/* 异步识别进度 / 结果（逐张可见，单张失败不挂整批）*/}
      {htRunning && (
        <OkLine>
          识别中 {htStatus.data?.current ?? 0}/{htStatus.data?.total ?? 0}
          {htStatus.data?.image ? ` · ${htStatus.data.image}` : ""}
          {(htStatus.data?.failed ?? 0) > 0 ? `（已失败 ${htStatus.data?.failed} 张）` : ""}
        </OkLine>
      )}
      {htStatus.data?.status === "done" && (
        <OkLine>
          温度页识别完成：{htStatus.data.rows ?? 0} 行，回填 {htStatus.data.backfilled ?? 0} 个真实代码
          {(htStatus.data.failed ?? 0) > 0 ? `，失败 ${htStatus.data.failed} 张（可重扫）` : ""}。
        </OkLine>
      )}
      {htStatus.data?.status === "error" && (
        <ErrLine>温度页识别出错：{htStatus.data.error ?? "未知错误"}</ErrLine>
      )}

      {/* ── results ── */}
      <div className="section-h">识别结果</div>

      {positions.length > 0 && (
        <div className="chunky" style={{ padding: 10, marginBottom: 10, background: unconfirmed ? "#fef3c7" : "#d1fae5", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
          <span style={{ fontSize: 10.5, fontWeight: 800 }}>
            {unconfirmed ? `${unconfirmed} 条 OCR 结果尚未进入正式持仓台账` : "持仓已人工确认并写入连续持仓台账"}
          </span>
          {unconfirmed > 0 && (
            <button className="cbtn cbtn-primary" disabled={missingCode > 0 || confirmPositions.isPending}
              onClick={() => confirmPositions.mutate(positions.filter((p) => !p.confirmed).map((p) => p.position_id))}
              title={missingCode > 0 ? "先完成真实代码映射" : "确认股数、成本与代码无误"}>
              {confirmPositions.isPending ? "确认中…" : "确认持仓无误"}
            </button>
          )}
        </div>
      )}

      {missingCode > 0 && (
        <div
          style={{
            fontSize: 10.5,
            color: "#92400e",
            fontWeight: 700,
            background: "#fef3c7",
            border: "1.5px solid #f59e0b",
            borderRadius: 6,
            padding: "6px 10px",
            marginBottom: 10,
          }}
        >
          {missingCode} 只持仓暂无真实代码 — 优先点「从 API 同步」；失败时再用截图降级通道。
        </div>
      )}

      {positionsQuery.isLoading ? (
        <div style={{ fontSize: 12, color: "#6b7280", padding: "14px 4px" }}>
          加载中…
        </div>
      ) : positionsQuery.isError ? (
        <div style={{ fontSize: 12, color: "#dc2626", fontWeight: 700, padding: "14px 4px" }}>
          加载失败：{(positionsQuery.error as Error).message}
        </div>
      ) : positions.length === 0 ? (
        <div style={{ fontSize: 12, color: "#9ca3af", padding: "14px 4px" }}>
          暂无持仓数据。上传截图并点击「识别」。
        </div>
      ) : (
        <div className="chunky" style={{ overflowX: "auto", padding: 0 }}>
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: 11,
            }}
          >
            <thead>
              <tr style={{ background: "var(--primary)" }}>
                {["代码", "名称", "股数", "成本", "现价", "盈亏%", "温度", "强度", "右侧天数", "右侧涨幅", "节气", "标签"].map((h, i) => (
                  <th
                    key={h}
                    style={{
                      textAlign: i >= 2 ? "right" : "left",
                      padding: "7px 10px",
                      fontSize: 9.5,
                      fontWeight: 800,
                      textTransform: "uppercase",
                      letterSpacing: "0.05em",
                      borderBottom: "2px solid var(--ink)",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {positions.map((p: Position) => {
                const normCode = (c: string | null | undefined) => (c || "").split(".")[0].trim();
                const tempMap = new Map((holdingTempsQuery.data ?? []).map((h) => [normCode(h.code), h]));
                const ht = tempMap.get(normCode(p.code)) ?? null;
                return (
                  <PositionRow
                    key={p.position_id}
                    p={p}
                    holdingTemps={holdingTempsQuery.data ?? []}
                    holdingTemp={ht}
                    pairing={pair.isPending}
                    onPair={(code) => pair.mutate({ positionId: p.position_id, code })}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function UploadZone({
  label, hint, inputRef, files, setFiles, pending, onRun, accent,
  dir, setDir, onScan, scanPending,
}: {
  label: string; hint: string;
  inputRef: React.RefObject<HTMLInputElement>;
  files: File[]; setFiles: (f: File[]) => void;
  pending: boolean; onRun: () => void; accent: string;
  dir: string; setDir: (v: string) => void; onScan: () => void; scanPending: boolean;
}) {
  const onPaste = (e: React.ClipboardEvent) => {
    const imgs: File[] = [];
    for (const it of Array.from(e.clipboardData.items)) {
      if (it.type.startsWith("image/")) {
        const f = it.getAsFile();
        if (f) imgs.push(f);
      }
    }
    if (imgs.length) {
      e.preventDefault();
      setFiles([...files, ...imgs]);
    }
  };
  return (
    <div className="chunky" onPaste={onPaste} tabIndex={0}
      style={{ padding: 12, marginBottom: 8, display: "flex", flexWrap: "wrap",
               alignItems: "center", gap: 10, background: accent }}>
      <div style={{ flexBasis: "100%", fontSize: 11, fontWeight: 800, color: "#374151" }}>
        {label}
        <span style={{ fontWeight: 600, color: "#9ca3af", marginLeft: 6 }}>{hint}</span>
      </div>
      {/* 扫目录（主路径，预填默认 pos 目录） */}
      <div style={{ flexBasis: "100%", display: "flex", gap: 8, alignItems: "center" }}>
        <input className="chat-input" value={dir} placeholder="/path/to/pos"
          onChange={(e) => setDir(e.target.value)} disabled={scanPending}
          style={{ flex: 1, fontSize: 11 }} />
        <button className="cbtn cbtn-primary" disabled={!dir.trim() || scanPending} onClick={onScan}
          style={{ opacity: !dir.trim() || scanPending ? 0.55 : 1 }}>
          {scanPending ? "扫描中…" : "扫目录识别"}
        </button>
      </div>
      <div style={{ flexBasis: "100%", fontSize: 10, color: "#9ca3af" }}>
        —— 或选文件 / 粘贴(⌘V) 后点识别 ——
      </div>
      <input ref={inputRef} type="file" multiple accept="image/*"
        onChange={(e) => setFiles(Array.from(e.target.files ?? []))} style={{ fontSize: 11 }} />
      <span style={{ fontSize: 11, color: "#6b7280", fontWeight: 700 }}>
        {files.length > 0 ? `已选 ${files.length} 张` : "可多选 / 可粘贴"}
      </span>
      <button className="cbtn cbtn-primary" disabled={files.length === 0 || pending} onClick={onRun}
        style={{ marginLeft: "auto", opacity: files.length === 0 || pending ? 0.55 : 1,
                 cursor: files.length === 0 || pending ? "not-allowed" : "pointer" }}>
        {pending ? "识别中…" : "识别"}
      </button>
    </div>
  );
}

function ErrLine({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 11, color: "#dc2626", fontWeight: 700, margin: "2px 0 10px" }}>
      {children}
    </div>
  );
}

function OkLine({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 11, color: "#16a34a", fontWeight: 700, margin: "2px 0 10px" }}>
      {children}
    </div>
  );
}

function PositionRow({
  p,
  holdingTemps,
  holdingTemp,
  pairing,
  onPair,
}: {
  p: Position;
  holdingTemps: HoldingTempLite[];
  holdingTemp: HoldingTempLite | null;
  pairing: boolean;
  onPair: (code: string) => void;
}) {
  const pnl = p.pnl_pct;
  const pnlColor = pnl > 0 ? "#dc2626" : pnl < 0 ? "#16a34a" : "#6b7280";
  const cell: React.CSSProperties = {
    padding: "7px 10px",
    borderBottom: "1.5px dashed #d4cfb8",
    whiteSpace: "nowrap",
  };
  const numCell: React.CSSProperties = { ...cell, textAlign: "right" };
  const fuzzy = p.code_source === "holding_temp_fuzzy" || p.code_source === "trend_api_fuzzy";
  const manual = p.code_source === "manual";

  const dash = <span style={{ color: "#9ca3af" }}>—</span>;
  const ht = holdingTemp;
  const gainPct = ht?.right_side_gain_pct;

  return (
    <tr>
      <td className="mono" style={{ ...cell, fontWeight: 700 }}>
        {p.code ? (
          <span
            title={fuzzy ? "模糊匹配（券商简称⊂温度页全称），请核对" : manual ? "人工配对" : undefined}
            style={fuzzy ? { color: "#b45309" } : undefined}
          >
            <CodeLink code={p.code} tags={ht?.tags} className="" style={fuzzy ? { color: "#b45309" } : undefined} />
            {fuzzy && <span title="模糊匹配，请核对"> ≈</span>}
            {manual && <span title="人工配对"> ✎</span>}
          </span>
        ) : (
          // 待回填 → 下拉人工配对（自动匹配救不了的简称重组）
          <select
            className="model-pill"
            style={{ fontSize: 10, maxWidth: 200 }}
            value=""
            disabled={pairing || holdingTemps.length === 0}
            title="从温度页手动配对真实代码"
            onChange={(e) => e.target.value && onPair(e.target.value)}
          >
            <option value="">
              {holdingTemps.length === 0 ? "待回填（先传温度页）" : "待回填·点选温度页…"}
            </option>
            {holdingTemps
              .filter((h) => h.code)
              .map((h) => (
                <option key={h.holding_id} value={h.code as string}>
                  {h.name}（{h.code}）
                </option>
              ))}
          </select>
        )}
      </td>
      <td style={cell}>{p.name}</td>
      <td className="num" style={numCell}>
        {fmt(p.shares, 0)}
      </td>
      <td className="num" style={numCell}>
        {fmt(p.avg_cost, 3)}
      </td>
      <td className="num" style={numCell}>
        {fmt(p.current_price, 3)}
      </td>
      <td className="num" style={{ ...numCell, color: pnlColor, fontWeight: 800 }}>
        {pnl > 0 ? "+" : ""}
        {fmt(pnl * 100, 2)}%
      </td>
      {/* 温度页数据：温度 / 强度 / 右侧天数 / 右侧涨幅 / 节气 */}
      <td style={{ ...numCell, textAlign: "center" }}>
        {ht?.temperature_status ?? dash}
      </td>
      <td className="num" style={numCell}>
        {ht?.strength != null ? ht.strength : dash}
      </td>
      <td className="num" style={numCell}>
        {ht?.right_side_days != null ? `第${ht.right_side_days}天` : dash}
      </td>
      <td className="num" style={numCell}>
        {gainPct != null ? (
          <span style={{ color: gainPct >= 0 ? "#dc2626" : "#16a34a", fontWeight: 700 }}>
            {gainPct >= 0 ? "+" : ""}
            {gainPct.toFixed(1)}%
          </span>
        ) : dash}
      </td>
      <td style={cell}>
        {ht?.jieqi ?? dash}
      </td>
      <td style={cell}>
        {ht?.tags && ht.tags.length ? (
          <span style={{ color: "#7c3aed" }}>{ht.tags.join(" · ")}</span>
        ) : dash}
        {ht?.signal_unavailable?.length ? (
          <div className="mono" style={{ fontSize: 8.5, color: "#9a3412", marginTop: 2 }}>
            未覆盖：{ht.signal_unavailable.join(" / ")}
          </div>
        ) : null}
        {ht && (
          <div className="mono" style={{ fontSize: 8.5, color: "#6b7280", marginTop: 2 }}>
            {ht.data_source === "trend_api" ? "API" : "截图 OCR"}
            {ht.as_of_date ? ` · ${ht.as_of_date}` : ""}
          </div>
        )}
      </td>
    </tr>
  );
}

function fmt(v: number | null | undefined, digits: number): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}
