// Chat - 核心域。SSE 流式 + 多轮 + 会话侧栏 + 信号轨迹
import { useEffect, useRef, useState } from "react";
import { MessageSquare, Plus, Send, Square, Trash2, User, Cpu } from "lucide-react";
import { chat, chatStream } from "../api/client";
import { ChatStreamProtocolError } from "../api/sseParser";
import type {
  ContextReferences,
  KnowledgeRef,
  MemoryRef,
} from "../api/chatEvents";
import type { ChatMessage, ConversationItem } from "../api/types";
import { Button, EmptyState, SignalTrace, ToastBar, useToast } from "../components/ui";
import Markdown from "../components/Markdown";
import ToolTrace, { type ToolActivity } from "../components/ToolTrace";
import AgentArchDiagram from "../components/AgentArchDiagram";
import {
  archCurrentOnModel,
  archCurrentOnToolCall,
  archOnCompleted,
  archOnModel,
  archOnReferences,
  archOnStarted,
  archOnToolCall,
} from "../components/archMap";
import { useAgentOps } from "../store/agentOps";
import {
  cancelledTurnMessages,
  ChatTurnController,
  optimisticTurnMessages,
  readCancelledTurnTruth,
  type ChatTurn,
} from "./chatTurnController";

const CANCELLED_STATUS = "生成已取消";
const INTERRUPTED_STATUS = "生成中断，已从服务器恢复消息";
const PROTOCOL_ERROR_STATUS = "聊天流协议错误，已从服务器恢复消息";

export default function ChatPage() {
  const toast = useToast();
  const [convs, setConvs] = useState<ConversationItem[]>([]);
  const [activeCid, setActiveCid] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  // ADR-0016：RAG/Agent 模式切换（按请求传 mode，覆盖后端 CHAT_MODE）
  const [chatMode, setChatMode] = useState<"rag" | "agent">("rag");
  // 架构图高亮（agent 模式）：lit=已用模块，current=当前执行模块，error=本轮失败
  const [archHighlight, setArchHighlight] = useState<{
    lit: Set<string>;
    current: string | null;
    error: boolean;
  }>({ lit: new Set(), current: null, error: false });
  const [streaming, setStreaming] = useState(false);
  const [loadingConv, setLoadingConv] = useState(false);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [turnStatus, setTurnStatus] = useState<string | null>(null);
  // C6: 当前轮参考来源 + 思考过程（ephemeral，不随消息持久化）
  const [turnMeta, setTurnMeta] = useState<{
    refs: ContextReferences | null;
    reasoning: string;
    tools: ToolActivity[];
  }>({ refs: null, reasoning: "", tools: [] });
  const [turnController] = useState(() => new ChatTurnController());
  const scrollRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      turnController.supersede();
    };
  }, [turnController]);

  const refreshConvs = async () => {
    try {
      const next = await chat.conversations();
      if (mountedRef.current) setConvs(next);
    } catch (e) {
      if (mountedRef.current) toast.show(e);
    }
  };
  useEffect(() => {
    void refreshConvs();
  }, []);

  // 自动滚到底
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  const resetArchHighlight = () =>
    setArchHighlight({ lit: new Set(), current: null, error: false });

  const newChat = () => {
    turnController.supersede();
    setActiveCid(null);
    setMessages([]);
    setInput("");
    setStreaming(false);
    setTurnStatus(null);
    setWarnings([]);
    setTurnMeta({ refs: null, reasoning: "", tools: [] });
    resetArchHighlight();
  };

  const selectConv = async (cid: string) => {
    if (streaming) return;
    setActiveCid(cid);
    setTurnStatus(null);
    setTurnMeta({ refs: null, reasoning: "", tools: [] });
    setLoadingConv(true);
    try {
      setMessages(await chat.messages(cid));
    } catch (e) {
      toast.show(e);
    } finally {
      setLoadingConv(false);
    }
  };

  // ADR-0017：从 Agent Runs"查看对话"跳转进来时自动打开目标会话
  const focusCid = useAgentOps((s) => s.conversationFocusId);
  const clearConversationFocus = useAgentOps((s) => s.clearConversationFocus);
  useEffect(() => {
    if (focusCid) {
      void selectConv(focusCid);
      clearConversationFocus();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusCid]);

  const deleteConv = async (cid: string) => {
    try {
      await chat.delete(cid);
      if (activeCid === cid) newChat();
      void refreshConvs();
    } catch (e) {
      toast.show(e);
    }
  };

  const reconcileUncommittedTurn = async (turn: ChatTurn, status: string) => {
    if (!mountedRef.current || !turnController.isCurrent(turn)) return;

    // 立即丢弃 optimistic USER 与 partial ASSISTANT，绝不把它们当作 PG 消息。
    setMessages(cancelledTurnMessages(turn.baseMessages));
    setTurnStatus(status);

    const {
      persistedMessages: persistedResult,
      conversations: conversationsResult,
    } = await readCancelledTurnTruth(turn, chat);

    if (!mountedRef.current || !turnController.isCurrent(turn)) return;

    if (persistedResult.status === "fulfilled") {
      setMessages(cancelledTurnMessages(turn.baseMessages, persistedResult.value));
      if (turn.conversationId) setActiveCid(turn.conversationId);
    }
    if (conversationsResult.status === "fulfilled") {
      setConvs(conversationsResult.value);
    }

    const failedResult =
      persistedResult.status === "rejected"
        ? persistedResult
        : conversationsResult.status === "rejected"
          ? conversationsResult
          : null;
    if (failedResult) toast.show(failedResult.reason);
  };

  const stopGeneration = () => {
    const turn = turnController.cancelCurrent();
    if (!turn || !mountedRef.current) return;
    // 用户点击后立即清除 partial；请求收尾后再从 PG 做最终校准。
    setMessages(cancelledTurnMessages(turn.baseMessages));
    setTurnStatus(CANCELLED_STATUS);
  };

  const send = async () => {
    const text = input.trim();
    if (!text || turnController.hasActive()) return;

    const ctrl = new AbortController();
    const turn = turnController.start({
      controller: ctrl,
      conversationId: activeCid,
      baseMessages: messages,
    });

    setInput("");
    setStreaming(true);
    setWarnings([]);
    setTurnStatus(null);
    setTurnMeta({ refs: null, reasoning: "", tools: [] });
    setMessages(optimisticTurnMessages(turn.baseMessages, text));
    resetArchHighlight(); // 每回合重置架构图点亮

    const light = (...ids: string[]) =>
      setArchHighlight((prev) => ({
        ...prev,
        lit: new Set([...prev.lit, ...ids]),
      }));

    try {
      const outcome = await chatStream(
        { conversation_id: turn.conversationId ?? undefined, message: text, mode: chatMode },
        {
          onStarted: (e) => {
            if (!turnController.rememberConversation(turn, e.conversation_id)) return;
            setActiveCid(e.conversation_id); // 立即拿到 cid，不猜最新
            light(...archOnStarted());
          },
          onReferences: (e) => {
            if (!turnController.acceptsEvents(turn)) return;
            setTurnMeta((prev) => ({ ...prev, refs: e }));
            light(...archOnReferences(e));
          },
          onReasoning: (e) => {
            if (!turnController.acceptsEvents(turn)) return;
            setTurnMeta((prev) => ({ ...prev, reasoning: prev.reasoning + e.delta }));
            light(...archOnModel());
            setArchHighlight((prev) => ({ ...prev, current: archCurrentOnModel() }));
          },
          onDelta: (e) => {
            setMessages((m) => {
              if (!turnController.acceptsEvents(turn)) return m;
              const next = [...m];
              const last = next[next.length - 1];
              if (!last || last.role !== "ASSISTANT") return m;
              next[next.length - 1] = {
                role: "ASSISTANT",
                content: last.content + e.delta,
              };
              return next;
            });
          },
          onToolCall: (e) => {
            if (!turnController.acceptsEvents(turn)) return;
            setTurnMeta((prev) => ({
              ...prev,
              tools: [
                ...prev.tools,
                {
                  callId: e.tool_call_id,
                  name: e.tool_name,
                  args: e.tool_args,
                  status: "running",
                },
              ],
            }));
            light(...archOnToolCall(e.tool_name));
            setArchHighlight((prev) => ({ ...prev, current: archCurrentOnToolCall(e.tool_name) }));
          },
          onToolResult: (e) => {
            if (!turnController.acceptsEvents(turn)) return;
            setTurnMeta((prev) => ({
              ...prev,
              tools: prev.tools.map((t) =>
                t.callId === e.tool_call_id
                  ? { ...t, status: e.status, result: e.result }
                  : t,
              ),
            }));
            // 工具完成 → current 回到模型（下一轮思考/终答）
            setArchHighlight((prev) => ({ ...prev, current: archCurrentOnModel() }));
          },
          onContextWarning: (e) =>
            setWarnings((w) =>
              turnController.acceptsEvents(turn) ? [...w, `[${e.component}] ${e.message}`] : w,
            ),
          onPostWarning: (e) =>
            setWarnings((w) =>
              turnController.acceptsEvents(turn) ? [...w, `[${e.component}] ${e.message}`] : w,
            ),
        },
        ctrl.signal,
      );

      if (!turnController.isCurrent(turn)) return;
      if (turn.cancelRequested || ctrl.signal.aborted || outcome.status === "aborted") {
        // Stop 已同步清掉 optimistic turn；即使与 completed 竞态，也必须从 PG 回读。
        await reconcileUncommittedTurn(turn, CANCELLED_STATUS);
      } else if (outcome.status === "completed") {
        // 只有已完整校验、且后面没有额外事件的 chat.completed 才进入成功刷新路径。
        light(...archOnCompleted());
        setArchHighlight((prev) => ({ ...prev, current: null }));
        try {
          const nextConvs = await chat.conversations();
          if (mountedRef.current && turnController.isCurrent(turn)) setConvs(nextConvs);
        } catch (e) {
          if (mountedRef.current && turnController.isCurrent(turn)) toast.show(e);
        }
      } else if (outcome.status === "failed") {
        const message = `生成失败：${outcome.event.error.message}`;
        setArchHighlight((prev) => ({ ...prev, error: true, current: null }));
        await reconcileUncommittedTurn(turn, message);
      }
    } catch (e) {
      if (!mountedRef.current || !turnController.isCurrent(turn)) return;

      const cancelled =
        turn.cancelRequested || ctrl.signal.aborted || (e instanceof Error && e.name === "AbortError");
      if (cancelled) {
        await reconcileUncommittedTurn(turn, CANCELLED_STATUS);
      } else {
        const status =
          e instanceof ChatStreamProtocolError
            ? PROTOCOL_ERROR_STATUS
            : INTERRUPTED_STATUS;
        await reconcileUncommittedTurn(turn, status);
        toast.show(e);
      }
    } finally {
      if (turnController.finish(turn) && mountedRef.current) {
        setStreaming(false);
      }
    }
  };

  return (
    <div className="flex h-full">
      <ToastBar err={toast.err} onClose={toast.clear} />
      {/* 会话侧栏 */}
      <div className="w-56 shrink-0 border-r border-line bg-panel flex flex-col">
        <div className="p-3 border-b border-line">
          <Button variant="ghost" onClick={newChat} className="w-full justify-center">
            <Plus className="w-4 h-4" /> 新对话
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto py-1">
          {convs.length === 0 ? (
            <p className="text-2xs text-mute text-center mt-4 font-mono">NO CONVERSATIONS</p>
          ) : (
            convs.map((c) => (
              <div
                key={c.conversation_id}
                onClick={() => selectConv(c.conversation_id)}
                className={`group mx-2 my-0.5 px-2.5 py-2 rounded cursor-pointer border transition-colors ${
                  activeCid === c.conversation_id
                    ? "bg-graph border-line"
                    : "border-transparent hover:bg-graph/60"
                }`}
              >
                <div className="flex items-center justify-between gap-1">
                  <span className="text-xs font-medium text-ink truncate flex-1">
                    {c.title || "新对话"}
                  </span>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      deleteConv(c.conversation_id);
                    }}
                    className="opacity-0 group-hover:opacity-100 text-mute hover:text-oxblood transition-opacity"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
                <div className="font-mono text-2xs text-mute tracking-techy mt-0.5 truncate">
                  {c.conversation_id.slice(0, 12)}…
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* 架构图（agent 模式，中间大区）+ 对话（右侧） */}
      {chatMode === "agent" && (
        <div className="flex-1 min-w-0 border-r border-line bg-panel flex flex-col">
          <AgentArchDiagram
            lit={archHighlight.lit}
            current={archHighlight.current}
            error={archHighlight.error}
          />
        </div>
      )}
      <div
        className={`flex flex-col min-w-0 ${
          chatMode === "agent" ? "w-[34rem] shrink-0" : "flex-1"
        }`}
      >
        <div className="px-5 py-3 border-b border-line flex items-center gap-2 bg-panel">
          <MessageSquare className="w-4 h-4 text-oxblood" />
          <span className="font-mono text-sm font-semibold tracking-techy">CHAT</span>
          <span className="font-mono text-2xs text-mute tracking-techy">
            · {chatMode === "agent" ? "Agent · ReAct 工具循环" : "两级记忆 + RAG 整合"}
          </span>
          {/* ADR-0016：RAG/Agent 分段控制（顶部 header，全局状态） */}
          <div className="ml-auto flex items-center rounded border border-line overflow-hidden">
            {(["rag", "agent"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => {
                  if (chatMode !== m) {
                    setChatMode(m);
                    resetArchHighlight();
                  }
                }}
                title={m === "rag" ? "RAG：确定性检索问答" : "Agent：ReAct 工具循环 + 架构图"}
                className={`px-3 py-1 text-xs font-mono tracking-techy transition-colors ${
                  chatMode === m
                    ? "bg-oxblood text-paper"
                    : "text-mute hover:text-ink hover:bg-paper"
                }`}
              >
                {m === "rag" ? "RAG" : "Agent"}
              </button>
            ))}
          </div>
        </div>

        <div ref={scrollRef} className="flex-1 overflow-y-auto px-5 py-5">
          {messages.length === 0 && !loadingConv ? (
            <EmptyState
              icon={<MessageSquare className="w-10 h-10" />}
              title="开始一段对话"
              hint="AI 会整合长期记忆、知识库 RAG 与对话历史作答。支持多轮上下文与流式输出。"
            />
          ) : (
            <div className="max-w-3xl mx-auto space-y-5">
              {messages.map((m, i) => {
                const last = i === messages.length - 1;
                return (
                  <MessageBubble
                    key={i}
                    msg={m}
                    streaming={streaming && last}
                    refs={last ? turnMeta.refs : undefined}
                    reasoning={last ? turnMeta.reasoning : undefined}
                    tools={last ? turnMeta.tools : undefined}
                  />
                );
              })}
            </div>
          )}
        </div>

        {/* 降级提示（非阻塞） */}
        {warnings.length > 0 && (
          <div className="px-5 py-2 border-t border-amber/20 bg-amber/5 flex flex-wrap gap-x-4 gap-y-1">
            {warnings.map((w, i) => (
              <span key={i} className="font-mono text-2xs text-amber tracking-techy">
                ⚠ {w}
              </span>
            ))}
          </div>
        )}

        {turnStatus && (
          <div
            role="status"
            className="px-5 py-2 border-t border-line bg-graph/50 font-mono text-2xs text-mute tracking-techy"
          >
            {turnStatus}
          </div>
        )}

        {/* 输入器 */}
        <div className="px-5 py-3 border-t border-line bg-panel">
          <div className="max-w-3xl mx-auto flex items-end gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
              placeholder="输入消息，Enter 发送，Shift+Enter 换行"
              rows={1}
              className="flex-1 resize-none px-3 py-2 bg-paper border border-line rounded text-sm text-ink placeholder:text-mute/60 focus:outline-none focus:border-oxblood max-h-32"
            />
            {streaming ? (
              <button
                onClick={stopGeneration}
                disabled={turnStatus === CANCELLED_STATUS}
                className="inline-flex items-center gap-2 px-3.5 py-2 text-sm font-medium rounded bg-amber text-paper hover:bg-amber-soft transition-colors"
              >
                <Square className="w-4 h-4" />
                {turnStatus === CANCELLED_STATUS ? "正在停止" : "停止生成"}
              </button>
            ) : (
              <Button onClick={send}>
                <Send className="w-4 h-4" /> 发送
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function MessageBubble({
  msg,
  streaming,
  refs,
  reasoning,
  tools,
}: {
  msg: ChatMessage;
  streaming: boolean;
  refs?: ContextReferences | null;
  reasoning?: string;
  tools?: ToolActivity[];
}) {
  const isUser = msg.role === "USER";
  const showThinking = !isUser && !!reasoning;
  const showRefs =
    !isUser &&
    !!refs &&
    (refs.knowledge_refs.length > 0 || refs.memory_refs.length > 0);
  const showTools = !isUser && !!tools && tools.length > 0;
  return (
    <div className={`flex gap-3 ${isUser ? "flex-row-reverse" : ""}`}>
      <div
        className={`w-7 h-7 shrink-0 rounded flex items-center justify-center ${
          isUser ? "bg-oxblood text-paper" : "bg-ink text-paper"
        }`}
      >
        {isUser ? <User className="w-3.5 h-3.5" /> : <Cpu className="w-3.5 h-3.5" />}
      </div>
      <div className={`flex-1 min-w-0 ${isUser ? "text-right" : ""}`}>
        <div
          className={`font-mono text-2xs uppercase tracking-techy mb-1 ${
            isUser ? "text-oxblood" : "text-mute"
          }`}
        >
          {isUser ? "YOU" : "AI"}
        </div>
        {showThinking && (
          <ThinkingPanel reasoning={reasoning!} answerStarted={!!msg.content} />
        )}
        <div
          className={`inline-block text-left rounded px-3.5 py-2.5 ${
            isUser ? "bg-oxblood/8 border border-oxblood/20" : "bg-panel border border-line"
          }`}
        >
          {isUser ? (
            <p className="text-sm text-ink whitespace-pre-wrap">{msg.content}</p>
          ) : msg.content ? (
            <Markdown>{msg.content}</Markdown>
          ) : (
            <SignalTrace />
          )}
          {streaming && msg.content && <SignalTrace label="STREAMING" />}
        </div>
        {showRefs && <SourceCards refs={refs!} />}
        {showTools && <ToolTrace tools={tools!} />}
      </div>
    </div>
  );
}

// C6: 思考过程折叠窗（流式时展开，答案开始后自动折叠，用户可手动切换）
function ThinkingPanel({
  reasoning,
  answerStarted,
}: {
  reasoning: string;
  answerStarted: boolean;
}) {
  const [forced, setForced] = useState<boolean | null>(null);
  const open = forced ?? !answerStarted;
  return (
    <div className="mb-2 rounded border border-line bg-graph/40">
      <button
        type="button"
        onClick={() => setForced(!open)}
        className="w-full flex items-center justify-between px-3 py-1.5 font-mono text-2xs uppercase tracking-techy text-mute"
      >
        <span>思考过程{!open ? " · 已折叠" : ""}</span>
        <span>{open ? "▾ 收起" : "▸ 展开"}</span>
      </button>
      {open && (
        <div className="px-3 pb-2 font-mono text-2xs text-mute/80 whitespace-pre-wrap max-h-40 overflow-y-auto border-t border-line/60">
          {reasoning}
        </div>
      )}
    </div>
  );
}

// C6: 参考来源折叠面板（默认折叠，用户手动展开）
function SourceCards({ refs }: { refs: ContextReferences }) {
  const [expanded, setExpanded] = useState(false);
  const count = refs.knowledge_refs.length + refs.memory_refs.length;
  return (
    <div className="mt-2 rounded border border-line bg-graph/40">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-3 py-1.5 font-mono text-2xs uppercase tracking-techy text-mute"
      >
        <span>参考来源 · {count} 项{!expanded ? " · 已折叠" : ""}</span>
        <span>{expanded ? "▾ 收起" : "▸ 展开"}</span>
      </button>
      {expanded && (
        <div className="px-3 pb-2 space-y-1 border-t border-line/60">
          {refs.knowledge_refs.map((r, i) => (
            <SourceCard key={`k-${i}`} index={i + 1} ref={r} />
          ))}
          {refs.memory_refs.map((r, i) => (
            <MemoryCard key={`m-${i}`} index={refs.knowledge_refs.length + i + 1} ref={r} />
          ))}
        </div>
      )}
    </div>
  );
}

function SourceCard({ index, ref }: { index: number; ref: KnowledgeRef }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="rounded border border-line bg-paper">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="w-full px-2.5 py-1.5 text-left flex items-center gap-2"
      >
        <span className="font-mono text-2xs text-mute shrink-0">[{index}]</span>
        <span className="text-xs text-ink truncate flex-1">📄 {ref.title}</span>
        <span className="font-mono text-2xs text-mute uppercase shrink-0">{ref.match_type}</span>
        <span className="text-mute text-2xs shrink-0">{expanded ? "▾" : "▸"}</span>
      </button>
      <div
        className={`px-2.5 pb-1.5 text-2xs text-mute whitespace-pre-wrap ${
          expanded ? "" : "truncate"
        }`}
      >
        {ref.snippet}
      </div>
    </div>
  );
}

function MemoryCard({ index, ref }: { index: number; ref: MemoryRef }) {
  return (
    <div className="rounded border border-line bg-paper px-2.5 py-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-2xs text-mute shrink-0">[{index}]</span>
        <span className="font-mono text-2xs text-mute uppercase flex-1 truncate">
          💭 记忆 · {ref.memory_type}
        </span>
        <span className="font-mono text-2xs text-mute shrink-0">
          sim {ref.similarity.toFixed(2)}
        </span>
      </div>
      <div className="text-2xs text-ink mt-0.5 whitespace-pre-wrap">{ref.content}</div>
    </div>
  );
}
