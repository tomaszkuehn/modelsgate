"""Dashboard — usage statistics with filters."""

from app.admin.routes_pkg._shared import (
    router, templates, settings,
    get_current_admin,
    get_stats_summary, get_usage_by_model, get_usage_by_day,
    get_usage_by_task_type, get_recent_requests,
    get_distinct_task_types, get_client_stats, get_routing_failures,
)

from fastapi import Request, Depends
from fastapi.responses import HTMLResponse
# === Dashboard ===

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    admin: str = Depends(get_current_admin),
    task_type: str = "",
    conversation_id: str = "",
):
    """Admin dashboard with usage statistics and optional filters."""
    task_filter = task_type if task_type else None
    conv_filter = conversation_id if conversation_id else None

    from app.database import async_session
    async with async_session() as session:
        summary = await get_stats_summary(
            session,
            task_type_filter=task_filter,
        )
        by_model = await get_usage_by_model(
            session,
            task_type_filter=task_filter,
        )
        by_day = await get_usage_by_day(
            session,
            days=30,
            task_type_filter=task_filter,
        )
        by_task = await get_usage_by_task_type(session)
        recent = await get_recent_requests(
            session,
            limit=20,
            task_type_filter=task_filter,
            conversation_id_filter=conv_filter,
        )
        distinct_tasks = await get_distinct_task_types(session)

        client_stats = await get_client_stats(session)
        failures = await get_routing_failures(session, limit=10)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "summary": summary,
            "by_model": by_model,
            "by_day": by_day,
            "by_task": by_task,
            "recent": recent,
            "distinct_tasks": distinct_tasks,
            "selected_task_type": task_type,
            "selected_conversation_id": conversation_id,
            "client_stats": client_stats,
            "failures": failures,
        },
    )

