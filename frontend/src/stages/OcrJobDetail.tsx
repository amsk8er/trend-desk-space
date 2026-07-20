import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getOcrResult, ocrImageUrl, rerunOcr, type OcrJob } from "../api";

const INK = "#1f2937";

// status → banner palette (skip 橙 / failed 红 / 其它中性)
function bannerStyle(status: string): { bg: string; border: string } {
  if (status === "failed") return { bg: "#fee2e2", border: "#dc2626" };
  if (status === "skipped") return { bg: "#ffedd5", border: "#ea580c" };
  return { bg: "#f3f4f6", border: "#9ca3af" };
}

// Minimal JSON syntax highlighter → colored spans on a light background.
// Escapes HTML first, then tags strings/keys/numbers/booleans/null by color.
const JSON_COLOR: Record<string, string> = {
  key: "#9a3412",   // 棕红：键名
  str: "#15803d",   // 绿：字符串
  num: "#b45309",   // 琥珀：数字
  bool: "#7c3aed",  // 紫：布尔
  null: "#6b7280",  // 灰：null
};

function highlightJson(value: unknown): string {
  let json = JSON.stringify(value ?? {}, null, 2);
  json = json.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return json.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false)\b|\bnull\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
    (m) => {
      let cls = "num";
      if (/^"/.test(m)) cls = /:$/.test(m) ? "key" : "str";
      else if (/true|false/.test(m)) cls = "bool";
      else if (/null/.test(m)) cls = "null";
      return `<span style="color:${JSON_COLOR[cls]}">${m}</span>`;
    },
  );
}

// Panel under the thumbnail grid when a screenshot square is clicked.
// Layout: header (id + actions) → optional reason banner → two columns
// (left 6 = screenshot, click to zoom full-screen · right 4 = OCR JSON).
export default function OcrJobDetail({ job, batchId }: { job: OcrJob; batchId: string }) {
  const qc = useQueryClient();
  const [imgErr, setImgErr] = useState(false);
  const [zoom, setZoom] = useState(false);
  const idx = job.image_index ?? job.job_id;

  const result = useQuery({
    queryKey: ["ocr_result", job.job_id],
    queryFn: () => getOcrResult(job.job_id),
  });

  const rerun = useMutation({
    mutationFn: () =>
      rerunOcr(batchId, job.image_index != null ? [job.image_index] : undefined),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ocr", batchId] }),
  });

  // Esc closes the full-screen viewer.
  useEffect(() => {
    if (!zoom) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setZoom(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoom]);

  function downloadJson() {
    getOcrResult(job.job_id).then((r) => {
      const blob = new Blob([JSON.stringify(r.raw_json, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${(job.image ?? `job_${job.job_id}`).replace(/\.[^.]+$/, "")}.json`;
      a.click();
      URL.revokeObjectURL(url);
    });
  }

  const bs = bannerStyle(job.status);
  const preStyle: React.CSSProperties = {
    margin: 0, background: "#f8fafc", color: INK, padding: "10px 12px",
    borderRadius: 8, border: "1px solid #e2e8f0", fontSize: 10, lineHeight: 1.6,
    maxHeight: 560, overflow: "auto",
  };

  return (
    <div
      className="chunky"
      style={{ border: `2px solid ${INK}`, borderRadius: 8, padding: 12, marginBottom: 14, background: "#fff" }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10, flexWrap: "wrap" }}>
        <span style={{ fontSize: 13, fontWeight: 800 }}>截图 #{idx}</span>
        <span className="mono" style={{ fontSize: 10.5, color: "#6b7280" }}>{job.image ?? "—"}</span>
        <span
          className="chip"
          style={{
            fontSize: 9, fontWeight: 800, padding: "2px 7px", border: `1.5px solid ${INK}`,
            borderRadius: 4, background: bs.bg, textTransform: "uppercase",
          }}
        >
          {job.status}
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button className="cbtn" onClick={downloadJson} style={{ fontSize: 11 }}>
            下载 JSON
          </button>
          <button
            className="cbtn cbtn-primary"
            disabled={rerun.isPending}
            onClick={() => rerun.mutate()}
            style={{ fontSize: 11, ...(rerun.isPending ? { opacity: 0.6, cursor: "not-allowed" } : {}) }}
          >
            {rerun.isPending ? "已触发…" : "重跑这张"}
          </button>
        </div>
      </div>

      {job.reason_friendly && (
        <div
          style={{
            border: `2px solid ${bs.border}`, background: bs.bg, borderRadius: 8,
            padding: "8px 10px", fontSize: 12, fontWeight: 700, color: INK, marginBottom: 10,
          }}
        >
          ⏭️ {job.reason_friendly}
        </div>
      )}

      {rerun.isError && (
        <div style={{ fontSize: 11, color: "#dc2626", marginBottom: 10 }}>
          重跑失败：{(rerun.error as Error).message}
        </div>
      )}

      {/* left 6 (screenshot, click to zoom) · right 4 (JSON) */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-start" }}>
        <div style={{ flex: "6 1 320px", minWidth: 0 }}>
          <div className="section-h" style={{ marginBottom: 6 }}>截图 · 点击放大</div>
          {imgErr ? (
            <div style={{ fontSize: 11, color: "#9ca3af", padding: "20px 0", textAlign: "center" }}>
              图片不可用（可能已被清理或迁移）
            </div>
          ) : (
            <img
              src={ocrImageUrl(job.job_id)}
              alt={job.image ?? `截图 #${idx}`}
              onError={() => setImgErr(true)}
              onClick={() => setZoom(true)}
              style={{
                display: "block", width: "100%", maxHeight: 560, objectFit: "contain",
                objectPosition: "top", borderRadius: 6, border: `1px solid ${INK}`,
                background: "#f9fafb", cursor: "zoom-in",
              }}
            />
          )}
        </div>

        <div style={{ flex: "4 1 240px", minWidth: 0 }}>
          <div className="section-h" style={{ marginBottom: 6 }}>OCR JSON</div>
          {result.isLoading ? (
            <pre className="mono" style={preStyle}>加载中…</pre>
          ) : result.isError ? (
            <pre className="mono" style={preStyle}>加载失败：{(result.error as Error).message}</pre>
          ) : (
            <pre
              className="mono"
              style={preStyle}
              dangerouslySetInnerHTML={{ __html: highlightJson(result.data?.raw_json) }}
            />
          )}
        </div>
      </div>

      {/* full-screen viewer */}
      {zoom && !imgErr && (
        <div
          onClick={() => setZoom(false)}
          style={{
            position: "fixed", inset: 0, zIndex: 1000, background: "rgba(0,0,0,0.85)",
            display: "flex", alignItems: "center", justifyContent: "center", padding: 24, cursor: "zoom-out",
          }}
        >
          <img
            src={ocrImageUrl(job.job_id)}
            alt={job.image ?? `截图 #${idx}`}
            style={{ maxWidth: "95vw", maxHeight: "95vh", objectFit: "contain", borderRadius: 6 }}
          />
        </div>
      )}
    </div>
  );
}
