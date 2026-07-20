import { useMutation } from "@tanstack/react-query";
import { runPush } from "../api";

export default function PushStage({ batchId }: { batchId: string | null }) {
  // runPush() returns { url: string } — the published 飞书 doc/card link.
  const mutation = useMutation({
    mutationFn: () => runPush(batchId as string),
  });

  if (!batchId) {
    return (
      <div className="panel" style={{ textAlign: "center", color: "#9ca3af" }}>
        <div style={{ fontSize: 28, marginBottom: 6 }}>📤</div>
        <div style={{ fontSize: 13, fontWeight: 700 }}>选择一个批次</div>
        <div style={{ fontSize: 11, marginTop: 4 }}>
          选中批次后即可手动推送到飞书
        </div>
      </div>
    );
  }

  const url = mutation.data?.url ?? null;

  return (
    <div className="panel">
      {/* header / toolbar */}
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <div className="toolbar-l">
          <span className="cnode-num" style={{ fontSize: 14 }}>
            ⑩ 推送
          </span>
          <span className="chip" style={{ background: "#93c5fd" }}>
            PUSH
          </span>
          <span className="pill pill-date mono">{batchId}</span>
        </div>
        <button
          className="cbtn cbtn-primary"
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending}
        >
          {mutation.isPending ? "推送中…" : url ? "重新推送到飞书" : "推送到飞书"}
        </button>
      </div>

      {/* D9 human-trigger notice */}
      <div
        className="chunky"
        style={{
          background: "#fffbe6",
          padding: "10px 12px",
          marginBottom: 12,
          fontSize: 11,
          lineHeight: 1.6,
        }}
      >
        <div style={{ fontWeight: 800, marginBottom: 4 }}>
          🖐 人工触发 · D9
        </div>
        <div>
          推送是流水线最后一步，<b>必须由你手动点击</b>触发，系统不会自动推送。
          确认日报内容无误后，再把当日结果推送到飞书。
        </div>
      </div>

      {/* push target description */}
      <div className="section-h">推送目标</div>
      <div
        className="chunky"
        style={{
          background: "#fffdf5",
          padding: "12px 14px",
          marginBottom: 12,
          fontSize: 11.5,
          lineHeight: 1.7,
        }}
      >
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
          <span style={{ fontSize: 16 }}>💬</span>
          <span style={{ fontWeight: 800 }}>飞书 · 趋势交易日报</span>
          <span className="chip" style={{ background: "#86efac", marginLeft: 0 }}>
            FEISHU
          </span>
        </div>
        <div style={{ color: "#4b5563" }}>
          当日日报将以卡片/文档形式发布到配置好的飞书目标。推送成功后会返回一个
          可点开的链接，便于你直接在飞书中查看与转发。
        </div>
      </div>

      {/* error */}
      {mutation.isError && (
        <div
          className="chunky"
          style={{
            background: "#fee2e2",
            borderColor: "#1f2937",
            padding: "10px 12px",
            marginBottom: 12,
            fontSize: 11,
            lineHeight: 1.5,
          }}
        >
          <div style={{ fontWeight: 800, marginBottom: 2 }}>推送失败</div>
          <div className="mono" style={{ wordBreak: "break-word" }}>
            {(mutation.error as Error)?.message ?? "未知错误"}
          </div>
        </div>
      )}

      {/* push result */}
      <div className="section-h">本次推送结果</div>

      {mutation.isPending && (
        <div
          style={{
            textAlign: "center",
            color: "#6b7280",
            padding: "28px 0",
            fontSize: 12,
            fontWeight: 700,
          }}
        >
          正在推送到飞书…
        </div>
      )}

      {!mutation.isPending && !mutation.isError && url === null && (
        <div
          style={{
            textAlign: "center",
            color: "#9ca3af",
            padding: "28px 0",
            fontSize: 12,
          }}
        >
          <div style={{ fontSize: 26, marginBottom: 6 }}>🚀</div>
          尚未推送 · 点击右上角「推送到飞书」开始
        </div>
      )}

      {url !== null && !mutation.isPending && (
        <div
          className="chunky"
          style={{
            background: "#d1fae5",
            padding: "12px 14px",
            fontSize: 11.5,
            lineHeight: 1.7,
          }}
        >
          <div style={{ fontWeight: 800, marginBottom: 6 }}>✅ 推送成功</div>
          <div style={{ marginBottom: 8, color: "#374151" }}>
            日报已发布到飞书，点击下方链接查看：
          </div>
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className="cbtn"
            style={{
              display: "inline-block",
              textDecoration: "none",
              background: "#93c5fd",
              wordBreak: "break-all",
            }}
          >
            🔗 在飞书中打开
          </a>
          <div
            className="mono"
            style={{
              marginTop: 8,
              fontSize: 10.5,
              color: "#4b5563",
              wordBreak: "break-all",
            }}
          >
            {url}
          </div>
        </div>
      )}
    </div>
  );
}
