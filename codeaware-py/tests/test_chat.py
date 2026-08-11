"""C1-A：TurnCoordinator - typed SSE 事件 / 时序 / 失败 / 并发 / USER 一次。"""

import asyncio
import json
import logging

import pytest
from sqlalchemy import delete, select

from app.ai.infra.vector_recall import VectorRecallService
from app.ai.memory.long_term import LongTermMemoryManager
from app.ai.rag.query_rewriter import QueryRewriter
from app.ai.services import turn_coordinator as turn_coordinator_module
from app.ai.services.chat import ChatService
from app.ai.services.turn_coordinator import (
    ChatConversationNotFound,
    ChatTurnFailed,
    ChatTurnInProgress,
    ChatTurnStartFailed,
    PreparedTurn,
    TurnResult,
    TurnCoordinator,
)
from app.api.v1.chat import _ClosingStreamingResponse, _format_sse, send, send_stream
from app.db.session import AsyncSessionLocal
from app.main import app
from app.models import Conversation, LongTermMemory, Message, PromptTemplate
from app.schemas.chat import ChatRequest
from app.schemas.chat_events import (
    ChatCompleted,
    ChatFailed,
    ChatStarted,
    ContextReferences,
    ContextWarning,
    PostTurnWarning,
    ReasoningDelta,
    TokenDelta,
)


@pytest.fixture(autouse=True)
async def _ensure_active_chat_template(setup_db):
    """create_all 不执行 Alembic seed；为本模块还原生产 CHAT 模板前置。"""
    inserted_id = None
    async with AsyncSessionLocal() as session:
        exists = await session.scalar(
            select(PromptTemplate.id).where(
                PromptTemplate.type == "CHAT",
                PromptTemplate.is_active.is_(True),
            )
        )
        if exists is None:
            session.add(
                PromptTemplate(
                    type="CHAT",
                    version=1,
                    name="test chat",
                    role_setting="你是测试助手。",
                    template_body=(
                        "{{long_term_memory}}\n{{rag_context}}\n"
                        "{{conversation_history}}\n{{user_message}}"
                    ),
                    is_active=True,
                )
            )
            await session.commit()
            inserted_id = await session.scalar(
                select(PromptTemplate.id).where(
                    PromptTemplate.type == "CHAT",
                    PromptTemplate.is_active.is_(True),
                )
            )
    yield
    if inserted_id is not None:
        async with AsyncSessionLocal() as cleanup_session:
            await cleanup_session.execute(
                delete(PromptTemplate).where(PromptTemplate.id == inserted_id)
            )
            await cleanup_session.commit()


class _StreamLLM:
    """支持 astream(分片) + ainvoke(摘要/抽取) 的 fake；记录捕获的 prompt。"""

    def __init__(self, tokens, ainvoke_text="pong摘要", astream_raises=False):
        self.tokens = tokens
        self.ainvoke_text = ainvoke_text
        self.astream_raises = astream_raises
        self.captured_prompt = None

    async def ainvoke(self, prompt, **kw):
        self.captured_prompt = prompt
        class _R:
            content = self.ainvoke_text
        return _R()

    async def astream(self, prompt, **kw):
        self.captured_prompt = prompt
        if self.astream_raises:
            raise RuntimeError("model boom")
        for t in self.tokens:
            class _C:
                content = t
            yield _C()


class _ReasoningLLM(_StreamLLM):
    """C6: astream 先产 reasoning_content(additional_kwargs) 再产 content 的 fake。"""

    def __init__(self, reasoning_tokens, content_tokens, ainvoke_text="pong摘要"):
        super().__init__(content_tokens, ainvoke_text=ainvoke_text)
        self.reasoning_tokens = reasoning_tokens

    async def astream(self, prompt, **kw):
        self.captured_prompt = prompt
        for rt in self.reasoning_tokens:
            class _R:
                content = None
                additional_kwargs = {"reasoning_content": rt}

            yield _R()
        for t in self.tokens:
            class _C:
                content = t
                additional_kwargs = {}

            yield _C()


class _AbortAwareLLM(_StreamLLM):
    """可在给定 token 后阻塞，并记录上游是否把取消传进 astream。"""

    def __init__(self, tokens=()):
        super().__init__(tokens)
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.abort_observed = False
        self.closed = False

    async def astream(self, prompt, **kw):
        self.captured_prompt = prompt
        try:
            for token in self.tokens:
                class _C:
                    content = token

                yield _C()
            self.waiting.set()
            await self.release.wait()
        except (asyncio.CancelledError, GeneratorExit):
            self.abort_observed = True
            raise
        finally:
            self.closed = True


class _CloseFailingStream:
    def __init__(self):
        self._sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._sent:
            raise StopAsyncIteration
        self._sent = True

        class _C:
            content = "ok"

        return _C()

    async def aclose(self):
        raise RuntimeError("sensitive provider close details")


class _CloseFailingLLM(_StreamLLM):
    def __init__(self):
        super().__init__([])

    def astream(self, prompt, **kw):
        self.captured_prompt = prompt
        return _CloseFailingStream()


class _CreationFailingLLM(_StreamLLM):
    def __init__(self):
        super().__init__([])

    def astream(self, _prompt, **_kw):
        raise RuntimeError("sensitive synchronous provider failure")


def _coord(redis_client, vector_recall, chunker, llm):
    return TurnCoordinator(llm, redis_client, vector_recall, chunker, QueryRewriter(llm))


async def _events(coord, cid, message):
    prepared = await coord.prepare_turn(cid, message)
    return [ev async for ev in coord.run(prepared, message)]


async def _sync_result(coord, cid, message):
    prepared = await coord.prepare_turn(cid, message)
    return await coord.run_sync(prepared, message)


async def _empty_events():
    if False:
        yield None


def _frame_payload(frame: str) -> dict:
    data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


class _EndpointCoordinator:
    """记录 router 调用顺序，避免测试依赖具体模型或数据库。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def prepare_turn(self, cid, message, user_id=None, mode=None):
        self.calls.append(("prepare_turn", cid, message, mode))
        return PreparedTurn(conversation_id=cid or "new-cid", created=cid is None, mode=mode)

    async def run_sync(self, prepared, message):
        self.calls.append(("run_sync", prepared.conversation_id, message))
        return TurnResult(
            conversation_id=prepared.conversation_id,
            reply="reply",
            assistant_message_id=1,
        )

    def run(self, prepared, message):
        self.calls.append(("run", prepared.conversation_id, message))
        return _empty_events()

    def release_turn(self, cid):
        self.calls.append(("release_turn", cid))


class _RejectingEndpointCoordinator:
    def __init__(self, error) -> None:
        self.error = error

    async def prepare_turn(self, _cid, _message, user_id=None, mode=None):
        raise self.error


class _BlockingExternalProbe:
    def __init__(self) -> None:
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.permits: asyncio.Queue[None] = asyncio.Queue()

    async def checkpoint(self, name: str) -> None:
        await self.started.put(name)
        await self.permits.get()


class _ProbeRedis:
    def __init__(self, probe: _BlockingExternalProbe) -> None:
        self.probe = probe

    async def get(self, *_args):
        await self.probe.checkpoint("redis.get")
        return None

    async def lrange(self, *_args):
        await self.probe.checkpoint("redis.lrange")
        return []

    async def delete(self, *_args):
        await self.probe.checkpoint("redis.delete")

    async def rpush(self, *_args):
        await self.probe.checkpoint("redis.rpush")

    async def ltrim(self, *_args):
        await self.probe.checkpoint("redis.ltrim")

    async def expire(self, *_args):
        await self.probe.checkpoint("redis.expire")


class _ProbeEmbedder:
    def __init__(self, probe: _BlockingExternalProbe) -> None:
        self.probe = probe

    async def aembed_query(self, text):
        await self.probe.checkpoint(f"embed:{text}")
        return [0.01] * 1024


class _ProbeQueryRewriter:
    def __init__(self, probe: _BlockingExternalProbe) -> None:
        self.probe = probe

    async def rewrite(self, _query):
        await self.probe.checkpoint("query_rewriter")
        return ["variant-1", "variant-2"]


async def test_stream_preflight_session_exits_before_response_is_returned(
    setup_db, client, monkeypatch
):
    original_factory = turn_coordinator_module.AsyncSessionLocal
    cid = "existing-preflight-cid"
    async with original_factory() as seed_session:
        seed_session.add(Conversation(conversation_id=cid, title="preflight"))
        await seed_session.commit()

    tracked_contexts = []

    class _TrackingSessionContext:
        def __init__(self) -> None:
            self.session = original_factory()
            self.exited = False

        async def __aenter__(self):
            return await self.session.__aenter__()

        async def __aexit__(self, exc_type, exc, tb):
            try:
                return await self.session.__aexit__(exc_type, exc, tb)
            finally:
                self.exited = True

    def tracked_factory():
        context = _TrackingSessionContext()
        tracked_contexts.append(context)
        return context

    monkeypatch.setattr(turn_coordinator_module, "AsyncSessionLocal", tracked_factory)
    coord = TurnCoordinator(None, None, None, None, None)

    response = await send_stream(
        ChatRequest(conversation_id=cid, message="事务边界检查"),
        coordinator=coord,
        user=None,
    )
    try:
        assert isinstance(response, _ClosingStreamingResponse)
        assert len(tracked_contexts) == 4
        assert all(context.exited for context in tracked_contexts)
        assert all(not context.session.in_transaction() for context in tracked_contexts)
        assert cid in TurnCoordinator._active
        async with original_factory() as verification_session:
            persisted = (
                await verification_session.execute(
                    select(Message).where(Message.conversation_id == cid)
                )
            ).scalars().all()
        assert [(message.role, message.content) for message in persisted] == [
            ("USER", "事务边界检查")
        ]
        history_response = await client.get(f"/api/chat/conversations/{cid}")
        assert history_response.status_code == 200
        assert history_response.json()["data"] == [
            {"role": "USER", "content": "事务边界检查"}
        ]
    finally:
        coord.release_turn(cid)
        async with original_factory() as cleanup_session:
            await cleanup_session.execute(
                delete(Conversation).where(Conversation.conversation_id == cid)
            )
            await cleanup_session.commit()


async def test_sync_and_stream_endpoints_share_prepare_path():
    req = ChatRequest(conversation_id="shared-cid", message="same path")

    sync_coord = _EndpointCoordinator()
    sync_result = await send(req, coordinator=sync_coord, user=None)
    assert sync_result.data.conversation_id == "shared-cid"
    assert sync_coord.calls == [
        ("prepare_turn", "shared-cid", "same path", None),
        ("run_sync", "shared-cid", "same path"),
    ]

    stream_coord = _EndpointCoordinator()
    stream_result = await send_stream(req, coordinator=stream_coord, user=None)
    assert isinstance(stream_result, _ClosingStreamingResponse)
    assert stream_coord.calls == [
        ("prepare_turn", "shared-cid", "same path", None),
        ("run", "shared-cid", "same path"),
    ]


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_message"),
    [
        (
            ChatConversationNotFound("missing"),
            404,
            "CHAT_CONVERSATION_NOT_FOUND",
        ),
        (ChatTurnInProgress("busy"), 409, "CHAT_TURN_IN_PROGRESS"),
        (ChatTurnStartFailed(), 500, "CHAT_START_FAILED"),
    ],
)
@pytest.mark.parametrize("endpoint", [send, send_stream])
async def test_chat_endpoints_reject_prepare_before_response_stream(
    endpoint, error, expected_status, expected_message
):
    response = await endpoint(
        ChatRequest(conversation_id="existing-cid", message="不会建立流"),
        coordinator=_RejectingEndpointCoordinator(error),
    )

    assert response.status_code == expected_status
    payload = json.loads(response.body)
    assert payload["msg"] == expected_message


async def test_transaction_a_failure_is_http_error_and_releases_guard(
    monkeypatch, caplog
):
    coord = TurnCoordinator(None, None, None, None, None)
    active_before = set(TurnCoordinator._active)

    async def fail_transaction_a(*_args, **_kwargs):
        raise RuntimeError("sensitive database detail")

    monkeypatch.setattr(coord, "_txn_user", fail_transaction_a)
    with caplog.at_level(logging.WARNING, logger="app.ai.services.turn_coordinator"):
        response = await send_stream(
            ChatRequest(message="sensitive user message"),
            coordinator=coord,
            user=None,
        )

    assert response.status_code == 500
    assert json.loads(response.body)["msg"] == "CHAT_START_FAILED"
    assert set(TurnCoordinator._active) == active_before
    assert coord._owned_guards == set()
    assert "transaction_a_failed" in caplog.text
    assert "sensitive database detail" not in caplog.text
    assert "sensitive user message" not in caplog.text


async def test_prepare_cancellation_releases_guard(monkeypatch, caplog):
    coord = TurnCoordinator(None, None, None, None, None)
    transaction_started = asyncio.Event()
    never_release = asyncio.Event()

    async def block_transaction_a(*_args, **_kwargs):
        transaction_started.set()
        await never_release.wait()

    monkeypatch.setattr(coord, "_txn_user", block_transaction_a)
    task = asyncio.create_task(coord.prepare_turn(None, "cancelled sensitive message"))
    await asyncio.wait_for(transaction_started.wait(), timeout=2)
    owned_cid = next(iter(coord._owned_guards))

    with caplog.at_level(logging.INFO, logger="app.ai.services.turn_coordinator"):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert owned_cid not in TurnCoordinator._active
    assert coord._owned_guards == set()
    assert "client_disconnected" in caplog.text
    assert "cancelled sensitive message" not in caplog.text


async def test_build_context_never_awaits_external_io_with_active_transaction(
    setup_db, monkeypatch
):
    original_factory = turn_coordinator_module.AsyncSessionLocal
    cid = "context-transaction-probe"
    async with original_factory() as seed_session:
        seed_session.add(Conversation(conversation_id=cid, title="probe"))
        seed_session.add(
            Message(
                conversation_id=cid,
                role="USER",
                content="事务边界问题",
                token_count=3,
            )
        )
        await seed_session.commit()

    tracked_contexts = []

    class _TrackingSessionContext:
        def __init__(self) -> None:
            self.session = original_factory()

        async def __aenter__(self):
            return await self.session.__aenter__()

        async def __aexit__(self, exc_type, exc, tb):
            return await self.session.__aexit__(exc_type, exc, tb)

    def tracked_factory():
        context = _TrackingSessionContext()
        tracked_contexts.append(context)
        return context

    probe = _BlockingExternalProbe()
    monkeypatch.setattr(turn_coordinator_module, "AsyncSessionLocal", tracked_factory)
    coord = TurnCoordinator(
        None,
        _ProbeRedis(probe),
        VectorRecallService(_ProbeEmbedder(probe)),
        None,
        _ProbeQueryRewriter(probe),
    )
    context_task = asyncio.create_task(coord._build_context(cid, "事务边界问题"))
    expected_external_calls = [
        "redis.lrange",
        "redis.delete",
        "redis.rpush",
        "redis.ltrim",
        "redis.expire",
        "redis.get",
        "embed:事务边界问题",
        "query_rewriter",
        "embed:variant-1",
        "embed:variant-2",
    ]

    try:
        for expected in expected_external_calls:
            observed = await asyncio.wait_for(probe.started.get(), timeout=2)
            assert observed == expected
            assert all(
                not context.session.in_transaction() for context in tracked_contexts
            )
            probe.permits.put_nowait(None)
        prompt, warnings, refs = await asyncio.wait_for(context_task, timeout=2)
    finally:
        if not context_task.done():
            context_task.cancel()

    assert prompt is not None
    assert warnings == []
    async with original_factory() as cleanup_session:
        await cleanup_session.execute(
            delete(Conversation).where(Conversation.conversation_id == cid)
        )
        await cleanup_session.commit()


async def test_post_turn_fact_embeddings_finish_before_write_transaction(
    setup_db, redis_client, chunker, monkeypatch
):
    cid = "fact-transaction-probe"
    async with AsyncSessionLocal() as seed_session:
        seed_session.add(Conversation(conversation_id=cid, title="facts"))
        for index in range(4):
            seed_session.add(
                Message(
                    conversation_id=cid,
                    role="USER" if index % 2 == 0 else "ASSISTANT",
                    content=f"message-{index}",
                    token_count=1,
                )
            )
        await seed_session.commit()

    probe = _BlockingExternalProbe()
    original_factory = turn_coordinator_module.AsyncSessionLocal
    tracked_contexts = []

    class _TrackingSessionContext:
        def __init__(self) -> None:
            self.session = original_factory()

        async def __aenter__(self):
            return await self.session.__aenter__()

        async def __aexit__(self, exc_type, exc, tb):
            return await self.session.__aexit__(exc_type, exc, tb)

    def tracked_factory():
        context = _TrackingSessionContext()
        tracked_contexts.append(context)
        return context

    async def fake_extract(_self, _messages, _chat_model):
        return ["fact-one", "fact-two"]

    monkeypatch.setattr(turn_coordinator_module, "AsyncSessionLocal", tracked_factory)
    monkeypatch.setattr(
        LongTermMemoryManager,
        "extract_facts_text",
        fake_extract,
    )
    coord = TurnCoordinator(
        None,
        redis_client,
        VectorRecallService(_ProbeEmbedder(probe)),
        chunker,
        None,
    )
    warnings = []
    extraction_task = asyncio.create_task(coord._post_turn_extraction(cid, warnings))
    try:
        for expected in ["embed:fact-one", "embed:fact-two"]:
            observed = await asyncio.wait_for(probe.started.get(), timeout=2)
            assert observed == expected
            assert all(
                not context.session.in_transaction() for context in tracked_contexts
            )
            probe.permits.put_nowait(None)
        await asyncio.wait_for(extraction_task, timeout=2)
    finally:
        if not extraction_task.done():
            extraction_task.cancel()

    assert warnings == []
    async with original_factory() as verification_session:
        memories = (
            await verification_session.execute(
                select(LongTermMemory)
                .where(LongTermMemory.conversation_id == cid)
                .order_by(LongTermMemory.id)
            )
        ).scalars().all()
        assert [memory.content for memory in memories] == ["fact-one", "fact-two"]
        await verification_session.execute(
            delete(LongTermMemory).where(LongTermMemory.conversation_id == cid)
        )
        await verification_session.execute(
            delete(Conversation).where(Conversation.conversation_id == cid)
        )
        await verification_session.commit()


async def test_delete_conversation_commits_pg_before_best_effort_cache_cleanup(
    setup_db, caplog
):
    cid = "delete-pg-first"
    async with AsyncSessionLocal() as seed_session:
        seed_session.add(Conversation(conversation_id=cid, title="delete"))
        seed_session.add(
            Message(
                conversation_id=cid,
                role="USER",
                content="must be deleted",
                token_count=2,
            )
        )
        await seed_session.commit()

    class _CheckingFailingRedis:
        async def delete(self, *_keys):
            async with AsyncSessionLocal() as verification_session:
                exists = await verification_session.scalar(
                    select(Conversation.id).where(Conversation.conversation_id == cid)
                )
            assert exists is None
            raise RuntimeError("sensitive redis endpoint")

    async with AsyncSessionLocal() as service_session:
        service = ChatService(service_session, _CheckingFailingRedis())
        with caplog.at_level(logging.WARNING, logger="app.ai.services.chat"):
            await service.delete_conversation(cid)

    async with AsyncSessionLocal() as verification_session:
        conversation = await verification_session.scalar(
            select(Conversation.id).where(Conversation.conversation_id == cid)
        )
        messages = (
            await verification_session.execute(
                select(Message).where(Message.conversation_id == cid)
            )
        ).scalars().all()
    assert conversation is None
    assert messages == []
    assert "conversation_cache_delete_failed" in caplog.text
    assert f"conversation_id={cid}" in caplog.text
    assert "sensitive redis endpoint" not in caplog.text


async def test_delete_conversation_does_not_touch_cache_when_pg_commit_fails():
    class _CommitFailingSession:
        async def scalar(self, _statement):
            return 1

        async def execute(self, _statement):
            return None

        async def commit(self):
            raise RuntimeError("pg commit failed")

    class _RecordingRedis:
        def __init__(self):
            self.called = False

        async def delete(self, *_keys):
            self.called = True

    redis = _RecordingRedis()
    service = ChatService(_CommitFailingSession(), redis)
    with pytest.raises(RuntimeError, match="pg commit failed"):
        await service.delete_conversation("commit-failure")
    assert redis.called is False


def test_chat_openapi_freezes_sync_and_stream_response_contracts():
    schema = app.openapi()
    send_200 = schema["paths"]["/api/chat/send"]["post"]["responses"]["200"]
    assert send_200["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Result_ChatResponseVO_"
    }
    response_schema = schema["components"]["schemas"]["ChatResponseVO"]
    assert response_schema["properties"]["warnings"]["items"] == {
        "$ref": "#/components/schemas/ChatWarning"
    }

    stream_200 = schema["paths"]["/api/chat/send/stream"]["post"]["responses"]["200"]
    assert set(stream_200["content"]) == {"text/event-stream"}
    assert stream_200["content"]["text/event-stream"]["schema"]["type"] == "string"
    for path in ["/api/chat/send", "/api/chat/send/stream"]:
        responses = schema["paths"][path]["post"]["responses"]
        assert {"200", "404", "409", "422", "500"}.issubset(responses)
        assert (
            responses["404"]["content"]["application/json"]["example"]["msg"]
            == "CHAT_CONVERSATION_NOT_FOUND"
        )
        assert (
            responses["409"]["content"]["application/json"]["example"]["msg"]
            == "CHAT_TURN_IN_PROGRESS"
        )
        assert (
            responses["422"]["content"]["application/json"]["example"]["msg"]
            == "CHAT_REQUEST_INVALID"
        )
    sync_error_examples = schema["paths"]["/api/chat/send"]["post"]["responses"][
        "500"
    ]["content"]["application/json"]["examples"]
    assert {
        "CHAT_START_FAILED",
        "CONTEXT_FAILED",
        "MODEL_STREAM_FAILED",
        "PERSIST_FAILED",
        "POST_TURN_FAILED",
    } == set(sync_error_examples)
    stream_500 = schema["paths"]["/api/chat/send/stream"]["post"]["responses"]["500"]
    assert (
        stream_500["content"]["application/json"]["example"]["msg"]
        == "CHAT_START_FAILED"
    )

    failed_schema = ChatFailed.model_json_schema()
    assert failed_schema["properties"]["partial_output_persisted"]["const"] is False
    assert ChatStarted.model_json_schema()["properties"]["sequence"]["minimum"] == 1
    assert TokenDelta.model_json_schema()["properties"]["delta"]["minLength"] == 1
    completed_properties = ChatCompleted.model_json_schema()["properties"]
    assert completed_properties["assistant_message_id"]["minimum"] == 1
    assert completed_properties["warning_count"]["minimum"] == 0


@pytest.mark.parametrize("path", ["/api/chat/send", "/api/chat/send/stream"])
async def test_chat_request_validation_uses_stable_result_envelope(client, path):
    response = await client.post(
        path,
        json={"conversation_id": "sensitive-input-without-message"},
    )
    assert response.status_code == 422
    assert response.json() == {
        "code": 0,
        "msg": "CHAT_REQUEST_INVALID",
        "data": None,
    }
    assert "sensitive-input-without-message" not in response.text


async def test_non_chat_validation_uses_stable_result_contract(client):
    response = await client.post("/api/knowledge/upload", json={})
    assert response.status_code == 422
    assert response.json() == {
        "code": 0,
        "msg": "KNOWLEDGE_REQUEST_INVALID",
        "data": None,
    }


async def test_sync_returns_reply_and_persists(db_session, redis_client, vector_recall, chunker):
    coord = _coord(redis_client, vector_recall, chunker, _StreamLLM(["hel", "lo"]))
    result = await _sync_result(coord, None, "你好")
    assert result.reply == "hello"
    assert result.conversation_id
    # USER + ASSISTANT 已落 PG
    msgs = (await db_session.execute(
        select(Message).where(Message.conversation_id == result.conversation_id).order_by(Message.id)
    )).scalars().all()
    assert [m.role for m in msgs] == ["USER", "ASSISTANT"]
    assert msgs[0].content == "你好"
    assert msgs[1].content == "hello"


async def test_stream_events_sequence_and_terminal(db_session, redis_client, vector_recall, chunker):
    coord = _coord(redis_client, vector_recall, chunker, _StreamLLM([" a", "b"]))
    evs = await _events(coord, None, "q")
    # 首事件 chat.started，含 cid
    assert isinstance(evs[0], ChatStarted)
    assert evs[0].created is True
    cid = evs[0].conversation_id
    assert cid
    # sequence 从 1 严格递增
    seqs = [e.sequence for e in evs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and seqs[0] == 1
    # token.delta 保真（含前导空格）
    deltas = [e.delta for e in evs if isinstance(e, TokenDelta)]
    assert "".join(deltas) == " ab"
    # 唯一终态 chat.completed
    assert isinstance(evs[-1], ChatCompleted)
    assert sum(1 for e in evs if isinstance(e, (ChatCompleted,))) == 1

    async def replay_events():
        for event in evs:
            yield event

    frames = [frame async for frame in _format_sse(replay_events())]
    for event, frame in zip(evs, frames, strict=True):
        id_line = next(line for line in frame.splitlines() if line.startswith("id: "))
        assert int(id_line.removeprefix("id: ")) == event.sequence
        assert _frame_payload(frame)["sequence"] == event.sequence
    assert "chat.completed" in frames[-1]


async def test_completed_waits_until_post_turn_finishes(
    redis_client, vector_recall, chunker, monkeypatch
):
    coord = _coord(redis_client, vector_recall, chunker, _StreamLLM(["done"]))
    prepared = await coord.prepare_turn(None, "post-turn ordering")
    post_turn_started = asyncio.Event()
    release_post_turn = asyncio.Event()
    observed_events = []

    async def fixed_context(_cid, _message):
        return "prompt", [], {"knowledge_refs": [], "memory_refs": []}

    async def blocking_post_turn(_cid, _text):
        post_turn_started.set()
        await release_post_turn.wait()
        return []

    monkeypatch.setattr(coord, "_build_context", fixed_context)
    monkeypatch.setattr(coord, "_post_turn", blocking_post_turn)

    async def consume():
        async for event in coord.run(prepared, "post-turn ordering"):
            observed_events.append(event)

    consume_task = asyncio.create_task(consume())
    await asyncio.wait_for(post_turn_started.wait(), timeout=2)
    assert any(isinstance(event, TokenDelta) for event in observed_events)
    assert not any(isinstance(event, ChatCompleted) for event in observed_events)

    release_post_turn.set()
    await asyncio.wait_for(consume_task, timeout=2)
    assert isinstance(observed_events[-1], ChatCompleted)


async def test_delta_preserves_newline(db_session, redis_client, vector_recall, chunker):
    coord = _coord(redis_client, vector_recall, chunker, _StreamLLM(["line1\n", "line2"]))
    evs = await _events(coord, None, "q")
    deltas = [e.delta for e in evs if isinstance(e, TokenDelta)]
    assert "".join(deltas) == "line1\nline2"


async def test_model_failure_keeps_user_no_assistant(
    db_session, redis_client, vector_recall, chunker, caplog
):
    coord = _coord(redis_client, vector_recall, chunker, _StreamLLM(["x"], astream_raises=True))
    with caplog.at_level(logging.ERROR, logger="app.ai.services.turn_coordinator"):
        evs = await _events(coord, None, "敏感模型问题")
    # 失败终态
    failed = [e for e in evs if isinstance(e, ChatFailed)]
    assert len(failed) == 1
    assert failed[0].phase == "model"
    assert failed[0].partial_output_persisted is False
    cid = evs[0].conversation_id
    # USER 保留、ASSISTANT 未持久化
    msgs = (await db_session.execute(
        select(Message).where(Message.conversation_id == cid)
    )).scalars().all()
    assert [m.role for m in msgs] == ["USER"]
    assert "phase=model" in caplog.text
    assert "component=model" in caplog.text
    assert "code=MODEL_STREAM_FAILED" in caplog.text
    assert "turn_id=" in caplog.text
    assert "model boom" not in caplog.text
    assert "敏感模型问题" not in caplog.text


async def test_synchronous_model_stream_creation_failure_emits_terminal_failed(
    db_session, redis_client, vector_recall, chunker, caplog
):
    coord = _coord(redis_client, vector_recall, chunker, _CreationFailingLLM())
    with caplog.at_level(logging.ERROR, logger="app.ai.services.turn_coordinator"):
        events = await _events(coord, None, "sensitive sync failure question")

    assert isinstance(events[0], ChatStarted)
    assert isinstance(events[-1], ChatFailed)
    assert events[-1].phase == "model"
    assert sum(isinstance(event, ChatFailed) for event in events) == 1
    assert events[0].conversation_id not in TurnCoordinator._active
    assert "code=MODEL_STREAM_FAILED" in caplog.text
    assert "sensitive synchronous provider failure" not in caplog.text
    assert "sensitive sync failure question" not in caplog.text


async def test_sync_failure_releases_guard_before_caller_can_retry_same_cid(
    setup_db, redis_client, vector_recall, chunker
):
    cid = "sync-failure-retry"
    async with AsyncSessionLocal() as seed_session:
        seed_session.add(Conversation(conversation_id=cid, title="retry"))
        await seed_session.commit()
    coord = _coord(
        redis_client,
        vector_recall,
        chunker,
        _StreamLLM([], astream_raises=True),
    )

    prepared = await coord.prepare_turn(cid, "first fails")
    with pytest.raises(ChatTurnFailed):
        await coord.run_sync(prepared, "first fails")
    assert cid not in TurnCoordinator._active

    retry = await coord.prepare_turn(cid, "retry immediately")
    assert retry.conversation_id == cid
    coord.release_turn(cid)


async def test_context_and_persist_failures_emit_sanitized_structured_logs(
    db_session, redis_client, vector_recall, chunker, monkeypatch, caplog
):
    context_coord = _coord(
        redis_client, vector_recall, chunker, _StreamLLM(["unused"])
    )

    async def fail_context(_cid, _message):
        return None, [], {"knowledge_refs": [], "memory_refs": []}

    monkeypatch.setattr(context_coord, "_build_context", fail_context)
    with caplog.at_level(logging.ERROR, logger="app.ai.services.turn_coordinator"):
        context_events = await _events(
            context_coord, None, "sensitive context content"
        )
    assert isinstance(context_events[-1], ChatFailed)
    assert context_events[-1].phase == "context"
    assert "phase=context" in caplog.text
    assert "component=prompt_context" in caplog.text
    assert "code=CONTEXT_FAILED" in caplog.text
    assert "sensitive context content" not in caplog.text

    caplog.clear()
    persist_coord = _coord(
        redis_client, vector_recall, chunker, _StreamLLM(["partial"])
    )

    async def fail_persist(_cid, _text):
        return None

    monkeypatch.setattr(persist_coord, "_txn_assistant", fail_persist)
    with caplog.at_level(logging.ERROR, logger="app.ai.services.turn_coordinator"):
        persist_events = await _events(
            persist_coord, None, "sensitive persist content"
        )
    assert isinstance(persist_events[-1], ChatFailed)
    assert persist_events[-1].phase == "persist"
    assert "phase=persist" in caplog.text
    assert "component=message_store" in caplog.text
    assert "code=PERSIST_FAILED" in caplog.text
    assert "partial" not in caplog.text
    assert "sensitive persist content" not in caplog.text
    persist_cid = persist_events[0].conversation_id
    pg_messages = (
        await db_session.execute(
            select(Message)
            .where(Message.conversation_id == persist_cid)
            .order_by(Message.id)
        )
    ).scalars().all()
    assert [(message.role, message.content) for message in pg_messages] == [
        ("USER", "sensitive persist content")
    ]
    cached_messages = await redis_client.lrange(f"msgs:{persist_cid}", 0, -1)
    assert cached_messages == ["USER:::sensitive persist content"]


async def test_missing_active_chat_template_is_context_failure(
    db_session, redis_client, vector_recall, chunker, caplog
):
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(PromptTemplate).where(
                PromptTemplate.type == "CHAT",
                PromptTemplate.is_active.is_(True),
            )
        )
        await session.commit()

    coord = _coord(redis_client, vector_recall, chunker, _StreamLLM(["must-not-run"]))
    with caplog.at_level(logging.ERROR, logger="app.ai.services.turn_coordinator"):
        events = await _events(coord, None, "missing template question")

    assert isinstance(events[0], ChatStarted)
    assert isinstance(events[-1], ChatFailed)
    assert events[-1].phase == "context"
    assert not any(isinstance(event, TokenDelta) for event in events)
    assert "code=CONTEXT_FAILED" in caplog.text
    assert "must-not-run" not in caplog.text
    assert "missing template question" not in caplog.text
    persisted = (
        await db_session.execute(
            select(Message)
            .where(Message.conversation_id == events[0].conversation_id)
            .order_by(Message.id)
        )
    ).scalars().all()
    assert [(message.role, message.content) for message in persisted] == [
        ("USER", "missing template question")
    ]


async def test_concurrent_same_cid_returns_409(redis_client, vector_recall, chunker):
    coord = _coord(redis_client, vector_recall, chunker, _StreamLLM(["a"]))
    cid = "conv-concurrent"
    coord.acquire_turn(cid)
    try:
        with pytest.raises(ChatTurnInProgress):
            coord.acquire_turn(cid)
    finally:
        coord._release(cid)


async def test_real_stream_endpoint_returns_409_before_second_same_cid_stream(
    setup_db, redis_client, vector_recall, chunker
):
    cid = "endpoint-concurrent"
    async with AsyncSessionLocal() as seed_session:
        seed_session.add(Conversation(conversation_id=cid, title="concurrent"))
        await seed_session.commit()
    coord = _coord(redis_client, vector_recall, chunker, _StreamLLM(["a"]))

    first_response = await send_stream(
        ChatRequest(conversation_id=cid, message="first"),
        coordinator=coord,
        user=None,
    )
    try:
        assert isinstance(first_response, _ClosingStreamingResponse)
        second_response = await send_stream(
            ChatRequest(conversation_id=cid, message="second"),
            coordinator=coord,
            user=None,
        )
        assert second_response.status_code == 409
        assert json.loads(second_response.body)["msg"] == "CHAT_TURN_IN_PROGRESS"
    finally:
        coord.release_turn(cid)
        async with AsyncSessionLocal() as cleanup_session:
            await cleanup_session.execute(
                delete(Conversation).where(Conversation.conversation_id == cid)
            )
            await cleanup_session.commit()


async def test_two_parallel_new_turns_receive_distinct_committed_conversation_ids(
    db_session, redis_client, vector_recall, chunker
):
    coord = _coord(redis_client, vector_recall, chunker, _StreamLLM(["a"]))
    prepared_one, prepared_two = await asyncio.gather(
        coord.prepare_turn(None, "parallel-one"),
        coord.prepare_turn(None, "parallel-two"),
    )
    try:
        assert prepared_one.conversation_id != prepared_two.conversation_id
        assert prepared_one.conversation_id in TurnCoordinator._active
        assert prepared_two.conversation_id in TurnCoordinator._active
        rows = (
            await db_session.execute(
                select(Message).where(
                    Message.conversation_id.in_(
                        [
                            prepared_one.conversation_id,
                            prepared_two.conversation_id,
                        ]
                    )
                )
            )
        ).scalars().all()
        assert {(row.conversation_id, row.content) for row in rows} == {
            (prepared_one.conversation_id, "parallel-one"),
            (prepared_two.conversation_id, "parallel-two"),
        }
    finally:
        coord.release_turn(prepared_one.conversation_id)
        coord.release_turn(prepared_two.conversation_id)


async def test_user_message_appears_once_in_prompt(db_session, redis_client, vector_recall, chunker):
    llm = _StreamLLM(["a"])
    coord = _coord(redis_client, vector_recall, chunker, llm)
    await _sync_result(coord, None, "独特的用户问题XYZ")
    # 本轮 USER 只通过 user_message 进 prompt，不因 history 重复
    assert llm.captured_prompt.count("独特的用户问题XYZ") == 1


async def test_multi_turn_reuses_cid(db_session, redis_client, vector_recall, chunker):
    coord = _coord(redis_client, vector_recall, chunker, _StreamLLM(["a"]))
    r1 = await _sync_result(coord, None, "第一问")
    cid = r1.conversation_id
    r2 = await _sync_result(coord, cid, "第二问")
    assert r2.conversation_id == cid
    msgs = (await db_session.execute(
        select(Message).where(Message.conversation_id == cid).order_by(Message.id)
    )).scalars().all()
    assert len(msgs) == 4  # 2 轮 × 2


async def test_post_commit_message_cache_rebuilds_cold_and_dirty_windows(
    setup_db, redis_client, vector_recall, chunker
):
    """USER 冷缓存与 ASSISTANT 脏缓存都必须由完整 PG 窗口自愈。"""
    cid = "post-commit-cache-rebuild"
    async with AsyncSessionLocal() as seed_session:
        seed_session.add(Conversation(conversation_id=cid, title="cache rebuild"))
        seed_session.add_all(
            [
                Message(
                    conversation_id=cid,
                    role="USER",
                    content="历史问题",
                    token_count=2,
                ),
                Message(
                    conversation_id=cid,
                    role="ASSISTANT",
                    content="历史回答",
                    token_count=2,
                ),
            ]
        )
        await seed_session.commit()

    await redis_client.delete(f"msgs:{cid}")  # USER commit 前是冷缓存
    coord = _coord(redis_client, vector_recall, chunker, _StreamLLM(["unused"]))
    prepared = await coord.prepare_turn(cid, "当前问题")
    try:
        assert await redis_client.lrange(f"msgs:{cid}", 0, -1) == [
            "USER:::历史问题",
            "ASSISTANT:::历史回答",
            "USER:::当前问题",
        ]
        prompt, _warnings, _refs = await coord._build_context(cid, "当前问题")
        assert prompt is not None
        assert "历史问题" in prompt
        assert "历史回答" in prompt
        assert prompt.count("当前问题") == 1

        # 模拟 ASSISTANT 已提交，但缓存仅剩一条脏值；公共 post-commit 路径必须
        # 精确替换成 PG 的完整窗口，而不是 append 形成伪完整缓存。
        async with AsyncSessionLocal() as assistant_session:
            assistant_session.add(
                Message(
                    conversation_id=cid,
                    role="ASSISTANT",
                    content="当前回答",
                    token_count=2,
                )
            )
            await assistant_session.commit()
        await redis_client.delete(f"msgs:{cid}")
        await redis_client.rpush(f"msgs:{cid}", "ASSISTANT:::孤立脏缓存")

        await coord._refresh_message_cache_after_commit(cid)
        assert await redis_client.lrange(f"msgs:{cid}", 0, -1) == [
            "USER:::历史问题",
            "ASSISTANT:::历史回答",
            "USER:::当前问题",
            "ASSISTANT:::当前回答",
        ]
    finally:
        coord.release_turn(cid)
        await redis_client.delete(f"msgs:{cid}")
        async with AsyncSessionLocal() as cleanup_session:
            await cleanup_session.execute(
                delete(Conversation).where(Conversation.conversation_id == cid)
            )
            await cleanup_session.commit()


class _FailingRedis:
    """刷新失败但读取委托真 redis 的包装（测 post-commit 缓存降级）。"""

    def __init__(self, real):
        self._real = real

    async def rpush(self, *a, **kw):
        raise RuntimeError("redis down")

    async def set(self, *a, **kw):
        raise RuntimeError("redis down")

    def __getattr__(self, name):
        return getattr(self._real, name)


class _SummarySetFailingRedis:
    """只让摘要 SET 失败；DELETE 仍成功，用于验证旧摘要不会伪命中。"""

    def __init__(self, real):
        self._real = real

    async def set(self, *args, **kwargs):
        raise RuntimeError("sensitive summary cache endpoint")

    def __getattr__(self, name):
        return getattr(self._real, name)


async def test_summary_set_failure_invalidates_stale_cache_and_still_completes(
    setup_db, redis_client, vector_recall, chunker, monkeypatch, caplog
):
    cid = "summary-cache-set-failure"
    async with AsyncSessionLocal() as seed_session:
        seed_session.add(
            Conversation(
                conversation_id=cid,
                title="summary cache",
                summary="旧 PG 摘要",
            )
        )
        for index in range(8):
            seed_session.add(
                Message(
                    conversation_id=cid,
                    role="USER" if index % 2 == 0 else "ASSISTANT",
                    content=f"历史消息-{index}",
                    token_count=2,
                )
            )
        await seed_session.commit()
    await redis_client.set(f"summary:{cid}", "过期 Redis 摘要")

    redis_with_set_failure = _SummarySetFailingRedis(redis_client)
    coord = _coord(
        redis_with_set_failure,
        vector_recall,
        chunker,
        _StreamLLM(["新回复"], ainvoke_text="新 PG 摘要"),
    )

    async def fixed_context(_cid, _message):
        return "固定 prompt", [], {"knowledge_refs": [], "memory_refs": []}

    async def skip_extraction(_cid, _warnings):
        return None

    monkeypatch.setattr(coord, "_build_context", fixed_context)
    monkeypatch.setattr(coord, "_post_turn_extraction", skip_extraction)
    with caplog.at_level(logging.WARNING, logger="app.ai.services.turn_coordinator"):
        events = await _events(coord, cid, "触发摘要")

    assert isinstance(events[-1], ChatCompleted)
    summary_warnings = [
        event
        for event in events
        if isinstance(event, PostTurnWarning)
        and event.component == "summary_cache"
        and event.code == "REDIS_UNAVAILABLE"
    ]
    assert len(summary_warnings) == 1
    assert events[-1].warning_count == 1
    async with AsyncSessionLocal() as verification_session:
        persisted_summary = await verification_session.scalar(
            select(Conversation.summary).where(
                Conversation.conversation_id == cid
            )
        )
    assert persisted_summary == "新 PG 摘要"
    assert await redis_client.get(f"summary:{cid}") is None
    assert "sensitive summary cache endpoint" not in caplog.text

    await redis_client.delete(f"msgs:{cid}", f"summary:{cid}")
    async with AsyncSessionLocal() as cleanup_session:
        await cleanup_session.execute(
            delete(Conversation).where(Conversation.conversation_id == cid)
        )
        await cleanup_session.commit()


async def test_redis_refresh_failure_warns_but_pg_truth_persists(
    db_session, redis_client, vector_recall, chunker, caplog
):
    coord = _coord(_FailingRedis(redis_client), vector_recall, chunker, _StreamLLM(["a"]))
    with caplog.at_level(logging.WARNING, logger="app.ai.services.turn_coordinator"):
        evs = await _events(coord, None, "敏感缓存问题")
    cid = evs[0].conversation_id
    assert isinstance(evs[0], ChatStarted)
    # USER/ASSISTANT 缓存刷新失败 -> context/post_turn warning(message_cache)
    assert any(isinstance(e, PostTurnWarning) and e.component == "message_cache" for e in evs)
    warning_events = [
        event for event in evs if isinstance(event, (ContextWarning, PostTurnWarning))
    ]
    # PG 真相仍存在（无孤儿问题：PG 有，Redis 无）
    msgs = (await db_session.execute(
        select(Message).where(Message.conversation_id == cid).order_by(Message.id)
    )).scalars().all()
    assert [m.role for m in msgs] == ["USER", "ASSISTANT"]
    # 核心结果仍完成
    assert isinstance(evs[-1], ChatCompleted)
    assert evs[-1].warning_count == len(warning_events)
    assert "phase=context" in caplog.text
    assert "phase=post_turn" in caplog.text
    assert "component=message_cache" in caplog.text
    assert "code=REDIS_UNAVAILABLE" in caplog.text
    assert "redis down" not in caplog.text
    assert "敏感缓存问题" not in caplog.text


async def test_sync_warning_contract_keeps_retryable(
    redis_client, vector_recall, chunker
):
    coord = _coord(
        _FailingRedis(redis_client),
        vector_recall,
        chunker,
        _StreamLLM(["reply"]),
    )
    result = await _sync_result(coord, None, "sync warning")
    assert result.warnings
    assert all(warning["retryable"] is True for warning in result.warnings)


async def test_abort_before_first_token_closes_model_and_releases_new_cid_guard(
    db_session, redis_client, vector_recall, chunker, caplog
):
    llm = _AbortAwareLLM()
    coord = _coord(redis_client, vector_recall, chunker, llm)
    prepared = await coord.prepare_turn(None, "首 token 前取消的敏感问题")
    event_gen = coord.run(prepared, "首 token 前取消的敏感问题")
    stream = _format_sse(event_gen)

    started = _frame_payload(await anext(stream))
    cid = started["conversation_id"]
    assert cid in TurnCoordinator._active

    # C6: started 之后是 context.references，再之后才是模型首 token
    references = _frame_payload(await anext(stream))
    assert "knowledge_refs" in references

    pending_frame = asyncio.create_task(anext(stream))
    await asyncio.wait_for(llm.waiting.wait(), timeout=2)
    with caplog.at_level(logging.INFO, logger="app.ai.services.turn_coordinator"):
        pending_frame.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending_frame

    assert llm.abort_observed is True
    assert llm.closed is True
    assert cid not in TurnCoordinator._active
    assert "client_disconnected" in caplog.text
    assert "首 token 前取消的敏感问题" not in caplog.text

    msgs = (await db_session.execute(
        select(Message).where(Message.conversation_id == cid).order_by(Message.id)
    )).scalars().all()
    assert [(m.role, m.content) for m in msgs] == [("USER", "首 token 前取消的敏感问题")]


async def test_abort_after_multiple_tokens_discards_partial_and_releases_guard(
    db_session, redis_client, vector_recall, chunker, caplog
):
    llm = _AbortAwareLLM(["partial", " answer"])
    coord = _coord(redis_client, vector_recall, chunker, llm)
    prepared = await coord.prepare_turn(None, "多个 token 后取消的敏感问题")
    event_gen = coord.run(prepared, "多个 token 后取消的敏感问题")
    response = _ClosingStreamingResponse(
        _format_sse(event_gen),
        event_gen=event_gen,
        on_close=lambda: coord.release_turn(prepared.conversation_id),
    )
    body_payloads: list[dict] = []

    async def interrupted_send(message):
        if message["type"] != "http.response.body" or not message.get("body"):
            return
        body_payloads.append(_frame_payload(message["body"].decode()))
        if len(body_payloads) == 4:
            raise OSError("client socket closed")

    with caplog.at_level(logging.INFO, logger="app.ai.services.turn_coordinator"):
        with pytest.raises(OSError, match="client socket closed"):
            await response.stream_response(interrupted_send)

    cid = body_payloads[0]["conversation_id"]
    deltas = [p for p in body_payloads if "delta" in p]
    assert len(deltas) >= 2
    assert deltas[0]["delta"] + deltas[1]["delta"] == "partial answer"
    assert llm.abort_observed is True
    assert llm.closed is True
    assert cid not in TurnCoordinator._active
    assert "client_disconnected" in caplog.text
    assert "多个 token 后取消的敏感问题" not in caplog.text

    msgs = (await db_session.execute(
        select(Message).where(Message.conversation_id == cid).order_by(Message.id)
    )).scalars().all()
    assert [(m.role, m.content) for m in msgs] == [("USER", "多个 token 后取消的敏感问题")]


async def test_response_start_failure_releases_existing_cid_guard(
    redis_client, vector_recall, chunker
):
    coord = _coord(redis_client, vector_recall, chunker, _AbortAwareLLM())
    cid = "response-start-failure"
    coord.acquire_turn(cid)
    prepared = PreparedTurn(conversation_id=cid, created=False)
    event_gen = coord.run(prepared, "不会进入 generator")
    response = _ClosingStreamingResponse(
        _format_sse(event_gen),
        event_gen=event_gen,
        on_close=lambda: coord.release_turn(cid),
    )

    async def fail_before_body(_message):
        raise OSError("response start failed")

    with pytest.raises(OSError, match="response start failed"):
        await response.stream_response(fail_before_body)

    assert cid not in TurnCoordinator._active


async def test_model_stream_close_failure_is_sanitized_and_does_not_override_completion(
    redis_client, vector_recall, chunker, caplog
):
    coord = _coord(redis_client, vector_recall, chunker, _CloseFailingLLM())

    with caplog.at_level(logging.WARNING, logger="app.ai.services.turn_coordinator"):
        evs = await _events(coord, None, "close failure question")

    assert isinstance(evs[-1], ChatCompleted)
    assert "model_stream_close_failed" in caplog.text
    assert "sensitive provider close details" not in caplog.text


async def test_completed_frame_send_failure_is_recorded_as_disconnect(
    redis_client, vector_recall, chunker, caplog
):
    coord = _coord(redis_client, vector_recall, chunker, _StreamLLM(["complete"]))
    prepared = await coord.prepare_turn(None, "terminal send failure")
    event_gen = coord.run(prepared, "terminal send failure")
    response = _ClosingStreamingResponse(
        _format_sse(event_gen),
        event_gen=event_gen,
        on_close=lambda: coord.release_turn(prepared.conversation_id),
    )
    cid = ""

    async def fail_on_completed(message):
        nonlocal cid
        if message["type"] != "http.response.body" or not message.get("body"):
            return
        payload = _frame_payload(message["body"].decode())
        cid = payload["conversation_id"]
        if "assistant_message_id" in payload:
            raise OSError("terminal send failed")

    with caplog.at_level(logging.INFO, logger="app.ai.services.turn_coordinator"):
        with pytest.raises(OSError, match="terminal send failed"):
            await response.stream_response(fail_on_completed)

    assert cid not in TurnCoordinator._active
    assert "client_disconnected" in caplog.text


async def test_c1a_demo_typed_sse_degradation_abort_and_concurrency(
    db_session,
    redis_client,
    vector_recall,
    chunker,
):
    """Deterministic C1-A closure output used by the repository-level demo."""
    coordinator = _coord(
        redis_client,
        vector_recall,
        chunker,
        _StreamLLM([" hello", "\n```python\n", "print('ok')\n```"]),
    )
    events = await _events(coordinator, None, "C1-A demo question")
    conversation_id = events[0].conversation_id
    reconstructed = "".join(
        event.delta for event in events if isinstance(event, TokenDelta)
    )
    assert isinstance(events[0], ChatStarted)
    assert isinstance(events[-1], ChatCompleted)
    assert reconstructed == " hello\n```python\nprint('ok')\n```"
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))

    degraded = _coord(
        _FailingRedis(redis_client),
        vector_recall,
        chunker,
        _StreamLLM(["degraded reply"]),
    )
    degraded_events = await _events(degraded, None, "C1-A Redis degradation")
    degraded_cid = degraded_events[0].conversation_id
    degraded_roles = list(
        (
            await db_session.scalars(
                select(Message.role)
                .where(Message.conversation_id == degraded_cid)
                .order_by(Message.id)
            )
        ).all()
    )
    warning_codes = [
        event.code
        for event in degraded_events
        if isinstance(event, (ContextWarning, PostTurnWarning))
    ]
    assert degraded_roles == ["USER", "ASSISTANT"]
    assert "REDIS_UNAVAILABLE" in warning_codes
    assert isinstance(degraded_events[-1], ChatCompleted)

    abort_llm = _AbortAwareLLM(["partial", " answer"])
    abort_coordinator = _coord(redis_client, vector_recall, chunker, abort_llm)
    prepared = await abort_coordinator.prepare_turn(None, "C1-A abort demo")
    event_generator = abort_coordinator.run(prepared, "C1-A abort demo")
    response = _ClosingStreamingResponse(
        _format_sse(event_generator),
        event_gen=event_generator,
        on_close=lambda: abort_coordinator.release_turn(prepared.conversation_id),
    )
    sent_payloads: list[dict] = []

    async def disconnect_after_tokens(message):
        if message["type"] != "http.response.body" or not message.get("body"):
            return
        sent_payloads.append(_frame_payload(message["body"].decode()))
        if len(sent_payloads) == 3:
            raise OSError("demo client disconnected")

    with pytest.raises(OSError, match="demo client disconnected"):
        await response.stream_response(disconnect_after_tokens)
    abort_cid = sent_payloads[0]["conversation_id"]
    abort_roles = list(
        (
            await db_session.scalars(
                select(Message.role)
                .where(Message.conversation_id == abort_cid)
                .order_by(Message.id)
            )
        ).all()
    )
    assert abort_roles == ["USER"]
    assert abort_llm.abort_observed is True
    assert abort_cid not in TurnCoordinator._active

    coordinator.acquire_turn(conversation_id)
    try:
        with pytest.raises(ChatTurnInProgress):
            coordinator.acquire_turn(conversation_id)
    finally:
        coordinator.release_turn(conversation_id)

    parallel_one, parallel_two = await asyncio.gather(
        coordinator.prepare_turn(None, "parallel-one"),
        coordinator.prepare_turn(None, "parallel-two"),
    )
    try:
        assert parallel_one.conversation_id != parallel_two.conversation_id
    finally:
        coordinator.release_turn(parallel_one.conversation_id)
        coordinator.release_turn(parallel_two.conversation_id)

    print(
        "[C1-A DEMO]",
        f"conversation_id={conversation_id}",
        f"events={[type(event).__name__ for event in events]}",
        f"reconstructed={reconstructed!r}",
        f"redis_warning_codes={warning_codes}",
        f"redis_pg_roles={degraded_roles}",
        f"abort_pg_roles={abort_roles}",
        "abort_guard_released=true",
        "same_cid_conflict=CHAT_TURN_IN_PROGRESS",
        "parallel_new_cids_distinct=true",
    )


# ---------- C6: context.references + reasoning.delta ----------


async def test_c6_stream_emits_context_references(
    redis_client, vector_recall, chunker
):
    """C6: 检索后下发 context.references（mock 无数据 -> 空列表，事件仍按协议出现）。"""
    coord = _coord(redis_client, vector_recall, chunker, _StreamLLM(["answer"]))
    events = await _events(coord, None, "参考来源测试")
    refs = [e for e in events if isinstance(e, ContextReferences)]
    assert len(refs) == 1
    assert refs[0].knowledge_refs == []
    assert refs[0].memory_refs == []
    # 事件顺序：started -> references -> token.delta -> completed
    types = [type(e).__name__ for e in events]
    assert types[0] == "ChatStarted"
    assert types[1] == "ContextReferences"
    assert "TokenDelta" in types


async def test_c6_stream_captures_reasoning_as_reasoning_delta(
    redis_client, vector_recall, chunker
):
    """C6: reasoning_content 分流 reasoning.delta，content 分流 token.delta。"""
    llm = _ReasoningLLM(["思考中", "继续思考"], ["answer"])
    coord = _coord(redis_client, vector_recall, chunker, llm)
    events = await _events(coord, None, "思考测试")
    reasoning = [e for e in events if isinstance(e, ReasoningDelta)]
    deltas = [e for e in events if isinstance(e, TokenDelta)]
    assert "".join(e.delta for e in reasoning) == "思考中继续思考"
    assert "".join(e.delta for e in deltas) == "answer"
    # reasoning 在 token 之前
    assert next(
        i for i, e in enumerate(events) if isinstance(e, ReasoningDelta)
    ) < next(i for i, e in enumerate(events) if isinstance(e, TokenDelta))
