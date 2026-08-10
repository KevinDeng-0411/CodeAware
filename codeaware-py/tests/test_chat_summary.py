"""C1-B：真实 Chat 路径上的增量摘要、水位线与降级闭环。"""

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.memory.short_term import (
    MessageEntry,
    ShortTermMemoryManager,
    SummaryWork,
)
from app.ai.rag.query_rewriter import QueryRewriter
from app.ai.services.turn_coordinator import TurnCoordinator
from app.api.v1.deps import get_turn_coordinator
from app.core.config import Settings
from app.db.session import AsyncSessionLocal
from app.main import app
from app.models import Conversation, Message, PromptTemplate


@pytest.fixture(autouse=True)
async def _active_chat_template(setup_db):
    """create_all 不执行 seed migration；本模块显式提供 CHAT 模板。"""
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(PromptTemplate).where(
                PromptTemplate.type == "CHAT",
                PromptTemplate.is_active.is_(True),
            )
        )
        session.add(
            PromptTemplate(
                type="CHAT",
                version=100,
                name="C1-B test chat",
                role_setting="测试助手",
                template_body=(
                    "{{long_term_memory}}\n{{rag_context}}\n"
                    "{{conversation_history}}\n{{user_message}}"
                ),
                is_active=True,
            )
        )
        await session.commit()
    yield
    app.dependency_overrides.pop(get_turn_coordinator, None)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _Chunk:
    def __init__(self, content: str) -> None:
        self.content = content


class _SummaryLLM:
    """区分 Chat stream、查询改写和摘要调用的确定性 fake。"""

    def __init__(self, *, summary_result: str = "C1-B 摘要", summary_raises: bool = False):
        self.summary_result = summary_result
        self.summary_raises = summary_raises
        self.summary_prompts: list[str] = []
        self.chat_prompts: list[str] = []

    @property
    def summary_calls(self) -> int:
        return len(self.summary_prompts)

    async def ainvoke(self, prompt, **_kwargs):
        if "## 新增对话" in prompt:
            self.summary_prompts.append(prompt)
            if self.summary_raises:
                raise RuntimeError("sensitive summary provider failure")
            return _Response(self.summary_result)
        return _Response('["测试查询"]')

    async def astream(self, prompt, **_kwargs):
        self.chat_prompts.append(prompt)
        yield _Chunk("assistant-reply")


class _SummarySetFailingRedis:
    def __init__(self, real):
        self._real = real

    async def set(self, *_args, **_kwargs):
        raise RuntimeError("sensitive summary cache endpoint")

    def __getattr__(self, name):
        return getattr(self._real, name)


class _UnavailableRedis:
    def __init__(self, real):
        self._real = real

    async def get(self, *_args, **_kwargs):
        raise RuntimeError("redis unavailable")

    async def lrange(self, *_args, **_kwargs):
        raise RuntimeError("redis unavailable")

    async def delete(self, *_args, **_kwargs):
        raise RuntimeError("redis unavailable")

    async def rpush(self, *_args, **_kwargs):
        raise RuntimeError("redis unavailable")

    async def ltrim(self, *_args, **_kwargs):
        raise RuntimeError("redis unavailable")

    async def expire(self, *_args, **_kwargs):
        raise RuntimeError("redis unavailable")

    async def set(self, *_args, **_kwargs):
        raise RuntimeError("redis unavailable")

    def __getattr__(self, name):
        return getattr(self._real, name)


def _coordinator(redis_client, vector_recall, chunker, llm):
    coordinator = TurnCoordinator(
        llm,
        redis_client,
        vector_recall,
        chunker,
        QueryRewriter(llm),
    )

    async def skip_extraction(_cid, _warnings):
        return None

    coordinator._post_turn_extraction = skip_extraction
    return coordinator


async def _seed_conversation(
    cid: str,
    count: int,
    *,
    summary: str | None = None,
    watermark: int = 0,
    user_id: int | None = None,
) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            Conversation(
                conversation_id=cid,
                title=cid,
                summary=summary,
                summary_message_count=watermark,
                user_id=user_id,  # P0-5：登录用户访问需归属匹配（否则 404）
            )
        )
        for index in range(count):
            session.add(
                Message(
                    conversation_id=cid,
                    role="USER" if index % 2 == 0 else "ASSISTANT",
                    content=f"message-{index:02d}",
                    token_count=2,
                )
            )
        await session.commit()


async def _state(cid: str) -> tuple[str | None, int, int]:
    async with AsyncSessionLocal() as session:
        conversation = (
            await session.execute(
                select(Conversation).where(Conversation.conversation_id == cid)
            )
        ).scalar_one()
        message_count = len(
            (
                await session.execute(
                    select(Message.id).where(Message.conversation_id == cid)
                )
            ).all()
        )
        return (
            conversation.summary,
            conversation.summary_message_count,
            message_count,
        )


def _sse_events(body: str) -> list[tuple[str, dict]]:
    events = []
    for frame in body.strip().split("\n\n"):
        lines = frame.splitlines()
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        events.append((event, data))
    return events


async def test_c1b_demo_threshold_idempotency_and_prompt_use(
    client, redis_client, vector_recall, chunker
):
    llm = _SummaryLLM()
    coordinator = _coordinator(redis_client, vector_recall, chunker, llm)
    app.dependency_overrides[get_turn_coordinator] = lambda: coordinator

    cid = None
    for turn in range(1, 6):
        response = await client.post(
            "/api/chat/send",
            json={"conversation_id": cid, "message": f"用户问题-{turn}"},
        )
        assert response.status_code == 200
        cid = response.json()["data"]["conversation_id"]
        assert llm.summary_calls == (1 if turn == 5 else 0)

    summary, watermark, message_count = await _state(cid)
    assert (summary, watermark, message_count) == ("C1-B 摘要", 10, 10)
    assert await redis_client.get(f"summary:{cid}") == summary

    conversations = await client.get("/api/chat/conversations")
    listed = next(
        item for item in conversations.json()["data"] if item["conversation_id"] == cid
    )
    assert listed["summary"] == summary

    # 同一消息数重复决策不调用 LLM，也不改写摘要。
    warnings = []
    await coordinator._post_turn_summary(cid, warnings)
    assert warnings == []
    assert llm.summary_calls == 1

    next_message = "唯一当前问题-C1B"
    response = await client.post(
        "/api/chat/send",
        json={"conversation_id": cid, "message": next_message},
    )
    assert response.status_code == 200
    assert "## 历史对话摘要\nC1-B 摘要" in llm.chat_prompts[-1]
    assert "## 最近对话" in llm.chat_prompts[-1]
    assert llm.chat_prompts[-1].count(next_message) == 1

    print(
        "C1-B demo:",
        f"conversation_id={cid}",
        "message_count=10",
        "summary_message_count=10",
        "pg_redis_summary_equal=true",
        "next_prompt_contains_summary=true",
    )


async def test_c1b_demo_stream_summary_cache_failure_warns_and_completes(
    client, redis_client, vector_recall, chunker, default_user
):
    cid = "c1b-stream-cache-warning"
    await _seed_conversation(cid, 8, user_id=default_user.id)
    llm = _SummaryLLM(summary_result="PG survives Redis")
    coordinator = _coordinator(
        _SummarySetFailingRedis(redis_client),
        vector_recall,
        chunker,
        llm,
    )
    app.dependency_overrides[get_turn_coordinator] = lambda: coordinator

    response = await client.post(
        "/api/chat/send/stream",
        json={"conversation_id": cid, "message": "触发流式摘要"},
    )
    assert response.status_code == 200
    events = _sse_events(response.text)
    assert events[-1][0] == "chat.completed"
    assert any(
        name == "post_turn.warning"
        and data["component"] == "summary_cache"
        and data["code"] == "REDIS_UNAVAILABLE"
        for name, data in events
    )
    assert await _state(cid) == ("PG survives Redis", 10, 10)
    assert await redis_client.get(f"summary:{cid}") is None

    print(
        "C1-B Redis degradation demo:",
        "warning=summary_cache/REDIS_UNAVAILABLE",
        "pg_summary_retained=true",
        "terminal=chat.completed",
    )


async def test_interval_and_oldest_backlog_are_decided_from_pg(
    setup_db, redis_client, mock_llm, vector_recall, chunker
):
    cid = "c1b-interval"
    await _seed_conversation(cid, 14, summary="old", watermark=10)
    async with AsyncSessionLocal() as session:
        manager = ShortTermMemoryManager(redis_client, session, mock_llm)
        assert (
            await manager.read_summary_work(
                cid, threshold=10, interval=5, batch_size=20
            )
            is None
        )
        session.add(
            Message(
                conversation_id=cid,
                role="USER",
                content="message-14",
                token_count=2,
            )
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        manager = ShortTermMemoryManager(redis_client, session, mock_llm)
        work = await manager.read_summary_work(
            cid, threshold=10, interval=5, batch_size=20
        )
    assert work is not None
    assert [message.content for message in work.messages] == [
        "message-10",
        "message-11",
        "message-12",
        "message-13",
        "message-14",
    ]

    backlog_cid = "c1b-backlog"
    await _seed_conversation(backlog_cid, 45)
    await redis_client.rpush(
        f"msgs:{backlog_cid}", *[f"USER:::cache-{i}" for i in range(20)]
    )
    async with AsyncSessionLocal() as session:
        manager = ShortTermMemoryManager(redis_client, session, mock_llm)
        backlog = await manager.read_summary_work(
            backlog_cid, threshold=10, interval=5, batch_size=20
        )
    assert backlog is not None
    assert len(backlog.messages) == 20
    assert backlog.messages[0].content == "message-00"
    assert backlog.messages[-1].content == "message-19"

    llm = _SummaryLLM(summary_result="backlog summary")
    coordinator = _coordinator(redis_client, vector_recall, chunker, llm)
    warnings = []
    await coordinator._post_turn_summary(backlog_cid, warnings)
    assert warnings == []
    assert await _state(backlog_cid) == ("backlog summary", 20, 45)
    assert "message-00" in llm.summary_prompts[0]
    assert "message-19" in llm.summary_prompts[0]
    assert "message-20" not in llm.summary_prompts[0]

    await coordinator._post_turn_summary(backlog_cid, warnings)
    assert warnings == []
    assert await _state(backlog_cid) == ("backlog summary", 40, 45)
    assert "message-20" in llm.summary_prompts[1]
    assert "message-39" in llm.summary_prompts[1]
    assert "message-40" not in llm.summary_prompts[1]


def test_summary_prompt_is_bounded_ordered_and_marks_truncation():
    work = SummaryWork(
        existing_summary="摘要开头-" + "旧" * 1000 + "-摘要结尾",
        expected_watermark=0,
        total_count=3,
        messages=(
            MessageEntry("USER", "first-message"),
            MessageEntry("ASSISTANT", "长" * 1000),
            MessageEntry("USER", "must-not-be-skipped"),
        ),
    )
    result = ShortTermMemoryManager.build_summary_prompt(work, max_chars=500)
    assert result is not None
    assert len(result.text) <= 500
    assert "摘要开头-" in result.text
    assert "-摘要结尾" in result.text
    assert "…[中间内容已截断]…" in result.text
    assert result.text.index("first-message") < result.text.index("ASSISTANT:")
    assert "…[内容已截断]" in result.text
    assert "must-not-be-skipped" not in result.text
    assert result.included_message_count == 2


async def test_conditional_update_rejects_stale_watermark(
    setup_db, redis_client, mock_llm
):
    cid = "c1b-stale-watermark"
    await _seed_conversation(cid, 10, summary="before", watermark=0)
    async with AsyncSessionLocal() as winner_session:
        winner = ShortTermMemoryManager(redis_client, winner_session, mock_llm)
        assert await winner.conditional_write_summary(
            cid,
            "winner",
            expected_watermark=0,
            target_watermark=10,
        )
        await winner_session.commit()

    async with AsyncSessionLocal() as stale_session:
        stale = ShortTermMemoryManager(redis_client, stale_session, mock_llm)
        assert not await stale.conditional_write_summary(
            cid,
            "stale",
            expected_watermark=0,
            target_watermark=10,
        )
        await stale_session.commit()
    assert await _state(cid) == ("winner", 10, 10)


@pytest.mark.parametrize(
    ("summary_result", "summary_raises"),
    [("", False), ("unused", True)],
)
async def test_summary_llm_failure_or_blank_warns_without_advancing(
    setup_db,
    redis_client,
    vector_recall,
    chunker,
    summary_result,
    summary_raises,
):
    cid = f"c1b-summary-failure-{summary_raises}"
    await _seed_conversation(cid, 10)
    llm = _SummaryLLM(
        summary_result=summary_result,
        summary_raises=summary_raises,
    )
    coordinator = _coordinator(redis_client, vector_recall, chunker, llm)
    warnings = []
    await coordinator._post_turn_summary(cid, warnings)
    assert [(warning["component"], warning["code"]) for warning in warnings] == [
        ("summary", "SUMMARY_FAILED")
    ]
    assert await _state(cid) == (None, 0, 10)
    assert await redis_client.get(f"summary:{cid}") is None


async def test_summary_pg_commit_failure_does_not_refresh_redis(
    setup_db,
    redis_client,
    vector_recall,
    chunker,
    monkeypatch,
):
    cid = "c1b-summary-commit-failure"
    await _seed_conversation(cid, 10, summary="old-pg")
    await redis_client.set(f"summary:{cid}", "old-cache")
    coordinator = _coordinator(
        redis_client,
        vector_recall,
        chunker,
        _SummaryLLM(summary_result="must-roll-back"),
    )

    async def fail_commit(_session):
        raise RuntimeError("sensitive pg commit failure")

    warnings = []
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", fail_commit)
        await coordinator._post_turn_summary(cid, warnings)

    assert [(warning["component"], warning["code"]) for warning in warnings] == [
        ("summary", "SUMMARY_FAILED")
    ]
    assert await _state(cid) == ("old-pg", 0, 10)
    assert await redis_client.get(f"summary:{cid}") == "old-cache"


async def test_redis_unavailable_from_turn_start_keeps_pg_summary(
    client, redis_client, vector_recall, chunker, default_user
):
    cid = "c1b-redis-down"
    await _seed_conversation(cid, 8, user_id=default_user.id)
    coordinator = _coordinator(
        _UnavailableRedis(redis_client),
        vector_recall,
        chunker,
        _SummaryLLM(summary_result="PG truth"),
    )
    app.dependency_overrides[get_turn_coordinator] = lambda: coordinator

    response = await client.post(
        "/api/chat/send",
        json={"conversation_id": cid, "message": "Redis 全程不可用"},
    )
    assert response.status_code == 200
    warnings = response.json()["data"]["warnings"]
    assert any(w["component"] == "summary_cache" for w in warnings)
    assert any(w["component"] == "message_cache" for w in warnings)
    assert await _state(cid) == ("PG truth", 10, 10)


async def test_p0_5_login_user_cannot_access_orphan_conversation(client, default_user):
    """P0-5: 登录用户不能访问无主会话（user_id IS NULL）——404（此前对所有用户可见）。"""
    cid = "orphan-conv-p0-5"
    async with AsyncSessionLocal() as session:
        session.add(Conversation(conversation_id=cid, title=cid))
        await session.commit()

    response = await client.post(
        "/api/chat/send/stream",
        json={"conversation_id": cid, "message": "hi"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "field",
    [
        "mem_window_size",
        "mem_summary_threshold",
        "mem_summary_interval",
        "mem_summary_batch_size",
        "mem_summary_max_chars",
    ],
)
def test_memory_configuration_requires_positive_values(field):
    with pytest.raises(ValidationError):
        Settings(**{field: 0})
