// AgentArchDiagram - Agent 全链路架构图（纵向主链 + 分支下拉折叠 + waku 四原则）
// ① 布局：手写坐标，语义重量决定尺寸；分支（记忆/历史、5 工具、检索栈）可下拉折叠，
//    折叠后只留紧凑主链 → 下方 llm/sse/agent_runs 全可见，无纵向遮挡
// ② 数据流：SSE 事件统一驱动（archMap 映射）
// ③ 视觉编码：单强调色(teal) + 灰度 + 激活实线/未激活虚线
// ④ 动画：稳定强调色 + transition（叙事非装饰）
// SVG 固定像素（不缩放）→ 字号恒定清晰，容器 overflow 滚动。
import { useState } from "react";
import { ARCH_NODES } from "./archMap";

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

const MAIN_W = 236;
const MAIN_H = 58;
const MAIN_X = 96;
const BRANCH_W = 150;
const BRANCH_H = 50;

// 主链 y（展开/折叠两套，折叠更紧凑）
const MAIN_Y = {
  unfolded: [12, 98, 184, 270, 356, 640, 726, 812],
  folded: [12, 82, 152, 222, 292, 362, 432, 502],
};
const MAIN_IDS = ["input", "guardrail", "coordinator", "context", "toolkit", "llm", "sse", "agent_runs"];

// 分支（仅展开时渲染）
const BRANCH_POS: Record<string, Rect> = {
  memory: { x: 380, y: 282, w: 170, h: 44 },
  history: { x: 580, y: 282, w: 170, h: 44 },
  "tool:search_knowledge": { x: 24, y: 442, w: BRANCH_W, h: BRANCH_H },
  "tool:get_document": { x: 194, y: 442, w: BRANCH_W, h: BRANCH_H },
  "tool:list_documents": { x: 364, y: 442, w: BRANCH_W, h: BRANCH_H },
  "tool:calculate": { x: 534, y: 442, w: BRANCH_W, h: BRANCH_H },
  "tool:get_current_time": { x: 704, y: 442, w: BRANCH_W, h: BRANCH_H },
  rag: { x: 24, y: 532, w: BRANCH_W, h: BRANCH_H },
  bm25: { x: 194, y: 532, w: BRANCH_W, h: BRANCH_H },
  vector: { x: 364, y: 532, w: BRANCH_W, h: BRANCH_H },
  rrf: { x: 534, y: 532, w: BRANCH_W, h: BRANCH_H },
  reranker: { x: 704, y: 532, w: BRANCH_W, h: BRANCH_H },
};

const SVG_W = 900;
const SVG_H_UNFOLDED = 890;
const SVG_H_FOLDED = 570;

const LABELS: Record<string, string> = {
  input: "用户问题",
  guardrail: "Guardrail",
  coordinator: "TurnCoordinator",
  context: "ContextBuilder",
  toolkit: "AgentToolkit",
  llm: "DeepSeek LLM",
  sse: "typed SSE",
  agent_runs: "agent_runs",
  memory: "记忆召回",
  history: "历史+摘要",
  "tool:search_knowledge": "search_knowledge",
  "tool:get_document": "get_document",
  "tool:list_documents": "list_documents",
  "tool:calculate": "calculate",
  "tool:get_current_time": "get_current_time",
  rag: "RagService",
  bm25: "BM25",
  vector: "Vector",
  rrf: "RRF",
  reranker: "Reranker",
};

// 分支边（仅展开）
const BRANCH_EDGES: [string, string][] = [
  ["context", "memory"],
  ["context", "history"],
  ["toolkit", "tool:search_knowledge"],
  ["toolkit", "tool:get_document"],
  ["toolkit", "tool:list_documents"],
  ["toolkit", "tool:calculate"],
  ["toolkit", "tool:get_current_time"],
  ["tool:search_knowledge", "rag"],
  ["rag", "bm25"],
  ["rag", "vector"],
  ["bm25", "rrf"],
  ["vector", "rrf"],
  ["rrf", "reranker"],
  ["reranker", "llm"],
];
// 主链纵向边（始终渲染；折叠时 toolkit→llm 直连）
const MAIN_EDGES: [string, string][] = [
  ["input", "guardrail"],
  ["guardrail", "coordinator"],
  ["coordinator", "context"],
  ["context", "toolkit"],
  ["toolkit", "llm"],
  ["llm", "sse"],
  ["sse", "agent_runs"],
];

interface AgentArchDiagramProps {
  lit: Set<string>;
  current: string | null;
  error?: boolean;
}

function edgePath(a: Rect, b: Rect): string {
  const x1 = a.x + a.w / 2;
  const y1 = a.y + a.h;
  const x2 = b.x + b.w / 2;
  const y2 = b.y;
  if (Math.abs(y1 - y2) < 4) {
    return `M ${Math.min(x1, x2)} ${y1} H ${Math.max(x1, x2)}`;
  }
  const midY = Math.max(y1 + 8, (y1 + y2) / 2 + 8);
  return `M ${x1} ${y1} V ${midY} H ${x2} V ${y2}`;
}

export default function AgentArchDiagram({ lit, current, error }: AgentArchDiagramProps) {
  const [showBranches, setShowBranches] = useState(true);
  const isError = error;
  const svgH = showBranches ? SVG_H_UNFOLDED : SVG_H_FOLDED;
  const mainY = MAIN_Y[showBranches ? "unfolded" : "folded"];

  const posFor = (id: string): Rect | undefined => {
    if (MAIN_IDS.includes(id)) {
      return { x: MAIN_X, y: mainY[MAIN_IDS.indexOf(id)], w: MAIN_W, h: MAIN_H };
    }
    return showBranches ? BRANCH_POS[id] : undefined;
  };

  const allEdges = showBranches ? [...MAIN_EDGES, ...BRANCH_EDGES] : MAIN_EDGES;
  // 折叠时 toolkit→llm 直连，去掉 reranker→llm/聚合边依赖
  const toolLit = ["tool:search_knowledge", "tool:get_document", "tool:list_documents", "tool:calculate", "tool:get_current_time"].some((t) => lit.has(t));

  return (
    <div className="flex-1 min-w-0 flex flex-col">
      {/* 面板头 + 图例（h-12 与 Chat/侧栏 header 对齐）+ 分支折叠 */}
      <div className="h-12 px-4 border-b border-line flex items-center gap-3">
        <div className="font-mono text-xs font-semibold tracking-techy text-ink">Agent 运行</div>
        <div className="font-mono text-2xs text-mute tracking-techy">
          {lit.size === 0 ? "发送问题后高亮实际路径" : `路径 ${lit.size} 模块`}
        </div>
        <button
          type="button"
          onClick={() => setShowBranches((v) => !v)}
          className="ml-auto font-mono text-[10px] text-mute hover:text-ink border border-line rounded px-2 py-0.5 transition-colors"
          title={showBranches ? "收起分支，只看主链" : "展开工具/检索栈分支"}
        >
          {showBranches ? "收起分支 ▾" : "展开分支 ▸"}
        </button>
        <div className="flex items-center gap-3 font-mono text-[10px] text-mute">
          <span className="flex items-center gap-1">
            <span className="w-3 h-0 border-t-2 border-teal" /> 已走
          </span>
          <span className="flex items-center gap-1">
            <span className="w-3 h-0 border-t border-dashed border-line" /> 未走
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2.5 h-2.5 rounded-sm border-2 border-amber bg-amber/10" /> 当前
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2.5 h-2.5 rounded-sm border-2 border-teal" /> 使用
          </span>
        </div>
      </div>
      {/* 大图区：固定像素 SVG（字不缩放），overflow 滚动 */}
      <div className="flex-1 overflow-auto p-4">
        <div style={{ width: SVG_W }}>
          <svg width={SVG_W} height={svgH} role="img" aria-label="Agent 全链路架构图">
            <defs>
              <marker id="arch-arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" className="fill-teal/70" />
              </marker>
            </defs>
            {showBranches && (
              <>
                <text x={24} y={436} className="fill-mute font-mono text-[10px]">Agent 工具</text>
                <text x={24} y={526} className="fill-mute font-mono text-[10px]">检索栈</text>
              </>
            )}
            {/* 边 */}
            {allEdges.map(([from, to], i) => {
              const a = posFor(from);
              const b = posFor(to);
              if (!a || !b) return null;
              const on = lit.has(from) && lit.has(to);
              return (
                <path
                  key={i}
                  d={edgePath(a, b)}
                  fill="none"
                  strokeWidth={on ? 2 : 1.2}
                  strokeDasharray={on ? undefined : "4 4"}
                  markerEnd={on ? "url(#arch-arrow)" : undefined}
                  className={on ? "stroke-teal/70" : "stroke-line"}
                />
              );
            })}
            {/* 展开态：工具结果返回聚合边（右侧绕行，不穿检索栈） */}
            {showBranches && (
              <path
                d={`M ${BRANCH_POS["tool:get_current_time"].x + BRANCH_POS["tool:get_current_time"].w} ${BRANCH_POS["tool:get_current_time"].y + BRANCH_POS["tool:get_current_time"].h} V ${MAIN_Y.unfolded[5] - 26} H ${MAIN_X - 12} V ${MAIN_Y.unfolded[5]}`}
                fill="none"
                strokeWidth={toolLit ? 2 : 1.2}
                strokeDasharray={toolLit ? undefined : "4 4"}
                markerEnd={toolLit ? "url(#arch-arrow)" : undefined}
                className={toolLit ? "stroke-teal/70" : "stroke-line"}
              />
            )}
            {/* 节点 */}
            {ARCH_NODES.filter((n) => posFor(n.id)).map((n) => {
              const p = posFor(n.id)!;
              const isLit = lit.has(n.id);
              const isCurrent = n.id === current;
              const isErr = isError && (n.id === "coordinator" || n.id === "llm");
              const main = MAIN_IDS.includes(n.id);
              const rectClass = [
                "fill-paper transition-colors duration-300",
                isErr
                  ? "stroke-oxblood stroke-[2]"
                  : isCurrent
                    ? "stroke-amber stroke-[2.5] fill-amber/10"
                    : isLit
                      ? "stroke-teal stroke-2"
                      : "stroke-line stroke-[1.5]",
              ].join(" ");
              return (
                <g key={n.id} transform={`translate(${p.x}, ${p.y})`}>
                  <rect width={p.w} height={p.h} rx="10" className={rectClass} />
                  <text
                    x={p.w / 2}
                    y={main ? 24 : 22}
                    textAnchor="middle"
                    className={[
                      "font-mono font-semibold transition-colors duration-300",
                      main ? "text-[14px]" : "text-[12px]",
                      isLit || isCurrent || isErr ? "fill-ink" : "fill-mute/60",
                    ].join(" ")}
                  >
                    {LABELS[n.id] ?? n.label}
                  </text>
                  {main && (
                    <text x={p.w / 2} y={44} textAnchor="middle" className="fill-mute font-mono text-[10px]">
                      {n.sub ?? ""}
                    </text>
                  )}
                </g>
              );
            })}
          </svg>
        </div>
      </div>
    </div>
  );
}
