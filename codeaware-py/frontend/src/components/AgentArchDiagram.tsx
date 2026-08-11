// AgentArchDiagram - Agent 模式全链路架构图（静态 SVG + 事件驱动高亮）
// 静态拓扑一次画好（模块+边固定），运行中高亮 = 切 CSS class（done=teal / current=amber pulse）。
// 无增量生长问题：图是系统级固定骨架，事件只点亮"这一轮用到的部分"。
import { ARCH_NODES } from "./archMap";

const NODE_W = 128;
const NODE_H = 44;
const ROW_GAP = 36;
const COL_GAP = 14;
const PAD_X = 28;
const PAD_Y = 22;

interface AgentArchDiagramProps {
  lit: Set<string>; // 已用模块（done）
  current: string | null; // 当前执行模块（live 高亮）
  error?: boolean; // 本轮失败（coordinator/llm 标 error）
}

// 节点 id → 像素坐标（按 row/col 排布）
function layout() {
  const pos = new Map<string, { x: number; y: number }>();
  const rows = new Map<number, number>();
  for (const n of ARCH_NODES) {
    const idx = rows.get(n.row) ?? 0;
    rows.set(n.row, idx + 1);
    pos.set(n.id, {
      x: PAD_X + idx * (NODE_W + COL_GAP),
      y: PAD_Y + n.row * (NODE_H + ROW_GAP),
    });
  }
  const totalRows = Math.max(...ARCH_NODES.map((n) => n.row)) + 1;
  const maxCols = Math.max(...rows.values());
  return {
    pos,
    svgW: PAD_X * 2 + maxCols * NODE_W + (maxCols - 1) * COL_GAP,
    svgH: PAD_Y * 2 + totalRows * NODE_H + (totalRows - 1) * ROW_GAP,
  };
}

const LAYOUT = layout();

// 显式边：按数据流连接
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
  ["tool:search_knowledge", "llm"],
  ["tool:get_document", "llm"],
  ["tool:list_documents", "llm"],
  ["tool:calculate", "llm"],
  ["tool:get_current_time", "llm"],
  ["reranker", "llm"],
  ["llm", "sse"],
  ["sse", "agent_runs"],
];

function EdgeLine({ from, to, lit }: { from: string; to: string; lit: boolean }) {
  const a = LAYOUT.pos.get(from);
  const b = LAYOUT.pos.get(to);
  if (!a || !b) return null;
  const x1 = a.x + NODE_W / 2;
  const y1 = a.y + NODE_H;
  const x2 = b.x + NODE_W / 2;
  const y2 = b.y;
  // 避免横跨行的重复竖线视觉重叠：简单画直线 + 拐角
  const midY = (y1 + y2) / 2;
  return (
    <g>
      <path
        d={`M ${x1} ${y1} V ${midY} H ${x2} V ${y2}`}
        fill="none"
        strokeWidth="1.5"
        markerEnd="url(#arch-arrow)"
        className={lit ? "stroke-teal/60" : "stroke-line"}
      />
    </g>
  );
}

export default function AgentArchDiagram({ lit, current, error }: AgentArchDiagramProps) {
  return (
    <div className="w-[24rem] shrink-0 border-l border-line bg-panel flex flex-col">
      <div className="px-4 py-3 border-b border-line">
        <div className="font-mono text-xs font-semibold tracking-techy text-ink">Agent 运行</div>
        <div className="font-mono text-2xs text-mute tracking-techy mt-0.5">
          {lit.size === 0 ? "发送问题后高亮实际路径" : `已点亮 ${lit.size} 个模块`}
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-3">
        <svg
          viewBox={`0 0 ${LAYOUT.svgW} ${LAYOUT.svgH}`}
          className="w-full h-auto"
          role="img"
          aria-label="Agent 全链路架构图"
        >
          <defs>
            <marker
              id="arch-arrow"
              viewBox="0 0 10 10"
              refX="5"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" className="fill-line" />
            </marker>
          </defs>
          {EDGES.map(([from, to], i) => (
            <EdgeLine key={i} from={from} to={to} lit={lit.has(from) && lit.has(to)} />
          ))}
          {ARCH_NODES.map((n) => {
            const p = LAYOUT.pos.get(n.id)!;
            const isLit = lit.has(n.id);
            const isCurrent = n.id === current;
            const isError = error && (n.id === "coordinator" || n.id === "llm");
            const rectClass = [
              "fill-paper",
              isError
                ? "stroke-oxblood stroke-[2]"
                : isCurrent
                  ? "stroke-amber stroke-[2.5] animate-pulse"
                  : isLit
                    ? "stroke-teal stroke-2"
                    : "stroke-line stroke-[1.5]",
            ].join(" ");
            return (
              <g key={n.id} transform={`translate(${p.x}, ${p.y})`}>
                <rect width={NODE_W} height={NODE_H} rx="8" className={rectClass} />
                <text
                  x={NODE_W / 2}
                  y={n.sub ? 20 : 27}
                  textAnchor="middle"
                  className={[
                    "font-mono text-[11px] font-semibold",
                    isLit || isCurrent ? "fill-ink" : "fill-mute/70",
                  ].join(" ")}
                >
                  {n.label}
                </text>
                {n.sub && (
                  <text x={NODE_W / 2} y={36} textAnchor="middle" className="fill-mute font-mono text-[9px]">
                    {n.sub}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}
