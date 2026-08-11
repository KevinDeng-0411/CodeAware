#!/usr/bin/env bash
# CodeAware 一键启动：基础服务 + Celery worker + 后端 + 前端 + admin 账号
# 用法：./start.sh   （可重复执行：幂等，已有进程不重启）
# 说明：worker 用 native（uv 环境秒起，免 Docker build）；Kafka 本地开发非必需不启。
set -euo pipefail
cd "$(dirname "$0")"

ROOT="$(pwd)"
PY="$ROOT/codeaware-py"
LOGDIR="$ROOT/.run"
mkdir -p "$LOGDIR"

# 默认后端模式：读 .env 的 CHAT_MODE，缺省 rag
CHAT_MODE="$(grep -E '^CHAT_MODE=' "$PY/.env" 2>/dev/null | cut -d= -f2 || echo rag)"

is_running() { # pidfile
  [ -f "$1" ] && kill -0 "$(cat "$1" 2>/dev/null)" 2>/dev/null
}

# ---- 1. 基础服务（postgres + redis；celery_worker/kafka 用 native/按需） ----
docker compose up -d postgres redis >/dev/null
echo "✓ postgres / redis"

# ---- 2. Alembic 迁移 ----
(cd "$PY" && uv run alembic upgrade head) >/dev/null 2>&1
echo "✓ alembic 迁移"

# ---- 3. Celery worker（native，分块/记忆抽取必需） ----
if ! is_running "$LOGDIR/worker.pid"; then
  nohup bash -c "cd '$PY' && exec uv run celery -A app.ai.celery_app worker \
    --loglevel=warning --concurrency=2" \
    >"$LOGDIR/worker.log" 2>&1 &
  echo $! > "$LOGDIR/worker.pid"
fi
echo "✓ celery worker (pid $(cat "$LOGDIR/worker.pid"))"

# ---- 4. 后端 ----
if ! is_running "$LOGDIR/backend.pid"; then
  nohup bash -c "cd '$PY' && CHAT_MODE='$CHAT_MODE' exec uv run uvicorn app.main:app \
    --host 127.0.0.1 --port 8000" >"$LOGDIR/backend.log" 2>&1 &
  echo $! > "$LOGDIR/backend.pid"
fi
echo "✓ 后端 :8000 (CHAT_MODE=$CHAT_MODE, pid $(cat "$LOGDIR/backend.pid"))"

# ---- 5. 前端 ----
if ! is_running "$LOGDIR/frontend.pid"; then
  nohup bash -c "cd '$PY/frontend' && exec npx vite --host 0.0.0.0 --port 5173" \
    >"$LOGDIR/frontend.log" 2>&1 &
  echo $! > "$LOGDIR/frontend.pid"
fi
echo "✓ 前端 :5173 (pid $(cat "$LOGDIR/frontend.pid"))"

# ---- 6. admin 账号（幂等：存在则重置密码为 admin123） ----
(cd "$PY" && CODEAWARE_TESTING=1 JWT_SECRET_KEY=test uv run python - <<'EOF' >/dev/null 2>&1 || true
import asyncio, os
os.environ.setdefault("RAG_RUNTIME", "service")
async def main():
    from sqlalchemy import select
    from app.core.security import hash_password
    from app.db.session import AsyncSessionLocal
    from app.models import User
    async with AsyncSessionLocal() as s:
        u = await s.scalar(select(User).where(User.username == "admin"))
        if u is None:
            s.add(User(username="admin", password_hash=hash_password("admin123"), role="admin", display_name="Admin"))
        else:
            u.password_hash = hash_password("admin123")
        await s.commit()
asyncio.run(main())
EOF
)
echo "✓ admin 账号 (admin/admin123)"

sleep 3
echo ""
echo "== 已启动 =="
echo "  前端      http://localhost:5173  (admin/admin123)"
echo "  后端      http://localhost:8000"
echo "  健康检查  http://localhost:8000/health/ready  (celery=up 才 ready；worker 缺失为 degraded)"
echo "  日志      $LOGDIR/*.log   （停止：kill \$(cat .run/*.pid)）"
