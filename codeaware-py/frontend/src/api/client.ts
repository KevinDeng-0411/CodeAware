// API client - 解包统一响应包络，失败抛 ApiError；SSE 流式单独处理
import { getToken, onAuthFailure } from "./auth";
import type {
  AgentRunDetail,
  AgentRunListItem,
  AgentRunListVO,
  AgentRunReviewInput,
  AgentRunStats,
  AiReadmeCapability,
  AiReadmeVO,
  ChatMessage,
  ChatResponseVO,
  CodeReviewVO,
  ConversationItem,
  DocumentDetailVO,
  DocumentListVO,
  Envelope,
  KnowledgeSearchHit,
  MemoryHit,
  MemoryListVO,
  PromptCreateInput,
  PromptTemplateItem,
  UnitTestVO,
} from "./types";
import {
  consumeChatStream,
  type ChatStreamHandlers,
  type ChatStreamOutcome,
} from "./sseParser";

export class ApiError extends Error {
  msg: string;
  constructor(msg: string) {
    super(msg);
    this.name = "ApiError";
    this.msg = msg;
  }
}

const BASE = ""; // 同源 / Vite 代理 /api

interface ErrorResponseLike {
  readonly status: number;
  json: () => Promise<unknown>;
}

/**
 * Prefer the backend's unified error envelope so stable business codes such as
 * CHAT_TURN_IN_PROGRESS survive pre-stream HTTP failures.
 */
export async function readApiErrorMessage(response: ErrorResponseLike): Promise<string> {
  try {
    const body = await response.json();
    if (
      typeof body === "object" &&
      body !== null &&
      "msg" in body &&
      typeof body.msg === "string" &&
      body.msg.trim()
    ) {
      return body.msg;
    }
  } catch {
    // Non-JSON/empty responses fall back to the transport status below.
  }
  return `HTTP ${response.status}`;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${BASE}${path}`, { headers, ...init });
  if (res.status === 401) {
    onAuthFailure();
    throw new ApiError("AUTH_TOKEN_REQUIRED");
  }
  let body: Envelope<T>;
  try {
    body = (await res.json()) as Envelope<T>;
  } catch {
    throw new ApiError(`HTTP ${res.status}: 响应非 JSON`);
  }
  if (body.code !== 1) throw new ApiError(body.msg || `HTTP ${res.status}`);
  return body.data;
}

// ---------- Code Review ----------
export const codeReview = {
  review: (p: { project_name: string; file_path: string; source_code: string }) =>
    call<CodeReviewVO>("/api/code-review/review", {
      method: "POST",
      body: JSON.stringify(p),
    }),
};

// ---------- Unit Test ----------
export const unitTest = {
  generate: (p: {
    project_name: string;
    file_path: string;
    source_code: string;
    test_framework: string;
  }) => call<UnitTestVO>("/api/unit-test/generate", { method: "POST", body: JSON.stringify(p) }),
};

// ---------- AI ReadMe ----------
export const aiReadme = {
  capabilities: () => call<AiReadmeCapability>("/api/ai-readme/capabilities"),
  generate: (p: { project_name: string; project_path: string }) =>
    call<AiReadmeVO>("/api/ai-readme/generate", { method: "POST", body: JSON.stringify(p) }),
  get: (project_name: string) => call<AiReadmeVO | null>(`/api/ai-readme/${encodeURIComponent(project_name)}`),
};

// ---------- Chat ----------
export const chat = {
  send: (p: { conversation_id?: string; message: string; mode?: "rag" | "agent" }) =>
    call<ChatResponseVO>("/api/chat/send", { method: "POST", body: JSON.stringify(p) }),
  conversations: () => call<ConversationItem[]>("/api/chat/conversations"),
  messages: (cid: string) => call<ChatMessage[]>(`/api/chat/conversations/${cid}`),
  delete: (cid: string) =>
    call<null>(`/api/chat/conversations/${cid}`, { method: "DELETE" }),
};

/**
 * typed SSE 流式对话（C1-A）。按事件分派；不 trim delta，不猜 cid。
 * chat.started 立即拿到 conversation_id；chat.completed 才完成。
 */
export async function chatStream(
  p: { conversation_id?: string; message: string; mode?: "rag" | "agent" },
  handlers: ChatStreamHandlers,
  signal?: AbortSignal,
): Promise<ChatStreamOutcome> {
  const token = getToken();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${BASE}/api/chat/send/stream`, {
    method: "POST",
    headers,
    body: JSON.stringify(p),
    signal,
  });
  if (res.status === 401) {
    onAuthFailure();
    throw new ApiError("AUTH_TOKEN_REQUIRED");
  }
  if (!res.ok) throw new ApiError(await readApiErrorMessage(res));
  if (!res.body) throw new ApiError(`HTTP ${res.status}: 响应流为空`);
  return consumeChatStream(res.body, handlers, signal);
}

// ---------- Knowledge ----------
export const knowledge = {
  upload: (p: { title: string; content: string; source_type: string; project_name?: string }) =>
    call<{ id: number; title: string }>("/api/knowledge/upload", {
      method: "POST",
      body: JSON.stringify(p),
    }),
  uploadFile: async (file: File, project_name?: string) => {
    const fd = new FormData();
    fd.append("file", file);
    if (project_name) fd.append("project_name", project_name);
    const token = getToken();
    // 无 token 时不设 headers（让浏览器自动设 multipart Content-Type+boundary）
    const headers = token ? { Authorization: `Bearer ${token}` } : undefined;
    const res = await fetch(`${BASE}/api/knowledge/upload-file`, { method: "POST", body: fd, headers });
    if (res.status === 401) {
      onAuthFailure();
      throw new ApiError("AUTH_TOKEN_REQUIRED");
    }
    if (!res.ok) throw new ApiError(await readApiErrorMessage(res));
    const body = (await res.json()) as Envelope<{ id: number; title: string }>;
    if (body.code !== 1) throw new ApiError(body.msg);
    return body.data;
  },
  search: (p: { query: string; top_k?: number }) =>
    call<KnowledgeSearchHit[]>("/api/knowledge/search", {
      method: "POST",
      body: JSON.stringify({ query: p.query, top_k: p.top_k ?? 5 }),
    }),
  remove: (id: number) => call<null>(`/api/knowledge/${id}`, { method: "DELETE" }),
  getDetail: (id: number) => call<DocumentDetailVO>(`/api/knowledge/${id}`),
  listDocuments: (params: { status?: string; page?: number; size?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.status) qs.set("status", params.status);
    if (params.page) qs.set("page", String(params.page));
    if (params.size) qs.set("size", String(params.size));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return call<DocumentListVO>(`/api/knowledge/documents${suffix}`);
  },
  replace: async (docId: number, file: File, project_name?: string) => {
    const fd = new FormData();
    fd.append("file", file);
    if (project_name) fd.append("project_name", project_name);
    const token = getToken();
    const headers = token ? { Authorization: `Bearer ${token}` } : undefined;
    const res = await fetch(`${BASE}/api/knowledge/${docId}/replace`, { method: "POST", body: fd, headers });
    if (res.status === 401) {
      onAuthFailure();
      throw new ApiError("AUTH_TOKEN_REQUIRED");
    }
    if (!res.ok) throw new ApiError(await readApiErrorMessage(res));
    const body = (await res.json()) as Envelope<{ id: number; title: string }>;
    if (body.code !== 1) throw new ApiError(body.msg);
    return body.data;
  },
};

// ---------- Memory ----------
export const memory = {
  save: (p: { content: string; memory_type?: string; conversation_id?: string; metadata?: object }) =>
    call<{ id: number; content: string }>("/api/memory/long-term", {
      method: "POST",
      body: JSON.stringify({ memory_type: "REFERENCE", ...p }),
    }),
  list: (params: { memory_type?: string; page?: number; size?: number }) =>
    call<MemoryListVO>(
      `/api/memory/long-term?memory_type=${params.memory_type || "ALL"}&page=${params.page || 1}&size=${params.size || 20}`,
    ),
  search: (query: string, threshold = 0.3, topK = 5) =>
    call<MemoryHit[]>(
      `/api/memory/long-term/search?query=${encodeURIComponent(query)}&threshold=${threshold}&top_k=${topK}`,
    ),
  remove: (id: number) => call<null>(`/api/memory/long-term/${id}`, { method: "DELETE" }),
};

// ---------- Agent Runs（ADR-0017）----------
export const agentRuns = {
  list: (params: {
    page?: number;
    size?: number;
    conversation_id?: string;
    needs_review?: boolean;
    review_status?: string;
  } = {}) => {
    const qs = new URLSearchParams();
    if (params.page) qs.set("page", String(params.page));
    if (params.size) qs.set("size", String(params.size));
    if (params.conversation_id) qs.set("conversation_id", params.conversation_id);
    if (params.needs_review !== undefined) qs.set("needs_review", String(params.needs_review));
    if (params.review_status) qs.set("review_status", params.review_status);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return call<AgentRunListVO>(`/api/chat/agent-runs${suffix}`);
  },
  detail: (turnId: string) =>
    call<AgentRunDetail>(`/api/chat/agent-runs/${encodeURIComponent(turnId)}`),
  review: (turnId: string, input: AgentRunReviewInput) =>
    call<AgentRunListItem>(`/api/chat/agent-runs/${encodeURIComponent(turnId)}/review`, {
      method: "POST",
      body: JSON.stringify(input),
    }),
  stats: () => call<AgentRunStats>("/api/chat/agent-runs/stats"),
};

// ---------- Prompt ----------
export const prompt = {
  list: (type?: string) =>
    call<PromptTemplateItem[]>(`/api/prompts${type ? `?type=${type}` : ""}`),
  create: (input: PromptCreateInput) =>
    call<PromptTemplateItem>("/api/prompts", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  preview: (id: number, sampleCode = "") =>
    call<{ rendered: string }>(
      `/api/prompts/${id}/preview?sample_code=${encodeURIComponent(sampleCode)}`,
    ),
  activate: (id: number) =>
    call<PromptTemplateItem>(`/api/prompts/${id}/activate`, {
      method: "POST",
    }),
};
