// C1-A: typed Chat SSE parser/state machine.
// Frames are assembled without trimming data, while protocol violations fail closed.

import type {
  ChatCompleted,
  ChatFailed,
  ChatStarted,
  ContextReferences,
  ContextWarning,
  EventName,
  FailurePhase,
  PostTurnWarning,
  ReasoningDelta,
  ToolCall,
  ToolResult,
  TokenDelta,
  WarningComponent,
} from "./chatEvents";
import { EVENT_NAMES, FAILURE_PHASES, WARNING_COMPONENTS } from "./chatEvents";

export interface RawSseEvent {
  id?: string;
  event?: string;
  data: string;
}

export type ChatStreamProtocolReason =
  | "MALFORMED_JSON"
  | "UNKNOWN_EVENT"
  | "UNSUPPORTED_PROTOCOL_VERSION"
  | "INVALID_EVENT"
  | "EVENT_ORDER"
  | "EVENT_AFTER_TERMINAL"
  | "SEQUENCE_MISMATCH"
  | "STREAM_IDENTITY_MISMATCH";

/** A stable, user-recognisable failure for an incompatible or corrupt SSE stream. */
export class ChatStreamProtocolError extends Error {
  readonly code = "CHAT_SSE_PROTOCOL_ERROR";
  readonly reason: ChatStreamProtocolReason;

  constructor(reason: ChatStreamProtocolReason, detail: string) {
    super(`聊天流协议错误（${reason}）：${detail}`);
    this.name = "ChatStreamProtocolError";
    this.reason = reason;
  }
}

/** The transport ended without the protocol's required terminal event. */
export class ChatStreamInterruptedError extends Error {
  readonly code = "CHAT_SSE_UNEXPECTED_EOF";

  constructor() {
    super("聊天流意外中断：未收到完成或失败事件");
    this.name = "ChatStreamInterruptedError";
  }
}

export type ChatStreamOutcome =
  | { status: "completed"; event: ChatCompleted }
  | { status: "failed"; event: ChatFailed }
  | { status: "aborted" };

/**
 * Parse complete SSE frames on blank-line boundaries.
 *
 * Only the one optional space immediately following `data:` is removed. JSON
 * content (including leading spaces and newlines inside delta) is untouched.
 */
export function parseSseEvents(buffer: string): { events: RawSseEvent[]; rest: string } {
  const events: RawSseEvent[] = [];
  const parts = buffer.split(/\r?\n\r?\n/);
  const rest = parts.pop() ?? "";

  for (const block of parts) {
    if (!block.trim()) continue;
    const dataLines: string[] = [];
    const event: RawSseEvent = { data: "" };
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("id:")) {
        event.id = line.slice(3).trim();
      } else if (line.startsWith("event:")) {
        event.event = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).replace(/^ /, ""));
      }
    }
    event.data = dataLines.join("\n");
    // SSE comments/heartbeats carry neither an event nor data. A data-bearing
    // frame without `event:` is the default "message" event and is therefore
    // unknown to this typed protocol; preserve it so the state machine rejects it.
    if (event.event || dataLines.length > 0) events.push(event);
  }
  return { events, rest };
}

export interface ChatStreamHandlers {
  onStarted?: (event: ChatStarted) => void;
  onReferences?: (event: ContextReferences) => void;
  onReasoning?: (event: ReasoningDelta) => void;
  onDelta?: (event: TokenDelta) => void;
  onToolCall?: (event: ToolCall) => void;
  onToolResult?: (event: ToolResult) => void;
  onContextWarning?: (event: ContextWarning) => void;
  onPostWarning?: (event: PostTurnWarning) => void;
  onCompleted?: (event: ChatCompleted) => void;
  onFailed?: (event: ChatFailed) => void;
}

type JsonRecord = Record<string, unknown>;

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isEventName(value: string | undefined): value is EventName {
  return typeof value === "string" && (EVENT_NAMES as readonly string[]).includes(value);
}

function isWarningComponent(value: unknown): value is WarningComponent {
  return (
    typeof value === "string" &&
    (WARNING_COMPONENTS as readonly string[]).includes(value)
  );
}

function isFailurePhase(value: unknown): value is FailurePhase {
  return (
    typeof value === "string" &&
    (FAILURE_PHASES as readonly string[]).includes(value)
  );
}

function isInteger(value: unknown, minimum = Number.MIN_SAFE_INTEGER): value is number {
  return Number.isSafeInteger(value) && (value as number) >= minimum;
}

function invalidEvent(eventName: string, detail: string): never {
  throw new ChatStreamProtocolError("INVALID_EVENT", `${eventName}: ${detail}`);
}

function validateBase(raw: RawSseEvent, eventName: EventName, payload: JsonRecord): void {
  if (payload.protocol_version !== 1) {
    throw new ChatStreamProtocolError(
      "UNSUPPORTED_PROTOCOL_VERSION",
      `${eventName} 使用了不受支持的 protocol_version`,
    );
  }
  if (typeof payload.conversation_id !== "string" || !payload.conversation_id) {
    invalidEvent(eventName, "conversation_id 缺失");
  }
  if (typeof payload.turn_id !== "string" || !payload.turn_id) {
    invalidEvent(eventName, "turn_id 缺失");
  }
  if (!isInteger(payload.sequence, 1)) {
    invalidEvent(eventName, "sequence 必须是正整数");
  }
  if (raw.id !== String(payload.sequence)) {
    throw new ChatStreamProtocolError(
      "SEQUENCE_MISMATCH",
      `${eventName} 的 SSE id 与 payload sequence 不一致`,
    );
  }
}

function validateEventShape(eventName: EventName, payload: JsonRecord): void {
  switch (eventName) {
    case "chat.started":
      if (typeof payload.created !== "boolean") invalidEvent(eventName, "created 必须是 boolean");
      return;
    case "token.delta":
    case "reasoning.delta":
      if (typeof payload.delta !== "string" || payload.delta.length === 0) {
        invalidEvent(eventName, "delta 必须是非空字符串");
      }
      return;
    case "context.references": {
      const krefs = payload.knowledge_refs;
      const mrefs = payload.memory_refs;
      const badKref =
        !Array.isArray(krefs) ||
        krefs.some(
          (r) =>
            !isRecord(r) ||
            typeof r.document_id !== "number" ||
            typeof r.title !== "string" ||
            typeof r.snippet !== "string" ||
            typeof r.match_type !== "string" ||
            typeof r.score !== "number",
        );
      const badMref =
        !Array.isArray(mrefs) ||
        mrefs.some(
          (r) =>
            !isRecord(r) ||
            typeof r.content !== "string" ||
            typeof r.memory_type !== "string" ||
            typeof r.similarity !== "number",
        );
      if (badKref || badMref) invalidEvent(eventName, "references 字段不完整");
      return;
    }
    case "tool.call":
      if (
        typeof payload.tool_name !== "string" ||
        !payload.tool_name ||
        !isRecord(payload.tool_args) ||
        typeof payload.tool_call_id !== "string" ||
        !payload.tool_call_id
      ) {
        invalidEvent(eventName, "tool.call 字段不完整");
      }
      return;
    case "tool.result":
      if (
        typeof payload.tool_call_id !== "string" ||
        !payload.tool_call_id ||
        typeof payload.tool_name !== "string" ||
        !payload.tool_name ||
        (payload.status !== "ok" && payload.status !== "error") ||
        typeof payload.result !== "string"
      ) {
        invalidEvent(eventName, "tool.result 字段不完整");
      }
      return;
    case "context.warning":
    case "post_turn.warning":
      if (
        !isWarningComponent(payload.component) ||
        typeof payload.code !== "string" ||
        !payload.code ||
        typeof payload.message !== "string" ||
        typeof payload.retryable !== "boolean"
      ) {
        invalidEvent(eventName, "warning 字段不完整");
      }
      return;
    case "chat.completed":
      if (
        !isInteger(payload.assistant_message_id, 1) ||
        !isInteger(payload.warning_count, 0)
      ) {
        invalidEvent(eventName, "completed 字段不完整");
      }
      return;
    case "chat.failed": {
      const error = payload.error;
      if (
        !isFailurePhase(payload.phase) ||
        !isRecord(error) ||
        typeof error.code !== "string" ||
        !error.code ||
        typeof error.message !== "string" ||
        typeof error.retryable !== "boolean" ||
        payload.partial_output_persisted !== false
      ) {
        invalidEvent(eventName, "failed 字段不完整");
      }
      return;
    }
  }
}

class ChatStreamState {
  private started = false;
  private expectedSequence = 1;
  private conversationId: string | null = null;
  private turnId: string | null = null;
  private postTurnStarted = false;
  private terminal: Exclude<ChatStreamOutcome, { status: "aborted" }> | null = null;

  accept(raw: RawSseEvent, handlers: ChatStreamHandlers): void {
    if (this.terminal) {
      throw new ChatStreamProtocolError(
        "EVENT_AFTER_TERMINAL",
        `终态之后又收到 ${raw.event ?? "无名称事件"}`,
      );
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(raw.data);
    } catch {
      throw new ChatStreamProtocolError("MALFORMED_JSON", `${raw.event ?? "无名称事件"} 数据不是 JSON`);
    }
    if (!isRecord(parsed)) {
      throw new ChatStreamProtocolError("INVALID_EVENT", `${raw.event ?? "无名称事件"} data 必须是对象`);
    }
    if (!isEventName(raw.event)) {
      throw new ChatStreamProtocolError("UNKNOWN_EVENT", `未知事件 ${raw.event ?? "<missing>"}`);
    }

    validateBase(raw, raw.event, parsed);
    validateEventShape(raw.event, parsed);

    if (!this.started && raw.event !== "chat.started") {
      throw new ChatStreamProtocolError("EVENT_ORDER", "chat.started 必须是首事件");
    }
    if (this.started && raw.event === "chat.started") {
      throw new ChatStreamProtocolError("EVENT_ORDER", "chat.started 只能出现一次");
    }
    if (parsed.sequence !== this.expectedSequence) {
      throw new ChatStreamProtocolError(
        "SEQUENCE_MISMATCH",
        `期望 sequence=${this.expectedSequence}，实际为 ${String(parsed.sequence)}`,
      );
    }
    if (
      this.postTurnStarted &&
      raw.event !== "post_turn.warning" &&
      raw.event !== "chat.completed" &&
      raw.event !== "chat.failed"
    ) {
      throw new ChatStreamProtocolError(
        "EVENT_ORDER",
        `进入 post-turn 阶段后不能再收到 ${raw.event}`,
      );
    }

    if (!this.started) {
      this.started = true;
      this.conversationId = parsed.conversation_id as string;
      this.turnId = parsed.turn_id as string;
    } else if (
      parsed.conversation_id !== this.conversationId ||
      parsed.turn_id !== this.turnId
    ) {
      throw new ChatStreamProtocolError(
        "STREAM_IDENTITY_MISMATCH",
        "conversation_id 或 turn_id 在同一流中发生变化",
      );
    }
    this.expectedSequence += 1;
    if (raw.event === "post_turn.warning") this.postTurnStarted = true;

    switch (raw.event) {
      case "chat.started":
        handlers.onStarted?.(parsed as unknown as ChatStarted);
        return;
      case "context.references":
        handlers.onReferences?.(parsed as unknown as ContextReferences);
        return;
      case "reasoning.delta":
        handlers.onReasoning?.(parsed as unknown as ReasoningDelta);
        return;
      case "token.delta":
        handlers.onDelta?.(parsed as unknown as TokenDelta);
        return;
      case "tool.call":
        handlers.onToolCall?.(parsed as unknown as ToolCall);
        return;
      case "tool.result":
        handlers.onToolResult?.(parsed as unknown as ToolResult);
        return;
      case "context.warning":
        handlers.onContextWarning?.(parsed as unknown as ContextWarning);
        return;
      case "post_turn.warning":
        handlers.onPostWarning?.(parsed as unknown as PostTurnWarning);
        return;
      case "chat.completed": {
        const event = parsed as unknown as ChatCompleted;
        this.terminal = { status: "completed", event };
        return;
      }
      case "chat.failed": {
        const event = parsed as unknown as ChatFailed;
        this.terminal = { status: "failed", event };
        return;
      }
    }
  }

  outcome(): Exclude<ChatStreamOutcome, { status: "aborted" }> | null {
    return this.terminal;
  }
}

/**
 * Consume one typed Chat stream.
 *
 * A non-aborted call resolves only after exactly one legal terminal event.
 * Protocol violations reject immediately and cancel the reader. EOF without a
 * terminal event is a distinct interruption error, never an implicit success.
 */
export async function consumeChatStream(
  body: ReadableStream<Uint8Array>,
  handlers: ChatStreamHandlers,
  signal?: AbortSignal,
): Promise<ChatStreamOutcome> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const state = new ChatStreamState();
  let buffer = "";
  const cancelReader = () => {
    void reader.cancel(signal?.reason).catch(() => {});
  };

  if (signal?.aborted) {
    await reader.cancel(signal.reason).catch(() => {});
    reader.releaseLock();
    return { status: "aborted" };
  }
  signal?.addEventListener("abort", cancelReader, { once: true });

  const consumeBuffer = (flush: boolean): void => {
    const source = flush && buffer.trim() ? `${buffer}\n\n` : buffer;
    const { events, rest } = parseSseEvents(source);
    buffer = flush ? "" : rest;
    for (const event of events) state.accept(event, handlers);
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      consumeBuffer(false);
    }
    buffer += decoder.decode();
    consumeBuffer(true);

    // User Abort wins even if a terminal frame was already buffered. The UI
    // has synchronously discarded its optimistic turn on Stop and must take
    // the PG reconciliation path to recover any assistant that committed just
    // before the cancellation raced with transport EOF.
    if (signal?.aborted) return { status: "aborted" };

    const terminal = state.outcome();
    if (terminal) {
      if (terminal.status === "completed") handlers.onCompleted?.(terminal.event);
      else handlers.onFailed?.(terminal.event);
      return terminal;
    }
    throw new ChatStreamInterruptedError();
  } catch (error) {
    if (signal?.aborted) return { status: "aborted" };
    await reader.cancel(error).catch(() => {});
    throw error;
  } finally {
    signal?.removeEventListener("abort", cancelReader);
    reader.releaseLock();
  }
}
