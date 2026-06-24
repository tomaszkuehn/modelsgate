# AI Model Backend

Unified API gateway for AI model providers with **application-layer encryption**. One endpoint, any content type, any provider — works securely over plain HTTP.

[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://python.org) [![Python 3.14](https://img.shields.io/badge/python-3.14-tested-green.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/fastapi-0.111-009688.svg)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](https://docker.com)

---

## Why This Exists

- **Multiple SDKs** — OpenAI, Anthropic, Gemini, Deepseek, and Qwen all have different APIs
- **TLS dependency** — You need HTTPS certificates everywhere, even for internal services
- **Hardcoded models** — Changing providers means rewriting client code
- **No visibility** — No built-in usage tracking across providers

This backend solves all four: **task-based routing**, **built-in encryption** (RSA+AES-GCM), **config-based model resolution**, and **built-in usage analytics**.

---

## Quick Start

### Prerequisites

- Python 3.12+ (tested up to 3.14; see [VPS.md](VPS.md#python-314-compatibility-notes) for 3.14 notes) or Docker
- At least one AI provider API key

### 1. Clone & Configure

```bash
git clone <repo-url> && cd backend-AI
cp .env.example .env
# Edit .env — add at least one API key
```

### 2. Run

```bash
# Python directly
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Docker (log level set to debug for development)
docker compose up --build
```

### 3. Verify

```bash
curl http://localhost:8000/                           # Health check (direct)
curl http://localhost/                                # Health check (via nginx)
curl http://localhost:8000/api/v1/public-key          # RSA public key
curl http://localhost:8000/api/v1/register            # Get a client ID
python test_client.py --task chat_with_context        # Test request
open http://localhost:8000/admin/login                # Admin panel (admin/admin123)
```

---

## How It Works

### Encryption

All payloads are encrypted with **RSA-2048 (OAEP+SHA256) + AES-256-GCM**. Works over plain HTTP.

### Task-Based Routing

Clients declare a **task type** — the backend picks the best model:

| Task Type | Content | Auto-Async |
|-----------|---------|:----------:|
| `chat_with_context` | Multi-turn text | — |
| `vision_describe` | Text + 1 image | — |
| `vision_qa` | Text + 1 image | — |
| `image_compare` | Text + 2+ images | — |
| `image_generate` | Text prompt → image | ✅ |
| `image_edit` | Text + image(s) → edited image(s) | ✅ |

**Routing constraints** (optional): `output_type`, `plan_tier`, `cost_class`, `preferred_provider`.

### Client Requirements

Every request must include a `client_id`. Register via `GET /api/v1/register` (returns `{client_id, plan: "free"}`). Unregistered or blocked clients get `403`.

---

## Providers

Models are configured via **Admin → Models** (DB-backed, synced to `models_config.yaml`). No predefined models — add your own.

| Provider | API Key Env Var | Format |
|----------|----------------|--------|
| OpenAI | `OPENAI_API_KEY` | Native SDK |
| Anthropic | `ANTHROPIC_API_KEY` | Native SDK |
| Google Gemini | `GEMINI_API_KEY` | Native SDK |
| Deepseek | `DEEPSEEK_API_KEY` | OpenAI-compatible |
| OpenRouter | `OPENROUTER_API_KEY` | OpenAI-compatible |
| Alibaba (Qwen) | `ALIBABA_API_KEY` | OpenAI-compatible |
| Ollama | `OLLAMA_BASE_URL` | HTTP API |

Per-model API keys can be set in Admin → Models (overrides env var). All providers give a clear error message when the key is missing — no more cryptic SDK errors.

---

## Admin Panel

`http://localhost:8000/admin/login` (admin / admin123)

| Page | Purpose |
|------|---------|
| **Dashboard** | Stats, daily charts, filter by task type & conversation ID |
| **Routing** | Task→model priority order, fallback chains, relaxation steps |
| **Tasks** | Per-task eligible models with priority & usage |
| **Capabilities** | Full capability matrix + task support grid |
| **Usage** | Task breakdowns, daily trends, token distribution |
| **Models** | Full CRUD — add/edit/delete models with capabilities, API keys, tier & cost |
| **Clients** | Generate client IDs, assign to groups, block/unblock, view token usage |
| **Groups** | Create groups with unique keys, view client counts |
| **Group Routing** | Assign specific models to tasks per group — overrides default routing |
| **Policies** | Routing policies with task/provider/token/capability restrictions |
| **Playground** | Build & send API requests, preview original→encrypted→response |
| **Logs** | Request trace log — original, converted, response. FIFO-capped at 1000 entries |
| **Settings** | API key status (all 7 providers), key rotation, password change |

---

## Key Features

### 🔑 Client Registration & Groups

Every client registers (`GET /api/v1/register`) and receives a unique `client_id`. Clients belong to groups (default: `default`). Groups can be assigned per-task model overrides via **Group Routing**.

**Group routing is strict:** when a client belongs to a group, every task type must be explicitly assigned a valid model. Tasks without an assignment return `GROUP_ROUTING_MISCONFIGURED` — no silent fallback to default routing.

### 🔄 Workflows

| Workflow | Task | What It Does |
|----------|------|-------------|
| Image Compare | `image_compare` | Validates 2+ images, injects structured JSON prompts, parses comparison results |
| Image Edit | `image_edit` | Validates 1+ source images + instructions, injects style/format prompts, returns edited images + metadata |

### ⏳ Async Jobs

`image_generate` and `image_edit` auto-run as background jobs. Poll `GET /api/v1/jobs/{job_id}`, cancel with `POST /api/v1/jobs/{job_id}/cancel`.

### 📈 Observability

- **Usage logs** — task type, model, provider, tokens, response time, modalities, client ID, group ID, asset tracking, routing decision JSON
- **Request traces** — `/admin/logs` shows original→converted→response pipeline (including provider-specific format), FIFO-capped at 1000
- **Registration logs** — every `GET /api/v1/register` is logged with `task_type: register`
- **Client ID in logs** — every log line includes `client=cl_xxx` for traceability

### ⚠️ Error Codes

Every error response includes a machine-readable `error_code`:

| error_code | Meaning |
|-----------|---------|
| `NO_MODEL_AVAILABLE` | No model can handle the requested task |
| `GROUP_ROUTING_MISCONFIGURED` | Group has routing configured but the assigned model doesn't support the task, or the task has no assignment |
| `POLICY_VIOLATION` | Request violates a routing policy rule |
| `WORKFLOW_VALIDATION_FAILED` | Invalid input (e.g., too few images for image_compare) |
| `PROVIDER_ERROR` | Provider returned an error (missing API key, timeout, etc.) |

### 🔧 Capability Validation

The admin Models page auto-links related checkboxes (e.g., checking "Multi Img" forces "Img In" on). On startup, contradictory capabilities are logged as warnings.

---

## Project Structure

```
backend-AI/
├── app/
│   ├── main.py                      # FastAPI app, lifespan, seed data
│   ├── config.py                    # Pydantic settings (all env vars)
│   ├── database.py                  # SQLAlchemy async engine + session
│   ├── security/
│   │   ├── encryption.py            # Hybrid RSA+AES-GCM
│   │   └── keys.py                  # Key generation, persistence, rotation
│   ├── api/
│   │   ├── routes.py                # /api/v1 endpoints + normalization + policy + workflows
│   │   └── schemas.py               # Task types, routing enums, workflow schemas
│   ├── models/
│   │   ├── base.py                  # Abstract provider + ModelConfig with capabilities
│   │   ├── registry.py              # Async DB-backed config, provider caching
│   │   ├── router.py                # Task→model routing with filters, sorting, relaxation, group overrides
│   │   └── providers/
│   │       ├── openai.py, anthropic.py, gemini.py   # Native SDK providers
│   │       ├── deepseek.py, openrouter.py, alibaba.py  # OpenAI-compatible providers
│   │       └── ollama.py                            # Local models via HTTP
│   ├── workflows/
│   │   ├── image_compare.py         # Structured comparison with JSON parsing
│   │   └── image_edit.py            # Image editing with style/resolution guidance
│   ├── policy/
│   │   └── enforcer.py              # Client/group policy validation + ceiling tracking
│   ├── jobs/
│   │   └── manager.py               # Async job creation, background processing, cancellation
│   ├── logs/
│   │   └── tracer.py                # Request trace logging with FIFO cap
│   ├── admin/
│   │   ├── auth.py, routes.py       # Session auth, 14 admin routes
│   │   ├── static/style.css         # Dark-themed UI
│   │   └── templates/               # 14 Jinja2 templates
│   └── stats/
│       ├── models.py                # 10 ORM models (UsageLog, Client, Group, Policy, Job, etc.)
│       └── tracker.py               # Usage recording + aggregation queries
├── tests/
│   ├── test_image_compare.py        # 33 tests
│   └── test_image_edit.py           # 36 tests
├── models_config.yaml               # Model config (DB-backed, synced)
├── test_client.py                   # Reference client
├── requirements.txt, Dockerfile, docker-compose.yml
└── API.md                           # Full API reference
```

---

## Configuration

### Environment Variables (`.env`)

| Variable | Provider | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | OpenAI | `sk-...` |
| `ANTHROPIC_API_KEY` | Anthropic | `sk-ant-...` |
| `GEMINI_API_KEY` | Google | AI Studio key |
| `DEEPSEEK_API_KEY` | Deepseek | Platform key |
| `OPENROUTER_API_KEY` | OpenRouter | `sk-or-...` |
| `ALIBABA_API_KEY` | Alibaba | DashScope key |
| `OLLAMA_BASE_URL` | Ollama | `http://localhost:11434` |
| `ADMIN_USERNAME` | — | `admin` |
| `ADMIN_PASSWORD` | — | `admin123` |
| `SESSION_SECRET` | — | Cookie signing key |

### Models

Add models via **Admin → Models**. Each model has:
- **Identity**: name, provider, model_id, description
- **Capabilities**: text/image input/output, multi-image, edit, streaming, max images, max image size
- **Routing**: plan tier (free/standard/premium), cost class (cheapest/balanced/best), cost weight
- **API key**: per-model override (falls back to env var)

---

## API Reference

➡️ **[API.md](API.md)**

Key endpoints:
```
GET  /                          Health check
GET  /api/v1/register           Register client → {client_id, plan}
GET  /api/v1/public-key         RSA public key
POST /api/v1/request            Encrypted AI request
GET  /api/v1/jobs/{id}          Poll async job
POST /api/v1/jobs/{id}/cancel   Cancel job
```

---

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
python -m pytest tests/ -v          # 69 tests
python test_client.py --task chat_with_context
```

### Adding a Provider

1. Create `app/models/providers/<name>.py`, subclass `BaseModelProvider`
2. Register in `app/models/registry.py` → `_instantiate_provider()`
3. Add via Admin → Models (or `models_config.yaml`)

---

## Deployment

### Docker

```bash
docker compose up --build -d       # Production
docker compose logs -f             # View logs
```

### Bare-metal (venv + systemd + nginx)

Full step-by-step guide for deploying on a VPS without Docker:

➡️ **[VPS.md](VPS.md)** — setup, updates, troubleshooting, HTTPS, backup.

Currently running on:
- **URL:** `https://modelsgate.eu`
- **OS:** Ubuntu 26.04 LTS, Python 3.14.4
- **Path:** `/opt/ai-backend` | **Service:** `ai-backend`

---

## License

MIT
