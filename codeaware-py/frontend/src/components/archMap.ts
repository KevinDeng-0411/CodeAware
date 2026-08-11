// archMap - Agent 模式全链路架构图定义 + SSE 事件→模块映射（ADR-0017 / Chat 页重设计）
// 静态拓扑：模块与连线固定（每次运行都一样），一次画好；实时高亮 = 事件驱动切 CSS class。
// 纯函数、事件源无关——回放 trace 与实时 SSE 走同一映射。
import type { ContextReferences } from "../api/chatEvents";

// ---------- 模块节点（全链路） ----------
export interface ArchNode {
  id: string;
  label: string;
  sub?: string;
  row: number;
  col: number; // 行内位置，用于布局（col = 显示顺序，非像素）
  kind: "pipeline" | "context" | "tool" | "retrieval" | "terminal";
}

export const ARCH_NODES: ArchNode[] = [
  // 行0：入口
  { id: "input", label: "用户问题", row: 0, col: 0, kind: "pipeline" },
  { id: "guardrail", label: "Guardrail", sub: "ChatRequest", row: 0, col: 1, kind: "pipeline" },
  // 行1：编排
  { id: "coordinator", label: "TurnCoordinator", sub: "编排状态机", row: 1, col: 0, kind: "pipeline" },
  // 行2：上下文
  { id: "context", label: "ContextBuilder", row: 2, col: 0, kind: "context" },
  { id: "memory", label: "记忆召回", sub: "长时记忆", row: 2, col: 1, kind: "context" },
  { id: "history", label: "历史+摘要", sub: "短时记忆", row: 2, col: 2, kind: "context" },
  // 行3：Agent 工具
  { id: "toolkit", label: "AgentToolkit", sub: "5 工具", row: 3, col: 0, kind: "pipeline" },
  { id: "tool:search_knowledge", label: "search_knowledge", row: 3, col: 1, kind: "tool" },
  { id: "tool:get_document", label: "get_document", row: 3, col: 2, kind: "tool" },
  { id: "tool:list_documents", label: "list_documents", row: 3, col: 3, kind: "tool" },
  { id: "tool:calculate", label: "calculate", row: 3, col: 4, kind: "tool" },
  { id: "tool:get_current_time", label: "get_current_time", row: 3, col: 5, kind: "tool" },
  // 行4：检索栈
  { id: "rag", label: "RagService", row: 4, col: 0, kind: "retrieval" },
  { id: "bm25", label: "BM25", sub: "pg_search", row: 4, col: 1, kind: "retrieval" },
  { id: "vector", label: "Vector", sub: "pgvector", row: 4, col: 2, kind: "retrieval" },
  { id: "rrf", label: "RRF", row: 4, col: 3, kind: "retrieval" },
  { id: "reranker", label: "Reranker", sub: "ONNX", row: 4, col: 4, kind: "retrieval" },
  // 行5：LLM
  { id: "llm", label: "DeepSeek", sub: "thinking", row: 5, col: 0, kind: "pipeline" },
  // 行6：输出
  { id: "sse", label: "typed SSE", sub: "10 事件", row: 6, col: 0, kind: "terminal" },
  { id: "agent_runs", label: "agent_runs", sub: "trace 落库", row: 6, col: 1, kind: "terminal" },
];

// 检索栈整组（search_knowledge 触发时一起点亮）
export const RETRIEVAL_STACK = ["rag", "bm25", "vector", "rrf", "reranker"] as const;

// ---------- 事件→模块点亮映射（纯函数） ----------
// 每次调用返回"本事件新增点亮的模块"，调用方累积 lit 集合。

export function archOnStarted(): string[] {
  return ["input", "guardrail", "coordinator"];
}

export function archOnReferences(refs: ContextReferences): string[] {
  const lit = ["context"];
  if (refs.memory_refs.length > 0) lit.push("memory");
  return lit;
}

export function archOnModel(): string[] {
  return ["llm"];
}

export function archOnToolCall(name: string): string[] {
  const lit = ["toolkit", `tool:${name}`];
  if (name === "search_knowledge") {
    lit.push(...RETRIEVAL_STACK);
  }
  return lit;
}

export function archOnCompleted(): string[] {
  return ["sse", "agent_runs"];
}

// 错误路径：编排/LLM 标 error（调用方置 error 态）
export const ARCH_ERROR_IDS = ["coordinator", "llm"];

// 当前执行模块（供 current 高亮；无则 null）
export function archCurrentOnToolCall(name: string): string {
  return `tool:${name}`;
}
export function archCurrentOnModel(): string {
  return "llm";
}
