**English** | [简体中文](README.zh-CN.md)

---

# CodeAware

An AI-driven developer productivity platform designed for **software engineering lab teams** (code review, onboarding new members, team knowledge retrieval).
The core is a **dual-mode Chat** (`CHAT_MODE=rag|agent`): **RAG mode** does hybrid-retrieval Q&A (BM25 + pgvector + ONNX reranker) with cited sources and visible chain-of-thought; **Agent mode** runs a ReAct tool loop — the model autonomously picks tools (knowledge search / document fetch / calc / time) with a visible tool trace and convergence-aware stopping.

![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688)
![React 19](https://img.shields.io/badge/React-19-61DAFB)
![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6)
![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-4169E1)
![Redis 7](https://img.shields.io/badge/Redis-7-DC382D)
![Celery](https://img.shields.io/badge/Celery-async-37814A)
![Kafka](https://img.shields.io/badge/Kafka-event--driven-231F20)
![LangGraph](https://img.shields.io/badge/LangGraph-orchestration-FF6F00)
![embedding](https://img.shields.io/badge/embedding-bge--m3-FFA500)

> The project was fully refactored from Java (Spring Boot + LangChain4j) to Python (FastAPI); the legacy Java implementation is kept in [java-legacy/](java-legacy/) for reference only.

---

## Core Features

| Feature | Description |
|---|---|
| 📄 **Knowledge-base Q&A** | Upload MD/DOCX/HTML/PDF → element-aware parsing → chunking & embedding → hybrid retrieval (BM25 + vector RRF) → answers **with cited sources** |
| 🧠 **Streamed chain-of-thought** | DeepSeek `reasoning_content` streamed separately from the answer (10-event typed SSE) — the model's reasoning is visible |
| 🇨🇳 **Chinese retrieval optimization** | jieba segmentation makes Chinese BM25 usable (exact Chinese R@5: 0.25 → **1.000**) |
| 🔀 **Smart routing + self-correction** | LangGraph orchestration: common-sense questions skip retrieval (saves latency); weak retrieval triggers query rewriting & retry (ADR-0015) |
| 🤖 **Agent mode** | Frontend switchable (`RAG`/`Agent`); ReAct tool loop: model autonomously picks tools (knowledge search / document fetch / calc / time), multi-step reasoning, convergence-aware stop (eval: avg_steps 2.28, closure 1.0) — ADR-0016 |
| 🗺️ **Architecture diagram (agent)** | Live full-chain map (guardrail → orchestration → context → tools → retrieval stack → LLM → SSE → `agent_runs`) with **used-parts highlighting** during a turn; vertical main-line layout, collapsible branches, fixed-pixel SVG |
| 📊 **Agent Runs page** | Every agent turn persisted as structured trace + context snapshot → replay (timeline / flow view) + review workflow (failures sink into the eval regression set) — ADR-0017 |
| 🛡️ **Request-boundary guardrail** | Prompt-injection detection at `ChatRequest` (fail-closed 422), both modes; deliberately *not* on tool results (KB is curated content) |
| 👥 **Team-ready** | JWT auth, per-user conversation isolation, shared knowledge base & memory (lab scenario) |
| 📚 **Document management** | List / detail (chunk visualization) / soft delete / replace-update (ADR-0013) |
| 🧩 **Long-term memory** | Facts auto-extracted from conversations + pgvector recall — team context persists across sessions |
| 🩺 **Readiness health** | `/health/ready` three-state (ready / degraded / not_ready) incl. **Celery worker probe** — catches missing async chunking early |

---

## Screenshots

![Chat Q&A](./docs/screenshots/chat.png)

*Chat: streaming answer + cited sources + chain-of-thought*

![Knowledge base management](./docs/screenshots/knowledge.png)

*Knowledge base: document list + chunk visualization + upload / replace / soft delete*

![Login page](./docs/screenshots/login.png)

*Login: JWT team authentication*

![Agent architecture diagram highlighting](./docs/screenshots/agent-arch.png)

*Agent mode: live full-chain architecture diagram — the modules a turn actually used light up (search question → retrieval stack lit; calc question → retrieval stack stays dim)*

![Agent Runs replay & review](./docs/screenshots/agent-runs.png)

*Agent Runs page: run list + statistics + replay timeline / flow view + failure review (accepted runs sink into the eval regression set)*

![RAG / Agent mode switch](./docs/screenshots/mode-switch.png)

*Header segmented control switching between RAG (deterministic state machine) and Agent (ReAct tool loop)*

---

## Quick Start

### Prerequisites

| Dependency | Version | Purpose |
|---|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | any | PostgreSQL / Redis / Ollama containers |
| [uv](https://docs.astral.sh/uv/) | ≥0.4 | Python package manager |
| Node.js | ≥18 | Frontend |
| DeepSeek API key | — | LLM (`api.deepseek.com`) |

> Local dev defaults to the `deepseek-v4-flash` model (configurable); embeddings run on local Ollama bge-m3 — **zero API cost**.

### Step 1: Configure environment variables

```bash
cd codeaware-py
cp .env.example .env        # copy the template
# edit .env — at minimum:
#   LLM_API_KEY=sk-...      ← required, DeepSeek key
#   JWT_SECRET_KEY=...      ← for production, use a random string (openssl rand -hex 32)
```

### Step 2: Start base services and pull the embedding model

```bash
cd ..                       # back to repo root
docker compose up -d        # PG(:5433) + Redis(:6380) + Kafka(:9093) + Celery Worker + Flower(:5555)
# Ollama runs natively (macOS Metal GPU): brew install ollama && ollama pull bge-m3
```

### Step 3: One-command startup (migrations + Celery worker + admin + backend + frontend)

```bash
./start.sh
```

Starts base services, runs migrations, boots a **native Celery worker** (async chunking / memory extraction — missing it makes uploaded docs keep `chunk_count=0`), seeds an `admin/admin123` account, then starts backend + frontend. Idempotent (re-run is safe). Then visit:

```text
Frontend:  http://localhost:5173          (admin / admin123)
OpenAPI:   http://localhost:8000/docs
Health:    http://localhost:8000/health/ready   # ready / degraded / not_ready (incl. celery probe)
```

### Manual startup (step by step)

```bash
docker compose up -d postgres redis
(cd codeaware-py && uv sync && uv run alembic upgrade head)
(cd codeaware-py && uv run celery -A app.ai.celery_app worker --loglevel=warning)  # required for async chunking
(cd codeaware-py && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000)
(cd codeaware-py/frontend && npm ci && npm run dev)
```

### Stop

```bash
kill $(cat .run/*.pid)        # stop worker + backend + frontend (keep docker)
docker compose down           # stop everything (data persists in volumes)
```

---

## Architecture Diagrams

### 1. System Layered Architecture

```mermaid
graph TB
    subgraph Presentation["Presentation Layer"]
        React["React 19 + Vite<br/>8-module SPA"]
        SSE["Typed SSE Parser<br/>10 events, protocol v1"]
    end

    subgraph Application["Application Layer (FastAPI)"]
        Router["API Router<br/>32 endpoints"]
        Auth["JWT Auth<br/>bcrypt"]
        TC["TurnCoordinator<br/>⚡ state machine"]

        subgraph Context["Context Building"]
            STM["ShortTermMemory<br/>PG messages + Redis window"]
            LTM["LongTermMemory<br/>atomic facts + pgvector"]
            RAG["RagService<br/>rewrite → hybrid → rerank"]
            RR["CrossEncoderReranker<br/>ONNX bge-reranker-v2-m3"]
            PT["PromptTemplate<br/>versioned"]
        end

        subgraph Agent["Agent Mode (CHAT_MODE=agent, ADR-0016)"]
            RL["ReAct Loop<br/>thinking 回注 + 防打转 + 收敛检测"]
            AT["AgentToolkit<br/>search / get_doc / list / calc / time"]
        end
    end

    subgraph Orchestration["Orchestration Layer"]
        LG["LangGraph<br/>router + self-correction"]
        Celery["Celery Worker<br/>parse + extract"]
        Flower["Flower<br/>:5555"]
    end

    subgraph Infrastructure["Infrastructure Layer"]
        PG["PostgreSQL 16<br/>pgvector + pg_search BM25"]
        Redis["Redis 7<br/>cache + Celery broker"]
        Kafka["Kafka<br/>audit + metrics"]
        Ollama["Ollama<br/>bge-m3 1024-d Metal GPU"]
        DS["DeepSeek v4-flash"]
    end

    React -->|"typed SSE (10 events)"| Router
    Router --> Auth
    Auth --> TC
    TC -->|"rag mode"| Context
    TC -->|"agent mode"| RL
    RL --> AT
    AT --> RAG
    RAG --> LG
    RAG --> RR
    TC -->|"submit async task"| Celery
    Flower --> Celery
    STM --> PG
    STM --> Redis
    LTM --> PG
    RR --> Ollama
    RAG --> Ollama
    TC -->|"ChatDeepSeek astream"| DS
    TC -->|"emit events"| Kafka
```

### 2. Chat/RAG Mode: Core Interaction Sequence

```mermaid
sequenceDiagram
    participant U as User
    participant F as Frontend
    participant B as Backend
    participant TC as TurnCoordinator
    participant CB as ContextBuilder
    participant RR as Reranker
    participant LLM as DeepSeek
    participant DB as PG/Redis

    U->>F: Input question
    F->>B: POST /chat/send/stream
    B->>TC: prepare_turn(message)
    TC->>DB: persist USER message (commit)
    TC-->>F: chat.started
    TC->>CB: build_context(message)
    CB->>RR: hybrid retrieval top_20<br/>RRF + cross-encoder re-rank
    RR->>DB: BM25 + pgvector
    RR-->>CB: top_5 after re-rank
    CB-->>TC: prompt + refs
    TC-->>F: context.references
    TC->>LLM: astream(prompt)
    LLM-->>F: reasoning.delta / token.delta
    TC->>DB: persist ASSISTANT (commit)
    TC-->>F: chat.completed
```

### 3. Smart Routing & Evaluation Decision Flow

```mermaid
flowchart TD
    A[User message] --> B{Smart routing<br/>LLM decision}
    B -->|direct: common-sense / chitchat| C[skip retrieval<br/>answer directly<br/>mark 「no KB retrieved」]
    B -->|retrieve: technical / docs| D[hybrid retrieval<br/>BM25 + pgvector RRF<br/>coarse top_20]
    D --> E[Reranker re-rank<br/>cross-encoder scoring]
    E --> F{Evaluation<br/>match_type detection}
    F -->|satisfied| G[inject top_5 → prompt<br/>→ LLM generation]
    F -->|unsatisfied & retries<2| H[rewrite query<br/>anti-loop + seen_queries fallback]
    H --> D
    F -->|limit reached or duplicate query| I[return 「not found」<br/>+ context.warning]
```

### 4. Agent Mode: ReAct Loop (CHAT_MODE=agent)

```mermaid
flowchart TD
    A[User message] --> B[Build messages<br/>memory + history + summary<br/>skip RAG pre-retrieval]
    B --> C[Model astream<br/>bind_tools auto decision]
    C --> D{Tool calls?}
    D -->|no| E[Final answer<br/>based on tool observations]
    D -->|yes| F[Execute tool<br/>search_knowledge / get_document<br/>list_documents / calculate / time]
    F --> G[Append ToolMessage<br/>carry reasoning_content]
    G --> H{Info sufficient?<br/>retrieval converged<br/>or max steps}
    H -->|no| C
    H -->|yes| E
    E --> I[Persist ASSISTANT<br/>Transaction B]
    I --> J[Post-turn<br/>summary + memory extract]
    J --> K[chat.completed]
```

**Live architecture display** (agent mode): the Chat page renders a static full-chain map and lights up the modules each turn actually uses — driven by the same SSE events (`tool.call` → the tool + retrieval stack, `context.references` → memory recall, `completed` → `sse`/`agent_runs`). Vertical main-line layout, collapsible branches, fixed-pixel SVG (readable text).

![Agent architecture diagram](./docs/screenshots/agent-arch.png)

### 5. System Context / Boundary

```mermaid
flowchart LR
    subgraph Team["Software Engineering Lab"]
        Dev["Developer<br/>upload docs / ask questions"]
        Newbie["New hire<br/>knowledge Q&A"]
    end

    subgraph System["CodeAware"]
        App["Chat/RAG platform<br/>KB + memory + async"]
    end

    subgraph External["External"]
        DS["DeepSeek API<br/>LLM generation"]
        Ollama["Ollama (local)<br/>bge-m3 embedding"]
        Docker["Docker<br/>PG / Redis / Kafka"]
    end

    Dev -->|"upload / ask"| App
    Newbie -->|"retrieve / Q&A"| App
    App -->|"LLM calls"| DS
    App -->|"embedding"| Ollama
    App -->|"data / events"| Docker
```

**Core principles**:

- **PostgreSQL is the source of truth; Redis is a disposable cache** — on Redis failure the system falls back to PG automatically, no feature degradation
- **No DB transaction is held while waiting on the model** — the connection pool is never blocked for long
- **Typed SSE with explicit semantics** — 10 event types with protocol version and strictly increasing sequence; the sync endpoint drains the same event stream — a single state machine
- **Dual runtime with rollback** — LangGraph retrieval enhancement (`RAG_RUNTIME=graph`) can be reverted to the original path (`service`) with one env change
- **Rerank is a reversible enhancement** — `reranker_enabled=False` reverts to pure RRF

Detailed design (Chat full-chain sequence, 9-table ER, RAG pipeline): see [docs/roadmap/current-release/README.md](docs/roadmap/current-release/README.md).
## Typed SSE Example (10-event protocol)

For a new conversation, `conversation_id` is created by the server and returned in `chat.started`:

```bash
curl -N http://localhost:8000/api/chat/send/stream \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '{"conversation_id":null,"message":"Explain the complete RAG pipeline"}'
```

The response is a stream of versioned events, not raw tokens or `[DONE]`:

```text
id: 1
event: chat.started
data: {"protocol_version":1,"conversation_id":"...","turn_id":"...","sequence":1}

id: 2
event: context.references
data: {"protocol_version":1,...,"knowledge_refs":[...],"memory_refs":[...],"sequence":2}

id: 3
event: reasoning.delta
data: {"protocol_version":1,...,"sequence":3,"delta":"First, analyze..."}

id: 4
event: token.delta
data: {"protocol_version":1,...,"sequence":4,"delta":"The full RAG pipeline includes..."}

event: chat.completed
data: {"protocol_version":1,...,"sequence":N}
```

---

## Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Framework | FastAPI + Pydantic v2 | async HTTP + typed SSE |
| LLM | DeepSeek v4-flash (langchain-deepseek) | ChatDeepSeek extracts reasoning_content |
| Embeddings | Ollama bge-m3 1024-d | local Metal GPU embedding, ~128ms/query, zero API cost |
| Relational DB | PostgreSQL 16 + asyncpg | PG-first source of truth |
| Vector index | pgvector HNSW cosine | inline vectors, same-transaction commit |
| Lexical search | ParadeDB pg_search BM25 | default tokenizer; pg_trgm fallback |
| Retrieval enhancement | LangGraph StateGraph (ADR-0015) | smart routing + self-correction (match_type detection) |
| Reranker | ONNX bge-reranker-v2-m3 | post-RRF cross-encoder re-rank (MRR +0.058) |
| Task queue | Celery + Redis | async document parsing, memory extraction, Flower monitoring |
| Event streaming | Kafka (Confluent) | audit trail, retrieval metrics, error events |
| Cache | Redis 7 | disposable, PG fallback |
| Frontend | React 19 + Vite + TypeScript | 8-module SPA (no router) |
| Tooling | uv + Alembic | locked dependencies + reversible migrations |

---

## Current Status

| Metric | Value |
|---|---|
| Backend tests | **352 passed**, 0 failed (async tasks + Kafka + LangGraph + Agent LLMOps) |
| Frontend tests | **61 passed** |
| API endpoints | 37 |
| Tables | 10 |
| ADRs | 17 (0001-0017) |
| Alembic head | 0012 |
| Delivered | C1-C6 + team A/B/C + document management + async task queue + Kafka event streaming + **Agent mode (frontend switch) + LLMOps closed loop (trace / replay / review / guardrail)** |

**Retrieval evaluation summary** (real bge-m3, 60 golden cases):

- Full evolution tracking (C3→C4→jieba→LangGraph→RAGAS): [retrieval-evolution.md](docs/optimization/retrieval-evolution.md)
- Hybrid retrieval R@5 = 0.975, MRR = 0.941 (with reranker) ([top_k sensitivity](docs/optimization/topk-sensitivity.md))
- jieba Chinese BM25: exact Chinese R@5 0.25 → **1.000** ([ADR-0011](docs/decisions/adr/0011-jieba-chinese-bm25-segmentation.md))
- LangGraph routing accuracy **60/60 = 1.000**, retry rate 0.019 ([eval report](docs/optimization/rag-graph-eval.md))
- Generation quality RAGAS: Faithfulness 0.931 / Answer Relevancy 0.793 ([eval report](docs/optimization/ragas-eval.md))

Full evaluation data (C3/C4 lexical upgrade, per-category, sensitivity analysis): [docs/optimization/](docs/optimization/README.md).

---

## Testing

Running bare `pytest` is forbidden for the backend — a safe runner creates disposable PG/Redis instances and refuses dev databases and remote targets:

```bash
# full test suite (safe)
(cd codeaware-py && uv run python scripts/run_tests_safe.py -q)

# coverage
(cd codeaware-py && uv run python scripts/run_tests_safe.py --cov=app --cov-report=term-missing -q)

# frontend
(cd codeaware-py/frontend && npm run test && npm run lint && npm run build)
```

---

## Technology Decisions

| Decision | Chosen | Rejected after evaluation |
|---|---|---|
| LLM adapter | ChatDeepSeek (extracts reasoning) | ChatOpenAI (drops 3rd-party fields) |
| Lexical search | ParadeDB BM25 (default tokenizer) + jieba | pg_trgm (C3 noise hurt RRF) |
| PDF parsing | pdfminer.six (font-size heading detection) | unstructured.partition.pdf (pulls in torch); pdfplumber (table extraction, deferred — no table-heavy docs yet) |
| Reranker | ONNX bge-reranker-v2-m3 (ADR-0009 re-evaluated) | torch CrossEncoder (heavy dependency) |
| Intent classification | not built (90% knowledge questions) | classifier risks missed retrieval |
| LangGraph | retrieval-layer routing + self-correction (ADR-0015) | orchestrating the Agent loop (ReAct stays a hand-written async generator, ADR-0016) |
| Refresh token | none (7-day access) | lab doesn't need rotation |
| Concurrency guard | in-process set[str] | PG advisory lock (when multi-worker) |
| Task queue | Celery + Redis | Kafka (event stream, not task queue) |
| Ollama deployment | native macOS (Metal GPU) | Docker container (CPU-only) |

---

## Current Boundaries

| Has | Does not have |
|---|---|
| JWT auth + per-user conversation isolation | project management (X-Project-ID) |
| shared knowledge base & memory | per-user KB permissions |
| 10-event typed SSE | WebSocket |
| BM25 + pgvector RRF **coarse rank** + ONNX cross-encoder **re-rank** | LLM-as-reranker / torch CrossEncoder |
| element-aware chunking + scanned-PDF rejection | OCR |
| PDF tables flattened into plain text (no row/column structure) | pdfplumber `extract_tables()` → Markdown table serialization (deferred until table-heavy docs exist) |
| fail-closed disposable test stack | bare pytest |
| single-worker local-first | multi-worker / K8s |
| Celery async task queue | external-action tools (sandbox / Git / MCP) |
| Kafka event streaming (audit/metrics) | Grafana / Loki dashboard |
| Flower task monitoring | — |
| deterministic Chat state machine **+ frontend-switchable Agent tool loop** (ADR-0016) | external-action tools |
| Agent run trace + replay + failure→eval sink (ADR-0017) | live Chat-page real-time highlight (P2, deferred — event-source-agnostic design already reserves it) |
| answer cache (sync endpoint only) | answer cache on streaming endpoint |

## Design vs Implementation Notes

Points where the design diverged from the final implementation, with the trade-off reasons:

| Design Point | Intended Design | Actual Implementation | Reason |
|---|---|---|---|
| **Answer cache** | Cache both sync & streaming | **Sync endpoint only** | Streaming must preserve citation/reasoning display, which cache replay would lose; sync fully blocks, so the win is largest (31s→0.02s). See [sync-vs-stream-endpoints.md](docs/optimization/sync-vs-stream-endpoints.md) |
| **Reranker** | Deferred (torch dependency) | **ONNX Runtime shipped** | 60 golden cases exposed the cross_doc MRR=0.750 shortfall; ONNX has no torch dependency, MRR +0.058 |
| **Negative-case fallback** | Unrelated queries return "not found" | **Keep hard answer + frontend hint** | Frontend already has a "no KB retrieved" marker; the fallback's benefit is small and the change is large, so it was not done |
| **Ollama deployment** | Docker container | **macOS native (Metal GPU)** | Docker cannot pass the GPU through to a container; native Metal is 45x faster |
| **Flower deployment** | Standalone container | **Merged into the Celery Worker container** | Same image, one entrypoint starts both processes, simplifies orchestration |
| **Kafka image** | bitnami/kafka | **confluentinc/cp-kafka** | bitnami image pull was rate-limited; the confluent image was already present locally |

> Principle: **only design changes with measured benefit or that resolve a dependency constraint get shipped**; the rest keep the original design, with the boundary recorded explicitly.

---

## Documentation

| Doc | Purpose |
|---|---|
| [AGENTS.md](AGENTS.md) | development rules |
| [docs/roadmap/current-release/README.md](docs/roadmap/current-release/README.md) | current roadmap (C1-C6) |
| [docs/roadmap/团队化升级计划.md](docs/roadmap/团队化升级计划.md) | team upgrade design |
| [docs/roadmap/团队化升级-实施计划.md](docs/roadmap/团队化升级-实施计划.md) | team upgrade implementation |
| [docs/roadmap/部署上线指南.md](docs/roadmap/部署上线指南.md) | deployment (LAN + cloud) |
| [docs/roadmap/chat-to-agent/personal/README.md](docs/roadmap/chat-to-agent/personal/README.md) | Agent roadmap (locked) |
| [docs/optimization/](docs/optimization/README.md) | retrieval optimization evals (jieba/top_k/LangGraph/RAGAS) |
| [docs/decisions/adr/](docs/decisions/adr/) | 17 architecture decision records (0001-0017) |
| [docs/interview/面试准备指南.md](docs/interview/面试准备指南.md) | interview deep-dive |
| [docs/interview/面试速通版.md](docs/interview/面试速通版.md) | interview speedrun |
| [docs/interview/项目简历介绍.md](docs/interview/项目简历介绍.md) | resume blurb |
| [docs/migration/Python重构迁移文档.md](docs/migration/Python重构迁移文档.md) | migration history |
