"""Routing and request logs."""

from app.admin.routes_pkg._shared import (
    router, templates,
    get_current_admin,
    get_usage_by_task_type, get_routing_failures,
)

from fastapi import Request, Depends
from fastapi.responses import HTMLResponse
# === Routing ===

@router.get("/routing", response_class=HTMLResponse)
async def routing_page(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """Routing rules — task→model mappings, priority order, fallback chains."""
    from app.models.router import ModelRouter

    router: ModelRouter = request.app.state.router
    matrix = router.get_task_model_matrix()
    summary = router.get_routing_summary()
    relaxation = router.get_relaxation_order()

    from app.database import async_session
    async with async_session() as session:
        by_task = await get_usage_by_task_type(session)
        failures = await get_routing_failures(session, limit=15)

    usage_map = {t["task_type"]: t for t in by_task} if by_task else {}

    return templates.TemplateResponse(
        "routing.html",
        {
            "request": request,
            "matrix": matrix,
            "summary": summary,
            "relaxation": relaxation,
            "usage_map": usage_map,
            "failures": failures,
        },
    )


# === Request Logs ===

@router.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """Request trace log — original → converted → response pipeline."""
    from app.logs.tracer import get_recent_traces, get_trace_count

    traces = await get_recent_traces(limit=50)
    count = await get_trace_count()

    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "traces": traces,
            "count": count,
            "max_entries": 1000,
        },
    )
