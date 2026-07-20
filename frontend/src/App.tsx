import { useEffect, useState, type ReactNode } from "react";
import {
  QueryClient, QueryClientProvider, useQuery, useMutation, useQueryClient,
} from "@tanstack/react-query";
import { NodesBar } from "./components/NodesBar";
import { Chatbox } from "./components/Chatbox";
import {
  getAuthStatus, getBatches, loginWithAccessKey, runAuto,
} from "./api";
import { registry } from "./stages/registry";
import SwingStage from "./stages/SwingStage";
import DisciplineDesk from "./discipline/DisciplineDesk";
import "./styles/chunky.css";

type View = "discipline" | "pipeline" | "swing";

const qc = new QueryClient();

function AccessGate({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [accessKey, setAccessKey] = useState("");
  const auth = useQuery({
    queryKey: ["auth-status"],
    queryFn: getAuthStatus,
    retry: false,
  });
  const login = useMutation({
    mutationFn: () => loginWithAccessKey(accessKey),
    onSuccess: () => {
      setAccessKey("");
      qc.invalidateQueries({ queryKey: ["auth-status"] });
    },
  });
  if (auth.isPending) return <div className="access-screen"><b>正在检查访问权限…</b></div>;
  if (auth.data?.required && !auth.data.authenticated) {
    return <main className="access-screen">
      <section className="access-card">
        <small>PRIVATE TRADING WORKSPACE</small>
        <h1>纪律交易台</h1>
        <p>这是私人交易空间。请输入 AI Builder Space Access Key 后继续。</p>
        <input
          className="desk-input mono"
          type="password"
          autoComplete="current-password"
          value={accessKey}
          onChange={event => setAccessKey(event.target.value)}
          onKeyDown={event => {
            if (event.key === "Enter" && accessKey && !login.isPending) login.mutate();
          }}
          placeholder="Space Access Key"
        />
        <button
          className="desk-button pink"
          disabled={!accessKey || login.isPending}
          onClick={() => login.mutate()}
        >
          {login.isPending ? "验证中…" : "进入交易台"}
        </button>
        {login.isError && <p className="access-error">访问密钥不正确，请重新输入。</p>}
      </section>
    </main>;
  }
  return <>{children}</>;
}

function Desk() {
  const qc = useQueryClient();
  const [batchId, setBatchId] = useState<string | null>(null);
  // 首次打开必须能创建第一批数据；默认落在导入/API 选股，而不是无批次的 OCR 空状态。
  const [selectedNode, setSelectedNode] = useState<string>("import");
  const [view, setView] = useState<View>("discipline");
  // 从列表页点代码跳「重要低点」：带 nonce 的对象，保证重复点同一代码也能触发查询。
  const [swingCode, setSwingCode] = useState<{ code: string; n: number; tags?: string[] } | null>(null);

  const { data: batches } = useQuery({
    queryKey: ["batches"],
    queryFn: getBatches,
  });

  // RUN ALL：一键顺序跑机械节点（聚合/初筛/B筛/出局/日报），人工节点仍手动。
  const runAll = useMutation({
    mutationFn: () => runAuto(batchId as string),
    onSuccess: () => qc.invalidateQueries(),
  });

  // default batchId to the first batch once the list loads (and only while unset)
  useEffect(() => {
    if (batchId === null && batches && batches.length > 0) {
      setBatchId(batches[0].batch_id);
    }
  }, [batches, batchId]);

  // listen for cross-component node-switch + batch-switch requests
  // (e.g. ImportStage 导入成功 → select-batch + select-node)
  useEffect(() => {
    const onNode = (e: Event) => {
      const node = (e as CustomEvent<string>).detail;
      if (node) setSelectedNode(node);
    };
    const onBatch = (e: Event) => {
      const b = (e as CustomEvent<string>).detail;
      if (b) setBatchId(b);
    };
    // 列表页代码超链接 → 切到「重要低点」并把代码交给 SwingStage 自动查询。
    const onOpenSwing = (e: Event) => {
      const d = (e as CustomEvent<{ code: string; tags?: string[] } | string>).detail;
      const code = typeof d === "string" ? d : d?.code;
      const tags = typeof d === "string" ? [] : (d?.tags ?? []);
      if (code) { setSwingCode({ code, n: Date.now(), tags }); setView("swing"); }
    };
    window.addEventListener("trenddesk:select-node", onNode);
    window.addEventListener("trenddesk:select-batch", onBatch);
    window.addEventListener("trenddesk:open-swing", onOpenSwing);
    return () => {
      window.removeEventListener("trenddesk:select-node", onNode);
      window.removeEventListener("trenddesk:select-batch", onBatch);
      window.removeEventListener("trenddesk:open-swing", onOpenSwing);
    };
  }, []);

  const Stage = registry[selectedNode];
  const current = batches?.find((b) => b.batch_id === batchId);

  return (
    <div style={{ padding: 14 }}>
      {/* 纪律台内已收纳二级工具；离开纪律台后才显示顶层返回器。 */}
      {view !== "discipline" && <div className="toolbar-l" style={{ marginBottom: 10, gap: 6 }}>
        <button
          className="cbtn"
          onClick={() => setView("discipline")}
        >
          纪律交易台
        </button>
        <button
          className={`cbtn${view === "pipeline" ? " cbtn-primary" : ""}`}
          onClick={() => setView("pipeline")}
        >
          数据流水线
        </button>
        <button
          className={`cbtn${view === "swing" ? " cbtn-primary" : ""}`}
          onClick={() => setView("swing")}
        >
          重要低点
        </button>
      </div>}

      {view === "discipline" ? (
        <DisciplineDesk
          onOpenPipeline={() => setView("pipeline")}
          onOpenSwing={() => setView("swing")}
        />
      ) : view === "swing" ? (
        <div className="chunky-app">
          <SwingStage initialCode={swingCode} />
        </div>
      ) : (
      <>
      {/* toolbar */}
      <div className="toolbar">
        <div className="toolbar-l">
          <select
            className="pill mono"
            value={batchId ?? ""}
            onChange={(e) => setBatchId(e.target.value || null)}
          >
            {(batches ?? []).length === 0 && <option value="">无批次</option>}
            {(batches ?? []).map((b) => (
              <option key={b.batch_id} value={b.batch_id}>
                {b.batch_id}
              </option>
            ))}
          </select>
          {current && <span className="pill pill-date">{current.date}</span>}
          {current && <span className="pill pill-live">● {current.status}</span>}
        </div>
        <div className="toolbar-l">
          {runAll.isSuccess && !runAll.isPending && (
            <span className="pill mono" style={{ fontSize: 10 }} title="机械节点已跑">
              ✓ {runAll.data.ran.length} 节点
              {runAll.data.failed.length > 0 && ` · ${runAll.data.failed.length} 失败`}
            </span>
          )}
          {runAll.isError && (
            <span className="pill mono" style={{ fontSize: 10, background: "#fee2e2" }}>
              RUN ALL 失败
            </span>
          )}
          <button
            className="cbtn cbtn-primary"
            disabled={!batchId || runAll.isPending}
            title="一键跑机械节点：聚合→初筛→B筛→出局→日报（人工节点仍手动）"
            onClick={() => runAll.mutate()}
          >
            {runAll.isPending ? "运行中…" : "RUN ALL"}
          </button>
        </div>
      </div>

      {/* responsive grid: main column + chat column */}
      <div className="chunky-app">
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <NodesBar
            batchId={batchId}
            selectedNode={selectedNode}
            onSelect={setSelectedNode}
          />
          {/* stage slot — per-node stage panels mount here */}
          <main id="stage-slot" style={{ minHeight: 400 }}>
            {Stage ? (
              <Stage batchId={batchId} />
            ) : (
              <div className="panel">未知节点：{selectedNode}</div>
            )}
          </main>
        </div>

        <Chatbox batchId={batchId} selectedNode={selectedNode} />
      </div>
      </>
      )}
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={qc}>
      <AccessGate><Desk /></AccessGate>
    </QueryClientProvider>
  );
}
