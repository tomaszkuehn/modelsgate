"""API endpoints for the AI backend.

Request pipeline (POST /api/v1/request):
  decrypt → parse (task-based or legacy) → client validation → async fork
  → policy → routing → workflow pre/post → provider → observability → encrypt

Error contract (1E): everything after decryption follows the unified shape —
an encrypted UnifiedResponse with error/error_code. Protocol failures
(decrypt/parse) remain plain HTTPException(4xx) because no session key or
task_type is reliably available to seal a UnifiedResponse.
"""

import json
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Request, HTTPException

from app.api.schemas import (
    TaskType,
    TaskRequest,
    NormalizedTaskRequest,
    UnifiedRequest,
    UnifiedResponse,
    EncryptedRequest,
    EncryptedResponse,
    PublicKeyResponse,
    ImageCompareResult,
    ImageEditResult,
    TextContent,
)
from app.security.encryption import decrypt_request, encrypt_response
from app.models.router import NoModelAvailableError  # imported early for except handler

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["api"])


def _resolve_api_key_prefix(model_name: str, registry) -> Optional[str]:
    """Return first 12 chars of the effective API key for a model, or None."""
    from app.config import settings

    config = registry.get_config(model_name)
    if config is None:
        return None

    # Per-model override takes priority
    if config.api_key:
        return config.api_key[:12]

    # Fall back to provider env var
    provider_key_map = {
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
        "gemini": settings.gemini_api_key,
        "openrouter": settings.openrouter_api_key,
        "alibaba": settings.alibaba_api_key,
        "deepseek": settings.deepseek_api_key,
    }
    env_key = provider_key_map.get(config.provider.lower(), "")
    return env_key[:12] if env_key else None


# ── Unified error envelope ───────────────────────────────────────────────

async def finish_error_response(
    err_response: UnifiedResponse,
    *,
    session_key: bytes,
    original: dict,
    task_type: Optional[str],
    client_id: Optional[str],
) -> EncryptedResponse:
    """Trace an error response to /admin/logs, then encrypt and return it."""
    try:
        from app.logs.tracer import trace_request
        await trace_request(
            request_id=err_response.id,
            original=original,
            response=err_response.model_dump(),
            task_type=task_type,
            model_name=err_response.model,
            provider=None,
            status="error",
            client_id=client_id,
            api_key_prefix=None,
        )
    except Exception as e:
        logger.error(f"Failed to trace error response: client={client_id} — {e}")
    encrypted = encrypt_response(err_response.model_dump(), session_key)
    return EncryptedResponse(**encrypted)


# ── Public key ──────────────────────────────────────────────────────────

@router.get("/public-key", response_model=PublicKeyResponse)
async def get_public_key(request: Request):
    """Return the server's RSA public key for encrypting session keys."""
    key_manager = request.app.state.key_manager
    return PublicKeyResponse(
        public_key=key_manager.public_key_pem,
    )


# ── Pipeline stages ──────────────────────────────────────────────────────

def _build_error(
    err: Exception,
    error_code: str,
    *,
    task_type: Optional[TaskType] = None,
    model: str = "none",
) -> UnifiedResponse:
    """Build a UnifiedResponse error envelope from an exception."""
    return UnifiedResponse(
        task_type=task_type,
        model=model,
        content=[],
        error=str(err),
        error_code=error_code,
    )


async def _enforce_policy(task_req: TaskRequest, client_id: Optional[str]) -> None:
    """Resolve the effective policy for a client and validate the request.

    Raises RequestPipelineError(POLICY_VIOLATION) when a policy rejects it;
    otherwise tightens the routing fields on task_req in place.
    """
    from app.policy.enforcer import PolicyEnforcer, PolicyViolationError
    from app.database import async_session

    async with async_session() as policy_session:
        enforcer = PolicyEnforcer(policy_session)
        resolved = await enforcer.resolve_policy(client_id)

        img_count = sum(
            1 for msg in task_req.messages
            for block in msg.content
            if hasattr(block, 'type') and block.type == 'image'
        )

        try:
            await enforcer.validate_request(
                policy=resolved,
                task_type=task_req.task_type,
                image_count=img_count,
                requested_tokens=(
                    task_req.parameters.max_tokens
                    if task_req.parameters and task_req.parameters.max_tokens
                    else 1024
                ),
                request_output_type=(
                    task_req.output_type.value
                    if task_req.output_type else None
                ),
            )
        except PolicyViolationError as e:
            logger.warning(f"Policy violation: client={client_id} — {e}")
            raise

        enforcer.apply_policy_constraints(resolved, task_req)


def _run_workflows_pre(
    task_req: TaskRequest,
    normalized: NormalizedTaskRequest,
) -> int:
    """Run workflow preprocessing; returns edit source image count.

    Raises RequestPipelineError(WORKFLOW_VALIDATION_FAILED) on bad input.
    """
    if normalized.task_type == TaskType.IMAGE_COMPARE:
        from app.workflows.image_compare import execute_image_compare
        _, _ = execute_image_compare_sync_guard(task_req, normalized)
        return 0

    if normalized.task_type == TaskType.IMAGE_EDIT:
        from app.workflows.image_edit import execute_image_edit
        _, source_count = execute_image_edit_sync_guard(task_req, normalized)
        return source_count

    return 0


def execute_image_compare_sync_guard(task_req, normalized):
    return _async_noop_placeholder()


def execute_image_edit_sync_guard(task_req, normalized):
    return _async_noop_placeholder()


def _async_noop_placeholder():
    return None, 0


async def _apply_workflows_pre(
    task_req: TaskRequest,
    normalized: NormalizedTaskRequest,
) -> int:
    """Async workflow preprocessing (executes actual workflow validation)."""
    if normalized.task_type == TaskType.IMAGE_COMPARE:
        from app.workflows.image_compare import execute_image_compare
        _, _ = await execute_image_compare(task_req, None, normalized)
        return 0

    if normalized.task_type == TaskType.IMAGE_EDIT:
        from app.workflows.image_edit import execute_image_edit
        _, source_count = await execute_image_edit(task_req, None, normalized)
        return source_count

    return 0


async def _apply_workflows_post(
    normalized: NormalizedTaskRequest,
    unified_response: UnifiedResponse,
    *,
    compare_options,
    edit_options,
    edit_source_count: int,
    client_id: Optional[str],
) -> None:
    """Attach workflow-derived metadata (compare_result / edit_result)."""
    if normalized.task_type == TaskType.IMAGE_COMPARE and compare_options:
        from app.workflows.image_compare import finalize_image_compare
        response_text = _extract_response_text(unified_response)
        compare_result = finalize_image_compare(response_text, compare_options)
        if compare_result:
            unified_response.compare_result = compare_result
            logger.info(
                f"image_compare: client={client_id or 'anonymous'} "
                f"extracted structured result "
                f"({len(compare_result.similarities)} similarities, "
                f"{len(compare_result.differences)} differences)"
            )

    if normalized.task_type == TaskType.IMAGE_EDIT:
        from app.workflows.image_edit import finalize_image_edit
        response_text = _extract_response_text(unified_response)
        edit_result = finalize_image_edit(
            content_blocks=unified_response.content,
            options=edit_options,
            source_image_count=edit_source_count,
            response_text=response_text,
        )
        unified_response.edit_result = edit_result
        logger.info(
            f"image_edit: client={client_id or 'anonymous'} "
            f"{edit_result.edited_images} edited image(s) "
            f"from {edit_result.source_images_used} source(s)"
        )


def _extract_response_text(unified_response: UnifiedResponse) -> str:
    return "".join(
        block.text for block in unified_response.content
        if isinstance(block, TextContent)
    )


def _sanitize_for_trace(decrypted: dict) -> dict:
    """Return a deep-enough copy of the decrypted request without image data.

    Original dict is left untouched (images are released from the live
    objects directly); the trace stores a text-only view.
    """
    sanitized = {k: v for k, v in decrypted.items() if k != "messages"}
    messages = []
    for m in decrypted.get("messages", []):
        if not isinstance(m, dict):
            messages.append(m)
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = [
                c for c in content
                if not (isinstance(c, dict) and (
                    c.get("image") or c.get("image_url") or c.get("type") == "image_url"
                ))
            ]
        messages.append({**m, "content": content})
    sanitized["messages"] = messages
    return sanitized


async def _record_usage(
    *,
    registry,
    decrypted: dict,
    normalized: NormalizedTaskRequest,
    unified_response: UnifiedResponse,
    routing_decision,
    response_time_ms: int,
    task_req: TaskRequest,
) -> None:
    """Record usage statistics (never raises)."""
    try:
        from app.stats.tracker import (
            record_usage,
            compute_input_modality,
            compute_output_modality,
            extract_asset_ids,
        )
        from app.database import async_session

        routing_json = None
        if routing_decision is not None:
            try:
                routing_json = json.dumps(routing_decision.model_dump(), default=str)
            except Exception:
                pass

        input_mod = compute_input_modality(decrypted.get("messages", []))
        output_mod = compute_output_modality(
            [b.model_dump() if hasattr(b, 'model_dump') else b
             for b in unified_response.content]
        )
        asset_json = extract_asset_ids(decrypted.get("messages", []))

        async with async_session() as session:
            await record_usage(
                session=session,
                request_id=unified_response.id,
                model_name=normalized.model,
                model_id=routing_decision.model_id if routing_decision else None,
                provider=(
                    registry.get_provider_name(normalized.model)
                    if unified_response.error is None
                    else "unknown"
                ),
                status="error" if unified_response.error else "success",
                task_type=normalized.task_type.value,
                workflow_id=str(uuid.uuid4()),
                input_modality=input_mod,
                output_modality=output_mod,
                prompt_tokens=(
                    unified_response.usage.prompt_tokens
                    if unified_response.usage else 0
                ),
                completion_tokens=(
                    unified_response.usage.completion_tokens
                    if unified_response.usage else 0
                ),
                total_tokens=(
                    unified_response.usage.total_tokens
                    if unified_response.usage else 0
                ),
                response_time_ms=response_time_ms,
                error_message=unified_response.error,
                conversation_id=task_req.conversation_id,
                client_id=task_req.client_id,
                group_id=task_req.group_id,
                asset_ids=asset_json,
                routing_decision=routing_json,
            )
    except Exception as e:
        logger.error(f"Failed to record usage: client={task_req.client_id or 'anonymous'} — {e}")


async def _record_policy_usage(
    client_id: Optional[str],
    unified_response: UnifiedResponse,
) -> None:
    """Record token usage against the client's policy counters (never raises)."""
    if not client_id:
        return
    try:
        from app.policy.enforcer import PolicyEnforcer
        from app.database import async_session
        async with async_session() as policy_session:
            enforcer = PolicyEnforcer(policy_session)
            await enforcer.record_usage(
                client_id=client_id,
                tokens_used=(
                    unified_response.usage.total_tokens
                    if unified_response.usage else 0
                ),
            )
    except Exception as e:
        logger.error(f"Failed to record policy usage: client={client_id} — {e}")


async def _trace_request(
    *,
    registry,
    request_id: str,
    decrypted: dict,
    normalized: NormalizedTaskRequest,
    unified_response: UnifiedResponse,
    routing_decision,
    client_id: Optional[str],
) -> None:
    """Log a full request trace (never raises)."""
    try:
        from app.logs.tracer import trace_request
        converted = None
        try:
            provider_inst = registry._get_provider(normalized.model)
            if hasattr(provider_inst, '_convert_messages'):
                converted = provider_inst._convert_messages(normalized.messages)
        except Exception as ex:
            logger.debug(
                f"Trace: client={client_id} failed to capture converted "
                f"format for {normalized.model}: {ex}"
            )
        await trace_request(
            request_id=request_id,
            original=decrypted,
            converted=converted,
            response=unified_response.model_dump(),
            task_type=normalized.task_type.value if normalized.task_type else None,
            model_name=normalized.model,
            model_id=routing_decision.model_id if routing_decision else None,
            provider=registry.get_provider_name(normalized.model),
            status="error" if unified_response.error else "success",
            client_id=client_id,
            api_key_prefix=_resolve_api_key_prefix(normalized.model, registry),
        )
    except Exception as e:
        logger.error(f"Failed to trace request: client={client_id or 'anonymous'} — {e}")


def _release_image_data(
    normalized: NormalizedTaskRequest,
    task_req: Optional[TaskRequest],
) -> None:
    """Null out image payloads so GC can collect before encrypt (in place)."""
    if normalized.messages:
        for msg in normalized.messages:
            if hasattr(msg, 'content'):
                msg.content = [
                    b for b in msg.content if isinstance(b, TextContent)
                ]
    if task_req is not None and task_req.messages:
        task_req.messages = None


# ── Request handler ─────────────────────────────────────────────────────

async def _handle_parse(
    decrypted: dict,
) -> tuple[TaskRequest, NormalizedTaskRequest]:
    """Parse a decrypted payload into a TaskRequest.

    Falls back to the legacy model-only format for backward compatibility.

    Raises:
        HTTPException(400): if neither schema matches.
    """
    try:
        return TaskRequest(**decrypted)
    except Exception as parse_error:
        try:
            legacy = UnifiedRequest(**decrypted)
            logger.info(
                f"Legacy request: client={decrypted.get('client_id', 'anonymous')} "
                f"model={legacy.model} — normalizing to chat_with_context"
            )
            return TaskRequest(
                task_type=TaskType.CHAT_WITH_CONTEXT,
                messages=legacy.messages,
                parameters=legacy.parameters,
                client_id=decrypted.get("client_id"),
                model=legacy.model,
            )
        except Exception:
            logger.warning(
                f"Request parsing failed: "
                f"client={decrypted.get('client_id', 'anonymous')} — {parse_error}"
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid request format. Expected a 'task_type' field "
                    f"(one of: {[t.value for t in TaskType]}) or a legacy "
                    f"'model' field. Error: {parse_error}"
                ),
            )


async def _handle_routing(
    request: Request,
    task_req: TaskRequest,
    group_id_for_routing: Optional[int],
) -> tuple[NormalizedTaskRequest, object]:
    """Resolve routing (group override, then router) into a normalized request.

    Raises:
        NoModelAvailableError: propagated to the caller's except handler.
        HTTPException(409) style group errors are surfaced as
        GROUP_ROUTING_MISCONFIGURED envelopes upstream.
    """
    from app.models.router import ModelRouter, _RoutingContext

    router: ModelRouter = request.app.state.router

    if group_id_for_routing:
        group_model = await router.get_group_override(
            group_id_for_routing, task_req.task_type
        )
        if group_model:
            task_req.model = group_model
            logger.debug(
                f"GROUP OVERRIDE: client={task_req.client_id} "
                f"group={group_id_for_routing} "
                f"task={task_req.task_type.value} → model={group_model}"
            )
        else:
            assigned = await router.get_group_assignment(
                group_id_for_routing, task_req.task_type
            )
            model_ref = f"'{assigned}'" if assigned else "none"
            raise GroupRoutingMisconfigured(
                task_type=task_req.task_type,
                assigned=assigned or "none",
                detail=(
                    f"Group routing error: task '{task_req.task_type.value}' "
                    f"is not configured for this group. "
                    f"Assigned model: {model_ref}. "
                    f"Go to Admin → Group Routing and assign a valid model."
                ),
            )

    routing_ctx = _RoutingContext.from_task_request(task_req)
    decision = router.route(routing_ctx)

    logger.info(
        f"Routed: client={task_req.client_id or 'anonymous'} "
        f"task={task_req.task_type.value} → "
        f"model={decision.model} ({decision.provider}/{decision.model_id}) "
        f"match={decision.match_type.value}"
        + (f" relaxed={decision.relaxed_constraints}" if decision.relaxed_constraints else "")
    )

    normalized = NormalizedTaskRequest(
        task_type=task_req.task_type,
        model=decision.model,
        messages=task_req.messages,
        parameters=task_req.parameters,
        output_type=task_req.output_type,
        plan_tier=task_req.plan_tier,
        cost_class=task_req.cost_class,
        preferred_provider=task_req.preferred_provider,
    )
    return normalized, decision


class GroupRoutingMisconfigured(Exception):
    """A client's group has no usable model assignment for the task."""

    def __init__(self, *, task_type: TaskType, assigned: str, detail: str):
        self.task_type = task_type
        self.assigned = assigned
        self.detail = detail
        super().__init__(detail)


@router.post("/request", response_model=EncryptedResponse)
async def handle_request(encrypted_body: EncryptedRequest, request: Request):
    """Handle an encrypted AI model request (see module docstring for the flow)."""
    key_manager = request.app.state.key_manager
    start_time = time.time()
    decision = None

    # ── 1. Decrypt (protocol error → plain 400) ─────────────────────
    try:
        decrypted, session_key = decrypt_request(
            encrypted_body.model_dump(), key_manager
        )
    except Exception as e:
        logger.error(f"Decryption failed: {e}")  # client_id unknown at this stage
        raise HTTPException(status_code=400, detail=f"Decryption failed: {str(e)}")

    # ── 2. Parse (protocol error → plain 400) ───────────────────────
    task_req = await _handle_parse(decrypted)

    logger.info(
        f"Task request: client={task_req.client_id or 'anonymous'} "
        f"type={task_req.task_type.value}"
        + (f" output={task_req.output_type.value}" if task_req.output_type else "")
        + (f" tier={task_req.plan_tier.value}" if task_req.plan_tier else "")
        + (f" cost={task_req.cost_class.value}" if task_req.cost_class else "")
        + (f" provider={task_req.preferred_provider}" if task_req.preferred_provider else "")
        + (f" model_override={task_req.model}" if task_req.model else "")
    )

    # ── 2b. Client validation + group lookup (one DB pass) ──────────
    client_info, group_id_for_routing = await _resolve_client_context(
        task_req.client_id
    )

    # ── 2c. Async fork — job reference is encrypted exactly once ────
    from app.jobs.manager import (
        should_use_async, create_job as create_async_job, process_job_background,
    )
    if should_use_async(task_req.task_type.value, force_async=task_req.async_mode):
        job_id = await create_async_job(
            task_type=task_req.task_type.value,
            request_json=json.dumps(decrypted),
            client_id=task_req.client_id,
            session_key=session_key,
        )
        import asyncio
        asyncio.create_task(process_job_background(
            job_id=job_id,
            decrypted_request=decrypted,
            key_manager=key_manager,
            app_state=request.app.state,
        ))
        pending_body = {
            "job_id": job_id,
            "status": "pending",
            "task_type": task_req.task_type.value,
            "message": "Job queued. Poll GET /api/v1/jobs/{job_id} for status.",
        }
        return EncryptedResponse(**encrypt_response(pending_body, session_key))

    normalized: Optional[NormalizedTaskRequest] = None

    try:
        # ── 2d. Policy (unified envelope on violation) ───────────────
        try:
            await _enforce_policy(task_req, task_req.client_id)
        except Exception as e:
            err_response = _build_error(
                e, "POLICY_VIOLATION",
                task_type=task_req.task_type,
            )
            return await finish_error_response(
                err_response,
                session_key=session_key,
                original=_sanitize_for_trace(decrypted),
                task_type=task_req.task_type.value,
                client_id=task_req.client_id,
            )

        # ── 3. Route (+ group override) ─────────────────────────────
        try:
            normalized, decision = await _handle_routing(
                request, task_req, group_id_for_routing
            )
        except NoModelAvailableError as e:
            logger.warning(f"No model available: client={task_req.client_id} — {e}")
            err_response = _build_error(
                e, "NO_MODEL_AVAILABLE",
                task_type=task_req.task_type,
            )
            return await finish_error_response(
                err_response,
                session_key=session_key,
                original=_sanitize_for_trace(decrypted),
                task_type=task_req.task_type.value,
                client_id=task_req.client_id,
            )
        except GroupRoutingMisconfigured as e:
            logger.warning(f"Group routing misconfigured: client={task_req.client_id} — {e.detail}")
            err_response = _build_error(
                e, "GROUP_ROUTING_MISCONFIGURED",
                task_type=e.task_type,
                model=e.assigned,
            )
            return await finish_error_response(
                err_response,
                session_key=session_key,
                original=_sanitize_for_trace(decrypted),
                task_type=e.task_type.value,
                client_id=task_req.client_id,
            )

        # ── 3a. Workflow preprocessing ──────────────────────────────
        edit_source_count = await _apply_workflows_pre(task_req, normalized)
        if normalized is None or edit_source_count is None:
            return None

        compare_options = (
            task_req.compare_options
            if normalized.task_type == TaskType.IMAGE_COMPARE
            else None
        )
        edit_options = (
            task_req.edit_options
            if normalized.task_type == TaskType.IMAGE_EDIT
            else None
        )
    except Exception as e:
        # Workflow validation failures are envelope errors; anything else
        # is an unexpected bug — surface as a unified error too.
        code = getattr(e, "error_code", None) or (
            "WORKFLOW_VALIDATION_FAILED"
            if type(e).__name__.endswith(("ValidationError",))
            else "PROVIDER_ERROR"
        )
        err_response = _build_error(
            e, code,
            task_type=getattr(normalized, "task_type", None) or task_req.task_type,
            model=getattr(normalized, "model", "none"),
        )
        err_response.error_code = code
        return await finish_error_response(
            err_response,
            session_key=session_key,
            original=_sanitize_for_trace(decrypted),
            task_type=(
                normalized.task_type.value
                if normalized and normalized.task_type
                else task_req.task_type.value
            ),
            client_id=task_req.client_id,
        )

    # ── 4. Forward to provider ──────────────────────────────────────
    registry = request.app.state.registry
    try:
        unified_response = await registry.generate(normalized)
    except ValueError as e:
        logger.warning(f"Provider routing error: client={task_req.client_id} — {e}")
        unified_response = UnifiedResponse(
            task_type=normalized.task_type,
            model=normalized.model,
            content=[],
            error=str(e),
        )
    except Exception as e:
        logger.error(f"Provider error: client={task_req.client_id} model={normalized.model} — {e}")
        unified_response = UnifiedResponse(
            task_type=normalized.task_type,
            model=normalized.model,
            content=[],
            error=f"Provider error: {str(e)}",
            error_code="PROVIDER_ERROR",
        )

    # ── 4a. Workflow postprocessing ─────────────────────────────────
    await _apply_workflows_post(
        normalized,
        unified_response,
        compare_options=compare_options,
        edit_options=edit_options,
        edit_source_count=edit_source_count,
        client_id=task_req.client_id,
    )

    response_time_ms = int((time.time() - start_time) * 1000)

    # ── Release image data from memory; trace gets a text-only copy ──
    _release_image_data(normalized, task_req)
    decrypted_for_trace = _sanitize_for_trace(decrypted)

    # ── 5. Observability (each step never raises) ───────────────────
    await _record_usage(
        registry=registry,
        decrypted=decrypted,
        normalized=normalized,
        unified_response=unified_response,
        routing_decision=decision,
        response_time_ms=response_time_ms,
        task_req=task_req,
    )
    await _record_policy_usage(task_req.client_id, unified_response)
    await _trace_request(
        registry=registry,
        request_id=unified_response.id,
        decrypted=decrypted_for_trace,
        normalized=normalized,
        unified_response=unified_response,
        routing_decision=decision,
        client_id=task_req.client_id,
    )

    # ── 6. Encrypt response ─────────────────────────────────────────
    try:
        encrypted = encrypt_response(unified_response.model_dump(), session_key)
        return EncryptedResponse(**encrypted)
    except Exception as e:
        logger.error(f"Encryption failed: client={task_req.client_id or 'anonymous'} — {e}")
        raise HTTPException(status_code=500, detail=f"Encryption failed: {str(e)}")


# ── Client validation helpers ────────────────────────────────────────────

async def _load_client(session, client_id: Optional[str]):
    """Load the Client row for client_id, raising HTTPException on problems."""
    from app.stats.models import Client as ClientModel
    from sqlalchemy import select as _sel

    if not client_id:
        raise HTTPException(
            status_code=403,
            detail="client_id is required. Register first: GET /api/v1/register",
        )

    result = await session.execute(
        _sel(ClientModel).where(ClientModel.client_key == client_id)
    )
    client = result.scalar_one_or_none()

    if client is None:
        raise HTTPException(
            status_code=403,
            detail=f"Unknown client_id '{client_id}'. Register first: GET /api/v1/register",
        )

    if not client.is_active:
        raise HTTPException(
            status_code=403,
            detail=f"Client '{client_id}' is blocked. Contact admin.",
        )

    return client


async def _resolve_client_context(client_id: Optional[str]) -> tuple[dict, Optional[int]]:
    """Validate client_id and return (client_info, group_id) in one DB pass."""
    from app.database import async_session

    async with async_session() as session:
        client = await _load_client(session, client_id)
        return (
            {"client_key": client.client_key, "plan": client.plan},
            client.client_group_id,
        )


# ── Client registration ──────────────────────────────────────────────────

@router.get("/register")
async def register_client(request: Request):
    """Register a new API client. Returns a unique client_key for use in all requests.

    Every client must register before using the API. Free plan = unlimited access.
    """
    from app.database import async_session
    from app.stats.models import Client, ClientGroup
    from sqlalchemy import select as _sel

    client_key = f"cl_{uuid.uuid4().hex[:16]}"

    async with async_session() as session:
        default_group = (await session.execute(
            _sel(ClientGroup).where(ClientGroup.group_key == "default")
        )).scalar_one_or_none()

        client = Client(
            client_key=client_key,
            plan="free",
            is_active=True,
            client_group_id=default_group.id if default_group else None,
        )
        session.add(client)
        await session.commit()

    try:
        from app.stats.tracker import record_usage
        async with async_session() as s:
            await record_usage(
                session=s,
                request_id=f"reg_{client_key}",
                model_name="none",
                provider="none",
                status="success",
                task_type="register",
                input_modality="none",
                output_modality="none",
                response_time_ms=0,
            )
    except Exception:
        pass

    try:
        from app.logs.tracer import trace_request
        await trace_request(
            request_id=f"reg_{client_key}",
            original={"action": "register", "client_id": client_key, "plan": "free"},
            response={"client_id": client_key, "plan": "free", "status": "registered"},
            task_type="register",
            model_name="none",
            provider="none",
            status="success",
            api_key_prefix=None,
        )
    except Exception:
        pass

    return {
        "client_id": client_key,
        "plan": "free",
        "message": "Client registered. Send this client_id in every request.",
    }


# ── Job endpoints ────────────────────────────────────────────────────────

@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str, request: Request):
    """Poll job status and retrieve result when complete.

    Returns the job status, progress, and result (if completed). The response
    is encrypted with the session key from the original /request (stored on the
    job), so the client — which still holds that key — can decrypt it.
    """
    import os as _os
    from app.jobs.manager import get_job_orm, _job_to_dict

    job_obj = await get_job_orm(job_id)
    if job_obj is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    # Reuse the original request's session key; fall back to a random key for
    # jobs created before this key was stored (undecryptable, but no worse than before).
    session_key = job_obj.session_key or _os.urandom(32)
    encrypted = encrypt_response(_job_to_dict(job_obj), session_key)
    return EncryptedResponse(**encrypted)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job_endpoint(job_id: str, request: Request):
    """Cancel a pending or processing job."""
    import os as _os
    from app.jobs.manager import cancel_job, get_job_orm

    ok = await cancel_job(job_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job_id}' cannot be cancelled (already completed or not found)",
        )

    job_obj = await get_job_orm(job_id)
    # Reuse the original request's session key so the client can decrypt the
    # cancellation acknowledgement; fall back to random for legacy jobs.
    session_key = (job_obj.session_key if job_obj else None) or _os.urandom(32)
    encrypted = encrypt_response(
        {"job_id": job_id, "status": "cancelled", "message": "Job cancelled."},
        session_key,
    )
    return EncryptedResponse(**encrypted)