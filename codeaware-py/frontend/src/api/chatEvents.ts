// C1-A: typed Chat SSE 事件类型（对齐后端 app/schemas/chat_events.py）

export interface ChatEventBase {
  protocol_version: 1;
  conversation_id: string;
  turn_id: string;
  sequence: number;
}

export interface ChatStarted extends ChatEventBase {
  created: boolean;
}

export const WARNING_COMPONENTS = [
  "message_cache",
  "summary_cache",
  "memory_recall",
  "rag_retrieval",
  "summary",
  "memory_extraction",
  "route", // LangGraph 智能路由（ROUTE_DIRECT / ROUTE_DECIDE_FAILED）
  "rag_graph", // LangGraph 检索图（RAG_NOT_FOUND / RAG_GRAPH_FAILED）
] as const;
export type WarningComponent = (typeof WARNING_COMPONENTS)[number];

export interface ContextWarning extends ChatEventBase {
  component: WarningComponent;
  code: string;
  message: string;
  retryable: boolean;
}
export interface TokenDelta extends ChatEventBase {
  delta: string;
}
export interface ReasoningDelta extends ChatEventBase {
  delta: string;
}
export interface KnowledgeRef {
  document_id: number;
  title: string;
  snippet: string;
  match_type: string;
  score: number;
}
export interface MemoryRef {
  content: string;
  memory_type: string;
  similarity: number;
}
export interface ContextReferences extends ChatEventBase {
  knowledge_refs: KnowledgeRef[];
  memory_refs: MemoryRef[];
}
export interface ToolCall extends ChatEventBase {
  tool_name: string;
  tool_args: Record<string, unknown>;
  tool_call_id: string;
}
export interface ToolResult extends ChatEventBase {
  tool_call_id: string;
  tool_name: string;
  status: "ok" | "error";
  result: string;
}
export interface PostTurnWarning extends ChatEventBase {
  component: WarningComponent;
  code: string;
  message: string;
  retryable: boolean;
}
export interface ChatCompleted extends ChatEventBase {
  assistant_message_id: number;
  warning_count: number;
}
export interface ErrorInfo {
  code: string;
  message: string;
  retryable: boolean;
}

export const FAILURE_PHASES = [
  "start",
  "context",
  "model",
  "persist",
  "post_turn",
  "cancelled",
] as const;
export type FailurePhase = (typeof FAILURE_PHASES)[number];

export interface ChatFailed extends ChatEventBase {
  phase: FailurePhase;
  error: ErrorInfo;
  partial_output_persisted: false;
}

export type ChatEvent =
  | ChatStarted
  | ContextWarning
  | ContextReferences
  | ReasoningDelta
  | TokenDelta
  | ToolCall
  | ToolResult
  | PostTurnWarning
  | ChatCompleted
  | ChatFailed;

export const EVENT_NAMES = [
  "chat.started",
  "context.warning",
  "context.references",
  "reasoning.delta",
  "token.delta",
  "tool.call",
  "tool.result",
  "post_turn.warning",
  "chat.completed",
  "chat.failed",
] as const;

export type EventName = (typeof EVENT_NAMES)[number];
