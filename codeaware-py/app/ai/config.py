"""AI 基建工厂（ADR-0001）：LLM/Embedding 单例 + 共享 VectorRecallService。

- LLM: DeepSeek（OpenAI 兼容 API）
- Embedding: Ollama bge-m3（本地，1024 维）
- VectorRecallService: Memory/Knowledge 共用，消除 Java 版两处复制的 embed+store+recall
"""

from functools import lru_cache

from langchain_deepseek import ChatDeepSeek
from langchain_ollama import OllamaEmbeddings

from app.ai.infra.vector_recall import VectorRecallService
from app.ai.rag.reranker import CrossEncoderReranker, RerankerPort
from app.core.config import settings


def _build_chat_model(extra_body) -> ChatDeepSeek:
    return ChatDeepSeek(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=120,
        extra_body=extra_body,
    )


@lru_cache
def get_chat_model() -> ChatDeepSeek:
    """LLM: DeepSeek（ChatDeepSeek 提取 reasoning_content，供 C6 思考过程展示）。

    切 ChatDeepSeek 而非 ChatOpenAI：ChatOpenAI 官方不提取第三方 provider 的
    reasoning_content（langchain-openai 文档明示）。C6 需流式捕获 reasoning。

    注意：此函数被 FastAPI 用作 Depends()，**不能加参数**（会被当成请求 query
    参数暴露进 OpenAPI）。非 thinking 模型见 get_reflection_model()。
    """
    return _build_chat_model(None)


@lru_cache
def get_reflection_model() -> ChatDeepSeek:
    """非 thinking 模型（extra_body thinking disabled）：Reflection 结构化输出用。

    thinking 下 function_calling 不可用（见 deepseek-notes.md），故反射评估用
    独立非 thinking 实例。独立函数而非给 get_chat_model 加参数：后者被 FastAPI
    Depends 引用，加参数会污染 OpenAPI。
    """
    return _build_chat_model({"thinking": {"type": "disabled"}})


@lru_cache
def get_embedding_model() -> OllamaEmbeddings:
    """Embedding: Ollama bge-m3（本地，1024 维）。"""
    return OllamaEmbeddings(
        base_url=settings.ollama_base_url,
        model=settings.ollama_embedding_model,
    )


@lru_cache
def get_vector_recall_service() -> VectorRecallService:
    """共享向量召回服务（Memory/Knowledge 共用，ADR-0001）。"""
    return VectorRecallService(get_embedding_model())


@lru_cache
def get_reranker() -> RerankerPort | None:
    """bge-reranker-v2-m3 cross-encoder（ONNX Runtime，ADR-0009 重新评估）。

    配置 reranker_enabled=False 时返回 None（纯 RRF 回退）。
    ONNX 模型在 models/bge-reranker-v2-m3/ 下（本地推理，无 torch/Ollama 依赖）。
    """
    if not settings.reranker_enabled:
        return None
    return CrossEncoderReranker()
