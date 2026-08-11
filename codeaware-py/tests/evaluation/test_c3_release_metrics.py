"""C3-B deterministic Chat and retrieval baselines.

These are repeatable comparison samples, not production load tests. Real-provider
connectivity and cost remain covered by the committed C2 live-smoke evidence.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select

from app.ai.infra.vector_recall import VectorRecallService
from app.ai.services.turn_coordinator import PreparedTurn
from app.api.v1.deps import get_turn_coordinator
from app.main import app
from app.models import Document, KnowledgeChunk
from app.schemas.chat_events import ChatCompleted, ChatStarted, TokenDelta


CASE_COUNT = 30
TOP_K = 5
RANK_LIMIT = 10


@dataclass(frozen=True)
class _GoldenCase:
    case_id: int
    category: str
    query: str
    content: str


def _golden_cases() -> list[_GoldenCase]:
    cases: list[_GoldenCase] = []
    for index in range(10):
        identifier = f"ERR_C3_{index:03d}"
        cases.append(
            _GoldenCase(
                case_id=index,
                category="rare_identifier",
                query=identifier,
                content=(
                    f"C3CASE{index:02d} 故障手册：{identifier} 表示对话缓存刷新失败。"
                    f"定位时先核对 PostgreSQL 真相源，再检查 {identifier} 的 Redis "
                    f"降级告警；恢复后再次确认 {identifier} 不影响消息持久化。"
                ),
            )
        )
    for index in range(10, 20):
        phrase = f"会话摘要水位线策略{index - 10}"
        cases.append(
            _GoldenCase(
                case_id=index,
                category="chinese_compound",
                query=phrase,
                content=(
                    f"C3CASE{index:02d} {phrase}：摘要只处理最旧未摘要消息，"
                    "条件更新成功后再刷新缓存，失败时不得跳过积压。"
                ),
            )
        )
    paraphrases = [
        ("怎样让聊天记录断线后仍能找回", "PostgreSQL 保存消息真相，Redis miss 时回查并重建窗口。"),
        ("两个请求同时问同一个会话怎么办", "同一 conversation 使用进程内 turn guard，冲突返回 409。"),
        ("模型输出到一半断开要保存半截吗", "流取消时只保留已提交的 USER，不持久化 partial assistant。"),
        ("知识文档删除后切片如何处理", "Document 删除通过外键 CASCADE 清理 KnowledgeChunk。"),
        ("摘要模型报错会不会阻断回答", "post-turn 摘要失败转 warning，核心 Chat 仍发送 completed。"),
        ("本地项目说明书如何避免读取密钥", "AIReadMe 使用 allowed root、排除规则和 symlink 拒绝策略。"),
        ("提示词更新如何避免出现两个生效版本", "同类型 Prompt 在事务中切换并由部分唯一索引兜底。"),
        ("向量服务不可用时问答还能继续吗", "RAG 与 memory recall 失败转 context warning 后继续模型回答。"),
        ("为什么模型调用期间不能占着数据库事务", "外部 await 与短事务分离，降低连接占用和锁竞争。"),
        ("怎么确认流式文字没有吞空格换行", "typed SSE 的 token delta 原样 JSON 编码并逐字符校验。"),
    ]
    for offset, (query, answer) in enumerate(paraphrases):
        case_id = 20 + offset
        cases.append(
            _GoldenCase(
                case_id=case_id,
                category="semantic_paraphrase",
                query=query,
                content=f"C3CASE{case_id:02d} 架构说明：{answer}",
            )
        )
    assert len(cases) == CASE_COUNT
    return cases


class _GoldenEmbedder:
    def __init__(self, cases: list[_GoldenCase]) -> None:
        self.query_ids = {case.query: case.case_id for case in cases}

    async def aembed_query(self, text: str) -> list[float]:
        case_id = self.query_ids.get(text)
        if case_id is None:
            marker = re.search(r"C3CASE(\d{2})", text)
            case_id = int(marker.group(1)) if marker else 1023
        vector = [0.0] * 1024
        vector[case_id] = 1.0
        return vector


class _MetricCoordinator:
    """Minimal coordinator used to measure the public SSE serialization path."""

    def __init__(self) -> None:
        self.first_token_ms: list[float] = []
        self._started: dict[str, float] = {}

    async def prepare_turn(
        self, conversation_id: str | None, _message: str, user_id: int | None = None, mode: str | None = None
    ) -> PreparedTurn:
        cid = conversation_id or f"c3-metric-{uuid.uuid4().hex}"
        self._started[cid] = time.perf_counter()
        return PreparedTurn(conversation_id=cid, created=conversation_id is None)

    async def run(self, prepared: PreparedTurn, _message: str):
        turn_id = uuid.uuid4().hex
        yield ChatStarted(
            conversation_id=prepared.conversation_id,
            turn_id=turn_id,
            sequence=1,
            created=prepared.created,
        )
        self.first_token_ms.append(
            (time.perf_counter() - self._started[prepared.conversation_id]) * 1000
        )
        yield TokenDelta(
            conversation_id=prepared.conversation_id,
            turn_id=turn_id,
            sequence=2,
            delta="C3 metric first ",
        )
        yield TokenDelta(
            conversation_id=prepared.conversation_id,
            turn_id=turn_id,
            sequence=3,
            delta="line\nsecond",
        )
        yield ChatCompleted(
            conversation_id=prepared.conversation_id,
            turn_id=turn_id,
            sequence=4,
            assistant_message_id=1,
            warning_count=0,
        )

    def release_turn(self, _conversation_id: str) -> None:
        return None


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[rank], 3)


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event = ""
        payload: dict | None = None
        event_id: int | None = None
        for line in block.splitlines():
            if line.startswith("id:"):
                event_id = int(line.removeprefix("id:").strip())
            elif line.startswith("event:"):
                event = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                payload = json.loads(line.removeprefix("data:").strip())
        assert payload is not None
        assert event_id == payload["sequence"]
        events.append((event, payload))
    return events


def _ranking_metrics(ranks: list[int | None]) -> dict[str, float]:
    recall = sum(rank is not None and rank <= TOP_K for rank in ranks) / len(ranks)
    reciprocal = sum(
        1.0 / rank if rank is not None and rank <= RANK_LIMIT else 0.0
        for rank in ranks
    ) / len(ranks)
    ndcg = sum(
        1.0 / math.log2(rank + 1)
        if rank is not None and rank <= RANK_LIMIT
        else 0.0
        for rank in ranks
    ) / len(ranks)
    return {
        f"recall@{TOP_K}": round(recall, 4),
        f"mrr@{RANK_LIMIT}": round(reciprocal, 4),
        f"ndcg@{RANK_LIMIT}": round(ndcg, 4),
    }


async def test_c3_release_metrics(client, db_session):
    cases = _golden_cases()
    recall = VectorRecallService(_GoldenEmbedder(cases))
    relevant_ids: dict[int, int] = {}

    for case in cases:
        document = Document(
            title=f"C3 metric {case.case_id}",
            source_type="EVALUATION",
            project_name="c3-release-metrics",
            content=case.content,
        )
        db_session.add(document)
        await db_session.flush()
        chunk = KnowledgeChunk(
            document_id=document.id,
            chunk_index=0,
            chunk_content=case.content,
        )
        await recall.store(db_session, chunk, case.content)
        relevant_ids[case.case_id] = chunk.id
        if case.category == "rare_identifier":
            distractor = KnowledgeChunk(
                document_id=document.id,
                chunk_index=1,
                chunk_content=f"错误码占位符：{case.query}",
            )
            await recall.store(db_session, distractor, "unrelated " + case.query)
    await db_session.flush()

    vector_ranks: list[int | None] = []
    lexical_ranks: list[int | None] = []
    fused_ranks: list[int | None] = []
    corpus_ids = list(
        (
            await db_session.scalars(
                select(KnowledgeChunk.id)
                .join(Document)
                .where(Document.project_name == "c3-release-metrics")
            )
        ).all()
    )
    for case in cases:
        vector_rows = await recall.recall(
            db_session,
            KnowledgeChunk,
            case.query,
            top_k=RANK_LIMIT,
            hybrid=False,
        )
        lexical_rows = list(
            (
                await db_session.scalars(
                    select(KnowledgeChunk)
                    .where(KnowledgeChunk.id.in_(corpus_ids))
                    .where(
                        func.similarity(
                            KnowledgeChunk.chunk_content, case.query
                        )
                        > 0.1
                    )
                    .order_by(
                        func.similarity(
                            KnowledgeChunk.chunk_content, case.query
                        ).desc(),
                        KnowledgeChunk.id.asc(),
                    )
                    .limit(RANK_LIMIT)
                )
            ).all()
        )
        fused_rows = await recall.recall(
            db_session,
            KnowledgeChunk,
            case.query,
            top_k=RANK_LIMIT,
            hybrid=True,
            text_column="chunk_content",
        )
        target = relevant_ids[case.case_id]
        vector_ranks.append(
            next(
                (rank for rank, row in enumerate(vector_rows, 1) if row[0].id == target),
                None,
            )
        )
        lexical_ranks.append(
            next(
                (rank for rank, row in enumerate(lexical_rows, 1) if row.id == target),
                None,
            )
        )
        fused_ranks.append(
            next(
                (rank for rank, row in enumerate(fused_rows, 1) if row[0].id == target),
                None,
            )
        )

    coordinator = _MetricCoordinator()
    app.dependency_overrides[get_turn_coordinator] = lambda: coordinator
    full_response_ms: list[float] = []
    fidelity_results: list[bool] = []
    try:
        for sample in range(20):
            started = time.perf_counter()
            response = await client.post(
                "/api/chat/send/stream",
                json={"message": f"C3 fixed metric sample {sample}"},
            )
            full_response_ms.append((time.perf_counter() - started) * 1000)
            assert response.status_code == 200
            events = _parse_sse(response.text)
            assert [name for name, _ in events] == [
                "chat.started",
                "token.delta",
                "token.delta",
                "chat.completed",
            ]
            rendered = "".join(
                payload["delta"]
                for event, payload in events
                if event == "token.delta"
            )
            fidelity_results.append(rendered == "C3 metric first line\nsecond")
    finally:
        app.dependency_overrides.pop(get_turn_coordinator, None)

    metrics = {
        "sample_type": "deterministic_fake_not_load_test",
        "chat": {
            "samples": len(full_response_ms),
            "first_token_ms": {
                "p50": round(statistics.median(coordinator.first_token_ms), 3),
                "p95": _percentile(coordinator.first_token_ms, 0.95),
            },
            "full_response_ms": {
                "p50": round(statistics.median(full_response_ms), 3),
                "p95": _percentile(full_response_ms, 0.95),
            },
            "sse_fidelity": round(sum(fidelity_results) / len(fidelity_results), 4),
        },
        "retrieval": {
            "cases": len(cases),
            "categories": {
                category: sum(case.category == category for case in cases)
                for category in {
                    "rare_identifier",
                    "chinese_compound",
                    "semantic_paraphrase",
                }
            },
            "pg_trgm": _ranking_metrics(lexical_ranks),
            "vector": _ranking_metrics(vector_ranks),
            "rrf": _ranking_metrics(fused_ranks),
        },
    }
    assert metrics["chat"]["sse_fidelity"] == 1.0
    assert metrics["retrieval"]["vector"][f"recall@{TOP_K}"] == 1.0
    assert metrics["retrieval"]["rrf"][f"recall@{TOP_K}"] == 1.0
    print("[C3 METRICS] " + json.dumps(metrics, ensure_ascii=False, sort_keys=True))
