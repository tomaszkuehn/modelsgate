"""Request/response tracer — logs full pipeline: original → converted → response.

FIFO-capped at 1000 entries. Oldest entries are deleted on each insert.
"""

import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select, func, delete

from app.database import async_session
from app.stats.models import RequestLog

logger = logging.getLogger(__name__)

MAX_ENTRIES = 1000


async def trace_request(
    request_id: str,
    original: dict,
    converted: Optional[dict] = None,
    response: Optional[dict] = None,
    task_type: Optional[str] = None,
    model_name: Optional[str] = None,
    provider: Optional[str] = None,
    status: str = "success",
    client_id: Optional[str] = None,
    api_key_prefix: Optional[str] = None,
    model_id: Optional[str] = None,
):
    """Log a full request trace including original, converted, and response.

    Args:
        request_id: The unique request ID.
        original: The decrypted unified request dict.
        converted: The provider-specific format the request was converted to.
        response: The unified response dict.
        task_type: The task type.
        model_name: The model alias used.
        model_id: Provider-specific model identifier.
        provider: The provider name.
        status: 'success' or 'error'.
        api_key_prefix: First 8 chars of the API key used.
    """
    try:
        async with async_session() as session:
            log = RequestLog(
                request_id=request_id,
                task_type=task_type,
                model_name=model_name,
                model_id=model_id,
                provider=provider,
                client_id=client_id,
                api_key_prefix=api_key_prefix,
                original_json=json.dumps(original, default=str),
                converted_json=json.dumps(converted, default=str) if converted else None,
                response_json=json.dumps(response, default=str) if response else None,
                status=status,
            )
            session.add(log)
            await session.commit()

            # Enforce FIFO cap
            count = (await session.execute(
                select(func.count(RequestLog.id))
            )).scalar() or 0
            if count > MAX_ENTRIES:
                excess = count - MAX_ENTRIES
                # Delete the oldest 'excess' entries
                subq = (
                    select(RequestLog.id)
                    .order_by(RequestLog.created_at.asc())
                    .limit(excess)
                ).subquery()
                await session.execute(
                    delete(RequestLog).where(RequestLog.id.in_(select(subq.c.id)))
                )
                await session.commit()
                logger.debug(f"RequestLog cleanup: removed {excess} oldest entries")

    except Exception as e:
        logger.error(f"Failed to trace request: {e}")


async def get_recent_traces(limit: int = 50) -> list:
    """Get the most recent request traces."""
    async with async_session() as session:
        result = await session.execute(
            select(RequestLog)
            .order_by(RequestLog.created_at.desc())
            .limit(limit)
        )
        logs = result.scalars().all()
        return [
            {
                "id": log.id,
                "request_id": log.request_id,
                "task_type": log.task_type,
                "model_name": log.model_name,
                "model_id": log.model_id,
                "provider": log.provider,
                "client_id": log.client_id,
                "api_key_prefix": log.api_key_prefix,
                "original_json": log.original_json,
                "converted_json": log.converted_json,
                "response_json": log.response_json,
                "status": log.status,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log in logs
        ]


async def get_trace_count() -> int:
    """Get the current number of stored traces."""
    async with async_session() as session:
        result = await session.execute(select(func.count(RequestLog.id)))
        return (result.scalar() or 0)
