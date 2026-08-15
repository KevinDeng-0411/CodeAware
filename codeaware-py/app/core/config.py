"""应用配置 - pydantic-settings，对应 Java application.yml。

字段一一对应 application.yml:64-101，从 .env 读取。LLM_API_KEY 默认空串，
P0 骨架无 LLM 调用可空启动；P2 起未配置将调用失败（明确报错）。
"""

from dataclasses import dataclass
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class LLMPricing:
    """DeepSeek 单价（元 / 百万 token）。估算用，非账单精确值。

    TODO: 实施时按实际 DeepSeek 价格填写（当前占位）。价格变化只改这里。
    """

    input_per_1m: float = 1.0
    output_per_1m: float = 2.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__", extra="ignore")

    # Web
    app_name: str = "codeaware"

    # PostgreSQL + pgvector（Python 版默认独立库 ai_center_py，与 Java ai_center 共存）
    pg_host: str = "localhost"
    pg_port: int = 5433
    pg_user: str = "aicenter"
    pg_password: str = "aicenter123"
    pg_db: str = "ai_center_py"

    # Redis（对应 application.yml:24-33）
    redis_host: str = "localhost"
    redis_port: int = 6380
    redis_db: int = 0

    # LLM: DeepSeek（OpenAI 兼容，对应 application.yml:64-71）
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-v4-flash"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 4096

    # Embedding: Ollama bge-m3（对应 application.yml:73-76）
    ollama_base_url: str = "http://localhost:11434"
    ollama_embedding_model: str = "bge-m3"

    # 记忆（对应 application.yml:86-93）
    mem_window_size: int = Field(default=20, gt=0)
    mem_summary_threshold: int = Field(default=10, gt=0)
    mem_summary_interval: int = Field(default=5, gt=0)
    mem_summary_batch_size: int = Field(default=20, gt=0)
    mem_summary_max_chars: int = Field(default=12000, gt=0)

    # Knowledge 文件上传（C1-C：请求内有界解析，不启用异步索引 Worker）
    knowledge_upload_max_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    knowledge_parsed_max_chars: int = Field(default=200_000, gt=0)

    # AIReadMe 本地只读项目快照（C1-E：默认关闭且无隐式宿主目录）
    ai_readme_snapshot_enabled: bool = False
    local_project_roots: list[Path] = Field(default_factory=list)
    ai_readme_snapshot_max_files: int = Field(default=200, gt=0)
    ai_readme_snapshot_max_file_bytes: int = Field(default=262_144, gt=0)
    ai_readme_snapshot_max_total_bytes: int = Field(default=2_097_152, gt=0)
    ai_readme_snapshot_max_prompt_chars: int = Field(default=60_000, gt=0)
    ai_readme_snapshot_timeout_seconds: float = Field(default=5.0, gt=0)

    # RAG 词法检索后端（C4-B：pg_trgm 回退 / bm25 ParadeDB pg_search 默认目标）
    rag_lexical_backend: str = "bm25"  # 默认 BM25（ParadeDB pg_search），改 "pg_trgm" 回退

    # RAG 运行时（LangGraph 检索增强）：graph=智能路由+自我纠错 / service=原路径回退
    rag_runtime: str = "graph"  # 出问题改 "service" 一键回退

    # Chat 模式（ADR-0016）：rag=确定性 RAG 状态机（默认） / agent=ReAct 工具循环
    chat_mode: str = "rag"  # 出问题改 "rag" 一键回退（agent 模式动 SSE 协议，需前端同步）

    # Reranker（检索后语义精排，ADR-0009 重新评估引入）：Ollama bge-reranker-v2-m3
    reranker_enabled: bool = True  # 出问题改 False 一键关闭，回退纯 RRF
    reranker_model: str = "bge-reranker-v2-m3"
    reranker_top_n: int = 20  # 粗排候选池大小（多路召回 chunk 数，可调 20-30）

    # LLMOps（ADR-0017）
    guardrails_enabled: bool = True  # 请求边界注入检测（fail-closed 拒绝可疑查询）
    agent_trace_include_reasoning: bool = False  # True=thought 完整 reasoning 保留进 trace（默认只存元数据）

    # Reflection（ADR-0018）：agent 生成后自评，不达标注入 feedback 再生成。
    # 默认 True = agent 模式启用（RAG 模式无 agent 循环，不受影响）；False 为 kill-switch 关闭。
    agent_reflection_enabled: bool = True
    agent_max_reflections: int = 1

    # 认证（团队化升级阶段 A：JWT access token，实验室内部使用）
    jwt_secret_key: str = ""  # 启动时校验非空（fail-closed）；测试由 fixture 注入
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 168  # 7 天

    # Celery 异步任务队列
    celery_broker_url: str = ""
    celery_result_backend: str = ""

    # Kafka 事件流
    kafka_bootstrap_servers: str = "localhost:9093"
    kafka_topic_prefix: str = "codeaware."

    @property
    def pg_url_async(self) -> str:
        return (
            f"postgresql+asyncpg://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def celery_broker(self) -> str:
        return self.celery_broker_url or f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def celery_backend(self) -> str:
        return self.celery_result_backend or f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


settings = Settings()
