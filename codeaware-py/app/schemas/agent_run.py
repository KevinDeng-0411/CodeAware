"""Agent Run 回放/评审 VO（ADR-0017）。

run = 一次 agent turn（turn_id 全局唯一）。trace 为按序 JSONB（thought/tool_call/
tool_result/answer + convergence_override），context_snapshot 为本轮上下文快照
（summary / window / memory_refs / knowledge_refs）。
"""

from typing import Any

from pydantic import BaseModel, Field


class AgentRunListItem(BaseModel):
    id: int
    turn_id: str
    conversation_id: str
    query: str
    status: str  # completed | empty | error | cancelled
    stop_reason: str  # final | no_output | max_steps | converged | error | cancelled
    steps: int
    tool_calls: int
    error_tools: int
    needs_review: bool
    review_status: str  # pending | accepted | rejected
    synced: bool
    error: str | None = None
    created_at: str | None = None


class AgentRunListVO(BaseModel):
    total: int
    page: int
    size: int
    records: list[AgentRunListItem]


class AgentRunDetail(BaseModel):
    id: int
    turn_id: str
    conversation_id: str
    query: str
    status: str
    stop_reason: str
    steps: int
    tool_calls: int
    error_tools: int
    needs_review: bool
    review_status: str
    expected_tools: list[str] | None = None
    category: str | None = None
    synced: bool
    error: str | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    context_snapshot: dict[str, Any] | None = None
    created_at: str | None = None


class AgentRunReviewRequest(BaseModel):
    decision: str = Field(pattern="^(accepted|rejected)$")
    expected_tools: list[str] | None = None
    category: str | None = None


class AgentRunStats(BaseModel):
    total: int
    needs_review_pending: int
    status_counts: dict[str, int]


class ToolUsageItem(BaseModel):
    tool: str
    calls: int
    errors: int  # tool_result status=error 次数（含正常错误结果）


class DailyTrend(BaseModel):
    date: str  # YYYY-MM-DD
    total: int
    completed: int
    error: int
    empty: int
    cancelled: int


class AgentRunReport(BaseModel):
    """Agent 运行报表（ADR-0017 数据分析层）：聚合 agent_runs 揭示改进方向。"""

    total: int
    status_counts: dict[str, int]
    stop_reason_counts: dict[str, int]
    closure_rate: float  # completed / total
    avg_steps: float
    avg_tool_calls: float
    error_tool_runs: int  # 含真实工具异常（error_tools>0）的 run 数
    review_funnel: dict[str, int]  # pending / accepted / rejected / synced
    tool_usage: list[ToolUsageItem]  # 按调用次数降序
    daily_trend: list[DailyTrend]  # 最近 7 天
