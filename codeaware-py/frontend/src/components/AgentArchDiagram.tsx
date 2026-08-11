// AgentArchDiagram - Agent 全链路架构图（纵向主链 + waku 四原则）
// ① 布局：手写坐标（纵向主链 + 每层横向分支），语义重量决定尺寸，零遮挡
// ② 数据流：SSE 事件统一驱动（archMap 映射）
// ③ 视觉编码：单强调色(teal) + 灰度 + 激活实线/未激活虚线
// ④ 动画：稳定强调色 + transition（叙事非装饰）
// 关键：SVG 用固定像素尺寸（不缩放）→ 字号恒定清晰，容器 overflow 滚动。
import { ARCH_NODES } from "./archMap";

// ---------- 手写坐标布局（纵向主链） ----------
// 主链节点（宽大、字大）：input→guardrail→coordinator→context→toolkit→llm→sse→agent_runs
// 分支节点（横向展开）：记忆/历史（context 旁）、5 工具（toolkit 下）、检索栈（search 下）
interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

const MAIN_W = 236;
const MAIN_H = 58;
const MAIN_X = 96; // 主链 x（左对齐，工具/检索栈与其对齐）
const BRANCH_W = 150;
const BRANCH_H = 50;

const POS: Record<string, Rect> = {
  // 主链（纵向，y 每层 +86）
  input: { x: MAIN_X, y: 12, w: MAIN_W, h: MAIN_H },
  guardrail: { x: MAIN_X, y: 98, w: MAIN_W, h: MAIN_H },
  coordinator: { x: MAIN_X, y: 184, w: MAIN_W, h: MAIN_H },
  context: { x: MAIN_X, y: 270, w: MAIN_W, h: MAIN_H },
  toolkit: { x: MAIN_X, y: 356, w: MAIN_W, h: MAIN_H },
  llm: { x: MAIN_X, y: 640, w: MAIN_W, h: MAIN_H },
  sse: { x: MAIN_X, y: 726, w: MAIN_W, h: MAIN_H },
  agent_runs: { x: MAIN_X, y: 812, w: MAIN_W, h: MAIN_H },
  // context 分支（右侧横排）
  memory: { x: 380, y: 282, w: 170, h: 44 },
  history: { x: 580, y: 282, w: 170, h: 44 },
  // 工具（toolkit 下横排 5 个）
  "tool:search_knowledge": { x: 24, y: 442, w: BRANCH_W, h: BRANCH_H },
  "tool:get_document": { x: 194, y: 442, w: BRANCH_W, h: BRANCH_H },
  "tool:list_documents": { x: 364, y: 442, w: BRANCH_W, h: BRANCH_H },
  "tool:calculate": { x: 534, y: 442, w: BRANCH_W, h: BRANCH_H },
  "tool:get_current_time": { x: 704, y: 442, w: BRANCH_W, h: BRANCH_H },
  // 检索栈（search 下横排 5 个）
  rag: { x: 24, y: 532, w: BRANCH_W, h: BRANCH_H },
  bm25: { x: 194, y: 532, w: BRANCH_W, h: BRANCH_H },
  vector: { x: 364, y: 532, w: BRANCH_W, h: BRANCH_H },
  rrf: { x: 534, y: 532, w: BRANCH_W, h: BRANCH_H },
  reranker: { x: 704, y: 532, w: BRANCH_W, h: BRANCH_H },
};

// SVG 固定像素（不缩放 → 字恒定清晰），容器 overflow-auto
const SVG_W = 900;
const SVG_H = 890;

// 节点标签（覆盖 ARCH_NODES 的 label，主链用中文+英文）
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

// ---------- 边（线型编码：激活 teal 实线 / 未激活灰虚线） ----------
// 工具→LLM 用一条"工具结果返回"聚合边（右侧绕行，避免横穿检索栈层）→ 零遮挡
const EDGES: [string, string][] = [
  ["input", "guardrail"],
  ["guardrail", "coordinator"],
  ["coordinator", "context"],
  ["context", "memory"],
  ["context", "history"],
  ["context", "toolkit"],
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
  ["llm", "sse"],
  ["sse", "agent_runs"],
];

interface AgentArchDiagramProps {
  lit: Set<string>;
  current: string | null;
  error?: boolean;
}

// 通用正交边：同层（y 近）画水平；跨层先下再横再下，拐角取下方空隙（避免穿检索栈层）
function EdgeLine({ from, to, lit }: { from: string; to: string; lit: boolean }) {
  const a = POS[from];
  const b = POS[to];
  if (!a || !b) return null;
  const x1 = a.x + a.w / 2;
  const y1 = a.y + a.h;
  const x2 = b.x + b.w / 2;
  const y2 = b.y;
  // 水平相邻（同层：context→memory / memory→history）
  if (Math.abs(y1 - y2) < 4) {
    const left = Math.min(x1, x2);
    const right = Math.max(x1, x2);
    return (
      <path
        d={`M ${left} ${y1} H ${right}`}
        fill="none"
        strokeWidth={lit ? 2 : 1.2}
        strokeDasharray={lit ? undefined : "4 4"}
        markerEnd={lit ? "url(#arch-arrow)" : undefined}
        className={lit ? "stroke-teal/70" : "stroke-line"}
      />
    );
  }
  // 跨层：拐角取两节点中点偏下（reranker→llm 落在检索栈下方空隙）
  const midY = Math.max(y1 + 8, (y1 + y2) / 2 + 8);
  return (
    <path
      d={`M ${x1} ${y1} V ${midY} H ${x2} V ${y2}`}
      fill="none"
      strokeWidth={lit ? 2 : 1.2}
      strokeDasharray={lit ? undefined : "4 4"}
      markerEnd={lit ? "url(#arch-arrow)" : undefined}
      className={lit ? "stroke-teal/70" : "stroke-line"}
    />
  );
}

// 工具结果返回聚合边：从工具行右端向下绕行到 LLM（不穿检索栈）
function ToolReturnEdge({ lit }: { lit: boolean }) {
  const startX = POS["tool:get_current_time"].x + POS["tool:get_current_time"].w; // 854
  const topY = POS["tool:get_current_time"].y + POS["tool:get_current_time"].h; // 492
  const llmLeft = POS.llm.x; // 96
  const llmTop = POS.llm.y; // 640
  const midY = llmTop - 26; // 检索栈(532-582)下方的空隙
  return (
    <path
      d={`M ${startX} ${topY} V ${midY} H ${llmLeft - 12} V ${llmTop}`}
      fill="none"
      strokeWidth={lit ? 2 : 1.2}
      strokeDasharray={lit ? undefined : "4 4"}
      markerEnd={lit ? "url(#arch-arrow)" : undefined}
      className={lit ? "stroke-teal/70" : "stroke-line"}
    />
  );
}

export default function AgentArchDiagram({ lit, current, error }: AgentArchDiagramProps) {
  const isError = error;
  return (
    <div className="flex-1 min-w-0 flex flex-col">
      {/* 面板头 + 图例 */}
      <div className="px-4 py-2.5 border-b border-line flex items-center gap-4 flex-wrap">
        <div className="font-mono text-xs font-semibold tracking-techy text-ink">Agent 运行</div>
        <div className="font-mono text-2xs text-mute tracking-techy">
          {lit.size === 0 ? "发送问题后高亮实际路径" : `路径 ${lit.size} 模块`}
        </div>
        <div className="ml-auto flex items-center gap-3 font-mono text-[10px] text-mute">
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
        <div className="w-[900px]">
          <svg width={SVG_W} height={SVG_H} role="img" aria-label="Agent 全链路架构图">
            <defs>
              <marker id="arch-arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" className="fill-teal/70" />
              </marker>
            </defs>
            {/* 层标签（语义分组） */}
            <text x={24} y={436} className="fill-mute font-mono text-[10px]">Agent 工具</text>
            <text x={24} y={526} className="fill-mute font-mono text-[10px]">检索栈</text>
            {/* 边 */}
            {EDGES.map(([from, to], i) => (
              <EdgeLine key={i} from={from} to={to} lit={lit.has(from) && lit.has(to)} />
            ))}
            {/* 工具结果返回聚合边（lit = 任意工具已用） */}
            <ToolReturnEdge
              lit={["tool:search_knowledge", "tool:get_document", "tool:list_documents", "tool:calculate", "tool:get_current_time"].some((t) => lit.has(t))}
            />
            {/* 节点 */}
            {ARCH_NODES.map((n) => {
              const p = POS[n.id];
              if (!p) return null;
              const isLit = lit.has(n.id);
              const isCurrent = n.id === current;
              const isErr = isError && (n.id === "coordinator" || n.id === "llm");
              const main = p.w >= 200; // 主链大节点
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
