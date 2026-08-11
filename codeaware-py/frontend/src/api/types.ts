// 后端 API 请求/响应类型 - 对齐 codeaware-py 的 Pydantic schemas + router 投影
// 统一响应包络：{ code: 1|0, msg, data }，code=1 成功
import type { WarningComponent } from "./chatEvents";

export interface Envelope<T> {
  code: number;
  msg: string;
  data: T;
}

// ---------- Code Review ----------
export interface ReviewIssue {
  dimension: string;
  severity: string; // Critical | Warning | Info
  line_range: string;
  title: string;
  description: string;
  suggestion: string;
  fix_code?: string | null;
}
export interface CodeReviewVO {
  id?: number;
  project_name?: string;
  file_path?: string;
  summary: string;
  score: number;
  issues: ReviewIssue[];
  highlights: string[];
  issues_count: number;
  critical_count: number;
  warning_count: number;
  info_count: number;
  ai_model?: string;
}

// ---------- Unit Test ----------
export interface UnitTestVO {
  id?: number;
  project_name?: string;
  file_path?: string;
  test_code: string;
  test_framework: string;
  ai_model?: string;
}

// ---------- AI ReadMe ----------
export interface AiReadmeVO {
  id?: number;
  project_name: string;
  title: string;
  content: string;
  version: number;
  snapshot_hash: string | null;
  snapshot_file_count: number | null;
  snapshot_generated_at: string | null;
  snapshot_truncated: boolean | null;
  ai_model?: string;
}

export interface AiReadmeCapability {
  enabled: boolean;
  reason: "available" | "disabled" | "roots_unavailable";
}

// ---------- Chat ----------
export interface ChatWarning {
  component: WarningComponent;
  code: string;
  message: string;
  retryable: boolean;
}

export interface ChatResponseVO {
  conversation_id: string;
  reply: string;
  memory_summary?: string | null;
  warnings: ChatWarning[];
}
export interface ConversationItem {
  id: number;
  conversation_id: string;
  title?: string | null;
  summary?: string | null;
}
export interface ChatMessage {
  role: string; // USER | ASSISTANT
  content: string;
}

// ---------- Knowledge ----------
export interface KnowledgeSearchHit {
  score: number;
  match_type: "vector" | "keyword" | "both";
  document_id: number;
  chunk_content: string;
}

export interface DocumentVO {
  id: number;
  title: string;
  source_type: string;
  project_name: string | null;
  status: "ACTIVE" | "DELETED";
  chunk_count: number;
  created_at: string;
  deleted_at: string | null;
}

export interface DocumentListVO {
  total: number;
  page: number;
  size: number;
  records: DocumentVO[];
}

export interface ChunkVO {
  chunk_index: number;
  chunk_content: string;
}

export interface DocumentDetailVO extends DocumentVO {
  updated_at: string;
  content: string;
  chunks: ChunkVO[];
}

// ---------- Memory ----------
export interface MemoryHit {
  id: number;
  content: string;
  memory_type: string;
  conversation_id?: string | null;
  source?: string; // "conversation"（对话内生）| "manual"（手动录入）
  similarity: number;
}

export interface MemoryListItem {
  id: number;
  content: string;
  memory_type: string;
  conversation_id?: string | null;
  source: string;
  created_at: string;
}

export interface MemoryListVO {
  total: number;
  page: number;
  size: number;
  records: MemoryListItem[];
}

// ---------- Agent Runs（ADR-0017）----------
export interface AgentRunListItem {
  id: number;
  turn_id: string;
  conversation_id: string;
  query: string;
  status: string; // completed | empty | error | cancelled
  stop_reason: string;
  steps: number;
  tool_calls: number;
  error_tools: number;
  needs_review: boolean;
  review_status: string; // pending | accepted | rejected
  synced: boolean;
  error: string | null;
  created_at: string | null;
}

export interface AgentRunListVO {
  total: number;
  page: number;
  size: number;
  records: AgentRunListItem[];
}

export interface TraceThought {
  type: "thought";
  step: number;
  chars: number;
  reasoning?: string; // 仅 agent_trace_include_reasoning=True 时存在
}
export interface TraceToolCall {
  type: "tool_call";
  step: number;
  name: string;
  args: Record<string, unknown>;
  call_id: string;
}
export interface TraceToolResult {
  type: "tool_result";
  step: number;
  call_id: string;
  status: "ok" | "error";
  result: string;
  doc_ids: number[];
}
export interface TraceAnswer {
  type: "answer";
  step: number;
  content: string;
}
export interface TraceConvergenceOverride {
  type: "convergence_override";
  step: number;
  tool_calls: unknown[];
}
export type TraceEntry =
  | TraceThought
  | TraceToolCall
  | TraceToolResult
  | TraceAnswer
  | TraceConvergenceOverride;

export interface MemoryRefVO {
  content: string;
  memory_type: string;
  similarity: number;
}
export interface ContextSnapshot {
  summary: string | null;
  window: { count: number };
  memory_refs: MemoryRefVO[];
  knowledge_refs: { document_id: number; title: string; snippet: string; match_type: string; score: number }[];
}

export interface AgentRunDetail extends AgentRunListItem {
  expected_tools: string[] | null;
  category: string | null;
  trace: TraceEntry[];
  context_snapshot: ContextSnapshot | null;
}

export interface AgentRunStats {
  total: number;
  needs_review_pending: number;
  status_counts: Record<string, number>;
}

export interface ToolUsageItem {
  tool: string;
  calls: number;
  errors: number;
}

export interface DailyTrend {
  date: string;
  total: number;
  completed: number;
  error: number;
  empty: number;
  cancelled: number;
}

export interface AgentRunReport {
  total: number;
  status_counts: Record<string, number>;
  stop_reason_counts: Record<string, number>;
  closure_rate: number;
  avg_steps: number;
  avg_tool_calls: number;
  error_tool_runs: number;
  review_funnel: Record<string, number>;
  tool_usage: ToolUsageItem[];
  daily_trend: DailyTrend[];
}

export interface AgentRunReviewInput {
  decision: "accepted" | "rejected";
  expected_tools?: string[];
  category?: string;
}

// ---------- Prompt ----------
export interface PromptTemplateItem {
  id: number;
  type: "CODE_REVIEW" | "UNIT_TEST" | "AI_README" | "CHAT";
  version: number;
  name: string;
  role_setting: string;
  template_body: string;
  review_dimensions: string | null;
  severity_levels: string | null;
  is_active: boolean;
  created_at: string;
}

export interface PromptCreateInput {
  type: PromptTemplateItem["type"];
  name: string;
  role_setting: string;
  template_body: string;
  review_dimensions?: string | null;
  severity_levels?: string | null;
}
