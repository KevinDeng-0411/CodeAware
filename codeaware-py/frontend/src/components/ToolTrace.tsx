// ToolTrace - Agent 工具调用轨迹折叠面板（ADR-0016/0017）
// 抽取自 Chat.tsx 实时轨迹，供 Agent Runs 回放页复用：渲染 tool.call → tool.result 配对。
import { useState } from "react";

export interface ToolActivity {
  callId: string;
  name: string;
  args: Record<string, unknown>;
  status: "ok" | "error" | "running";
  result?: string;
}

export default function ToolTrace({ tools }: { tools: ToolActivity[] }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="mt-2 rounded border border-line bg-graph/40">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-3 py-1.5 font-mono text-2xs uppercase tracking-techy text-mute"
      >
        <span>工具调用 · {tools.length} 次{!expanded ? " · 已折叠" : ""}</span>
        <span>{expanded ? "▾ 收起" : "▸ 展开"}</span>
      </button>
      {expanded && (
        <div className="px-3 py-2 space-y-2 border-t border-line/60">
          {tools.map((t) => (
            <div key={t.callId} className="font-mono text-2xs">
              <div className="flex items-center gap-2 text-mute">
                <span className={t.status === "running" ? "text-amber" : t.status === "ok" ? "text-graph" : "text-oxblood"}>
                  {t.status === "running" ? "⟳" : t.status === "ok" ? "✓" : "✗"}
                </span>
                <span className="text-ink">{t.name}</span>
                {t.args && Object.keys(t.args).length > 0 && (
                  <span className="text-mute/70">{JSON.stringify(t.args)}</span>
                )}
              </div>
              {t.result && (
                <pre className="mt-1 whitespace-pre-wrap text-mute/70 max-h-24 overflow-y-auto bg-panel/60 rounded px-2 py-1">
                  {t.result}
                </pre>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
