"""TurnCoordinator - C1-A/C1-B: Chat 单轮编排状态机。

同步 /api/chat/send 与流式 /api/chat/send/stream 共用本协调器。
- 自管 session 生命周期：每段事务自建 AsyncSessionLocal，显式 commit；模型流式期间不持有 DB 事务。
- PG 真相源：USER/ASSISTANT/summary 先 PG commit，再 post-commit 刷 Redis；Redis 故障转 warning。
- 产出 typed ChatEvent；流式端点格式化 SSE，同步端点 drain 收集。
- per-conversation turn guard（进程内）：同 cid 进行中返回 409。
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select

from app.ai.agent.react_loop import ReactLoopState, react_loop
from app.ai.agent.tools import AgentToolkit
from app.ai.config import get_reflection_model, get_reranker
from app.ai.memory.long_term import LongTermMemoryManager
from app.ai.memory.short_term import ShortTermMemoryManager, MessageEntry
from app.ai.prompt.template_manager import PromptTemplateManager
from app.ai.rag.hybrid_retriever import HybridRetriever
from app.ai.rag.query_rewriter import QueryRewriter
from app.ai.rag.semantic_chunker import SemanticChunker
from app.ai.services.context_builder import ContextBuilder
from app.ai.services.post_turn_processor import PostTurnProcessor
from app.ai.services.rag import RagService
from app.core.config import settings
from app.core.enums import PromptType
from app.db.session import AsyncSessionLocal
from app.models import AgentRun, Conversation, Document, LongTermMemory
from app.schemas.chat_events import (
    ChatCompleted,
    ChatFailed,
    ChatStarted,
    ContextReferences,
    ContextWarning,
    ErrorInfo,
    KnowledgeRef,
    MemoryRef,
    PostTurnWarning,
    ReasoningDelta,
    TokenDelta,
)

MEMORY_EXTRACT_THRESHOLD = 4

logger = logging.getLogger(__name__)


def _compute_needs_review(status: str, state: ReactLoopState) -> bool:
    """失败沉淀判定（ADR-0017）：error/empty/工具真实异常 → 待评审；cancelled 除外。

    与 eval 的 closure 语义一致：空终答 = 失败（用户什么都没得到）。
    """
    if status == "cancelled":
        return False
    return status in ("error", "empty") or state.error_tools > 0


def _strip_reasoning(trace: list) -> list:
    """按 agent_trace_include_reasoning 脱敏 thought 条目的 reasoning 全文（ADR-0017 D1）。

    默认只留 {type, step, chars}；开启后保留完整思考文本（调试/复盘用）。
    """
    if settings.agent_trace_include_reasoning:
        return trace
    stripped = []
    for entry in trace:
        if entry.get("type") == "thought":
            e = dict(entry)
            e.pop("reasoning", None)
            stripped.append(e)
        else:
            stripped.append(entry)
    return stripped


class ChatTurnInProgress(Exception):
    def __init__(self, cid: str) -> None:
        super().__init__(f"chat turn in progress: {cid}")
        self.cid = cid


class ChatConversationNotFound(Exception):
    def __init__(self, cid: str) -> None:
        super().__init__(f"chat conversation not found: {cid}")
        self.cid = cid


class ChatTurnStartFailed(Exception):
    """Transaction A 在响应创建前失败；只向 router 暴露稳定错误类型。"""


class ChatTurnFailed(Exception):
    def __init__(self, event: ChatFailed) -> None:
        super().__init__(f"chat failed: {event.phase}")
        self.event = event


@dataclass
class TurnResult:
    conversation_id: str
    reply: str
    assistant_message_id: int
    warnings: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class PreparedTurn:
    """已完成 Transaction A、可安全创建同步或流式响应的单轮输入。"""

    conversation_id: str
    created: bool
    warnings: list[dict] = field(default_factory=list)
    mode: str | None = None  # 前端切换 rag|agent；None = 用 settings.chat_mode


class TurnCoordinator:
    _active: set[str] = set()  # 类级 turn guard（local-first 单 worker）

    def __init__(self, chat_model, redis_client, vector_recall, chunker, query_rewriter, lexical_recall=None) -> None:
        self.chat_model = chat_model
        self.redis = redis_client
        self.vector_recall = vector_recall
        self.chunker = chunker
        self.query_rewriter = query_rewriter
        self.lexical_recall = lexical_recall
        self.reranker = get_reranker()
        self.context_builder = ContextBuilder(chat_model, redis_client, vector_recall, lexical_recall, query_rewriter, chunker, self.reranker)
        self.post_turn_processor = PostTurnProcessor(chat_model, redis_client, vector_recall)
        self._owned_guards: set[str] = set()

    def _managers(self, session):
        st = ShortTermMemoryManager(self.redis, session, self.chat_model)
        lt = LongTermMemoryManager(session, self.vector_recall)
        hybrid = HybridRetriever(session, self.vector_recall, self.lexical_recall)
        rag = RagService(session, self.chunker, self.vector_recall, self.query_rewriter, hybrid)
        pm = PromptTemplateManager(session)
        return st, lt, rag, pm

    @staticmethod
    def _log_degradation(cid: str, phase: str, component: str, code: str) -> None:
        """只记录稳定字段；禁止异常正文、Prompt、用户消息和连接信息。"""
        logger.warning(
            "chat degraded phase=%s component=%s code=%s conversation_id=%s",
            phase,
            component,
            code,
            cid,
        )
        from app.ai.events.producer import emit_error_event

        emit_error_event(component=component, code=code, message="", details={"conversation_id": cid})

    async def _persist_agent_run(
        self,
        cid: str,
        turn_id: str,
        query: str,
        state: ReactLoopState,
        *,
        status: str,
        context: dict | None = None,
        stop_reason: str | None = None,
        error: str | None = None,
    ) -> None:
        """Best-effort 写 agent_runs（ADR-0017）。失败只 warning，不 fail turn。

        短事务（AsyncSessionLocal 自建自关，不跨模型等待持有连接）。只记录稳定标识
        与脱敏错误码，不记录用户消息/reasoning 全文（reasoning 按配置脱敏）。
        """
        try:
            run = AgentRun(
                turn_id=turn_id,
                conversation_id=cid,
                query=query,
                status=status,
                stop_reason=stop_reason or state.stop_reason,
                steps=state.steps,
                tool_calls=state.tool_calls,
                error_tools=state.error_tools,
                needs_review=_compute_needs_review(status, state),
                trace=_strip_reasoning(state.trace),
                context_snapshot=context,
                error=error,
            )
            async with AsyncSessionLocal() as s:
                s.add(run)
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "agent run persist failed code=agent_run_persist_failed "
                "conversation_id=%s turn_id=%s error_type=%s",
                cid,
                turn_id,
                type(exc).__name__,
            )

    def _context_warning(
        self, cid: str, component: str, code: str, message: str
    ) -> tuple[str, str, str]:
        self._log_degradation(cid, "context", component, code)
        return component, code, message

    def _post_warning(
        self, cid: str, component: str, code: str, message: str
    ) -> dict:
        self._log_degradation(cid, "post_turn", component, code)
        return {"component": component, "code": code, "message": message}

    @staticmethod
    def _log_failure(
        cid: str, turn_id: str, phase: str, component: str, code: str
    ) -> None:
        logger.error(
            "chat failed phase=%s component=%s code=%s conversation_id=%s turn_id=%s",
            phase,
            component,
            code,
            cid,
            turn_id,
        )

    def _acquire(self, cid: str) -> bool:
        if cid in TurnCoordinator._active:
            return False
        TurnCoordinator._active.add(cid)
        self._owned_guards.add(cid)
        return True

    def _release(self, cid: str) -> None:
        if cid in self._owned_guards:
            self._owned_guards.discard(cid)
            TurnCoordinator._active.discard(cid)

    def acquire_turn(self, cid: str | None) -> None:
        """显式获取 turn guard；主要供内部 prepare 与生命周期边界测试使用。"""
        if cid is not None and not self._acquire(cid):
            raise ChatTurnInProgress(cid)

    def release_turn(self, cid: str | None) -> None:
        """响应在 body iterator 尚未启动时也可幂等释放已领用的 guard。"""
        if cid is not None:
            self._release(cid)

    async def prepare_turn(
        self, conversation_id: str | None, message: str, user_id: int | None = None,
        mode: str | None = None,
    ) -> PreparedTurn:
        """响应创建前完成 existence preflight、guard 与 Transaction A。

        成功返回时 Conversation 和 USER Message 已 commit，且所有自管 session 均已
        退出；失败使用 HTTP 前置错误语义，并幂等释放已领取的 guard。

        user_id：路由层注入当前用户 id；None 时跳过归属校验（直连服务测试）。
        """
        if conversation_id is not None:
            try:
                async with AsyncSessionLocal() as session:
                    stmt = select(Conversation.id).where(
                        Conversation.conversation_id == conversation_id
                    )
                    if user_id is not None:
                        # 归属校验（P0-5 收紧）：登录用户只见自己的会话（user_id 精确匹配）；
                        # 无主会话（user_id IS NULL，直连测试/遗留）不再对所有用户可见。
                        # 直连调用（user_id=None）不受影响，仍见全部（向后兼容测试/遗留）。
                        stmt = stmt.where(Conversation.user_id == user_id)
                    exists = await session.scalar(stmt)
            except Exception as exc:
                logger.warning(
                    "chat turn prepare failed code=conversation_preflight_failed "
                    "conversation_id=%s",
                    conversation_id,
                )
                raise ChatTurnStartFailed from exc
            if exists is None:
                raise ChatConversationNotFound(conversation_id)
            cid = conversation_id
            created = False
        else:
            cid = uuid.uuid4().hex
            while cid in TurnCoordinator._active:
                cid = uuid.uuid4().hex
            created = True

        self.acquire_turn(cid)
        try:
            warnings = await self._txn_user(cid, message, created=created, user_id=user_id)
        except BaseException as exc:
            self.release_turn(cid)
            if isinstance(exc, asyncio.CancelledError):
                logger.info(
                    "chat turn prepare cancelled code=client_disconnected "
                    "conversation_id=%s",
                    cid,
                )
                raise
            if not isinstance(exc, Exception):
                raise
            logger.warning(
                "chat turn prepare failed code=transaction_a_failed conversation_id=%s",
                cid,
            )
            raise ChatTurnStartFailed from exc
        return PreparedTurn(conversation_id=cid, created=created, warnings=warnings, mode=mode)

    async def run(self, prepared: PreparedTurn, message: str):
        """产出 typed 事件；Transaction A 已由 prepare_turn 在响应创建前提交。"""
        turn_id = uuid.uuid4().hex
        seq = 0

        def nxt() -> int:
            nonlocal seq
            seq += 1
            return seq

        cid = prepared.conversation_id
        terminal_emitted = False

        try:
            yield ChatStarted(
                conversation_id=cid,
                turn_id=turn_id,
                sequence=nxt(),
                created=prepared.created,
            )

            for w in prepared.warnings:
                yield ContextWarning(
                    conversation_id=cid, turn_id=turn_id, sequence=nxt(),
                    component=w["component"], code=w["code"], message=w["message"], retryable=True,
                )

            # ---- build context (exclude current USER) ----
            # CHAT_MODE（ADR-0016）：rag=确定性状态机 / agent=ReAct 工具循环
            # 前端可通过 ChatRequest.mode 按请求覆盖 settings.chat_mode
            agent_mode = (prepared.mode or settings.chat_mode) == "agent"
            prompt: str | None = None
            messages = None
            context_snapshot: dict | None = None  # agent 模式：本轮上下文快照（ADR-0017）
            if agent_mode:
                # agent 模式：构造 LangChain messages（记忆/历史/摘要注入，
                # 跳过 RAG 预检索，检索决策交给 search_knowledge 工具）
                try:
                    toolkit = AgentToolkit(
                        self.vector_recall, self.lexical_recall,
                        self.query_rewriter, self.chunker, self.reranker,
                    )
                    tools = toolkit.get_tools()
                    tool_map = {t.name: t for t in tools}
                    tools_desc = "\n".join(f"- {t.name}: {t.description}" for t in tools)
                    messages, ctx_warns, refs, snapshot = (
                        await self.context_builder.build_agent_messages(
                            cid, message, self._context_warning,
                            self._build_agent_system_prompt(tools_desc),
                        )
                    )
                    context_snapshot = snapshot
                except Exception:
                    self._log_failure(
                        cid, turn_id, "context", "prompt_context", "CONTEXT_FAILED"
                    )
                    yield ChatFailed(
                        conversation_id=cid, turn_id=turn_id, sequence=nxt(), phase="context",
                        error=ErrorInfo(code="CONTEXT_FAILED", message="上下文构建失败", retryable=True),
                        partial_output_persisted=False,
                    )
                    terminal_emitted = True
                    return
            else:
                prompt, ctx_warns, refs = await self._build_context(cid, message)
                if prompt is None:
                    self._log_failure(
                        cid, turn_id, "context", "prompt_context", "CONTEXT_FAILED"
                    )
                    yield ChatFailed(
                        conversation_id=cid, turn_id=turn_id, sequence=nxt(), phase="context",
                        error=ErrorInfo(code="CONTEXT_FAILED", message="上下文构建失败", retryable=True),
                        partial_output_persisted=False,
                    )
                    terminal_emitted = True
                    return
            for comp, code, msg in ctx_warns:
                yield ContextWarning(
                    conversation_id=cid, turn_id=turn_id, sequence=nxt(),
                    component=comp, code=code, message=msg, retryable=True,
                )

            # C6: 检索后、模型前下发本轮参考来源（知识 chunk + 记忆）
            yield ContextReferences(
                conversation_id=cid, turn_id=turn_id, sequence=nxt(),
                knowledge_refs=[KnowledgeRef(**r) for r in refs["knowledge_refs"]],
                memory_refs=[MemoryRef(**r) for r in refs["memory_refs"]],
            )

            # ---- model generation ----
            # agent：ReAct 工具循环（模型自主决策）；rag：单次 astream
            text = ""
            model_stream = None
            state = ReactLoopState()  # agent run 状态（预创建，异常/cancelled 路径可用）
            try:
                if agent_mode:
                    bound = self.chat_model.bind_tools(
                        list(tool_map.values()), tool_choice="auto",
                        extra_body={"thinking": {"type": "enabled"}},
                    )
                    async for ev in react_loop(
                        bound, messages, tool_map, cid, turn_id, nxt, state,
                        # 反射用独立非 thinking 模型（thinking 下 function_calling 不可用）
                        reflection_model=get_reflection_model(),
                    ):
                        yield ev
                    text = state.text
                else:
                    model_stream = self.chat_model.astream(prompt)
                    async for chunk in model_stream:
                        # C6: reasoning_content 与 content 在不同 chunk，分别分流
                        reasoning = (
                            chunk.additional_kwargs.get("reasoning_content")
                            if hasattr(chunk, "additional_kwargs")
                            else None
                        )
                        if reasoning:
                            yield ReasoningDelta(
                                conversation_id=cid, turn_id=turn_id, sequence=nxt(), delta=reasoning
                            )
                        delta = chunk.content if hasattr(chunk, "content") else str(chunk)
                        if delta:
                            text += delta
                            yield TokenDelta(
                                conversation_id=cid, turn_id=turn_id, sequence=nxt(), delta=delta
                            )
            except asyncio.CancelledError:
                if agent_mode:
                    # 客户端断开也落 cancelled run（可观测的失败模式，best-effort）
                    await self._persist_agent_run(
                        cid, turn_id, message, state,
                        status="cancelled", stop_reason="cancelled",
                        context=context_snapshot,
                    )
                raise  # 客户端断开：丢弃 partial、保留 USER、不伪造终态
            except Exception:
                self._log_failure(
                    cid, turn_id, "model", "model", "MODEL_STREAM_FAILED"
                )
                yield ChatFailed(
                    conversation_id=cid, turn_id=turn_id, sequence=nxt(), phase="model",
                    error=ErrorInfo(code="MODEL_STREAM_FAILED", message="模型生成失败", retryable=True),
                    partial_output_persisted=False,
                )
                if agent_mode:
                    # 模型异常也落 error run（best-effort，含失败前 partial trace）
                    await self._persist_agent_run(
                        cid, turn_id, message, state,
                        status="error", stop_reason="error",
                        error="MODEL_STREAM_FAILED", context=context_snapshot,
                    )
                terminal_emitted = True
                return
            finally:
                # agent 模式的 astream 由 react_loop 内部管理（每轮自开自关）；
                # 仅 rag 模式的 model_stream 需显式 aclose 把 Abort 传播到上游。
                if model_stream is not None:
                    close_model_stream = getattr(model_stream, "aclose", None)
                    if close_model_stream is not None:
                        try:
                            await close_model_stream()
                        except Exception:
                            # 关闭失败不能覆盖既有业务终态或阻止 guard 的 finally；
                            # 只记录稳定标识，不记录 Prompt、partial 或异常正文。
                            logger.warning(
                                "model stream close failed code=model_stream_close_failed "
                                "conversation_id=%s turn_id=%s",
                                cid,
                                turn_id,
                            )

            # ---- LLMOps（ADR-0017）：agent 成功路径落 run（best-effort）----
            if agent_mode:
                run_status = "empty" if not text.strip() else "completed"
                await self._persist_agent_run(
                    cid, turn_id, message, state,
                    status=run_status, context=context_snapshot,
                )

            # ---- Transaction B: persist ASSISTANT + commit ----
            assistant_id = await self._txn_assistant(cid, text)
            if assistant_id is None:
                self._log_failure(
                    cid, turn_id, "persist", "message_store", "PERSIST_FAILED"
                )
                yield ChatFailed(
                    conversation_id=cid, turn_id=turn_id, sequence=nxt(), phase="persist",
                    error=ErrorInfo(code="PERSIST_FAILED", message="回复持久化失败", retryable=True),
                    partial_output_persisted=False,
                )
                terminal_emitted = True
                return

            # ---- post-turn (cache refresh + summary + extraction) ----
            try:
                post_warns = await self._post_turn(cid, text)
            except Exception:
                self._log_failure(
                    cid, turn_id, "post_turn", "post_turn", "POST_TURN_FAILED"
                )
                yield ChatFailed(
                    conversation_id=cid,
                    turn_id=turn_id,
                    sequence=nxt(),
                    phase="post_turn",
                    error=ErrorInfo(
                        code="POST_TURN_FAILED",
                        message="回复后处理失败",
                        retryable=True,
                    ),
                    partial_output_persisted=False,
                )
                terminal_emitted = True
                return
            for w in post_warns:
                yield PostTurnWarning(
                    conversation_id=cid, turn_id=turn_id, sequence=nxt(),
                    component=w["component"], code=w["code"], message=w["message"], retryable=True,
                )

            yield ChatCompleted(
                conversation_id=cid, turn_id=turn_id, sequence=nxt(),
                assistant_message_id=assistant_id,
                warning_count=len(prepared.warnings) + len(ctx_warns) + len(post_warns),
            )
            terminal_emitted = True
        except (asyncio.CancelledError, GeneratorExit):
            if not terminal_emitted:
                # 只记录稳定标识和脱敏错误码；不记录用户消息、Prompt 或模型 partial。
                logger.info(
                    "chat stream closed code=client_disconnected conversation_id=%s turn_id=%s",
                    cid or "pending",
                    turn_id,
                )
            raise
        finally:
            self._release(cid)

    async def _txn_user(self, cid: str, message: str, *, created: bool, user_id: int | None = None) -> list[dict]:
        """Transaction A：必要时创建 Conversation，写 USER 并显式 commit。"""
        warns: list[dict] = []
        async with AsyncSessionLocal() as s:
            st, _, _, _ = self._managers(s)
            if created:
                s.add(
                    Conversation(
                        conversation_id=cid,
                        title=(message[:30] if message else "新对话"),
                        user_id=user_id,
                    )
                )
                await s.flush()
            await st.persist_message(cid, "USER", message)
            await s.commit()
        # post-commit USER cache refresh：以 PG 最近窗口全量替换，避免冷/脏缓存被
        # 单条 append 伪装成完整窗口。
        try:
            await self._refresh_message_cache_after_commit(cid)
        except Exception:
            self._log_degradation(cid, "context", "message_cache", "REDIS_UNAVAILABLE")
            warns.append(
                {
                    "component": "message_cache",
                    "code": "REDIS_UNAVAILABLE",
                    "message": "用户消息缓存刷新失败，已保留 PostgreSQL 真相",
                }
            )
        return warns

    async def _refresh_message_cache_after_commit(self, cid: str) -> None:
        """从 PG 真相重建消息缓存，且 Redis I/O 不与 DB transaction 重叠。

        USER/ASSISTANT 都走同一路径。每次 commit 后的小窗口查询换取缓存自愈：
        即使 Redis 在 USER 阶段不可用、ASSISTANT 阶段恢复，也不会得到仅含回复的
        伪完整缓存。
        """
        async with AsyncSessionLocal() as s:
            st = ShortTermMemoryManager(self.redis, s, self.chat_model)
            messages = await st.read_recent_messages(cid)
        async with AsyncSessionLocal() as s:
            st = ShortTermMemoryManager(self.redis, s, self.chat_model)
            await st.refill_message_cache(cid, messages)

    async def _txn_assistant(self, cid, text) -> int | None:
        """Transaction B。返回 message_id；None 表示 persist 失败。"""
        try:
            async with AsyncSessionLocal() as s:
                st, _, _, _ = self._managers(s)
                msg = await st.persist_message(cid, "ASSISTANT", text)
                await s.commit()
                return msg.id
        except Exception:
            return None

    async def _load_messages(self, cid: str):
        """Delegate to ContextBuilder. @deprecated: use self.context_builder.load_messages()"""
        return await self.context_builder.load_messages(cid)

    async def _load_summary(self, cid: str):
        """Delegate to ContextBuilder. @deprecated: use self.context_builder.load_summary()"""
        return await self.context_builder.load_summary(cid)

    async def _build_context(self, cid, message):
        """Delegate to ContextBuilder. @deprecated: use self.context_builder.build()"""
        return await self.context_builder.build(cid, message, self._context_warning)

    @staticmethod
    def _build_agent_system_prompt(tools_desc: str) -> str:
        """Agent 模式 system prompt（ADR-0016）。硬编码而非 DB 模板：工具描述天然来自
        @tool docstring，与代码同步；稳定后可迁移 PromptTemplate。"""
        return (
            "你是 CodeAware Agent，一个能自主调用工具来回答问题的智能助手。\n"
            "你的知识库包含团队的技术文档，回答问题时优先检索知识库，确保答案有依据。\n\n"
            "## 可用工具\n"
            f"{tools_desc}\n\n"
            "## 行为规则\n"
            "1. 需要知识库内容时，调用 search_knowledge 检索；只看片段不够时，用 get_document 看全文。\n"
            "2. 需要精确计算或当前时间时，调用对应工具，不要凭记忆口算或臆测时间。\n"
            "3. 一次可并行调用多个独立工具。\n"
            "4. **自评循环（每轮工具结果后必做）**：先评估——基于当前已获得的信息能否完整回答？\n"
            "   能 → 立即停止调用任何工具，直接给出最终回答。\n"
            "   不能 → 明确说明还缺什么信息，然后调用**一次**针对性工具（不要连续调用同类工具）。\n"
            "5. 检索到答案所需的文档后立即停止，不要为'确认'或'追求完整'继续取文档。\n"
            "6. 若检索不到相关内容，诚实说明知识库中没有该信息，不要编造。"
        )

    async def _post_turn_summary(self, cid, warnings):
        """Delegate to PostTurnProcessor. @deprecated: use self.post_turn_processor.run_summary()"""
        await self.post_turn_processor.run_summary(cid, warnings, self._post_warning)

    async def _post_turn_extraction(self, cid, warnings):
        """Delegate to PostTurnProcessor. @deprecated: use self.post_turn_processor.run_extraction()"""
        await self.post_turn_processor.run_extraction(cid, warnings, self._post_warning)

    async def _post_turn(self, cid: str, assistant_text: str) -> list[dict]:
        """Transaction C：post-commit 后处理（摘要 + 记忆抽取 + 缓存刷新）。"""
        warnings: list[dict] = []
        try:
            try:
                async with AsyncSessionLocal() as s:
                    st = ShortTermMemoryManager(self.redis, s, self.chat_model)
                    await st.refresh_message_cache(cid, "ASSISTANT", assistant_text)
            except Exception:
                warnings.append(self._post_warning(cid, "message_cache", "REDIS_UNAVAILABLE", "消息缓存回填失败，已使用 PostgreSQL 真相"))

            await self._post_turn_summary(cid, warnings)
            await self._post_turn_extraction(cid, warnings)
        except Exception as exc:
            logger.warning("post turn failed conversation_id=%s error=%s", cid, exc)
            warnings.append(self._post_warning(cid, "post_turn", "POST_TURN_FAILED", "回复后处理降级"))
        return warnings
    async def run_sync(self, prepared: PreparedTurn, message: str) -> TurnResult:
        """同步端点：drain run()，收集 reply + warnings；遇 ChatFailed 抛 ChatTurnFailed。"""
        reply_parts: list[str] = []
        warnings: list[dict] = []
        cid = prepared.conversation_id
        assistant_id = 0
        failed_event: ChatFailed | None = None
        event_gen = self.run(prepared, message)
        try:
            async for ev in event_gen:
                cid = ev.conversation_id or cid
                if isinstance(ev, TokenDelta):
                    reply_parts.append(ev.delta)
                elif isinstance(ev, (ContextWarning, PostTurnWarning)):
                    warnings.append(
                        {
                            "component": ev.component,
                            "code": ev.code,
                            "message": ev.message,
                            "retryable": ev.retryable,
                        }
                    )
                elif isinstance(ev, ChatCompleted):
                    assistant_id = ev.assistant_message_id
                elif isinstance(ev, ChatFailed):
                    # 先让 run() 从 failed yield 恢复并进入 finally 释放 guard，
                    # 再向同步 endpoint 抛出稳定失败。
                    failed_event = ev
        finally:
            await event_gen.aclose()
        if failed_event is not None:
            raise ChatTurnFailed(failed_event)
        return TurnResult(
            conversation_id=cid,
            reply="".join(reply_parts),
            assistant_message_id=assistant_id,
            warnings=warnings,
        )
