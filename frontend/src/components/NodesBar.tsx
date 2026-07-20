import { useQuery } from "@tanstack/react-query";
import { getState } from "../api";

type NodeStatus = "done" | "running" | "todo" | "failed";

interface NodeDef {
  key: string;
  label: string;
}

// canonical 10-node pipeline (聚合 inserted after 校对; jieqi node removed 2026-06-21)
const NODES: NodeDef[] = [
  { key: "import", label: "导入" },
  { key: "ocr", label: "OCR" },
  { key: "review", label: "校对" },
  { key: "aggregate", label: "聚合" },
  { key: "prescreen", label: "初筛" },
  { key: "b_filter", label: "B 过滤" },
  { key: "positions", label: "持仓" },
  { key: "exit_check", label: "出局" },
  { key: "report", label: "日报" },
  { key: "push", label: "推送" },
];

const STATUS_ICON: Record<NodeStatus, string> = {
  done: "✓",
  running: "●",
  todo: "○",
  failed: "✕",
};

const STATUS_TEXT: Record<NodeStatus, string> = {
  done: "完成",
  running: "进行",
  todo: "待",
  failed: "失败",
};

// Normalize whatever the backend stores in pipeline_state[node] into one of the
// four visual states. The value may be a bare status string, or an object with
// a `status`/`state` field.
function normStatus(raw: unknown): NodeStatus {
  let s: string | undefined;
  if (typeof raw === "string") s = raw;
  else if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    s = (o.status ?? o.state) as string | undefined;
  }
  switch (s) {
    case "done":
    case "ok":
    case "success":
    case "complete":
    case "completed":
      return "done";
    case "running":
    case "in_progress":
      return "running";
    case "failed":
    case "error":
      return "failed";
    default:
      return "todo";
  }
}

interface Props {
  batchId: string | null;
  selectedNode: string;
  onSelect: (nodeId: string) => void;
}

export function NodesBar({ batchId, selectedNode, onSelect }: Props) {
  const { data } = useQuery({
    queryKey: ["state", batchId],
    queryFn: () => getState(batchId as string),
    enabled: !!batchId,
    refetchInterval: 2000,
  });

  const pipeline = data?.pipeline_state ?? {};

  return (
    <div className="panel">
      <div className="section-h" style={{ marginBottom: 10 }}>
        PIPELINE · 10 节点
      </div>
      <div className="nodes-row">
        {NODES.map((node, i) => {
          const status: NodeStatus = batchId
            ? normStatus(pipeline[node.key])
            : "todo";
          const isSelected = selectedNode === node.key;
          const dim = status === "todo" || status === "failed";
          return (
            <div key={node.key} style={{ display: "contents" }}>
              <div
                className={`cnode ${status}${isSelected ? " selected" : ""}`}
                onClick={() => onSelect(node.key)}
              >
                <div className="cnode-head">
                  <span>{STATUS_ICON[status]}</span>
                  <span>{i + 1}</span>
                </div>
                <div className="cnode-body">
                  <div className="cnode-num">{node.label}</div>
                  <div className="cnode-label">{STATUS_TEXT[status]}</div>
                </div>
              </div>
              {i < NODES.length - 1 && (
                <div className={`arrow${dim ? " arrow-dim" : ""}`}>→</div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
