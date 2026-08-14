// STAGE 映射表（ADR-0017 流程视图三层架构第③层）
// 把 trace 条目 → SVG node id + 跳转目标（docIds）。纯函数，事件源无关：
// 回放喂 trace、未来实时喂 SSE 都走同一映射，组件不感知数据源。
import type { TraceEntry } from "../api/types";

export type FlowNodeKind =
  | "start"
  | "thought"
  | "tool"
  | "observation"
  | "answer"
  | "override"
  | "reflection";

export interface FlowNode {
  id: string;
  kind: FlowNodeKind;
  label: string;
  sub?: string; // 次要行（args 预览 / chars / status）
  docIds?: number[]; // 知识库跳转目标
  status?: "ok" | "error";
}

export interface FlowEdge {
  from: string;
  to: string;
}

export interface FlowGraph {
  nodes: FlowNode[];
  edges: FlowEdge[];
  /** trace 条目 index → 应点亮节点 id（STAGE 映射） */
  stage: Map<number, string>;
}

function preview(o: unknown, max = 60): string {
  try {
    const s = JSON.stringify(o);
    return s.length > max ? `${s.slice(0, max)}…` : s;
  } catch {
    return String(o).slice(0, max);
  }
}

export function buildFlowGraph(trace: TraceEntry[], query: string): FlowGraph {
  const nodes: FlowNode[] = [{ id: "start", kind: "start", label: "用户问题", sub: query.slice(0, 40) }];
  const edges: FlowEdge[] = [];
  const stage = new Map<number, string>();
  let prev = "start";
  let counter = 0;

  trace.forEach((entry, i) => {
    switch (entry.type) {
      case "thought": {
        const id = `t${counter++}`;
        nodes.push({ id, kind: "thought", label: `思考 #${entry.step}`, sub: `· ${entry.chars} 字符` });
        edges.push({ from: prev, to: id });
        prev = id;
        stage.set(i, id);
        break;
      }
      case "tool_call": {
        const id = `tc${counter++}`;
        nodes.push({ id, kind: "tool", label: entry.name, sub: preview(entry.args) });
        edges.push({ from: prev, to: id });
        prev = id;
        stage.set(i, id);
        break;
      }
      case "tool_result": {
        const id = `tr${counter++}`;
        nodes.push({
          id,
          kind: "observation",
          label: "观察",
          sub: `${entry.status === "ok" ? "✓" : "✗"} ${entry.result.slice(0, 46)}`,
          docIds: entry.doc_ids.length ? entry.doc_ids : undefined,
          status: entry.status,
        });
        edges.push({ from: prev, to: id });
        prev = id;
        stage.set(i, id);
        break;
      }
      case "answer": {
        const id = "ans";
        if (!nodes.some((n) => n.id === id)) {
          nodes.push({ id, kind: "answer", label: "答案", sub: entry.content.slice(0, 40) });
          edges.push({ from: prev, to: id });
          prev = id;
        } else if (prev !== id) {
          edges.push({ from: prev, to: id });
          prev = id;
        }
        stage.set(i, id);
        break;
      }
      case "convergence_override": {
        const id = `ovr${counter++}`;
        nodes.push({
          id,
          kind: "override",
          label: "强制终答",
          sub: `忽略 ${entry.tool_calls?.length ?? 0} 个工具调用`,
        });
        edges.push({ from: prev, to: id });
        prev = id;
        stage.set(i, id);
        break;
      }
      case "reflection": {
        const id = `rf${counter++}`;
        nodes.push({
          id,
          kind: "reflection",
          label: `反射判定 #${entry.attempt}`,
          sub: `${entry.accepted ? "✓ 接受" : "✗ 拒绝"}${entry.feedback ? ` · ${entry.feedback.slice(0, 40)}` : ""}`,
        });
        edges.push({ from: prev, to: id });
        prev = id;
        stage.set(i, id);
        break;
      }
    }
  });

  return { nodes, edges, stage };
}
