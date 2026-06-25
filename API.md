# AI Model Backend — API Reference

**Version:** 1.0.0  
**Base URL:** `https://modelsgate.eu` (production) or `http://localhost:8000` (local dev)  
**Content-Type:** `application/json`

---

## Table of Contents

1. [Overview](#1-overview)
2. [Task Types & Routing](#2-task-types--routing)
3. [Encryption Model](#3-encryption-model)
4. [Endpoints](#4-endpoints)
5. [Request Schema](#5-request-schema)
6. [Response Schema](#6-response-schema)
7. [Content Types](#7-content-types)
8. [Structured Comparison](#8-structured-comparison)
9. [Available Models](#9-available-models)
10. [Error Handling](#10-error-handling)
11. [Client Implementation Guide](#11-client-implementation-guide)
12. [Backward Compatibility](#12-backward-compatibility)
13. [Admin API](#13-admin-api)

---

## 1. Overview

The AI Model Backend is a unified API gateway that provides a **single interface** for all AI model requests regardless of the underlying provider (OpenAI, Anthropic, Gemini, Ollama). Key design properties:

| Property | Description |
|----------|-------------|
| **Task-based routing** | Clients specify *what they want to do* (`vision_describe`), not *which model to use*. The backend picks the best model. |
| **Constrained routing** | Optional constraints on output type, plan tier, cost class, and provider preference steer model selection. |
| **Unified schema** | One request/response format for text, image, and multi-image content |
| **Application-layer encryption** | Full payload encryption — doubly secure with HTTPS in production |
| **Provider agnostic** | Swap models without changing client code — the backend translates between formats |
| **Usage tracking** | Every request is logged with token counts and response times |

### Architecture

```
┌──────────────┐  Encrypted   ┌──────────────────────────────┐  Provider API  ┌─────────────┐
│  Your App    │◄──────────►  │         AI Backend           │◄─────────────► │   OpenAI    │
│  (Client)    │  AES + RSA   │                              │                ├─────────────┤
│              │              │  ┌──────────┐ ┌────────────┐ │                │  Anthropic  │
│              │              │  │  Router  │ │  Registry   │ │                ├─────────────┤
│              │              │  │  (select │ │  (provider  │ │                │   Gemini    │
│              │              │  │   model) │ │  instances) │ │                ├─────────────┤
│              │              │  └──────────┘ └────────────┘ │                │   Ollama    │
└──────────────┘              └──────────────────────────────┘                └─────────────┘
```

### Routing Flow

```
Client Request                    Router                         Provider
{                                 ┌──────────────────────┐
  task_type: "vision_describe",   │ 1. Filter by task     │
  output_type: "text",            │ 2. Filter by output   │
  plan_tier: "standard",          │ 3. Filter by tier     │
  cost_class: "cheapest",         │ 4. Sort by cost       │  ┌──────────────┐
  preferred_provider: "anthropic" │ 5. Check availability │→ │ claude-haiku │
}                                 │ 6. Pick best match    │  └──────────────┘
                                  └──────────────────────┘
                                        ↓
                                  RouteDecision {
                                    model: "claude-haiku",
                                    match_type: "exact",
                                    alternatives: ["gpt-4o-mini", "gemini-flash"]
                                  }
```

---

## 2. Task Types & Routing

Instead of hardcoding model names, clients declare a **task type** — what they want to accomplish. The backend resolves it to the best available model.

| Task Type | Description | Message Shape |
|-----------|-------------|---------------|
| `chat_with_context` | Multi-turn conversation with context | Text messages, any number of turns |
| `vision_describe` | Describe the contents of a single image | Text prompt + 1 image |
| `vision_qa` | Answer a specific question about an image | Text question + 1 image |
| `image_compare` | Compare two or more images, describe differences | Text prompt + 2+ images |
| `image_generate` | Generate an image from a text description | Text prompt only |
| `image_edit` | Edit or transform an input image | Text instruction + 1 image |

### Model Resolution

```
Client sends:                     Backend resolves:
{                                  ┌─ config: gpt-4o (supports vision_qa)  ✓ enabled
  "task_type": "vision_qa",   →   │  ┌─ config: claude-sonnet (supports vision_qa)  ✓ enabled
  "messages": [...]               │  │
}                                 ─→│  → picks first enabled match: gpt-4o
                                   │
                                   └─ If model override is provided, uses that instead
```

To see the current task→model mapping: `GET /admin/dashboard` or check server logs on startup.

### Routing Constraints

Clients can optionally steer model selection with these fields on the request:

| Field | Type | Description |
|-------|------|-------------|
| `output_type` | `"text"` \| `"image"` \| `"text_and_image"` | Filter models by their output capability. Use `"image"` or `"text_and_image"` when you need image generation. |
| `plan_tier` | `"free"` \| `"standard"` \| `"premium"` | Restrict to models within this service tier. `"premium"` includes all models; `"free"` only includes free/local models. |
| `cost_class` | `"cheapest"` \| `"balanced"` \| `"best"` | Sort preference for model selection. `"cheapest"` picks the lowest-cost model; `"best"` picks the highest-capability. |
| `preferred_provider` | `"openai"` \| `"anthropic"` \| `"gemini"` \| `"ollama"` | Prefer models from this provider. The router will try it first but fall back if unavailable. |
| `model` | string | **[DEPRECATED]** Exact model override. Bypasses the router entirely. Use routing constraints instead. |

**Constraint strictness:**
- `task_type`, `output_type`, `plan_tier` → **strict filters** (model must satisfy them)
- `cost_class`, `preferred_provider` → **soft preferences** (sort, don't filter)
- If strict filters eliminate all candidates, the router **progressively relaxes** them: tier first, then output_type

**Example — cheapest vision model from Anthropic:**
```json
{
  "task_type": "vision_describe",
  "output_type": "text",
  "plan_tier": "standard",
  "cost_class": "cheapest",
  "preferred_provider": "anthropic",
  "messages": [...]
}
```
→ Would select `claude-haiku` (standard tier, cheapest cost class, anthropic provider).

### Group Routing

When a client belongs to a group configured via **Admin → Group Routing**, routing behavior changes:

- **Strict mode**: every task type must be explicitly assigned a valid model.
- Tasks without an assignment return `error_code: GROUP_ROUTING_MISCONFIGURED`.
- Tasks assigned to a model that doesn't support them also return `GROUP_ROUTING_MISCONFIGURED`.
- There is **no fallback** to default routing for clients in a routed group.
- If a group has zero assignments configured, requests fall through to default routing.

**Admin panel:** `/admin/group-routing` — select a group, pick a model for each task type, save.

---

## 3. Encryption Model

All API payloads are **encrypted at the application layer** using a hybrid scheme. This means the API is secure even on HTTP-only servers.

| Layer | Algorithm | Key Size | Purpose |
|-------|-----------|----------|---------|
| Key exchange | **RSA-OAEP** with SHA-256 | 2048-bit | Encrypt the per-request AES session key |
| Payload encryption | **AES-256-GCM** | 256-bit | Encrypt the actual request/response JSON |
| Authentication | GCM auth tag | 128-bit | Tamper detection |

### Encryption Flow

```
CLIENT                                          SERVER
──────                                          ──────
1. GET /api/v1/public-key ──────────────────►   Returns RSA public key (PEM)
   ◄───────────────────────────────────────

2. Generate random: session_key (32B) + nonce (12B)

3. ciphertext = AES-256-GCM(json_payload, key=session_key, nonce=nonce)
4. encrypted_key = RSA-OAEP(session_key, public_key=server_pub)

5. POST /api/v1/request ───────────────────►   6. Decrypt session key, then payload
   { encrypted_key, encrypted_payload,          7. Normalize task_type → model
     nonce }                                     8. Route to provider, get response
                                                 9. Encrypt response (fresh nonce)
   ◄───────────────────────────────────────
   { encrypted_payload, nonce }

10. response = AES-256-GCM-decrypt(ciphertext, session_key, nonce)
```

---

## 4. Endpoints

### 4.1 Client Registration

```
GET /api/v1/register
```

**No encryption required.** Registers a new API client and returns a unique `client_id`. Every request to `/api/v1/request` must include this ID in the `client_id` field.

**Response `200 OK`:**
```json
{
  "client_id": "cl_a1b2c3d4e5f6g7h8",
  "plan": "free",
  "message": "Client registered. Send this client_id in every request."
}
```

**Without a valid client_id, requests return `403 Forbidden`.**

### 4.2 Health Check

```
GET /
```

No encryption required.

**Response `200 OK`:**
```json
{ "status": "ok", "service": "AI Model Backend", "version": "1.0.0" }
```

### 4.2 Get Public Key

```
GET /api/v1/public-key
```

No encryption required. Cache the result until key rotation.

**Response `200 OK`:**
```json
{
  "public_key": "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAu1SU1LfVLXR...\n-----END PUBLIC KEY-----\n",
  "key_size": 2048,
  "algorithm": "RSA-OAEP+SHA256/AES-256-GCM"
}
```

### 4.3 Send Request (Sync)

```
POST /api/v1/request
```

**All payloads MUST be encrypted.** Returns the response directly. For `image_generate` and `image_edit`, the server auto-switches to async mode (see [4.4 Async Jobs](#44-async-jobs)). Set `"async_mode": true` to force async for any task type.

**Outer Envelope (Raw POST Body):**
```json
{
  "encrypted_key": "<base64 RSA-encrypted AES key>",
  "encrypted_payload": "<base64 AES-256-GCM encrypted inner JSON>",
  "nonce": "<base64 12-byte nonce>"
}
```

**Outer Response Envelope:**
```json
{
  "encrypted_payload": "<base64 AES-256-GCM encrypted response JSON>",
  "nonce": "<base64 12-byte nonce (different from request)>"
}
```

**HTTP Status Codes:**
| Code | Meaning |
|------|---------|
| `200` | Request processed — check `error` field in decrypted response, or `job_id` if async |
| `400` | Decryption failed or invalid request format |
| `500` | Internal server error |

### 4.4 Async Jobs

```
POST /api/v1/request  (with async_mode: true, or auto for image_generate/image_edit)
GET  /api/v1/jobs/{job_id}
POST /api/v1/jobs/{job_id}/cancel
```

**Auto-async task types:** `image_generate`, `image_edit` (long-running, large outputs).  
**Force async:** Set `"async_mode": true` on any request.  
**Force sync:** Set `"async_mode": false` to override auto-async behavior.

**Decryption contract — retain the session key.** The job-reference response from `POST /api/v1/request` **and** every `GET /api/v1/jobs/{job_id}` and `POST /api/v1/jobs/{job_id}/cancel` response use the same outer envelope as `/request` (`{encrypted_payload, nonce}`) and are AES-256-GCM sealed with the **same `session_key`** the client generated for the original request. The server persists that key on the job row so poll/cancel responses stay decryptable. **Keep the `session_key` for the life of the job** — it is the only key that decrypts any async response. (The JSON examples below show the *decrypted* payload.)

**Sync Response (job reference):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "task_type": "image_generate",
  "message": "Job queued. Poll GET /api/v1/jobs/{job_id} for status."
}
```

**Poll Response (in progress):**
```json
{
  "job_id": "550e8400-...",
  "task_type": "image_generate",
  "status": "processing",
  "progress_percent": 30,
  "model_used": "gemini-pro",
  "created_at": "2026-06-13T12:00:00",
  "started_at": "2026-06-13T12:00:01"
}
```

**Poll Response (completed):**
```json
{
  "job_id": "550e8400-...",
  "status": "completed",
  "progress_percent": 100,
  "result": { /* Full UnifiedResponse */ }
}
```

**Cancel Response:**
```json
{
  "job_id": "550e8400-...",
  "status": "cancelled",
  "message": "Job cancelled."
}
```

**Job Statuses:** `pending` → `processing` → `completed` | `failed` | `cancelled`

---

## 5. Request Schema

The decrypted inner payload sent to `POST /api/v1/request`.

### New Style — Task-Based (Recommended)

```json
{
  "task_type": "vision_describe",
  "messages": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "Describe this image in detail." },
        { "type": "image", "image": "<base64>" }
      ]
    }
  ],
  "parameters": {
    "temperature": 0.7,
    "max_tokens": 1000
  }
}
```

| Field | Type | Required | Description |
|-------|------|:--------:|-------------|
| `task_type` | string | **Yes** | One of the [task types](#2-task-types). Determines which model handles the request. |
| `messages` | Message[] | **Yes** | Ordered conversation messages with text and/or image content blocks. |
| `messages[].role` | string | **Yes** | `"user"`, `"assistant"`, or `"system"` |
| `messages[].content` | ContentBlock[] | **Yes** | Array of `{"type":"text", "text":"..."}` and/or `{"type":"image", "image":"<base64>"}` blocks |
| `parameters` | object | No | Generation parameters |
| `parameters.temperature` | float | No | 0.0 – 2.0 |
| `parameters.max_tokens` | int | No | 1 – 128000 |
| `parameters.top_p` | float | No | 0.0 – 1.0 |
| `parameters.stop` | string[] | No | Stop sequences |
| `output_type` | string | No | Desired output: `"text"`, `"image"`, or `"text_and_image"`. Filters models that can't produce this. |
| `plan_tier` | string | No | Service tier: `"free"`, `"standard"`, or `"premium"`. Restricts to models within this tier. |
| `cost_class` | string | No | Cost preference: `"cheapest"`, `"balanced"`, or `"best"`. Sorts candidates by cost profile. |
| `preferred_provider` | string | No | Provider preference: `"openai"`, `"anthropic"`, `"gemini"`, or `"ollama"`. Boosts matching models. |
| `model` | string | No | **[DEPRECATED]** Exact model override. Bypasses routing — use constraints above instead. |
| `compare_options` | object | No | Options for the `image_compare` workflow. See [Structured Workflows](#8-structured-workflows). |
| `edit_options` | object | No | Options for the `image_edit` workflow. See [Structured Workflows](#8-structured-workflows). |
| `client_id` | string | No | Identifier for the calling application — stored in usage logs. |
| `group_id` | string | No | Organizational grouping (team, tenant, project) — stored in usage logs. |
| `conversation_id` | string | No | Groups multi-turn conversation requests — stored in usage logs. |

### Legacy Style — Model-Only (Backward Compatible)

```json
{
  "model": "gpt-4o",
  "messages": [...],
  "parameters": {...}
}
```

When only `model` is provided (no `task_type`), the server defaults to `chat_with_context`. See [Backward Compatibility](#12-backward-compatibility).

---

## 6. Response Schema

The decrypted inner payload returned from `POST /api/v1/request`.

### Success

```json
{
  "id": "req_a1b2c3d4e5f6",
  "task_type": "vision_describe",
  "model": "gpt-4o",
  "content": [
    { "type": "text", "text": "The image shows a serene mountain landscape..." }
  ],
  "usage": {
    "prompt_tokens": 150,
    "completion_tokens": 80,
    "total_tokens": 230
  },
  "error": null
}
```

### Error (Provider Level)

```json
{
  "id": "req_error_01",
  "task_type": "vision_qa",
  "model": "gpt-4o",
  "content": [],
  "usage": null,
  "error": "OpenAI error: Error code: 401 — Invalid API key"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique request ID (`req_` + 12 hex chars) |
| `task_type` | string \| null | The task type that was performed |
| `model` | string | Model alias that handled the request |
| `content` | ContentBlock[] | Array of text and/or image blocks. Empty on error. |
| `usage` | UsageInfo \| null | Token counts. `null` on error. |
| `usage.prompt_tokens` | int | Input tokens |
| `usage.completion_tokens` | int | Output tokens |
| `usage.total_tokens` | int | Sum |
| `compare_result` | object \| null | Structured comparison output. Only populated for `image_compare` tasks when `structured_output: true`. See [Structured Comparison](#8-structured-comparison). |
| `error` | string \| null | Error description if failed, `null` on success |
| `error_code` | string \| null | Machine-readable code: `NO_MODEL_AVAILABLE`, `GROUP_ROUTING_MISCONFIGURED`, `POLICY_VIOLATION`, `WORKFLOW_VALIDATION_FAILED`, `PROVIDER_ERROR` |

---

## 7. Content Types

Content is composed of **blocks** in an ordered array. Each block has a `type` field.

### Text Block
```json
{ "type": "text", "text": "Your text here..." }
```

### Image Block
```json
{ "type": "image", "image": "iVBORw0KGgoAAAANSUhEUgAAAAE..." }
```
Accepts raw base64 or full data URI (`data:image/png;base64,...`).

### Composition Examples

**chat_with_context — Multi-turn:**
```json
{
  "task_type": "chat_with_context",
  "messages": [
    { "role": "system", "content": [{"type":"text","text":"You are a helpful assistant."}] },
    { "role": "user", "content": [{"type":"text","text":"What is Python?"}] },
    { "role": "assistant", "content": [{"type":"text","text":"Python is a high-level..."}] },
    { "role": "user", "content": [{"type":"text","text":"What are its main use cases?"}] }
  ]
}
```

**vision_describe — Single image:**
```json
{
  "task_type": "vision_describe",
  "messages": [
    { "role": "user", "content": [
      {"type":"text","text":"Describe this photo."},
      {"type":"image","image":"<base64>"}
    ]}
  ]
}
```

**image_compare — Two images:**
```json
{
  "task_type": "image_compare",
  "messages": [
    { "role": "user", "content": [
      {"type":"text","text":"Which design is better and why?"},
      {"type":"image","image":"<base64-design-A>"},
      {"type":"image","image":"<base64-design-B>"}
    ]}
  ]
}
```

**image_generate — Text to image:**
```json
{
  "task_type": "image_generate",
  "messages": [
    { "role": "user", "content": [
      {"type":"text","text":"A futuristic city skyline at sunset, digital art style"}
    ]}
  ]
}
```

**image_edit — Transform an image:**
```json
{
  "task_type": "image_edit",
  "messages": [
    { "role": "user", "content": [
      {"type":"text","text":"Make the background blue instead of red."},
      {"type":"image","image":"<base64>"}
    ]}
  ]
}
```

---

## 8. Structured Workflows

The backend includes dedicated workflow modules for `image_compare` and `image_edit` task types. Workflows validate inputs, inject system prompts, transform messages, and parse/enrich responses before they reach the client.

### 8.1 Image Comparison (`image_compare`)

The `image_compare` workflow (`app/workflows/image_compare.py`) validates 2+ images, injects structured-output prompts, and parses JSON results from model responses.

**Stages:**
```
TaskRequest (image_compare, 2+ images)
  → validate_inputs()          # Requires ≥2 images
  → build_workflow_messages()  # Injects system prompt if structured_output
  → provider.generate()
  → finalize_image_compare()   # Parses JSON → compare_result
  → UnifiedResponse with compare_result
```

**Options (`compare_options`):**

| Field | Type | Default | Description |
|-------|------|:-------:|-------------|
| `structured_output` | bool | `false` | Request structured JSON comparison |
| `comparison_focus` | string \| null | `null` | Focus area for comparison |
| `include_similarities` | bool | `true` | Include similarities array |
| `include_differences` | bool | `true` | Include differences array |

**Example:**
```json
{
  "task_type": "image_compare",
  "messages": [{"role":"user","content":[
    {"type":"text","text":"Compare these two UI designs."},
    {"type":"image","image":"<base64-A>"},
    {"type":"image","image":"<base64-B>"}
  ]}],
  "compare_options": {
    "structured_output": true,
    "comparison_focus": "layout and typography"
  }
}
```

### 8.2 Image Editing (`image_edit`)

The `image_edit` workflow (`app/workflows/image_edit.py`) accepts 1+ source images plus editing instructions, transforms them into provider-specific edit prompts, and returns edited image content blocks with metadata.

**Router requirement:** Only models with `supports_image_output: true` AND `supports_image_edit: true` are eligible (currently only `gemini-pro`).

**Stages:**
```
TaskRequest (image_edit, 1+ images + instructions)
  → validate_edit_inputs()          # Requires ≥1 source image + text instruction
  → build_edit_workflow_messages()  # System prompt with style/format/resolution guidance
  → provider.generate()             # Model returns edited image(s)
  → finalize_image_edit()           # Counts images, builds edit_result metadata
  → UnifiedResponse with image blocks + edit_result
```

Because `image_edit` is auto-async (see [4.4 Async Jobs](#44-async-jobs)), this workflow also runs in the **async job path** — `edit_options` are honored and `edit_result` is populated in the polled job's `result` field exactly as in a sync response.

**Options (`edit_options`):**

| Field | Type | Default | Description |
|-------|------|:-------:|-------------|
| `style_guidance` | string \| null | `null` | Natural-language style guidance (e.g., `"watercolor painting"`, `"cyberpunk"`) |
| `output_format` | string | `"png"` | Output image format: `"png"`, `"jpeg"`, or `"webp"` |
| `output_quality` | int | `90` | Output quality 1–100 (lossy formats) |
| `num_outputs` | int | `1` | Number of edited variants to generate (1–4) |
| `preserve_aspect_ratio` | bool | `true` | Preserve source image aspect ratio |
| `target_resolution` | string \| null | `null` | Resolution hint (e.g., `"1024x1024"`) |

**Edit Result (`edit_result`):**

```json
{
  "source_images_used": 2,
  "edited_images": 1,
  "style_applied": "minimalist flat design",
  "edit_description": "Combined the two logos using shared iconography and a clean sans-serif typeface.",
  "output_format": "png"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `source_images_used` | int | Number of source images in the request |
| `edited_images` | int | Number of edited output images in the response |
| `style_applied` | string \| null | The style guidance that was applied |
| `edit_description` | string | What the model changed (extracted from response text) |
| `output_format` | string | Output format used |

**Example — Two-source-image edit:**
```json
{
  "task_type": "image_edit",
  "messages": [
    {"role":"user","content":[
      {"type":"text","text":"Combine these two logos into one cohesive design."},
      {"type":"image","image":"<base64-logo-A>"},
      {"type":"image","image":"<base64-logo-B>"}
    ]}
  ],
  "edit_options": {
    "style_guidance": "minimalist flat design",
    "output_format": "png",
    "output_quality": 95,
    "num_outputs": 1,
    "preserve_aspect_ratio": true,
    "target_resolution": "512x512"
  }
}
```

**Validation Errors:**

| Error | Cause |
|-------|-------|
| `image_edit requires at least 1 source image, got N` | No source images. Use `image_generate` for text-to-image. |
| `image_edit requires editing instructions` | No text instruction provided. |

---

## 9. Available Models

| Alias | Provider | Text | Img In | Multi | Img Out | Edit | Stream | Max Img | Tier | Cost | Status |
|-------|----------|:---:|:------:|:-----:|:-------:|:----:|:------:|:-------:|------|------|:------:|
| `gpt-4o` | OpenAI | ✅ | ✅ | ✅ | ✅ | — | ✅ | 10 | premium | best | Enabled |
| `gpt-4o-mini` | OpenAI | ✅ | ✅ | ✅ | — | — | ✅ | 5 | standard | cheapest | Enabled |
| `gpt-4-turbo` | OpenAI | ✅ | ✅ | ✅ | ✅ | — | ✅ | 10 | premium | balanced | Enabled |
| `claude-sonnet` | Anthropic | ✅ | ✅ | ✅ | — | — | ✅ | 5 | premium | best | Enabled |
| `claude-haiku` | Anthropic | ✅ | ✅ | ✅ | — | — | ✅ | 3 | standard | cheapest | Enabled |
| `gemini-pro` | Google | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 16 | premium | balanced | Enabled |
| `gemini-flash` | Google | ✅ | ✅ | ✅ | — | — | ✅ | 8 | standard | cheapest | Enabled |
| `llama-local` | Ollama | ✅ | — | — | — | — | — | 0 | free | cheapest | Disabled |
| `mistral-local` | Ollama | ✅ | — | — | — | — | — | 0 | free | cheapest | Disabled |

**Capability reference:**
| Capability | Field | What It Means |
|-----------|-------|---------------|
| Text In | `supports_text_input` | Accepts text prompts |
| Img In | `supports_image_input` | Accepts images as input |
| Multi | `supports_multi_image_input` | Accepts 2+ images per request |
| Img Out | `supports_image_output` | Can generate/edit images |
| Edit | `supports_image_edit` | Can transform input images |
| Stream | `supports_streaming` | Supports real-time token streaming |
| Max Img | `max_images` | Maximum images per request |
| Max MB | `max_image_size_mb` | Maximum image file size |

### Adding Custom Models

Edit `models_config.yaml`:

```yaml
models:
  - name: "my-model"
    provider: "openai"
    model_id: "gpt-4o-2024-08-06"
    description: "Pinned GPT-4o snapshot"
    capabilities:
      text_input: true
      image_input: true
      multi_image_input: true
      text_output: true
      image_output: true
      image_edit: false
      streaming: true
      max_images: 10
      max_image_size_mb: 20.0
    plan_tier: premium
    cost_class: best
    enabled: true
    # Optional: per-model API key (overrides env var)
    api_key: "sk-..."
```

Restart the server to apply. Models can also be added via **Admin → Models** without editing YAML.

All providers return a clear error when the API key is missing — e.g. `"Deepseek API key not configured. Set DEEPSEEK_API_KEY in .env or add an api_key on the model in Admin → Models."`

---

## 10. Error Handling

### Transport/Encryption Errors (HTTP status codes)

| Status | Cause | Fix |
|--------|-------|-----|
| `400` | Decryption failed or invalid request format | Re-fetch public key; check `task_type` is valid |
| `500` | Server-side failure | Check server logs |

### Provider Errors (HTTP 200, `error` field set)

| Error Pattern | Cause |
|---------------|-------|
| `No model available for task '...'` | No enabled model supports this task type |
| `Model '...' is disabled` | Model in config but `enabled: false` |
| `Model '...' does not support task '...'` | Task type mismatch (only when model override used) |
| `Unknown model '...'` | Model alias not found in config |
| `OpenAI error: ... 401` | Invalid/missing `OPENAI_API_KEY` |
| `Anthropic error: ... 403` | Permission denied |
| `Gemini error: ...` | Invalid key or quota exceeded |
| `Ollama error: ...` | Ollama not running |

---

## 11. Client Implementation Guide

### Python (Minimal)

```python
import base64, json, os, requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BASE_URL = "http://localhost:8000"

class AIClient:
    def __init__(self, base_url=BASE_URL):
        self.base_url = base_url.rstrip("/")
        self._pubkey = None

    def fetch_public_key(self):
        resp = requests.get(f"{self.base_url}/api/v1/public-key")
        resp.raise_for_status()
        self._pubkey = serialization.load_pem_public_key(
            resp.json()["public_key"].encode()
        )

    def run_task(self, task_type, messages, *,
                 model=None, output_type=None, plan_tier=None,
                 cost_class=None, preferred_provider=None, **params):
        """Execute an AI task with optional routing constraints.

        Args:
            task_type: One of the task type strings (e.g., 'vision_describe').
            messages: List of message dicts with content blocks.
            model: [DEPRECATED] Exact model override.
            output_type: 'text', 'image', or 'text_and_image'.
            plan_tier: 'free', 'standard', or 'premium'.
            cost_class: 'cheapest', 'balanced', or 'best'.
            preferred_provider: 'openai', 'anthropic', 'gemini', or 'ollama'.
            **params: Generation parameters (temperature, max_tokens, etc.).
        """
        if not self._pubkey:
            self.fetch_public_key()

        payload = {"task_type": task_type, "messages": messages}
        if model:
            payload["model"] = model
        if output_type:
            payload["output_type"] = output_type
        if plan_tier:
            payload["plan_tier"] = plan_tier
        if cost_class:
            payload["cost_class"] = cost_class
        if preferred_provider:
            payload["preferred_provider"] = preferred_provider
        if params:
            payload["parameters"] = params

        # Encrypt
        session_key = os.urandom(32)
        nonce = os.urandom(12)
        aesgcm = AESGCM(session_key)
        plaintext = json.dumps(payload).encode()
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)
        encrypted_key = self._pubkey.encrypt(
            session_key,
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                         algorithm=hashes.SHA256(), label=None)
        )

        envelope = {
            "encrypted_key": base64.b64encode(encrypted_key).decode(),
            "encrypted_payload": base64.b64encode(ciphertext).decode(),
            "nonce": base64.b64encode(nonce).decode(),
        }

        resp = requests.post(f"{self.base_url}/api/v1/request", json=envelope)
        resp.raise_for_status()
        encrypted_resp = resp.json()

        # Decrypt response
        resp_nonce = base64.b64decode(encrypted_resp["nonce"])
        resp_ct = base64.b64decode(encrypted_resp["encrypted_payload"])
        plain = aesgcm.decrypt(resp_nonce, resp_ct, None)
        return json.loads(plain)


# ── Usage ────────────────────────────────────────────────────────────────

client = AIClient()

# Chat (no model specified — backend picks default)
response = client.run_task("chat_with_context", [
    {"role": "user", "content": [{"type": "text", "text": "Hello!"}]}
])
print(response["content"][0]["text"])
print(f"Used model: {response['model']}")

# Vision — describe an image
with open("photo.jpg", "rb") as f:
    img_b64 = base64.b64encode(f.read()).decode()

response = client.run_task("vision_describe", [
    {"role": "user", "content": [
        {"type": "text", "text": "Describe this image."},
        {"type": "image", "image": img_b64}
    ]}
])

# Compare two images
with open("before.jpg", "rb") as f:
    before = base64.b64encode(f.read()).decode()
with open("after.jpg", "rb") as f:
    after = base64.b64encode(f.read()).decode()

response = client.run_task("image_compare", [
    {"role": "user", "content": [
        {"type": "text", "text": "What changed?"},
        {"type": "image", "image": before},
        {"type": "image", "image": after}
    ]}
])

# ── Routing constraints ──────────────────────────────────────────────────

# Cheapest model for a simple chat
response = client.run_task("chat_with_context", [
    {"role": "user", "content": [{"type": "text", "text": "Summarize AI in one sentence."}]}
], cost_class="cheapest")
print(f"Used: {response['model']}")  # e.g., claude-haiku or gpt-4o-mini

# Best model, Anthropic preferred, vision task
response = client.run_task("vision_qa", [
    {"role": "user", "content": [
        {"type": "text", "text": "What color is the car?"},
        {"type": "image", "image": img_b64}
    ]}
], cost_class="best", preferred_provider="anthropic")
print(f"Used: {response['model']}")  # claude-sonnet

# Free tier only (local models), no API keys needed
response = client.run_task("chat_with_context", [
    {"role": "user", "content": [{"type": "text", "text": "Hello!"}]}
], plan_tier="free")
# → picks llama-local or mistral-local if Ollama is running

# Image generation — must have text_and_image output
response = client.run_task("image_generate", [
    {"role": "user", "content": [
        {"type": "text", "text": "A golden retriever puppy in a field of sunflowers"}
    ]}
], output_type="text_and_image", cost_class="balanced")
print(f"Used: {response['model']}")  # gpt-4o or gemini-pro

# Explicit override (bypasses the router)
response = client.run_task("vision_qa", [
    {"role": "user", "content": [
        {"type": "text", "text": "What color is the car?"},
        {"type": "image", "image": img_b64}
    ]}
], model="claude-sonnet")  # uses Claude regardless of routing rules
```

### Node.js / TypeScript

```typescript
import * as crypto from "crypto";

class AIClient {
  private publicKey: crypto.KeyObject | null = null;

  constructor(private baseUrl = "http://localhost:8000") {}

  async fetchPublicKey(): Promise<void> {
    const resp = await fetch(`${this.baseUrl}/api/v1/public-key`);
    this.publicKey = crypto.createPublicKey((await resp.json()).public_key);
  }

  async runTask(
    taskType: string,
    messages: Array<{ role: string; content: Array<{ type: string; text?: string; image?: string }> }>,
    model?: string
  ): Promise<any> {
    if (!this.publicKey) await this.fetchPublicKey();

    const payload = JSON.stringify({ task_type: taskType, messages, ...(model ? { model } : {}) });
    const sessionKey = crypto.randomBytes(32);
    const nonce = crypto.randomBytes(12);

    const aesgcm = crypto.createCipheriv("aes-256-gcm", sessionKey, nonce) as crypto.CipherGCM;
    const ciphertext = Buffer.concat([aesgcm.update(payload), aesgcm.final()]);
    const authTag = aesgcm.getAuthTag();

    const encryptedKey = crypto.publicEncrypt(
      { key: this.publicKey!, padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: "sha256" },
      sessionKey
    );

    const envelope = {
      encrypted_key: encryptedKey.toString("base64"),
      encrypted_payload: Buffer.concat([ciphertext, authTag]).toString("base64"),
      nonce: nonce.toString("base64"),
    };

    const resp = await fetch(`${this.baseUrl}/api/v1/request`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(envelope),
    });
    const er = await resp.json();

    const decipher = crypto.createDecipheriv("aes-256-gcm", sessionKey, Buffer.from(er.nonce, "base64"));
    const plain = Buffer.concat([decipher.update(Buffer.from(er.encrypted_payload, "base64")), decipher.final()]);
    return JSON.parse(plain.toString());
  }
}

// Usage
const client = new AIClient();
const response = await client.runTask("chat_with_context", [
  { role: "user", content: [{ type: "text", text: "Hello!" }] },
]);
console.log(response.model); // e.g., "gpt-4o-mini"
```

### Test Client (included)

```bash
# New task-based requests
python test_client.py --task chat_with_context
python test_client.py --task vision_describe
python test_client.py --task image_compare

# With model override
python test_client.py --task vision_qa --model claude-sonnet

# Legacy backward compat test
python test_client.py --legacy --model gpt-4o-mini
```

---

## 12. Backward Compatibility

Clients using the old `model`-only format continue to work:

**Old request (still accepted):**
```json
{ "model": "gpt-4o", "messages": [...] }
```

**How it's handled:**
1. Server detects missing `task_type` field
2. Falls back to `UnifiedRequest` parser
3. Normalizes to `task_type = chat_with_context`, `model = "gpt-4o"` (bypasses the router)
4. Processes normally

**Migration path:**
1. Update clients to send `task_type` instead of `model`
2. Use `output_type`, `plan_tier`, `cost_class`, `preferred_provider` for fine-grained control
3. Remove `model` from requests (unless you need a specific override)
4. The old format will be supported for at least 2 major versions

---

## 13. Admin API

Web UI at `/admin/`. Default credentials: `admin` / `admin123`.

| Method | Path | Auth | Description |
|--------|------|:----:|-------------|
| `GET` | `/admin/login` | — | Login form |
| `POST` | `/admin/login` | — | Authenticate |
| `GET` | `/admin/dashboard` | Session | Usage stats, charts, filter by task_type & conversation_id |
| `GET` | `/admin/routing` | Session | Task→model priority order, fallback chains, constraint relaxation, routing failures |
| `GET` | `/admin/tasks` | Session | Per-task model mappings with priority/fallback, usage per task |
| `GET` | `/admin/capabilities` | Session | Full model capability matrix and task support grid |
| `GET` | `/admin/usage` | Session | Task usage breakdowns, daily charts, token distribution, error rates |
| `GET` | `/admin/models` | Session | Model config with task types |
| `GET` | `/admin/clients` | Session | Register API clients with keys, assign groups/policies |
| `POST` | `/admin/clients/create` | Session | Create a new client |
| `POST` | `/admin/clients/{id}/toggle` | Session | Activate/deactivate a client |
| `GET` | `/admin/groups` | Session | Manage client groups with policy assignment |
| `POST` | `/admin/groups/create` | Session | Create a new client group |
| `GET` | `/admin/policies` | Session | View routing policies with all restrictions |
| `POST` | `/admin/policies/create` | Session | Create a new policy with defaults |
| `POST` | `/admin/policies/{id}/toggle` | Session | Activate/deactivate a policy |
| `GET` | `/admin/settings` | Session | Key rotation, password change |
| `POST` | `/admin/settings/rotate-keys` | Session | Rotate encryption keys |
| `POST` | `/admin/settings/change-password` | Session | Change admin password |

```bash
# Login
curl -X POST http://localhost:8000/admin/login \
  -d "username=admin&password=admin123" -c cookies.txt

# Dashboard
curl http://localhost:8000/admin/dashboard -b cookies.txt
```

---

## Quick Reference Card

```
┌──────────────────────────────────────────────────────────────┐
│                     API QUICK REFERENCE                       │
├──────────────────────────────────────────────────────────────┤
│ Base URL:      https://modelsgate.eu                        │
│                (or http://localhost:8000 for local dev)      │
│ Encryption:    RSA-2048 (OAEP+SHA256) + AES-256-GCM           │
│ Content-Type:  application/json                               │
├──────────────────────────────────────────────────────────────┤
│ GET  /                                  Health check          │
│ GET  /api/v1/public-key                 Get RSA public key    │
│ POST /api/v1/request                    Send encrypted req    │
├──────────────────────────────────────────────────────────────┤
│ Task Types:                                                   │
│   chat_with_context    Multi-turn conversation                │
│   vision_describe      Describe a single image                │
│   vision_qa            Answer question about an image         │
│   image_compare        Compare 2+ images                      │
│   image_generate       Generate image from text               │
│   image_edit           Edit/transform an image                │
├──────────────────────────────────────────────────────────────┤
│ Request (after decryption):                                   │
│   task_type: "vision_describe"                                │
│   messages:  [{role, content: [{type, text|image}]}]          │
│   output_type:     "text" | "image" | "text_and_image"        │
│   plan_tier:       "free" | "standard" | "premium"            │
│   cost_class:      "cheapest" | "balanced" | "best"           │
│   preferred_provider: "openai" | "anthropic" | ...            │
│   model:     "..." (explicit override, bypasses router)       │
│   compare_options: {structured_output, comparison_focus, ...} │
│   parameters: {temperature, max_tokens, top_p, stop}          │
├──────────────────────────────────────────────────────────────┤
│ Response (after decryption):                                  │
│   id:         "req_..."                                       │
│   task_type:  "vision_describe"                               │
│   model:      "gpt-4o"                                        │
│   content:    [{type: "text"|"image", ...}]                   │
│   compare_result: {similarities, differences, ...} | null     │
│   usage:      {prompt_tokens, completion_tokens, total}       │
│   error:      null | "error message"                           │
├──────────────────────────────────────────────────────────────┤
│ Admin:     https://modelsgate.eu/admin/login                │
│ Default:   admin / admin123                                    │
└──────────────────────────────────────────────────────────────┘
```
