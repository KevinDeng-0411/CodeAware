"""C1-A: typed SSE 事件契约 - 冻结 Chat 流式协议。

所有事件继承 ChatEventBase（protocol_version/conversation_id/turn_id/sequence）。
事件类型只由 SSE `event:` 行承载；data 是单行 JSON。SSE `id` 必须等于十进制 sequence。
sequence 从 1 开始、单 stream 内严格递增。
"""

from typing import Literal

from pydantic import BaseModel, Field

ProtocolVersion = Literal[1]

# phase: 失败发生的阶段
Phase = Literal["start", "context", "model", "tool", "persist", "post_turn", "cancelled"]
# component: 降级发生的子系统（不用异常类名）
Component = Literal[
    "message_cache",
    "summary_cache",
    "memory_recall",
    "rag_retrieval",
    "summary",
    "memory_extraction",
    "route",       # LangGraph 智能路由（ROUTE_DIRECT / ROUTE_DECIDE_FAILED）
    "rag_graph",   # LangGraph 检索图（RAG_NOT_FOUND / RAG_GRAPH_FAILED）
]


class ChatEventBase(BaseModel):
    protocol_version: ProtocolVersion = 1
    conversation_id: str
    turn_id: str
    sequence: int = Field(ge=1)


class ChatStarted(ChatEventBase):
    """首事件：Conversation + USER Message 已 PG commit。"""

    created: bool  # 是否新建了会话


class ContextWarning(ChatEventBase):
    """上下文增益降级（出现在 started 之后、模型完成之前）。"""

    component: Component
    code: str
    message: str
    retryable: bool


class TokenDelta(ChatEventBase):
    """单个非空模型 chunk；delta 原样 JSON 编码，不 trim。"""

    delta: str = Field(min_length=1)


class ReasoningDelta(ChatEventBase):
    """模型 reasoning_content 流式 delta（C6：与 token.delta 分开）。

    思考是过程不是内容：只流式展示，不持久化到消息表。
    """

    delta: str = Field(min_length=1)


class KnowledgeRef(BaseModel):
    """知识库参考来源：文档标题 + chunk 摘要片段 + 命中腿（C6）。"""

    document_id: int
    title: str
    snippet: str  # chunk 摘要片段（~100 字），前端卡片展示
    match_type: str  # vector / keyword / both
    score: float


class MemoryRef(BaseModel):
    """长期记忆参考来源：原子事实 + 类型 + 相似度（C6）。"""

    content: str
    memory_type: str  # FACT / REFERENCE
    similarity: float


class ContextReferences(ChatEventBase):
    """检索后、模型前下发：本轮实际注入 context 的知识 chunk + 记忆（C6）。

    措辞为"参考来源"（被检索并注入 prompt），不依赖 LLM 显式 cite。
    """

    knowledge_refs: list[KnowledgeRef] = Field(default_factory=list)
    memory_refs: list[MemoryRef] = Field(default_factory=list)


class ToolCall(ChatEventBase):
    """Agent 模式（ADR-0016）：模型决定调用工具（工具名 + 参数）。

    与 reasoning.delta 同哲学：展示过程，不持久化到消息表。
    """

    tool_name: str
    tool_args: dict = Field(default_factory=dict)
    tool_call_id: str


class ToolResult(ChatEventBase):
    """Agent 模式（ADR-0016）：工具执行结果（状态 + 摘要）。

    result 为截断后的文本摘要（观察结果给前端展示），完整结果回注模型。
    """

    tool_call_id: str
    tool_name: str
    status: Literal["ok", "error"]
    result: str


class PostTurnWarning(ChatEventBase):
    """assistant 已持久化后的 post-turn 降级（摘要/记忆/缓存刷新）。"""

    component: Component
    code: str
    message: str
    retryable: bool


class ErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool


class ChatCompleted(ChatEventBase):
    """成功终态：assistant 已 commit，post-turn 已完成或转 warning。"""

    assistant_message_id: int = Field(ge=1)
    warning_count: int = Field(ge=0)


class ChatFailed(ChatEventBase):
    """失败终态：partial assistant 固定不持久化。"""

    phase: Phase
    error: ErrorInfo
    partial_output_persisted: Literal[False] = False


# 事件名 -> 类型映射（供序列化/反序列化对齐）
EVENT_TYPES = {
    "chat.started": ChatStarted,
    "context.warning": ContextWarning,
    "context.references": ContextReferences,
    "reasoning.delta": ReasoningDelta,
    "token.delta": TokenDelta,
    "tool.call": ToolCall,
    "tool.result": ToolResult,
    "post_turn.warning": PostTurnWarning,
    "chat.completed": ChatCompleted,
    "chat.failed": ChatFailed,
}
