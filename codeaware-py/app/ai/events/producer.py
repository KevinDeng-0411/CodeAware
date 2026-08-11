"""Kafka Producer 单例 — 非阻塞 fire-and-forget。

Producer 在应用启动时惰性初始化，初始化失败不阻塞应用启动。
所有 send 调用都是异步 fire-and-forget，失败只记日志不抛异常。
"""

import json
import logging
import os
import uuid
from typing import Any

from kafka import KafkaProducer

from app.core.config import settings

logger = logging.getLogger(__name__)

_INIT_SENTINEL = object()
_producer: KafkaProducer | object = _INIT_SENTINEL


def get_producer() -> KafkaProducer | None:
    global _producer
    if _producer is _INIT_SENTINEL:
        _producer = _init_producer()
    return _producer if _producer is not None else None


def _json_serialize(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _str_serialize(value: str | None) -> bytes | None:
    return value.encode("utf-8") if value else None


def _init_producer() -> KafkaProducer | None:
    # 测试环境跳过 Kafka 连接
    import os
    if os.environ.get("CODEAWARE_TESTING") == "1":
        return None
    try:
        producer = KafkaProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            value_serializer=_json_serialize,
            key_serializer=_str_serialize,
            acks="all",
            retries=3,
            enable_idempotence=True,
            max_in_flight_requests_per_connection=5,
            request_timeout_ms=3000,
        )
        logger.info("Kafka producer initialized servers=%s", settings.kafka_bootstrap_servers)
        return producer
    except Exception as exc:
        logger.warning("Kafka producer init failed (non-blocking): %s", exc)
        return None


def emit_event(topic: str, key: str | None, data: dict[str, Any]) -> None:
    producer = get_producer()
    if producer is None:
        return
    try:
        future = producer.send(
            f"{settings.kafka_topic_prefix}{topic}",
            key=key,
            value=data,
        )
        future.add_errback(
            lambda e: logger.warning("Kafka send failed topic=%s error=%s", topic, e)
        )
    except Exception as exc:
        logger.warning("Kafka emit failed topic=%s error=%s", topic, exc)


def emit_document_event(action: str, document_id: int, title: str,
                        user_id: str | None = None, **kwargs) -> None:
    from app.ai.events.schemas import DocumentAuditEvent
    event = DocumentAuditEvent(
        event_id=uuid.uuid4().hex, action=action, document_id=document_id,
        title=title, user_id=user_id, **kwargs,
    )
    emit_event("audit.document", key=str(document_id), data=event.model_dump())


def emit_retrieval_metrics(**kwargs) -> None:
    from app.ai.events.schemas import RetrievalMetricsEvent
    event = RetrievalMetricsEvent(event_id=uuid.uuid4().hex, **kwargs)
    emit_event("metrics.retrieval", key=None, data=event.model_dump())


def emit_error_event(component: str, code: str, message: str, **kwargs) -> None:
    from app.ai.events.schemas import ErrorEvent
    event = ErrorEvent(
        event_id=uuid.uuid4().hex, component=component, code=code,
        message=message, details=kwargs or None,
    )
    emit_event("ops.error", key=code, data=event.model_dump())


def emit_conversation_audit(action: str, conversation_id: str, turn_id: str,
                            user_id: str | None = None, **kwargs) -> None:
    from app.ai.events.schemas import ConversationAuditEvent
    event = ConversationAuditEvent(
        event_id=uuid.uuid4().hex, action=action,
        conversation_id=conversation_id, turn_id=turn_id,
        user_id=user_id, **kwargs,
    )
    emit_event("audit.conversation", key=conversation_id, data=event.model_dump())


def emit_task_metrics(task_id: str, task_name: str, status: str, **kwargs) -> None:
    from app.ai.events.schemas import TaskMetricsEvent
    event = TaskMetricsEvent(
        event_id=uuid.uuid4().hex, task_id=task_id,
        task_name=task_name, status=status, **kwargs,
    )
    emit_event("metrics.task", key=task_id, data=event.model_dump())


def emit_memory_metrics(event_type: str, conversation_id: str,
                        count: int, memory_type: str = "") -> None:
    """Memory-Ops（ADR-0017）：记忆抽取/召回计数。fire-and-forget，无 producer 时静默。"""
    from app.ai.events.schemas import MemoryMetricsEvent
    event = MemoryMetricsEvent(
        event_id=uuid.uuid4().hex, event_type=event_type,
        conversation_id=conversation_id, count=count, memory_type=memory_type,
    )
    emit_event("metrics.memory", key=conversation_id, data=event.model_dump())
