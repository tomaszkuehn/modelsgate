"""Async job processing for long-running tasks (image_generate, image_edit).

Jobs are stored in SQLite, processed via asyncio background tasks.
The processing function reuses the existing request pipeline:
  decrypt → policy → route → provider → encrypt.

API:
  POST /api/v1/jobs              → Create job, return job_id
  GET  /api/v1/jobs/{job_id}     → Poll status + get result when done
  POST /api/v1/jobs/{job_id}/cancel → Cancel pending/processing job
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.stats.models import Job

logger = logging.getLogger(__name__)

# ── Task types that should use async by default ──────────────────────────

ASYNC_TASK_TYPES = {"image_generate", "image_edit"}


def should_use_async(task_type: str, force_async: bool = False) -> bool:
    """Determine whether a task should be processed asynchronously.

    Args:
        task_type: The task_type value.
        force_async: If True, always use async regardless of task_type.

    Returns:
        True if the task should be queued as a background job.
    """
    if force_async:
        return True
    return task_type in ASYNC_TASK_TYPES


# ── Cancellation registry ─────────────────────────────────────────────────

# In-memory set of cancelled job_ids. Checked by the worker during processing.
_cancelled_jobs: set = set()


def mark_cancelled(job_id: str):
    _cancelled_jobs.add(job_id)


def is_cancelled(job_id: str) -> bool:
    return job_id in _cancelled_jobs


def clear_cancelled(job_id: str):
    _cancelled_jobs.discard(job_id)


# ── CRUD ─────────────────────────────────────────────────────────────────

async def create_job(
    task_type: str,
    request_json: str,
    client_id: Optional[str] = None,
    session_key: Optional[bytes] = None,
) -> str:
    """Create a new job row. Returns the job_id UUID string."""
    job_id = str(uuid.uuid4())
    async with async_session() as session:
        job = Job(
            job_id=job_id,
            task_type=task_type,
            status="pending",
            request_json=request_json,
            client_id=client_id,
            progress_percent=0,
            session_key=session_key,
        )
        session.add(job)
        await session.commit()
    logger.info(f"Job created: {job_id} ({task_type}) client={client_id or 'anonymous'}")
    return job_id


async def get_job(job_id: str) -> Optional[dict]:
    """Retrieve a job's status and result."""
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.job_id == job_id)
        )
        job = result.scalar_one_or_none()
        if job is None:
            return None
        return _job_to_dict(job)


async def get_job_orm(job_id: str) -> Optional[Job]:
    """Retrieve a job's ORM object (carries session_key for encrypting poll responses)."""
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.job_id == job_id)
        )
        return result.scalar_one_or_none()


async def cancel_job(job_id: str) -> bool:
    """Cancel a pending or processing job. Returns True if cancelled."""
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.job_id == job_id)
        )
        job = result.scalar_one_or_none()
        if job is None:
            return False
        if job.status in ("pending", "processing"):
            job.status = "cancelled"
            job.completed_at = datetime.now(timezone.utc)
            await session.commit()
            mark_cancelled(job_id)
            logger.info(f"Job cancelled: {job_id}")
            return True
        return False


def _job_to_dict(job: Job) -> dict:
    """Convert a Job ORM object to a response dict."""
    d = {
        "job_id": job.job_id,
        "task_type": job.task_type,
        "status": job.status,
        "progress_percent": job.progress_percent,
        "client_id": job.client_id,
        "model_used": job.model_used,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
    if job.result_json:
        try:
            d["result"] = json.loads(job.result_json)
        except json.JSONDecodeError:
            d["result"] = {"raw": job.result_json}
    if job.error_message:
        d["error"] = job.error_message
    return d


# ── Background processing ────────────────────────────────────────────────

async def process_job_background(
    job_id: str,
    decrypted_request: dict,
    key_manager,
    app_state,
):
    """Process a job in the background using the existing request pipeline.

    This function is spawned via asyncio.create_task() after the job is created.
    It reuses the same decrypt → enforce → route → provider flow as the sync path.

    Args:
        job_id: The job UUID.
        decrypted_request: Already-decrypted TaskRequest dict.
        key_manager: Server's KeyManager for encrypting the response.
        app_state: FastAPI app.state (for router + registry access).
    """
    async with async_session() as session:
        # Mark as processing
        result = await session.execute(select(Job).where(Job.job_id == job_id))
        job = result.scalar_one_or_none()
        if job is None:
            return
        job.status = "processing"
        job.started_at = datetime.now(timezone.utc)
        job.progress_percent = 10
        await session.commit()

    try:
        # Check cancellation before starting heavy work
        if is_cancelled(job_id):
            await _fail_job(job_id, "Job was cancelled before processing")
            return

        # ── Parse request ──────────────────────────────────────────
        from app.api.schemas import (
            TaskRequest,
            NormalizedTaskRequest,
            UnifiedResponse,
            TaskType,
            TextContent,
        )
        task_req = TaskRequest(**decrypted_request)

        # ── Policy enforcement ─────────────────────────────────────
        from app.policy.enforcer import PolicyEnforcer, PolicyViolationError
        async with async_session() as policy_session:
            enforcer = PolicyEnforcer(policy_session)
            resolved = await enforcer.resolve_policy(task_req.client_id)
            try:
                img_count = sum(
                    1 for msg in task_req.messages
                    for block in msg.content
                    if hasattr(block, 'type') and block.type == 'image'
                )
                await enforcer.validate_request(
                    policy=resolved, task_type=task_req.task_type,
                    image_count=img_count,
                    requested_tokens=(
                        task_req.parameters.max_tokens
                        if task_req.parameters and task_req.parameters.max_tokens
                        else 1024
                    ),
                )
                enforcer.apply_policy_constraints(resolved, task_req)
            except PolicyViolationError as e:
                await _fail_job(job_id, str(e))
                return

        # Progress update
        await _update_progress(job_id, 20)

        # ── Route ──────────────────────────────────────────────────
        from app.models.router import ModelRouter, _RoutingContext
        router: ModelRouter = app_state.router
        routing_ctx = _RoutingContext.from_task_request(task_req)
        decision = router.route(routing_ctx)

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

        # ── Workflow preprocessing (image_edit) ──────────────────
        # Mirror the sync path: validate inputs and inject the edit system
        # prompt so edit_options (style_guidance/output_format/...) are
        # honored. Without this the async path sends raw messages and
        # silently drops edit_options.
        edit_options = (
            task_req.edit_options
            if normalized.task_type == TaskType.IMAGE_EDIT
            else None
        )
        _edit_source_count: int = 0
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
                await _fail_job(job_id, f"image_edit validation failed: {e}")
                return

        # Update model_used
        async with async_session() as session:
            r = await session.execute(select(Job).where(Job.job_id == job_id))
            j = r.scalar_one_or_none()
            if j:
                j.model_used = decision.model
                await session.commit()

        await _update_progress(job_id, 30)

        # Check cancellation
        if is_cancelled(job_id):
            await _fail_job(job_id, "Job was cancelled before provider call")
            return

        # ── Call provider ───────────────────────────────────────────
        from app.models.registry import ModelRegistry
        registry: ModelRegistry = app_state.registry

        start_time = time.time()
        try:
            unified_response = await registry.generate(normalized)
        except Exception as e:
            await _fail_job(job_id, f"Provider error: {e}")
            return

        await _update_progress(job_id, 80)

        # ── Workflow postprocessing (image_edit) ─────────────────
        # Build edit_result metadata from the provider's response so the
        # client receives structured edit info (mirror sync path).
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

        # ── Record usage ────────────────────────────────────────────
        try:
            from app.stats.tracker import record_usage, compute_input_modality, compute_output_modality, extract_asset_ids
            async with async_session() as sess:
                await record_usage(
                    session=sess,
                    request_id=unified_response.id,
                    model_name=normalized.model,
                    model_id=decision.model_id,
                    provider=registry.get_provider_name(normalized.model),
                    status="error" if unified_response.error else "success",
                    task_type=normalized.task_type.value,
                    workflow_id=str(uuid.uuid4()),
                    input_modality=compute_input_modality(decrypted_request.get("messages", [])),
                    output_modality=compute_output_modality(
                        [b.model_dump() if hasattr(b, 'model_dump') else b for b in unified_response.content]
                    ),
                    prompt_tokens=unified_response.usage.prompt_tokens if unified_response.usage else 0,
                    completion_tokens=unified_response.usage.completion_tokens if unified_response.usage else 0,
                    total_tokens=unified_response.usage.total_tokens if unified_response.usage else 0,
                    response_time_ms=int((time.time() - start_time) * 1000),
                    error_message=unified_response.error,
                    client_id=task_req.client_id,
                    routing_decision=json.dumps(decision.model_dump(), default=str),
                )
                if task_req.client_id:
                    enforcer2 = PolicyEnforcer(sess)
                    await enforcer2.record_usage(
                        client_id=task_req.client_id,
                        tokens_used=unified_response.usage.total_tokens if unified_response.usage else 0,
                    )
        except Exception as e:
            logger.error(f"Job usage recording failed: client={task_req.client_id or 'anonymous'} job={job_id} — {e}")

        # ── Trace request log ───────────────────────────────────────
        # Mirrors the sync path so image_generate/image_edit jobs appear
        # in /admin/logs (previously only usage stats were recorded).
        try:
            from app.logs.tracer import trace_request
            await trace_request(
                request_id=unified_response.id,
                original=decrypted_request,
                response=unified_response.model_dump(),
                task_type=normalized.task_type.value,
                model_name=normalized.model,
                model_id=decision.model_id,
                provider=registry.get_provider_name(normalized.model),
                status="error" if unified_response.error else "success",
                client_id=task_req.client_id,
                api_key_prefix=None,
            )
        except Exception as e:
            logger.error(f"Job trace failed: job={job_id} — {e}")

        # ── Complete ────────────────────────────────────────────────
        await _complete_job(job_id, unified_response.model_dump())

    except Exception as e:
        logger.error(f"Job {job_id} failed: client={decrypted_request.get('client_id', 'anonymous')} — {e}")
        await _fail_job(job_id, str(e))
    finally:
        clear_cancelled(job_id)


# ── Helpers ──────────────────────────────────────────────────────────────

async def _update_progress(job_id: str, percent: int):
    """Update the progress_percent on a job."""
    try:
        async with async_session() as session:
            r = await session.execute(select(Job).where(Job.job_id == job_id))
            j = r.scalar_one_or_none()
            if j:
                j.progress_percent = percent
                await session.commit()
    except Exception:
        pass


async def _complete_job(job_id: str, result: dict):
    """Mark a job as completed with its result."""
    async with async_session() as session:
        r = await session.execute(select(Job).where(Job.job_id == job_id))
        j = r.scalar_one_or_none()
        if j and j.status != "cancelled":
            j.status = "completed"
            j.result_json = json.dumps(result)
            j.progress_percent = 100
            j.completed_at = datetime.now(timezone.utc)
            await session.commit()
            logger.info(f"Job completed: {job_id} client={j.client_id or 'anonymous'}")


async def _fail_job(job_id: str, error: str):
    """Mark a job as failed and trace it so failures appear in /admin/logs.

    Every failure path (NoModelAvailable, policy violation, provider error,
    cancellation, workflow validation) routes through here, so this is the
    single place that makes failed async jobs visible in the admin log.
    """
    job_task_type: Optional[str] = None
    job_client_id: Optional[str] = None
    job_model_used: Optional[str] = None
    job_request_json: Optional[str] = None

    async with async_session() as session:
        r = await session.execute(select(Job).where(Job.job_id == job_id))
        j = r.scalar_one_or_none()
        if j and j.status != "cancelled":
            j.status = "failed"
            j.error_message = error
            j.completed_at = datetime.now(timezone.utc)
            await session.commit()
            logger.error(f"Job failed: {job_id} client={j.client_id or 'anonymous'} — {error}")
            # Capture fields for the trace before the session closes.
            job_task_type = j.task_type
            job_client_id = j.client_id
            job_model_used = j.model_used
            job_request_json = j.request_json

    # Trace the failure outside the session (avoid nesting). Only when the
    # row was actually marked failed — cancelled jobs are not failures.
    if job_task_type is not None:
        try:
            from app.logs.tracer import trace_request
            try:
                original = json.loads(job_request_json) if job_request_json else {}
            except (json.JSONDecodeError, TypeError):
                original = {"raw": job_request_json}
            await trace_request(
                request_id=job_id,
                original=original,
                response={"error": error, "status": "failed"},
                task_type=job_task_type,
                model_name=job_model_used or "none",
                provider=None,
                status="error",
                client_id=job_client_id,
                api_key_prefix=None,
            )
        except Exception as e:
            logger.error(f"Failed to trace failed job: job={job_id} — {e}")
