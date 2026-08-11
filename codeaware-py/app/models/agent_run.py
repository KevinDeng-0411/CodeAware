"""AgentRun - Agent 模式单轮 run 轨迹（LLMOps 闭环，ADR-0017）。

记录一次 agent turn 的完整过程与结果：trace（thought/tool_call/tool_result/answer
按序 JSONB）、context_snapshot（本轮注入的短时记忆=摘要+消息窗口边界、长时记忆
memory_refs）、停止原因、失败沉淀（needs_review / review_status / expected_tools /
category / synced）。observability 记录，run 对应一个 SSE turn（turn_id 全局唯一）。

消息全文不重复存（在 messages 表），context_snapshot 只存窗口边界 + 摘要。
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 全局唯一（= SSE turn_id，uuid4().hex），唯一约束防并发/重试重复写
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    query: Mapped[str] = mapped_column(Text, nullable=False, comment="用户问题，供失败沉淀为 eval 回归")
    # completed | empty | error | cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="completed")
    # final | no_output | max_steps | converged | error | cancelled
    stop_reason: Mapped[str] = mapped_column(String(20), nullable=False, default="final")
    steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_tools: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 失败沉淀：error/empty/工具真实异常 → 待人工评审
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    # pending | accepted | rejected（评审状态）
    review_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    expected_tools: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    synced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    context_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
