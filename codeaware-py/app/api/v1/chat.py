"""Chat API - /api/chat（核心域，C1-A typed SSE + TurnCoordinator）。"""

import hashlib
import logging

import anyio
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.services.chat import ChatService
from app.ai.services.turn_coordinator import (
    ChatConversationNotFound,
    ChatTurnFailed,
    ChatTurnInProgress,
    ChatTurnStartFailed,
    TurnCoordinator,
)
from app.api.v1.deps import get_chat_service, get_current_user, get_db, get_turn_coordinator
from app.core.config import settings
from app.core.response import Result
from app.db.redis import redis_client
from app.models import AgentRun, Conversation, Message, User
from app.schemas.agent_run import (
    AgentRunDetail,
    AgentRunListItem,
    AgentRunListVO,
    AgentRunReviewRequest,
    AgentRunStats,
)
from app.schemas.chat import (
    ChatMessageVO,
    ChatRequest,
    ChatResponseVO,
    ConversationItem,
)
from app.schemas.chat_events import EVENT_TYPES

router = APIRouter(prefix="/api/chat", tags=["Chat"])
logger = logging.getLogger(__name__)

_ANSWER_CACHE_TTL = 300  # 5 分钟


def _answer_cache_key(message: str, mode: str = "rag") -> str:
    """精准匹配缓存 key：MD5(strip(message)) + mode（rag/agent 答案不互串）。"""
    return f"answer:{mode}:{hashlib.md5(message.strip().encode()).hexdigest()}"

# 事件类 -> SSE event 名
_EVENT_NAME = {cls: name for name, cls in EVENT_TYPES.items()}

_CHAT_COMMON_ERROR_RESPONSES = {
    422: {
        "model": Result[None],
        "description": "请求字段校验失败",
        "content": {
            "application/json": {
                "example": {
                    "code": 0,
                    "msg": "CHAT_REQUEST_INVALID",
                    "data": None,
                }
            }
        },
    },
    404: {
        "model": Result[None],
        "description": "conversation_id 不存在",
        "content": {
            "application/json": {
                "example": {
                    "code": 0,
                    "msg": "CHAT_CONVERSATION_NOT_FOUND",
                    "data": None,
                }
            }
        },
    },
    409: {
        "model": Result[None],
        "description": "同一会话已有 turn 正在执行",
        "content": {
            "application/json": {
                "example": {
                    "code": 0,
                    "msg": "CHAT_TURN_IN_PROGRESS",
                    "data": None,
                }
            }
        },
    },
}

_SYNC_CHAT_ERROR_RESPONSES = {
    **_CHAT_COMMON_ERROR_RESPONSES,
    500: {
        "model": Result[None],
        "description": "初始化失败或同步 Chat 核心失败",
        "content": {
            "application/json": {
                "examples": {
                    code: {
                        "summary": code,
                        "value": {"code": 0, "msg": code, "data": None},
                    }
                    for code in [
                        "CHAT_START_FAILED",
                        "CONTEXT_FAILED",
                        "MODEL_STREAM_FAILED",
                        "PERSIST_FAILED",
                        "POST_TURN_FAILED",
                    ]
                }
            }
        },
    },
}

_STREAM_CHAT_ERROR_RESPONSES = {
    **_CHAT_COMMON_ERROR_RESPONSES,
    500: {
        "model": Result[None],
        "description": "SSE 响应建立前 Transaction A 初始化失败",
        "content": {
            "application/json": {
                "example": {
                    "code": 0,
                    "msg": "CHAT_START_FAILED",
                    "data": None,
                }
            }
        },
    },
}


class _ClosingStreamingResponse(StreamingResponse):
    """无论迭代、socket send 或断开监听在哪一步退出，都确定性关闭流。"""

    def __init__(self, content, *args, event_gen=None, on_close=None, **kwargs) -> None:
        super().__init__(content, *args, **kwargs)
        self._event_gen = event_gen
        self._on_close = on_close

    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            # 旧 ASGI 规范下 Starlette 会在取消域中终止发送任务；shield 保证
            # cleanup 仍能执行完，从而取消模型并释放 per-cid guard。
            try:
                with anyio.CancelScope(shield=True):
                    try:
                        close_body = getattr(self.body_iterator, "aclose", None)
                        if close_body is not None:
                            await close_body()
                    finally:
                        # async generator 若尚未开始迭代，aclose() 不会进入函数体，
                        # 因而外层 formatter 的 finally 也不会运行。响应直接持有并
                        # 关闭内层 generator，覆盖 response-start send 失败的边界。
                        close_events = getattr(self._event_gen, "aclose", None)
                        if close_events is not None:
                            await close_events()
            finally:
                if self._on_close is not None:
                    self._on_close()


def _error(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=Result.error(msg).model_dump())


async def _format_sse(event_gen):
    """Typed ChatEvent -> SSE 帧：id/event/data 单行 JSON。"""
    try:
        async for ev in event_gen:
            name = _EVENT_NAME.get(type(ev), "unknown")
            yield f"id: {ev.sequence}\nevent: {name}\ndata: {ev.model_dump_json()}\n\n"
    finally:
        # StreamingResponse 在客户端断开时会关闭外层 body iterator。显式关闭
        # TurnCoordinator generator，才能继续向内取消模型流并立即释放 turn guard。
        close_event_gen = getattr(event_gen, "aclose", None)
        if close_event_gen is not None:
            await close_event_gen()


@router.post(
    "/send",
    response_model=Result[ChatResponseVO],
    responses=_SYNC_CHAT_ERROR_RESPONSES,
)
async def send(req: ChatRequest, coordinator: TurnCoordinator = Depends(get_turn_coordinator), user: User | None = Depends(get_current_user)):
    """同步对话：drain TurnCoordinator -> ChatResponseVO。

    user 经 DI 解析为 User（HTTP）；直连调用时为 Depends 实例或 None，跳过归属校验。
    """
    uid = user.id if isinstance(user, User) else None
    try:
        prepared = await coordinator.prepare_turn(req.conversation_id, req.message, user_id=uid, mode=req.mode)
    except ChatConversationNotFound:
        return _error(404, "CHAT_CONVERSATION_NOT_FOUND")
    except ChatTurnInProgress:
        return _error(409, "CHAT_TURN_IN_PROGRESS")
    except ChatTurnStartFailed:
        return _error(500, "CHAT_START_FAILED")

    # 答案缓存：精准匹配（含有效 mode，rag/agent 不互串），缺省用后端配置
    cache_key = _answer_cache_key(req.message, req.mode or settings.chat_mode)
    try:
        cached = await redis_client.get(cache_key)
    except Exception:
        cached = None
    if cached:
        logger.info("answer cache hit key=%s conversation_id=%s", cache_key[:16], prepared.conversation_id)
        return Result.ok(ChatResponseVO(
            conversation_id=prepared.conversation_id,
            reply=cached,
            warnings=[],
        ))

    try:
        result = await coordinator.run_sync(prepared, req.message)
    except ChatTurnFailed as e:
        return _error(500, e.event.error.code)
    # 缓存回复
    try:
        await redis_client.setex(cache_key, _ANSWER_CACHE_TTL, result.reply)
    except Exception:
        pass
    return Result.ok(ChatResponseVO(
        conversation_id=result.conversation_id,
        reply=result.reply,
        warnings=result.warnings,
    ))


@router.post(
    "/send/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "版本化 typed SSE Chat 事件流",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        **_STREAM_CHAT_ERROR_RESPONSES,
    },
)
async def send_stream(req: ChatRequest, coordinator: TurnCoordinator = Depends(get_turn_coordinator), user: User | None = Depends(get_current_user)):
    """流式对话：typed SSE。user 经 DI 解析；直连调用时跳过归属校验。"""
    uid = user.id if isinstance(user, User) else None
    try:
        prepared = await coordinator.prepare_turn(req.conversation_id, req.message, user_id=uid, mode=req.mode)
    except ChatConversationNotFound:
        return _error(404, "CHAT_CONVERSATION_NOT_FOUND")
    except ChatTurnInProgress:
        return _error(409, "CHAT_TURN_IN_PROGRESS")
    except ChatTurnStartFailed:
        return _error(500, "CHAT_START_FAILED")
    try:
        event_gen = coordinator.run(prepared, req.message)
        return _ClosingStreamingResponse(
            _format_sse(event_gen),
            event_gen=event_gen,
            on_close=lambda: coordinator.release_turn(prepared.conversation_id),
            media_type="text/event-stream",
        )
    except BaseException:
        coordinator.release_turn(prepared.conversation_id)
        raise


@router.get("/conversations", response_model=Result[list[ConversationItem]])
async def list_conversations(svc: ChatService = Depends(get_chat_service), user: User = Depends(get_current_user)):
    convs = await svc.list_conversations(user_id=user.id)
    return Result.ok(
        [
            {"id": c.id, "conversation_id": c.conversation_id, "title": c.title, "summary": c.summary}
            for c in convs
        ]
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=Result[list[ChatMessageVO]],
)
async def get_conversation(conversation_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """会话消息历史（PG 真相，按时间正序）。归属不匹配返回 404（不泄露存在性）。"""
    from sqlalchemy import select

    exists = await db.scalar(
        select(Conversation.id).where(
            Conversation.conversation_id == conversation_id,
            # 归属校验：user_id 为 null 的会话（直连测试/遗留）对所有用户可见
            (Conversation.user_id == user.id) | (Conversation.user_id.is_(None)),
        )
    )
    if exists is None:
        return _error(404, "CHAT_CONVERSATION_NOT_FOUND")
    r = await db.execute(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id.asc())
    )
    msgs = list(r.scalars().all())
    return Result.ok([{"role": m.role, "content": m.content} for m in msgs])


@router.delete(
    "/conversations/{conversation_id}",
    response_model=Result[None],
)
async def delete_conversation(conversation_id: str, svc: ChatService = Depends(get_chat_service), user: User = Depends(get_current_user)):
    await svc.delete_conversation(conversation_id, user_id=user.id)
    return Result.ok()


# ============ Agent Runs 回放/评审（ADR-0017）============
# 路由顺序注意：/agent-runs/stats 必须在 /agent-runs/{turn_id} 之前声明，
# 否则 "stats" 会被 {turn_id} 吞掉。


def _agent_run_item(r: AgentRun) -> dict:
    return {
        "id": r.id,
        "turn_id": r.turn_id,
        "conversation_id": r.conversation_id,
        "query": r.query,
        "status": r.status,
        "stop_reason": r.stop_reason,
        "steps": r.steps,
        "tool_calls": r.tool_calls,
        "error_tools": r.error_tools,
        "needs_review": r.needs_review,
        "review_status": r.review_status,
        "synced": r.synced,
        "error": r.error,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _ownership_filter(user: User):
    """归属校验（照抄 get_conversation）：null 会话对登录用户可见。"""
    return (Conversation.user_id == user.id) | (Conversation.user_id.is_(None))


@router.get("/agent-runs", response_model=Result[AgentRunListVO])
async def list_agent_runs(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    conversation_id: str | None = Query(None, max_length=64),
    needs_review: bool | None = Query(None),
    review_status: str | None = Query(None, pattern="^(pending|accepted|rejected)$"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent run 列表（分页 + 可选过滤），归属 JOIN conversations。"""
    base = select(AgentRun).join(Conversation, AgentRun.conversation_id == Conversation.conversation_id)
    count_stmt = select(func.count()).select_from(AgentRun).join(Conversation, AgentRun.conversation_id == Conversation.conversation_id)
    cond = [_ownership_filter(user)]
    if conversation_id:
        cond.append(AgentRun.conversation_id == conversation_id)
    if needs_review is not None:
        cond.append(AgentRun.needs_review.is_(needs_review))
    if review_status:
        cond.append(AgentRun.review_status == review_status)
    base = base.where(*cond)
    count_stmt = count_stmt.where(*cond)
    total = await db.scalar(count_stmt) or 0
    rows = (
        await db.execute(base.order_by(AgentRun.id.desc()).offset((page - 1) * size).limit(size))
    ).scalars().all()
    return Result.ok(
        AgentRunListVO(total=total, page=page, size=size, records=[_agent_run_item(r) for r in rows])
    )


@router.get("/agent-runs/stats", response_model=Result[AgentRunStats])
async def get_agent_run_stats(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Agent run 统计条（total / 待评审 / status 分布），页面头部展示。"""
    base = (
        select(func.count(AgentRun.id))
        .join(Conversation, AgentRun.conversation_id == Conversation.conversation_id)
        .where(_ownership_filter(user))
    )
    total = await db.scalar(base) or 0
    pending = (
        await db.scalar(
            base.where(AgentRun.needs_review.is_(True), AgentRun.review_status == "pending")
        )
        or 0
    )
    rows = await db.execute(
        select(AgentRun.status, func.count(AgentRun.id))
        .join(Conversation, AgentRun.conversation_id == Conversation.conversation_id)
        .where(_ownership_filter(user))
        .group_by(AgentRun.status)
    )
    status_counts = {status: count for status, count in rows.all()}
    return Result.ok(
        AgentRunStats(total=total, needs_review_pending=pending, status_counts=status_counts)
    )


@router.get("/agent-runs/{turn_id}", response_model=Result[AgentRunDetail])
async def get_agent_run(turn_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """单 run 完整回放：metadata + trace + context_snapshot。归属不匹配返回 404。"""
    run = await db.scalar(
        select(AgentRun)
        .join(Conversation, AgentRun.conversation_id == Conversation.conversation_id)
        .where(AgentRun.turn_id == turn_id, _ownership_filter(user))
    )
    if run is None:
        return _error(404, "AGENT_RUN_NOT_FOUND")
    return Result.ok(
        AgentRunDetail(
            id=run.id, turn_id=run.turn_id, conversation_id=run.conversation_id,
            query=run.query, status=run.status, stop_reason=run.stop_reason,
            steps=run.steps, tool_calls=run.tool_calls, error_tools=run.error_tools,
            needs_review=run.needs_review, review_status=run.review_status,
            expected_tools=run.expected_tools, category=run.category, synced=run.synced,
            error=run.error, trace=run.trace or [], context_snapshot=run.context_snapshot,
            created_at=run.created_at.isoformat() if run.created_at else None,
        )
    )


@router.post("/agent-runs/{turn_id}/review", response_model=Result[AgentRunListItem])
async def review_agent_run(
    turn_id: str,
    req: AgentRunReviewRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """评审（失败沉淀）：accepted 需 expected_tools + category；rejected 直接拒绝。"""
    run = await db.scalar(
        select(AgentRun)
        .join(Conversation, AgentRun.conversation_id == Conversation.conversation_id)
        .where(AgentRun.turn_id == turn_id, _ownership_filter(user))
    )
    if run is None:
        return _error(404, "AGENT_RUN_NOT_FOUND")
    if req.decision == "accepted":
        if not req.expected_tools or not req.category:
            return _error(422, "AGENT_RUN_REVIEW_INVALID")
        run.review_status = "accepted"
        run.expected_tools = req.expected_tools
        run.category = req.category
    else:
        run.review_status = "rejected"
    await db.commit()
    return Result.ok(_agent_run_item(run))
