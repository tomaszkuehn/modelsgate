"""API endpoints for the AI backend."""

import json
import time
import logging
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


# ── Public key ──────────────────────────────────────────────────────────

@router.get("/public-key", response_model=PublicKeyResponse)
async def get_public_key(request: Request):
    """Return the server's RSA public key for encrypting session keys."""
    key_manager = request.app.state.key_manager
    return PublicKeyResponse(
        public_key=key_manager.public_key_pem,
    )


# ── Request handler ─────────────────────────────────────────────────────

@router.post("/request", response_model=EncryptedResponse)
async def handle_request(encrypted_body: EncryptedRequest, request: Request):
    """Handle an encrypted AI model request.

    1. Decrypt the outer envelope
    2. Parse as TaskRequest (new) or UnifiedRequest (legacy backward compat)
    3. Route through ModelRouter — resolves task_type + constraints → best model
    4. Forward to the selected provider via the registry
    5. Record usage statistics
    6. Encrypt and return the response
    """
    key_manager = request.app.state.key_manager
    start_time = time.time()
    routing_decision_obj = None  # captured for usage logging

    # ── 1. Decrypt ──────────────────────────────────────────────────
    try:
        decrypted, session_key = decrypt_request(
            encrypted_body.model_dump(), key_manager
        )
    except Exception as e:
        logger.error(f"Decryption failed: {e}")  # client_id unknown at this stage
        raise HTTPException(status_code=400, detail=f"Decryption failed: {str(e)}")

    # ── 2. Parse ────────────────────────────────────────────────────
    normalized: NormalizedTaskRequest

    try:
        # New task-based request with optional routing constraints
        task_req = TaskRequest(**decrypted)
        logger.info(
            f"Task request: client={task_req.client_id or 'anonymous'} "
            f"type={task_req.task_type.value}"
            + (f" output={task_req.output_type.value}" if task_req.output_type else "")
            + (f" tier={task_req.plan_tier.value}" if task_req.plan_tier else "")
            + (f" cost={task_req.cost_class.value}" if task_req.cost_class else "")
            + (f" provider={task_req.preferred_provider}" if task_req.preferred_provider else "")
            + (f" model_override={task_req.model}" if task_req.model else "")
        )

        # ── 2b. Client validation ───────────────────────────────────
        from app.database import async_session as _as

        async with _as() as _val_session:
            await validate_client_id(_val_session, task_req.client_id)

        # ── 2b2. Group routing override ────────────────────────────
        _group_id_for_routing = None
        if task_req.client_id:
            async with _as() as _g_sess:
                from app.stats.models import Client
                from sqlalchemy import select as _gsel
                _client_row = (await _g_sess.execute(
                    _gsel(Client).where(Client.client_key == task_req.client_id)
                )).scalar_one_or_none()
                if _client_row and _client_row.client_group_id:
                    _group_id_for_routing = _client_row.client_group_id
                    logger.warning(
                        f"GROUP ROUTING: client={task_req.client_id} "
                        f"group_id={_group_id_for_routing}"
                    )
                else:
                    logger.warning(
                        f"GROUP ROUTING FAIL: client={task_req.client_id} "
                        f"found={_client_row is not None} "
                        f"group_id={_client_row.client_group_id if _client_row else 'N/A'}"
                    )

        # ── 2c. Async mode decision ───────────────────────────────
        from app.jobs.manager import should_use_async, create_job as create_async_job, process_job_background

        use_async = should_use_async(
            task_req.task_type.value,
            force_async=task_req.async_mode,
        )

        if use_async:
            import asyncio as _asyncio
            job_id = await create_async_job(
                task_type=task_req.task_type.value,
                request_json=json.dumps(decrypted),
                client_id=task_req.client_id,
            )
            # Spawn background processing
            _asyncio.create_task(
                process_job_background(
                    job_id=job_id,
                    decrypted_request=decrypted,
                    key_manager=key_manager,
                    app_state=request.app.state,
                )
            )
            # Return job reference immediately
            return EncryptedResponse(
                encrypted_payload=encrypt_response(
                    {
                        "job_id": job_id,
                        "status": "pending",
                        "task_type": task_req.task_type.value,
                        "message": "Job queued. Poll GET /api/v1/jobs/{job_id} for status.",
                    },
                    session_key,
                )["encrypted_payload"],
                nonce=encrypt_response(
                    {"job_id": job_id, "status": "pending"},
                    session_key,
                )["nonce"],
            )

        # ── 2c. Policy enforcement ────────────────────────────────
        from app.policy.enforcer import PolicyEnforcer, PolicyViolationError
        from app.database import async_session as _async_sess

        async with _async_sess() as policy_session:
            enforcer = PolicyEnforcer(policy_session)
            resolved = await enforcer.resolve_policy(task_req.client_id)

            # Count images for capability checks
            _img_count = sum(
                1 for msg in task_req.messages
                for block in msg.content
                if hasattr(block, 'type') and block.type == 'image'
            )

            # Validate against policy
            try:
                await enforcer.validate_request(
                    policy=resolved,
                    task_type=task_req.task_type,
                    image_count=_img_count,
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
                logger.warning(f"Policy violation: client={task_req.client_id} — {e}")
                err_response = UnifiedResponse(
                    task_type=task_req.task_type,
                    model="none",
                    content=[],
                    error=str(e),
                    error_code="POLICY_VIOLATION",
                )
                encrypted = encrypt_response(
                    err_response.model_dump(), session_key
                )
                return EncryptedResponse(**encrypted)

            # Apply policy constraints (tightens routing fields)
            enforcer.apply_policy_constraints(resolved, task_req)

        # ── 3. Route (select best model) ──────────────────────────
        from app.models.router import ModelRouter, _RoutingContext

        router: ModelRouter = request.app.state.router

        # Check for group-level routing override
        if _group_id_for_routing:
            group_model = await router.get_group_override(
                _group_id_for_routing, task_req.task_type
            )
            if group_model:
                task_req.model = group_model
                logger.warning(
                    f"GROUP OVERRIDE: client={task_req.client_id} "
                    f"group={_group_id_for_routing} "
                    f"task={task_req.task_type.value} → model={group_model}"
                )
            else:
                # Client is in a group — all tasks must be explicitly assigned. No fallback.
                assigned = await router.get_group_assignment(
                    _group_id_for_routing, task_req.task_type
                )
                model_ref = f"'{assigned}'" if assigned else "none"
                err_response = UnifiedResponse(
                    task_type=task_req.task_type,
                    model=assigned or "none",
                    content=[],
                    error=(
                        f"Group routing error: task '{task_req.task_type.value}' "
                        f"is not configured for this group. "
                        f"Assigned model: {model_ref}. "
                        f"Go to Admin → Group Routing and assign a valid model."
                    ),
                    error_code="GROUP_ROUTING_MISCONFIGURED",
                )
                encrypted = encrypt_response(err_response.model_dump(), session_key)
                return EncryptedResponse(**encrypted)

        routing_ctx = _RoutingContext.from_task_request(task_req)
        decision = router.route(routing_ctx)
        routing_decision_obj = decision  # capture for logging

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

    except Exception as _e:
        if isinstance(_e, NoModelAvailableError):
            logger.warning(f"No model available: client={task_req.client_id if task_req else 'unknown'} — {_e}")
            err_response = UnifiedResponse(
                task_type=decrypted.get("task_type", TaskType.CHAT_WITH_CONTEXT),
                model="none",
                content=[],
                error=str(_e),
                error_code="NO_MODEL_AVAILABLE",
            )
            encrypted = encrypt_response(err_response.model_dump(), session_key)
            return EncryptedResponse(**encrypted)

        parse_error = _e
        # ── Backward compat ──
        # ── Backward compat: old UnifiedRequest (model-only) ────────
        try:
            legacy = UnifiedRequest(**decrypted)
            logger.info(
                f"Legacy request: client={decrypted.get('client_id', 'anonymous')} "
                f"model={legacy.model} — "
                f"normalizing to chat_with_context"
            )
            normalized = NormalizedTaskRequest(
                task_type=TaskType.CHAT_WITH_CONTEXT,
                model=legacy.model,
                messages=legacy.messages,
                parameters=legacy.parameters,
            )
        except Exception:
            logger.warning(f"Request parsing failed: client={decrypted.get('client_id', 'anonymous')} — {parse_error}")
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid request format. Expected a 'task_type' field "
                    f"(one of: {[t.value for t in TaskType]}) or a legacy "
                    f"'model' field. Error: {parse_error}"
                ),
            )

    # ── 3a. Workflow preprocessing ──────────────────────────────────
    # Workflows enhance messages and/or parse structured results
    # for specific task types before the provider is called.
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
    compare_result: Optional[ImageCompareResult] = None  # type: ignore[name-defined]
    edit_result: Optional[ImageEditResult] = None  # type: ignore[name-defined]
    _edit_source_count: int = 0  # carried from pre- to post-processing

    if normalized.task_type == TaskType.IMAGE_COMPARE:
        from app.workflows.image_compare import (
            execute_image_compare,
            WorkflowValidationError,
        )
        try:
            _, _ = await execute_image_compare(
                task_req, None, normalized
            )
        except WorkflowValidationError as e:
            logger.warning(f"image_compare validation failed: client={task_req.client_id} — {e}")
            err_response = UnifiedResponse(
                task_type=normalized.task_type,
                model=normalized.model,
                content=[],
                error=str(e),
                error_code="WORKFLOW_VALIDATION_FAILED",
            )
            encrypted = encrypt_response(err_response.model_dump(), session_key)
            return EncryptedResponse(**encrypted)

    if normalized.task_type == TaskType.IMAGE_EDIT:
        from app.workflows.image_edit import (
            execute_image_edit,
            WorkflowEditValidationError,
        )
        try:
            _, _edit_source_count = await execute_image_edit(
                task_req, None, normalized
            )
        except WorkflowEditValidationError as e:
            logger.warning(f"image_edit validation failed: client={task_req.client_id} — {e}")
            err_response = UnifiedResponse(
                task_type=normalized.task_type,
                model=normalized.model,
                content=[],
                error=str(e),
                error_code="WORKFLOW_VALIDATION_FAILED",
            )
            encrypted = encrypt_response(err_response.model_dump(), session_key)
            return EncryptedResponse(**encrypted)

    # ── 4. Forward to provider ──────────────────────────────────────
    from app.models.registry import ModelRegistry
    registry: ModelRegistry = request.app.state.registry

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
    # Extract structured comparison results from the model response.
    if normalized.task_type == TaskType.IMAGE_COMPARE and compare_options:
        from app.workflows.image_compare import finalize_image_compare
        # Gather all text from the response
        response_text = ""
        for block in unified_response.content:
            if isinstance(block, TextContent):
                response_text += block.text
        compare_result = finalize_image_compare(response_text, compare_options)
        if compare_result:
            unified_response.compare_result = compare_result
            logger.info(
                f"image_compare: client={task_req.client_id or 'anonymous'} "
                f"extracted structured result "
                f"({len(compare_result.similarities)} similarities, "
                f"{len(compare_result.differences)} differences)"
            )

    # ── 4b. Image edit postprocessing ────────────────────────────────
    # Build edit metadata from the provider's response.
    if normalized.task_type == TaskType.IMAGE_EDIT:
        from app.workflows.image_edit import finalize_image_edit
        response_text = ""
        for block in unified_response.content:
            if isinstance(block, TextContent):
                response_text += block.text
        edit_result = finalize_image_edit(
            content_blocks=unified_response.content,
            options=edit_options,
            source_image_count=_edit_source_count,
            response_text=response_text,
        )
        unified_response.edit_result = edit_result
        logger.info(
            f"image_edit: client={task_req.client_id or 'anonymous'} "
            f"{edit_result.edited_images} edited image(s) "
            f"from {edit_result.source_images_used} source(s)"
        )

    response_time_ms = int((time.time() - start_time) * 1000)

    # ── Release image data from memory after provider call ────────────
    # Base64 images can be large — null them so GC can collect before
    # the slower usage/trace/encrypt steps.
    if normalized.messages:
        for msg in normalized.messages:
            if hasattr(msg, 'content'):
                msg.content = [b for b in msg.content if isinstance(b, TextContent)]
    if task_req is not None and task_req.messages:
        task_req.messages = None
    # Strip base64 image data from decrypted dict before trace duplicates it
    _messages_for_trace = decrypted.get("messages", [])
    for _m in _messages_for_trace:
        if isinstance(_m, dict) and "content" in _m and isinstance(_m["content"], list):
            _m["content"] = [
                c for c in _m["content"]
                if not (isinstance(c, dict) and (c.get("image") or c.get("image_url") or c.get("type") == "image_url"))
            ]

    # ── 5. Record usage stats ───────────────────────────────────────
    try:
        import uuid as _uuid
        from app.stats.tracker import (
            record_usage,
            compute_input_modality,
            compute_output_modality,
            extract_asset_ids,
        )
        from app.database import async_session

        # Serialize routing decision
        routing_json = None
        if routing_decision_obj is not None:
            try:
                import json as _json
                routing_json = _json.dumps(
                    routing_decision_obj.model_dump(), default=str
                )
            except Exception:
                pass

        # Compute modality summaries from raw message dicts
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
                model_id=routing_decision_obj.model_id if routing_decision_obj else None,
                provider=(
                    registry.get_provider_name(normalized.model)
                    if unified_response.error is None
                    else "unknown"
                ),
                status="error" if unified_response.error else "success",
                task_type=normalized.task_type.value,
                workflow_id=str(_uuid.uuid4()),
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

    # ── 5b. Record policy usage ──────────────────────────────────────
    if task_req.client_id:
        try:
            async with _async_sess() as policy_session:
                enforcer2 = PolicyEnforcer(policy_session)
                await enforcer2.record_usage(
                    client_id=task_req.client_id,
                    tokens_used=(
                        unified_response.usage.total_tokens
                        if unified_response.usage else 0
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to record policy usage: client={task_req.client_id} — {e}")

    # ── 5c. Trace request log ────────────────────────────────────────
    try:
        from app.logs.tracer import trace_request
        converted = None
        # Attempt to capture provider-converted format
        if normalized.task_type and normalized.model:
            try:
                provider_inst = registry._get_provider(normalized.model)
                if hasattr(provider_inst, '_convert_messages'):
                    converted = provider_inst._convert_messages(normalized.messages)
                    logger.info(
                        f"Trace converted: client={task_req.client_id} "
                        f"model={normalized.model} "
                        f"keys={list(converted[0].keys()) if converted else 'empty'}"
                    )
                else:
                    logger.warning(
                        f"Trace: client={task_req.client_id} "
                        f"provider for {normalized.model} has no _convert_messages"
                    )
            except Exception as ex:
                logger.warning(
                    f"Trace: client={task_req.client_id} "
                    f"failed to capture converted format for "
                    f"{normalized.model}: {ex}"
                )
        await trace_request(
            request_id=unified_response.id,
            original=decrypted,
            converted=converted,
            response=unified_response.model_dump(),
            task_type=normalized.task_type.value if normalized.task_type else None,
            model_name=normalized.model,
            model_id=routing_decision_obj.model_id if routing_decision_obj else None,
            provider=registry.get_provider_name(normalized.model),
            status="error" if unified_response.error else "success",
            client_id=task_req.client_id,
            api_key_prefix=_resolve_api_key_prefix(normalized.model, registry),
        )
    except Exception as e:
        logger.error(f"Failed to trace request: client={task_req.client_id or 'anonymous'} — {e}")

    # ── 6. Encrypt response ─────────────────────────────────────────
    try:
        encrypted = encrypt_response(unified_response.model_dump(), session_key)
        return EncryptedResponse(**encrypted)
    except Exception as e:
        logger.error(f"Encryption failed: client={task_req.client_id or 'anonymous'} — {e}")
        raise HTTPException(status_code=500, detail=f"Encryption failed: {str(e)}")


# ── Client registration ──────────────────────────────────────────────────

@router.get("/register")
async def register_client(request: Request):
    """Register a new API client. Returns a unique client_key for use in all requests.

    Every client must register before using the API. Free plan = unlimited access.
    """
    import uuid as _uuid
    from app.database import async_session
    from app.stats.models import Client

    client_key = f"cl_{_uuid.uuid4().hex[:16]}"

    async with async_session() as session:
        # Get default group
        from app.stats.models import ClientGroup
        from sqlalchemy import select as _sel
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

    # Log registration to usage stats
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

    # Log to request trace
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


# ── Client validation middleware for /request ─────────────────────────────

async def validate_client_id(session, client_id: Optional[str]) -> dict:
    """Validate that a client_id is registered and active.

    Returns the client dict if valid. Raises HTTPException if invalid.
    """
    if not client_id:
        raise HTTPException(
            status_code=403,
            detail="client_id is required. Register first: GET /api/v1/register",
        )

    from app.stats.models import Client
    from sqlalchemy import select as _sel

    result = await session.execute(
        _sel(Client).where(Client.client_key == client_id)
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

    return {"client_key": client.client_key, "plan": client.plan}


# ── Job endpoints ────────────────────────────────────────────────────────

@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str, request: Request):
    """Poll job status and retrieve result when complete.

    Returns the job status, progress, and result (if completed).
    Response is encrypted with the same scheme as /request.
    """
    from app.jobs.manager import get_job

    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    key_manager = request.app.state.key_manager
    session_key = __import__("os").urandom(32)
    encrypted = encrypt_response(job, session_key)
    return EncryptedResponse(**encrypted)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job_endpoint(job_id: str, request: Request):
    """Cancel a pending or processing job."""
    from app.jobs.manager import cancel_job

    ok = await cancel_job(job_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job_id}' cannot be cancelled (already completed or not found)",
        )

    key_manager = request.app.state.key_manager
    session_key = __import__("os").urandom(32)
    encrypted = encrypt_response(
        {"job_id": job_id, "status": "cancelled", "message": "Job cancelled."},
        session_key,
    )
    return EncryptedResponse(**encrypted)
