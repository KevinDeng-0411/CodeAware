// FlowTrace - Agent 执行流程视图（ADR-0017 三层架构）
// ① 静态 SVG 层：每 run 生成一次节点/边骨架，稳定 node id
// ② 事件流层：归一化 trace 条目按序喂入（回放 = 步进；未来实时 = SSE）
// ③ STAGE 映射表（flowMap）：trace 条目 → node id，点亮 = CSS class 切换（非逐帧 JS 动画）
// 事件源无关：本组件只消费 FlowGraph，回放/实时同一实现。
import { useEffect, useMemo, useState } from "react";
import { Pause, Play, RotateCcw, StepForward } from "lucide-react";
import type { FlowGraph } from "./flowMap";

const NODE_W = 250;
const NODE_H = 62;
const GAP_Y = 30;
const PAD_X = 48;
const PAD_Y = 24;

export default function FlowTrace({
  graph,
  onOpenDoc,
}: {
  graph: FlowGraph;
  onOpenDoc?: (docIds: number[]) => void;
}) {
  const [current, setCurrent] = useState(-1); // 当前步进到的 trace 条目 index（-1=未开始）
  const [playing, setPlaying] = useState(false);
  const entryCount = graph.stage.size;

  // 播放：逐条喂事件（SSE 推实时 + 轮询补全局在 P2；回放 = 本地定时步进）
  useEffect(() => {
    if (!playing) return;
    const t = setInterval(() => {
      setCurrent((c) => {
        if (c >= entryCount - 1) {
          setPlaying(false);
          return c;
        }
        return c + 1;
      });
    }, 650);
    return () => clearInterval(t);
  }, [playing, entryCount]);

  const currentNodeId = current >= 0 ? graph.stage.get(current) ?? null : null;
  const visited = useMemo(() => {
    const s = new Set<string>();
    for (let i = 0; i < current; i++) {
      const nid = graph.stage.get(i);
      if (nid) s.add(nid);
    }
    return s;
  }, [current, graph.stage]);

  const nodeY = (i: number) => PAD_Y + i * (NODE_H + GAP_Y);
  const svgH = PAD_Y * 2 + graph.nodes.length * NODE_H + (graph.nodes.length - 1) * GAP_Y;

  const togglePlay = () => {
    if (playing) {
      setPlaying(false);
    } else if (current >= entryCount - 1) {
      setCurrent(-1);
      setPlaying(true);
    } else {
      setPlaying(true);
    }
  };
  const step = () => {
    setPlaying(false);
    setCurrent((c) => Math.min(c + 1, Math.max(entryCount - 1, 0)));
  };
  const reset = () => {
    setPlaying(false);
    setCurrent(-1);
  };

  const controls = (
    <div className="flex items-center gap-2 mb-3">
      <button
        type="button"
        onClick={reset}
        title="重置"
        className="p-1.5 rounded border border-line text-mute hover:text-ink hover:border-teal/60 transition-colors"
      >
        <RotateCcw className="w-3.5 h-3.5" />
      </button>
      <button
        type="button"
        onClick={togglePlay}
        title={playing ? "暂停" : "播放"}
        className="p-1.5 rounded border border-line text-mute hover:text-ink hover:border-amber/60 transition-colors"
      >
        {playing ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
      </button>
      <button
        type="button"
        onClick={step}
        title="单步"
        className="p-1.5 rounded border border-line text-mute hover:text-ink hover:border-teal/60 transition-colors"
      >
        <StepForward className="w-3.5 h-3.5" />
      </button>
      <span className="font-mono text-2xs text-mute tracking-techy">
        {current + 1} / {entryCount}
      </span>
      <span className="ml-auto font-mono text-2xs text-mute/60 tracking-techy hidden sm:inline">
        点击 📄 节点 → 知识库
      </span>
    </div>
  );

  return (
    <div className="rounded border border-line bg-panel/40 p-3 overflow-x-auto">
      {controls}
      {entryCount === 0 && (
        <p className="font-mono text-2xs text-mute/70 mb-2">该 run 无工具轨迹（直接回答）</p>
      )}
      <svg
        viewBox={`0 0 ${PAD_X * 2 + NODE_W} ${svgH}`}
        className="min-w-[360px]"
        role="img"
        aria-label="Agent 执行流程"
      >
        <defs>
          <marker
            id="flow-arrow"
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
        {graph.edges.map((e, i) => {
          const fromIdx = graph.nodes.findIndex((n) => n.id === e.from);
          const toIdx = graph.nodes.findIndex((n) => n.id === e.to);
          if (fromIdx < 0 || toIdx < 0) return null;
          const x = PAD_X + NODE_W / 2;
          return (
            <line
              key={i}
              x1={x}
              y1={nodeY(fromIdx) + NODE_H}
              x2={x}
              y2={nodeY(toIdx)}
              className="stroke-line"
              strokeWidth="1.5"
              markerEnd="url(#flow-arrow)"
            />
          );
        })}
        {graph.nodes.map((n, i) => {
          const y = nodeY(i);
          const isCurrent = n.id === currentNodeId;
          const isDone = visited.has(n.id);
          const clickable = Boolean(n.docIds && n.docIds.length > 0 && onOpenDoc);
          const rectClass = [
            "fill-panel",
            isCurrent
              ? "stroke-amber stroke-[2.5] animate-pulse"
              : isDone
                ? "stroke-teal/70 stroke-[1.5]"
                : "stroke-line stroke-[1.5]",
          ].join(" ");
          return (
            <g
              key={n.id}
              transform={`translate(${PAD_X}, ${y})`}
              onClick={clickable ? () => onOpenDoc?.(n.docIds ?? []) : undefined}
              role={clickable ? "button" : undefined}
              aria-label={n.label}
              className={clickable ? "cursor-pointer" : undefined}
            >
              <rect width={NODE_W} height={NODE_H} rx="10" className={rectClass} />
              <text x={14} y={26} className="fill-ink font-mono text-2xs font-semibold">
                {n.label}
              </text>
              {n.sub && (
                <text x={14} y={46} className="fill-mute font-mono text-[10px]">
                  {n.sub}
                </text>
              )}
              {n.docIds && n.docIds.length > 0 && (
                <text x={NODE_W - 22} y={26} className="fill-amber text-xs">
                  📄
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
