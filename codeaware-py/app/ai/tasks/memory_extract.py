"""Post-turn 记忆抽取异步任务。"""
import logging
import asyncio
from app.ai.celery_app import celery_app
from app.ai.infra.vector_recall import VectorRecallService
from app.ai.tasks.base import CodeAwareTask
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


def _emit_extraction_metrics(conversation_id: str, count: int, reason: str = "") -> None:
    """Memory-Ops（ADR-0017）：抽取计数（含 count=0 的早退原因）。best-effort。"""
    try:
        from app.ai.events.producer import emit_memory_metrics

        emit_memory_metrics(
            event_type="extraction", conversation_id=conversation_id,
            count=count, memory_type=reason,
        )
    except Exception:  # noqa: BLE001
        logger.warning("memory extraction metric emit failed conversation_id=%s", conversation_id)


@celery_app.task(bind=True, base=CodeAwareTask, name="memory.extract", max_retries=2)
def extract_memory_task(self, conversation_id: str, message_count: int) -> dict:
    async def _run():
        from app.ai.config import get_embedding_model, get_chat_model
        from app.ai.memory.long_term import LongTermMemoryManager

        vector_recall = VectorRecallService(get_embedding_model())
        chat_model = get_chat_model()

        async with AsyncSessionLocal() as session:
            lt = LongTermMemoryManager(session, vector_recall)
            has_mem = await lt.has_memories(conversation_id)
            if has_mem:
                _emit_extraction_metrics(conversation_id, 0, "already_has_memories")
                return {"conversation_id": conversation_id, "facts_count": 0,
                        "reason": "already_has_memories"}

            messages = await lt.read_recent_messages(conversation_id)
            if len(messages) < message_count:
                _emit_extraction_metrics(conversation_id, 0, "insufficient_messages")
                return {"conversation_id": conversation_id, "facts_count": 0,
                        "reason": f"insufficient_messages ({len(messages)} < {message_count})"}

            tuples = [(m[0], m[1]) for m in messages]
            facts = await lt.extract_facts_text(tuples, chat_model)
            if not facts:
                _emit_extraction_metrics(conversation_id, 0, "no_facts_extracted")
                return {"conversation_id": conversation_id, "facts_count": 0,
                        "reason": "no_facts_extracted"}

            prepared = await lt.prepare_facts(facts)

        async with AsyncSessionLocal() as s2:
            lt2 = LongTermMemoryManager(s2, vector_recall)
            await lt2.save_prepared_facts(conversation_id, prepared)
            await s2.commit()

        _emit_extraction_metrics(conversation_id, len(prepared), "success")
        return {"conversation_id": conversation_id, "facts_count": len(prepared)}

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()
