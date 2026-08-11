// AgentArchDiagram - Agent 模式全链路架构图（waku trace 四原则重设计）
// ① 布局：手写 SVG 坐标，语义重量决定尺寸位置（核心模块大、辅助模块小），算法只对齐
// ② 数据流：SSE 事件统一驱动（架构插桩，非后加皮肤）——来自 archMap 映射
// ③ 视觉编码：1 强调色(teal) + 灰度 + 线型区分（激活实线/未激活虚线），每个变量承载一个语义
// ④ 动画：STAGE 映射 + 叙事节奏（点亮沉淀 lit、当前用稳定强调色，不用装饰性闪烁）
import { ARCH_NODES } from "./archMap";

// ---------- 语义重量（布局：语义决定尺寸） ----------
// 大 = 编排/工具循环/检索/LLM/输出 等关键链路；小 = 入口/守卫/辅助
const SIZE: Record<string, { w: number; h: number }> = {
  large: { w: 176, h: 54 },
  medium: { w: 132, h: 44 },
  small: { w: 104, h: 40 },
};

function sizeOf(id: string): { w: number; h: number } {
  if (
    ["coordinator", "toolkit", "search_knowledge", "rag", "llm", "sse"].includes(id)
  ) {
    return SIZE.large;
  }
  if (
    ["get_document", "list_documents", "calculate", "get_current_time", "reranker", "agent_runs"].includes(id)
  ) {
    return SIZE.medium;
  }
  return SIZE.small; // input/guardrail/context/memory/history/bm25/vector/rrf
}

const ROW_GAP = 30;
const COL_GAP = 16;
const PAD_X = 30;
const PAD_Y = 24;

interface AgentArchDiagramProps {
  lit: Set<string>;
  current: string | null;
  error?: boolean;
}

// 节点 → 像素坐标（按 row/col 排布，宽度用语义重量）
function layout() {
  const pos = new Map<string, { x: number; y: number; w: number; h: number }>();
  const rowHeights = new Map<number, number>();
  const rowCols = new Map<number, number>();
  for (const n of ARCH_NODES) {
    const sz = sizeOf(n.id);
    const idx = rowCols.get(n.row) ?? 0;
    rowCols.set(n.row, idx + 1);
    rowHeights.set(n.row, Math.max(rowHeights.get(n.row) ?? 0, sz.h));
  }
  const rowY = new Map<number, number>();
  let y = PAD_Y;
  for (let r = 0; r <= Math.max(...ARCH_NODES.map((n) => n.row)); r++) {
    rowY.set(r, y);
    y += (rowHeights.get(r) ?? SIZE.small.h) + ROW_GAP;
  }
  for (const n of ARCH_NODES) {
    const sz = sizeOf(n.id);
    const idx = rowCols.get(n.row) ?? 0;
    // 行内按语义重量累加列偏移（col 顺序），当前实现按声明顺序依次排开
    pos.set(n.id, { x: PAD_X + idx * (sz.w + COL_GAP), y: rowY.get(n.row)!, w: sz.w, h: sz.h });
  }
  const maxCols = Math.max(...rowCols.values());
  const svgW = PAD_X * 2 + maxCols * SIZE.large.w + (maxCols - 1) * COL_GAP;
  const svgH = y;
  return { pos, svgW, svgH };
}

const LAYOUT = layout();

// ---------- 边（线型编码：激活=teal 实线，未激活=灰虚线） ----------
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
  const x1 = a.x + a.w / 2;
  const y1 = a.y + a.h;
  const x2 = b.x + b.w / 2;
  const y2 = b.y;
  const midY = (y1 + y2) / 2;
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

export default function AgentArchDiagram({ lit, current, error }: AgentArchDiagramProps) {
  const isError = error;
  return (
    <div className="flex-1 min-w-0 flex flex-col">
      {/* 面板头 + 图例（waku ③ 视觉编码说明） */}
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
      {/* 大图区域（中间 flex-1，节点可放大） */}
      <div className="flex-1 overflow-auto p-4">
        <svg
          viewBox={`0 0 ${LAYOUT.svgW} ${LAYOUT.svgH}`}
          className="w-full h-auto"
          role="img"
          aria-label="Agent 全链路架构图"
        >
          <defs>
            <marker id="arch-arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" className="fill-teal/70" />
            </marker>
          </defs>
          {EDGES.map(([from, to], i) => (
            <EdgeLine key={i} from={from} to={to} lit={lit.has(from) && lit.has(to)} />
          ))}
          {ARCH_NODES.map((n) => {
            const p = LAYOUT.pos.get(n.id)!;
            const isLit = lit.has(n.id);
            const isCurrent = n.id === current;
            const isErr = isError && (n.id === "coordinator" || n.id === "llm");
            // ④ 叙事动画：current 用稳定强调色（transition 平滑，无装饰性闪烁）
            const rectClass = [
              "fill-paper transition-colors duration-300",
              isErr
                ? "stroke-oxblood stroke-2"
                : isCurrent
                  ? "stroke-amber stroke-[2.5] fill-amber/10"
                  : isLit
                    ? "stroke-teal stroke-2"
                    : "stroke-line stroke-[1.5]",
            ].join(" ");
            return (
              <g key={n.id} transform={`translate(${p.x}, ${p.y})`}>
                <rect width={p.w} height={p.h} rx="9" className={rectClass} />
                <text
                  x={p.w / 2}
                  y={n.sub ? p.h / 2 - 2 : p.h / 2 + 4}
                  textAnchor="middle"
                  className={[
                    "font-mono font-semibold transition-colors duration-300",
                    p.w >= 150 ? "text-[12px]" : "text-[11px]",
                    isLit || isCurrent || isErr ? "fill-ink" : "fill-mute/60",
                  ].join(" ")}
                >
                  {n.label}
                </text>
                {n.sub && (
                  <text x={p.w / 2} y={p.h / 2 + 13} textAnchor="middle" className="fill-mute font-mono text-[9px]">
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
