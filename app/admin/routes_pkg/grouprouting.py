"""Per-group task-to-model assignments."""

from app.admin.routes_pkg._shared import (
    router, templates, settings,
    get_current_admin,
    get_stats_summary, get_usage_by_model, get_usage_by_day,
    get_usage_by_task_type, get_recent_requests,
    get_distinct_task_types, get_client_stats, get_routing_failures,
)

from fastapi import Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
# === Group Task Routing ===

@router.get("/group-routing", response_class=HTMLResponse)
async def group_routing_page(
    request: Request,
    admin: str = Depends(get_current_admin),
    group_id: int = 1,
):
    """Per-group task→model assignment matrix."""
    from app.database import async_session
    from app.stats.models import ClientGroup, GroupTaskRouting, ModelConfigRow
    from sqlalchemy import select as _sel

    async with async_session() as session:
        groups = (await session.execute(
            _sel(ClientGroup).order_by(ClientGroup.group_key)
        )).scalars().all()
        models = (await session.execute(
            _sel(ModelConfigRow).where(ModelConfigRow.enabled == True).order_by(ModelConfigRow.name)
        )).scalars().all()

        # Load existing assignments for the selected group
        assignments = {}
        selected_group = None
        if group_id > 0:
            rows = (await session.execute(
                _sel(GroupTaskRouting).where(GroupTaskRouting.group_id == group_id)
            )).scalars().all()
            assignments = {r.task_type: r.model_name for r in rows}
            selected_group = next((g for g in groups if g.id == group_id), None)

    from app.api.schemas import TaskType
    task_types = [t.value for t in TaskType]

    return templates.TemplateResponse(
        "group_routing.html",
        {
            "request": request,
            "groups": [{"id": g.id, "group_key": g.group_key, "name": g.name} for g in groups],
            "models": [{"name": m.name, "provider": m.provider} for m in models],
            "task_types": task_types,
            "assignments": assignments,
            "selected_group": {"id": selected_group.id, "group_key": selected_group.group_key} if selected_group else None,
            "selected_group_id": group_id,
        },
    )


@router.post("/group-routing/save")
async def save_group_routing(
    request: Request,
    group_id: int = Form(...),
    admin=Depends(get_current_admin),
):
    """Save task→model assignments for a group."""
    from app.database import async_session
    from app.stats.models import GroupTaskRouting
    from sqlalchemy import select as _sel, delete as _del
    from app.api.schemas import TaskType

    async with async_session() as session:
        # Delete existing assignments for this group
        await session.execute(
            _del(GroupTaskRouting).where(GroupTaskRouting.group_id == group_id)
        )

        # Insert new assignments from form data
        form = await request.form()
        for task in TaskType:
            field_name = f"model_{task.value}"
            model_name = form.get(field_name, "").strip()
            if model_name:  # only store non-empty assignments
                session.add(GroupTaskRouting(
                    group_id=group_id,
                    task_type=task.value,
                    model_name=model_name,
                ))

        await session.commit()

    return RedirectResponse(
        url=f"/admin/group-routing?group_id={group_id}&saved=1",
        status_code=303,
    )

