"""Chat schemas - 请求/响应。"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.ai.agent.guardrails import detect_query_injection
from app.core.config import settings
from app.schemas.chat_events import Component


class ChatRequest(BaseModel):
    conversation_id: str | None = Field(default=None, min_length=1, max_length=64)
    message: str = Field(default=..., min_length=1, max_length=20_000)
    # 前端 RAG/Agent 切换（ADR-0016）：按请求覆盖 settings.chat_mode；缺省用后端配置
    mode: Literal["rag", "agent"] | None = Field(default=None)

    @field_validator("conversation_id")
    @classmethod
    def conversation_id_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("conversation_id must not be blank")
        return value

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value

    @field_validator("message")
    @classmethod
    def message_reject_injection(cls, value: str) -> str:
        """Guardrail（ADR-0017 D2）：请求边界 fail-closed 拒绝疑似提示注入查询。

        RAG/Agent 双模式生效（都在请求边界）；guardrails_enabled=False 时跳过。
        """
        if settings.guardrails_enabled and detect_query_injection(value):
            raise ValueError("message 疑似提示注入，已拒绝")
        return value


class ChatWarning(BaseModel):
    component: Component
    code: str
    message: str
    retryable: bool


class ChatResponseVO(BaseModel):
    conversation_id: str
    reply: str
    memory_summary: str | None = None
    warnings: list[ChatWarning] = Field(default_factory=list)


class ConversationItem(BaseModel):
    id: int
    conversation_id: str
    title: str | None = None
    summary: str | None = None


class ChatMessageVO(BaseModel):
    role: str
    content: str
