"""Application liveness and dependency readiness endpoints."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import settings
from app.core.response import Result
from app.db.redis import redis_client
from app.db.session import engine

router = APIRouter(prefix="/health", tags=["系统"])
_READINESS_TIMEOUT_SECONDS = 2.0


async def _check_postgres() -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def _check_redis() -> None:
    await redis_client.ping()


async def _check_ollama() -> None:
    async with httpx.AsyncClient(timeout=_READINESS_TIMEOUT_SECONDS) as client:
        response = await client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
        response.raise_for_status()


async def _check_deepseek() -> None:
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY not configured")
    async with httpx.AsyncClient(timeout=_READINESS_TIMEOUT_SECONDS) as client:
        response = await client.get(
            f"{settings.llm_base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )
        response.raise_for_status()


async def _check_celery() -> None:
    """Celery worker 探活（control.ping 广播）。缺 worker → 异步分块/记忆抽取不可用。

    放 to_thread：celery control 是同步客户端，避免阻塞事件循环。
    ping timeout=1：只要任一 worker pong 即 up（等全部会拖 3s+，超通用 2s 判定）。
    """
    from app.ai.celery_app import celery_app

    def _ping() -> bool:
        replies = celery_app.control.ping(timeout=1)
        return bool(replies)

    ok = await asyncio.to_thread(_ping)
    if not ok:
        raise RuntimeError("no celery worker")


async def _bounded(check, timeout: float = _READINESS_TIMEOUT_SECONDS) -> str:
    try:
        async with asyncio.timeout(timeout):
            await check()
        return "up"
    except Exception:  # noqa: BLE001 - readiness must return a sanitized aggregate
        return "down"


@router.get("/live")
async def liveness() -> Result:
    """Process-only liveness; does not contact dependencies."""
    return Result.ok({"status": "up"})


@router.get("/ready")
async def readiness():
    """Readiness 三态：ready（全 up）/ degraded（主链路 up 但 celery down，异步分块不可用）/ not_ready。

    主链路（PG/Redis/Ollama/DeepSeek）决定同步 Chat/RAG 可用性；celery worker 只影响
    上传分块/记忆抽取等异步任务，缺失时降级而不 fail 主链路。
    """
    postgres, redis, ollama, deepseek, celery = await asyncio.gather(
        _bounded(_check_postgres),
        _bounded(_check_redis),
        _bounded(_check_ollama),
        _bounded(_check_deepseek),
        # celery 广播需等 worker 应答（多 worker 更慢），单独放宽超时
        _bounded(_check_celery, timeout=4.0),
    )
    checks = {
        "postgres": postgres, "redis": redis, "ollama": ollama,
        "deepseek": deepseek, "celery": celery,
    }
    core_ready = all(checks[k] == "up" for k in ("postgres", "redis", "ollama", "deepseek"))
    if core_ready and celery == "up":
        status, code, msg = "ready", 1, "success"
    elif core_ready:
        # 异步链路（worker）不可用：主链路仍可用 → degraded，不 fail 同步请求
        status, code, msg = "degraded", 1, "degraded: celery worker down"
    else:
        status, code, msg = "not_ready", 0, "not ready"
    payload = Result(code=code, msg=msg, data={"status": status, "checks": checks})
    if not core_ready:
        return JSONResponse(status_code=503, content=payload.model_dump())
    return payload
