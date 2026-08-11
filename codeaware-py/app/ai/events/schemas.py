"""事件类型定义（Pydantic），对齐 Kafka topic 路由。

Topic 命名规则：{prefix}{domain}.{action}
- 审计类：audit.document, audit.conversation
- 指标类：metrics.retrieval, metrics.task
- 运维类：ops.error

投递语义：
- 审计/异常/任务类（audit.*, ops.*）→ 至少一次（Producer 幂等 + Consumer 去重）
- 指标类（metrics.*）→ 至多一次（可丢，不重复）
- 去重方式：Consumer 按 event_id 幂等消费
"""

from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field


class BaseEvent(BaseModel):
    """所有 Kafka 事件的基类。"""
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    event_id: str = ""


class DocumentAuditEvent(BaseEvent):
    """文档操作审计（上传/删除/替换）。"""
    action: str
    document_id: int
    title: str
    user_id: str | None = None
    source_type: str = ""
    project_name: str | None = None


class ConversationAuditEvent(BaseEvent):
    """对话操作审计。"""
    action: str
    conversation_id: str
    turn_id: str
    user_id: str | None = None
    message_count: int = 0
    elapsed_ms: int = 0


class RetrievalMetricsEvent(BaseEvent):
    """检索指标（每次查询）。"""
    query: str = ""
    route: str
    lexical_backend: str
    elapsed_ms: int
    doc_count: int
    retries: int = 0
    match_types: list[str] = []
    rag_runtime: str = "graph"


class TaskMetricsEvent(BaseEvent):
    """任务生命期事件。"""
    task_id: str
    task_name: str
    status: str
    elapsed_ms: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None


class MemoryMetricsEvent(BaseEvent):
    """Memory-Ops 指标（ADR-0017）：记忆抽取/召回计数。

    - extraction：count = 抽取到的事实数（0 也发，reason 存 memory_type）
    - recall：count = 本轮注入的长期记忆数（命中才发）
    """
    event_type: str  # extraction | recall
    conversation_id: str
    count: int = 0
    memory_type: str = ""  # extraction 的 reason（already_has_memories 等）/ 记忆类型


class ErrorEvent(BaseEvent):
    """系统异常事件。"""
    component: str
    code: str
    message: str
    details: dict[str, Any] | None = None
