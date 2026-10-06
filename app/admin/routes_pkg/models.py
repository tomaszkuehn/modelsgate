"""Model management — CRUD on model_configs, registry reload."""

from app.admin.routes_pkg._shared import (
    router, templates, settings,
    get_current_admin,
    get_stats_summary, get_usage_by_model, get_usage_by_day,
    get_usage_by_task_type, get_recent_requests,
    get_distinct_task_types, get_client_stats, get_routing_failures,
)

from fastapi import Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
# === Model management ===

@router.get("/models", response_class=HTMLResponse)
async def models_page(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """Model configuration page with CRUD."""
    from app.models.registry import ModelRegistry
    registry = ModelRegistry()
    models = registry.list_models()

    from app.database import async_session
    async with async_session() as session:
        by_model = await get_usage_by_model(session)

    usage_map = {m["model_name"]: m for m in by_model}

    # Get DB rows for edit state
    from app.stats.models import ModelConfigRow
    from sqlalchemy import select as _sel
    async with async_session() as session:
        rows_result = await session.execute(_sel(ModelConfigRow).order_by(ModelConfigRow.name))
        db_rows = {r.name: r for r in rows_result.scalars().all()}

    return templates.TemplateResponse(
        "models.html",
        {
            "request": request,
            "models": models,
            "usage_map": usage_map,
            "db_rows": db_rows,
            "config_path": "Database (model_configs table)",
        },
    )


@router.post("/models/create")
async def create_model(
    request: Request,
    name: str = Form(...),
    provider: str = Form(...),
    model_id: str = Form(...),
    description: str = Form(""),
    api_key: str = Form(""),
    base_url: str = Form(""),
    plan_tier: str = Form("standard"),
    cost_class: str = Form("balanced"),
    cost_weight: float = Form(1.0),
    text_input: bool = Form(True),
    image_input: bool = Form(False),
    multi_image_input: bool = Form(False),
    text_output: bool = Form(True),
    image_output: bool = Form(False),
    image_edit: bool = Form(False),
    streaming: bool = Form(False),
    max_images: int = Form(0),
    max_image_size_mb: float = Form(0.0),
    admin=Depends(get_current_admin),
):
    """Create a new model configuration."""
    import json
    from app.database import async_session
    from app.stats.models import ModelConfigRow
    from sqlalchemy import select as _sel

    caps = {
        "text_input": text_input, "image_input": image_input,
        "multi_image_input": multi_image_input,
        "text_output": text_output, "image_output": image_output,
        "image_edit": image_edit, "streaming": streaming,
        "max_images": max_images, "max_image_size_mb": max_image_size_mb,
    }

    async with async_session() as session:
        # Block duplicate names
        existing = (await session.execute(
            _sel(ModelConfigRow).where(ModelConfigRow.name == name)
        )).scalar_one_or_none()
        if existing:
            return RedirectResponse(
                url=f"/admin/models?error=Name+'{name}'+already+exists",
                status_code=303,
            )

        row = ModelConfigRow(
            name=name, provider=provider, model_id=model_id,
            description=description or None,
            api_key=api_key or None,
            base_url=base_url or None,
            capabilities_json=json.dumps(caps),
            plan_tier=plan_tier, cost_class=cost_class,
            cost_weight=cost_weight, enabled=True,
        )
        session.add(row)
        await session.commit()

    # Reload registry from database
    from app.models.registry import ModelRegistry
    await ModelRegistry().reload_from_db()

    registry = ModelRegistry()
    request.app.state.router.update_configs(registry.get_all_configs())

    return RedirectResponse(url="/admin/models?created=1", status_code=303)


@router.post("/models/{model_name}/update")
async def update_model(
    request: Request,
    model_name: str,
    name: str = Form(...),
    original_name: str = Form(""),
    provider: str = Form(...),
    model_id: str = Form(...),
    description: str = Form(""),
    api_key: str = Form(""),
    base_url: str = Form(""),
    plan_tier: str = Form("standard"),
    cost_class: str = Form("balanced"),
    cost_weight: float = Form(1.0),
    enabled: str = Form("off"),
    text_input: bool = Form(True),
    image_input: bool = Form(False),
    multi_image_input: bool = Form(False),
    text_output: bool = Form(True),
    image_output: bool = Form(False),
    image_edit: bool = Form(False),
    streaming: bool = Form(False),
    max_images: int = Form(0),
    max_image_size_mb: float = Form(0.0),
    admin=Depends(get_current_admin),
):
    """Update an existing model configuration."""
    import json
    from app.database import async_session
    from app.stats.models import ModelConfigRow
    from sqlalchemy import select as _sel, update

    caps = {
        "text_input": text_input, "image_input": image_input,
        "multi_image_input": multi_image_input,
        "text_output": text_output, "image_output": image_output,
        "image_edit": image_edit, "streaming": streaming,
        "max_images": max_images, "max_image_size_mb": max_image_size_mb,
    }

    async with async_session() as session:
        lookup_name = original_name or model_name

        # Block rename to an already-existing name
        if name != lookup_name:
            dup = (await session.execute(
                _sel(ModelConfigRow).where(ModelConfigRow.name == name)
            )).scalar_one_or_none()
            if dup:
                return RedirectResponse(
                    url=f"/admin/models?error=Name+'{name}'+already+exists",
                    status_code=303,
                )

        result = await session.execute(
            _sel(ModelConfigRow).where(ModelConfigRow.name == lookup_name)
        )
        row = result.scalar_one_or_none()
        if row:
            row.name = name
            row.provider = provider
            row.model_id = model_id
            row.description = description or None
            row.api_key = api_key or None
            row.base_url = base_url or None
            row.capabilities_json = json.dumps(caps)
            row.plan_tier = plan_tier
            row.cost_class = cost_class
            row.cost_weight = cost_weight
            row.enabled = (enabled == "on")

            # Propagate rename to group routing, logs, and traces
            if name != lookup_name:
                from app.stats.models import GroupTaskRouting, UsageLog, RequestLog
                from sqlalchemy import update as _upd
                for tbl in (GroupTaskRouting, UsageLog, RequestLog):
                    await session.execute(
                        _upd(tbl)
                        .where(tbl.model_name == lookup_name)
                        .values(model_name=name)
                    )

            await session.commit()

    from app.models.registry import ModelRegistry
    await ModelRegistry().reload_from_db()
    registry = ModelRegistry()
    request.app.state.router.update_configs(registry.get_all_configs())

    return RedirectResponse(url="/admin/models?updated=1", status_code=303)


@router.post("/models/{model_name}/delete")
async def delete_model(
    request: Request,
    model_name: str,
    admin=Depends(get_current_admin),
):
    """Delete a model configuration."""
    from app.database import async_session
    from app.stats.models import ModelConfigRow, UsageLog
    from sqlalchemy import select as _sel, select, func

    async with async_session() as session:
        # Check usage
        usage_count = (
            await session.execute(
                select(func.count(UsageLog.id)).where(UsageLog.model_name == model_name)
            )
        ).scalar() or 0

        if usage_count > 0:
            return templates.TemplateResponse(
                "models.html",
                {
                    "request": request,
                    "error": (
                        f"Cannot delete '{model_name}': it has {usage_count} usage records. "
                        f"Disable it instead."
                    ),
                    "models": {},
                    "usage_map": {},
                    "db_rows": {},
                    "config_path": "Database (model_configs table)",
                },
            )

        result = await session.execute(
            _sel(ModelConfigRow).where(ModelConfigRow.name == model_name)
        )
        row = result.scalar_one_or_none()
        if row:
            await session.delete(row)
            await session.commit()

    from app.models.registry import ModelRegistry
    await ModelRegistry().reload_from_db()
    registry = ModelRegistry()
    request.app.state.router.update_configs(registry.get_all_configs())

    return RedirectResponse(url="/admin/models?deleted=1", status_code=303)


@router.get("/models/{model_name}/usage", response_class=HTMLResponse)
async def model_usage_warning(
    request: Request,
    model_name: str,
    admin=Depends(get_current_admin),
):
    """Check if a model has usage history (for delete confirmation)."""
    from app.database import async_session
    from app.stats.models import UsageLog
    from sqlalchemy import select as _sel, select, func

    async with async_session() as session:
        count = (
            await session.execute(
                select(func.count(UsageLog.id)).where(UsageLog.model_name == model_name)
            )
        ).scalar() or 0

    from fastapi.responses import JSONResponse
    return JSONResponse({"model_name": model_name, "usage_count": count})

