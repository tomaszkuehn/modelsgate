"""Usage statistics recording and querying utility."""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.stats.models import UsageLog

logger = logging.getLogger(__name__)


# ── Modality helpers ──────────────────────────────────────────────────────

def compute_input_modality(messages: list) -> str:
    """Derive an input modality summary string from request messages.

    Returns strings like: "text", "text+image", "text+2_images", "text+3_images".
    """
    image_count = 0
    has_text = False
    for msg in messages:
        for block in msg.get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                has_text = True
            elif block.get("type") == "image":
                image_count += 1

    parts = []
    if has_text:
        parts.append("text")
    if image_count == 1:
        parts.append("image")
    elif image_count > 1:
        parts.append(f"{image_count}_images")

    return "+".join(parts) if parts else "unknown"


def compute_output_modality(content_blocks: list) -> str:
    """Derive an output modality summary string from response content blocks.

    Returns strings like: "text", "image", "text+image".
    """
    has_text = False
    has_image = False
    for block in content_blocks:
        if block.get("type") == "text":
            has_text = True
        elif block.get("type") == "image":
            has_image = True

    if has_text and has_image:
        return "text+image"
    elif has_image:
        return "image"
    elif has_text:
        return "text"
    return "empty"


def extract_asset_ids(messages: list) -> Optional[str]:
    """Extract asset/image identifiers from request messages.

    If an image block has an 'asset_id' field, it's collected.
    Otherwise, a truncated base64 prefix is used as a fingerprint.
    """
    ids = []
    for msg in messages:
        for block in msg.get("content", []):
            if block.get("type") == "image":
                asset_id = block.get("asset_id")
                if not asset_id:
                    # Use first 20 chars of base64 as fingerprint
                    b64 = block.get("image", "")
                    if b64 and len(b64) > 20:
                        asset_id = f"img_{b64[:20]}"
                    else:
                        asset_id = "img_unknown"
                ids.append(asset_id)
    return json.dumps(ids) if ids else None


# ── Recording ─────────────────────────────────────────────────────────────

async def record_usage(
    session: AsyncSession,
    *,
    request_id: str,
    model_name: str,
    provider: str,
    status: str,
    task_type: Optional[str] = None,
    workflow_id: Optional[str] = None,
    input_modality: Optional[str] = None,
    output_modality: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    response_time_ms: int = 0,
    error_message: Optional[str] = None,
    conversation_id: Optional[str] = None,
    client_id: Optional[str] = None,
    group_id: Optional[str] = None,
    asset_ids: Optional[str] = None,
    routing_decision: Optional[str] = None,
):
    """Record a single usage entry with full observability fields."""
    try:
        log = UsageLog(
            request_id=request_id,
            task_type=task_type,
            workflow_id=workflow_id,
            model_name=model_name,
            provider=provider,
            routing_decision=routing_decision,
            input_modality=input_modality,
            output_modality=output_modality,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            response_time_ms=response_time_ms,
            status=status,
            error_message=error_message,
            conversation_id=conversation_id,
            client_id=client_id,
            group_id=group_id,
            asset_ids=asset_ids,
        )
        session.add(log)
        await session.commit()
    except Exception as e:
        logger.error(f"Failed to record usage: {e}")
        await session.rollback()


# ── Aggregation queries ───────────────────────────────────────────────────

async def get_stats_summary(
    session: AsyncSession,
    task_type_filter: Optional[str] = None,
    client_id_filter: Optional[str] = None,
) -> dict:
    """Get aggregate usage statistics, optionally filtered."""
    q = select(func.count(UsageLog.id))
    if task_type_filter:
        q = q.where(UsageLog.task_type == task_type_filter)
    if client_id_filter:
        q = q.where(UsageLog.client_id == client_id_filter)
    total_requests = (await session.execute(q)).scalar() or 0

    q = select(func.count(UsageLog.id)).where(UsageLog.status == "success")
    if task_type_filter:
        q = q.where(UsageLog.task_type == task_type_filter)
    if client_id_filter:
        q = q.where(UsageLog.client_id == client_id_filter)
    success_requests = (await session.execute(q)).scalar() or 0

    q = select(func.count(UsageLog.id)).where(UsageLog.status == "error")
    if task_type_filter:
        q = q.where(UsageLog.task_type == task_type_filter)
    if client_id_filter:
        q = q.where(UsageLog.client_id == client_id_filter)
    error_requests = (await session.execute(q)).scalar() or 0

    q = select(func.coalesce(func.sum(UsageLog.total_tokens), 0))
    if task_type_filter:
        q = q.where(UsageLog.task_type == task_type_filter)
    if client_id_filter:
        q = q.where(UsageLog.client_id == client_id_filter)
    total_tokens = (await session.execute(q)).scalar() or 0

    q = select(func.coalesce(func.avg(UsageLog.response_time_ms), 0))
    if task_type_filter:
        q = q.where(UsageLog.task_type == task_type_filter)
    if client_id_filter:
        q = q.where(UsageLog.client_id == client_id_filter)
    avg_response_time = round((await session.execute(q)).scalar() or 0, 1)

    return {
        "total_requests": total_requests,
        "success_requests": success_requests,
        "error_requests": error_requests,
        "total_tokens": total_tokens,
        "avg_response_time_ms": avg_response_time,
    }


async def get_usage_by_model(
    session: AsyncSession,
    task_type_filter: Optional[str] = None,
) -> List[dict]:
    """Get usage breakdown by model, optionally filtered by task_type."""
    q = (
        select(
            UsageLog.model_name,
            func.count(UsageLog.id).label("count"),
            func.coalesce(func.sum(UsageLog.total_tokens), 0).label("tokens"),
            func.coalesce(func.avg(UsageLog.response_time_ms), 0).label("avg_time"),
        )
        .group_by(UsageLog.model_name)
    )
    if task_type_filter:
        q = q.where(UsageLog.task_type == task_type_filter)
    result = await session.execute(q)
    return [
        {
            "model_name": row.model_name,
            "count": row.count,
            "total_tokens": row.tokens,
            "avg_response_time_ms": round(row.avg_time, 1),
        }
        for row in result
    ]


async def get_usage_by_task_type(session: AsyncSession) -> List[dict]:
    """Get usage breakdown by task type."""
    result = await session.execute(
        select(
            UsageLog.task_type,
            func.count(UsageLog.id).label("count"),
            func.coalesce(func.sum(UsageLog.total_tokens), 0).label("tokens"),
            func.coalesce(func.avg(UsageLog.response_time_ms), 0).label("avg_time"),
        )
        .where(UsageLog.task_type.isnot(None))
        .group_by(UsageLog.task_type)
    )
    return [
        {
            "task_type": row.task_type,
            "count": row.count,
            "total_tokens": row.tokens,
            "avg_response_time_ms": round(row.avg_time, 1),
        }
        for row in result
    ]


async def get_usage_by_day(
    session: AsyncSession,
    days: int = 7,
    task_type_filter: Optional[str] = None,
) -> List[dict]:
    """Get daily usage for the past N days, optionally filtered."""
    since = datetime.utcnow() - timedelta(days=days)
    q = (
        select(
            func.date(UsageLog.created_at).label("day"),
            func.count(UsageLog.id).label("count"),
            func.coalesce(func.sum(UsageLog.total_tokens), 0).label("tokens"),
        )
        .where(UsageLog.created_at >= since)
        .group_by(func.date(UsageLog.created_at))
        .order_by(func.date(UsageLog.created_at))
    )
    if task_type_filter:
        q = q.where(UsageLog.task_type == task_type_filter)
    result = await session.execute(q)
    return [
        {"day": str(row.day), "count": row.count, "total_tokens": row.tokens}
        for row in result
    ]


async def get_recent_requests(
    session: AsyncSession,
    limit: int = 50,
    task_type_filter: Optional[str] = None,
    conversation_id_filter: Optional[str] = None,
) -> List[dict]:
    """Get the most recent request logs with optional filters."""
    q = select(UsageLog).order_by(UsageLog.created_at.desc())
    if task_type_filter:
        q = q.where(UsageLog.task_type == task_type_filter)
    if conversation_id_filter:
        q = q.where(UsageLog.conversation_id == conversation_id_filter)
    q = q.limit(limit)
    result = await session.execute(q)
    logs = result.scalars().all()
    return [
        {
            "request_id": log.request_id,
            "task_type": log.task_type,
            "model_name": log.model_name,
            "provider": log.provider,
            "input_modality": log.input_modality,
            "output_modality": log.output_modality,
            "total_tokens": log.total_tokens,
            "response_time_ms": log.response_time_ms,
            "status": log.status,
            "error_message": log.error_message,
            "conversation_id": log.conversation_id,
            "client_id": log.client_id,
            "group_id": log.group_id,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]


async def get_distinct_task_types(session: AsyncSession) -> List[str]:
    """Get all distinct task types that have been logged."""
    result = await session.execute(
        select(UsageLog.task_type)
        .where(UsageLog.task_type.isnot(None))
        .distinct()
    )
    return [row[0] for row in result if row[0]]


async def get_routing_failures(
    session: AsyncSession,
    limit: int = 20,
) -> List[dict]:
    """Get recent requests that failed due to routing/model-selection errors.

    Routing failures have error messages containing:
      - 'No model available'
      - 'Unknown model'
      - 'does not support task'
      - 'is disabled'
      - 'workflow validation failed' (image_compare/image_edit input errors)
    """
    from sqlalchemy import or_

    routing_error_patterns = [
        UsageLog.error_message.ilike("%No model available%"),
        UsageLog.error_message.ilike("%Unknown model%"),
        UsageLog.error_message.ilike("%does not support task%"),
        UsageLog.error_message.ilike("%is disabled%"),
        UsageLog.error_message.ilike("%requires at least%"),
        UsageLog.error_message.ilike("%requires editing instructions%"),
    ]

    q = (
        select(UsageLog)
        .where(
            UsageLog.status == "error",
            or_(*routing_error_patterns),
        )
        .order_by(UsageLog.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(q)
    logs = result.scalars().all()
    return [
        {
            "request_id": log.request_id,
            "task_type": log.task_type,
            "model_name": log.model_name,
            "error_message": log.error_message,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]


async def get_conversation_requests(
    session: AsyncSession,
    conversation_id: str,
) -> List[dict]:
    """Get all requests in a conversation, ordered chronologically."""
    result = await session.execute(
        select(UsageLog)
        .where(UsageLog.conversation_id == conversation_id)
        .order_by(UsageLog.created_at.asc())
    )
    logs = result.scalars().all()
    return [
        {
            "request_id": log.request_id,
            "task_type": log.task_type,
            "model_name": log.model_name,
            "total_tokens": log.total_tokens,
            "status": log.status,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]
