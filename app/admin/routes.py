"""Admin panel routes — dashboard, model management, settings."""

from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import settings
from app.admin.auth import authenticate_user, get_current_admin, hash_password
from app.stats.tracker import (
    get_stats_summary,
    get_usage_by_model,
    get_usage_by_day,
    get_usage_by_task_type,
    get_recent_requests,
    get_distinct_task_types,
    get_routing_failures,
)

router = APIRouter(prefix="/admin", tags=["admin"])

templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


# === Auth routes ===

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Show login form."""
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """Authenticate admin user."""
    from app.database import async_session
    async with async_session() as session:
        user = await authenticate_user(session, username, password)

    if user is None:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid username or password"},
            status_code=401,
        )

    request.session["admin_username"] = user.username
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    """Clear admin session."""
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


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
        },
    )


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

    caps = {
        "text_input": text_input, "image_input": image_input,
        "multi_image_input": multi_image_input,
        "text_output": text_output, "image_output": image_output,
        "image_edit": image_edit, "streaming": streaming,
        "max_images": max_images, "max_image_size_mb": max_image_size_mb,
    }

    async with async_session() as session:
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
    provider: str = Form(...),
    model_id: str = Form(...),
    description: str = Form(""),
    api_key: str = Form(""),
    base_url: str = Form(""),
    plan_tier: str = Form("standard"),
    cost_class: str = Form("balanced"),
    cost_weight: float = Form(1.0),
    enabled: bool = Form(True),
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
        result = await session.execute(
            _sel(ModelConfigRow).where(ModelConfigRow.name == model_name)
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
            row.enabled = enabled
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
    from sqlalchemy import select as _sel, func

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
    from sqlalchemy import select as _sel, func

    async with async_session() as session:
        count = (
            await session.execute(
                select(func.count(UsageLog.id)).where(UsageLog.model_name == model_name)
            )
        ).scalar() or 0

    from fastapi.responses import JSONResponse
    return JSONResponse({"model_name": model_name, "usage_count": count})


# === Settings ===

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """Server settings and key rotation page."""
    key_manager = request.app.state.key_manager

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "public_key": key_manager.public_key_pem[:200] + "..." if len(key_manager.public_key_pem) > 200 else key_manager.public_key_pem,
            "api_keys": {
                "openai": bool(settings.openai_api_key),
                "anthropic": bool(settings.anthropic_api_key),
                "gemini": bool(settings.gemini_api_key),
                "openrouter": bool(settings.openrouter_api_key),
                "alibaba": bool(settings.alibaba_api_key),
                "ollama_url": settings.ollama_base_url,
            },
        },
    )


@router.post("/settings/rotate-keys")
async def rotate_keys(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """Rotate the RSA encryption keys."""
    key_manager = request.app.state.key_manager
    key_manager.rotate_keys()

    return RedirectResponse(url="/admin/settings?rotated=1", status_code=303)


@router.post("/settings/change-password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    admin=Depends(get_current_admin),
):
    """Change the admin password."""
    from app.database import async_session
    from app.stats.models import AdminUser
    from sqlalchemy import select, update

    async with async_session() as session:
        result = await session.execute(
            select(AdminUser).where(AdminUser.username == admin.username)
        )
        user = result.scalar_one()

        from app.admin.auth import verify_password
        if not verify_password(current_password, user.password_hash):
            return templates.TemplateResponse(
                "settings.html",
                {
                    "request": request,
                    "error": "Current password is incorrect",
                    "public_key": request.app.state.key_manager.public_key_pem[:200] + "...",
                    "api_keys": {
                        "openai": bool(settings.openai_api_key),
                        "anthropic": bool(settings.anthropic_api_key),
                        "gemini": bool(settings.gemini_api_key),
                        "openrouter": bool(settings.openrouter_api_key),
                        "alibaba": bool(settings.alibaba_api_key),
                        "deepseek": bool(settings.deepseek_api_key),
                        "ollama_url": settings.ollama_base_url,
                    },
                },
            )

        user.password_hash = hash_password(new_password)
        await session.commit()

    return RedirectResponse(url="/admin/settings?password_changed=1", status_code=303)


# === Routing rules ===

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
        failures = await get_routing_failures(session, limit=15)

    return templates.TemplateResponse(
        "routing.html",
        {
            "request": request,
            "matrix": matrix,
            "summary": summary,
            "relaxation": relaxation,
            "failures": failures,
        },
    )


# === Model capabilities ===

@router.get("/capabilities", response_class=HTMLResponse)
async def capabilities_page(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """Model capabilities — detailed grid of every model's inputs, outputs, limits."""
    from app.models.router import ModelRouter

    router: ModelRouter = request.app.state.router
    table = router.get_routing_table()
    summary = router.get_routing_summary()

    return templates.TemplateResponse(
        "capabilities.html",
        {
            "request": request,
            "table": table,
            "summary": summary,
            "TaskType": __import__("app.api.schemas", fromlist=["TaskType"]).TaskType,
            "OutputType": __import__("app.api.schemas", fromlist=["OutputType"]).OutputType,
        },
    )


# === Task-to-model mappings ===

@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """Task mappings — which models serve each task, priority, fallback chains."""
    from app.models.router import ModelRouter

    router: ModelRouter = request.app.state.router
    matrix = router.get_task_model_matrix()
    eligible = router.get_eligible_models_for_tasks()
    relaxation = router.get_relaxation_order()

    from app.database import async_session
    async with async_session() as session:
        by_task = await get_usage_by_task_type(session)

    # Build usage lookup
    usage_map = {t["task_type"]: t for t in by_task} if by_task else {}

    return templates.TemplateResponse(
        "tasks.html",
        {
            "request": request,
            "matrix": matrix,
            "eligible": eligible,
            "relaxation": relaxation,
            "usage_map": usage_map,
        },
    )


# === Usage breakdowns ===

@router.get("/usage", response_class=HTMLResponse)
async def usage_page(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """Usage breakdowns — per-task-type charts, token distribution, error rates."""
    from app.database import async_session
    async with async_session() as session:
        by_task = await get_usage_by_task_type(session)
        by_model = await get_usage_by_model(session)
        by_day = await get_usage_by_day(session, days=30)
        summary = await get_stats_summary(session)
        recent = await get_recent_requests(session, limit=20)
        failures = await get_routing_failures(session, limit=15)

    return templates.TemplateResponse(
        "usage.html",
        {
            "request": request,
            "by_task": by_task,
            "by_model": by_model,
            "by_day": by_day,
            "summary": summary,
            "recent": recent,
            "failures": failures,
        },
    )


# === Clients ===

@router.get("/clients", response_class=HTMLResponse)
async def clients_page(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """Manage API clients."""
    from app.database import async_session
    from app.stats.models import Client, ClientGroup, RoutingPolicy
    from sqlalchemy import select as _sel

    async with async_session() as session:
        from app.stats.models import ClientGroup
        clients_result = await session.execute(
            _sel(Client).order_by(Client.registered_at.desc())
        )
        clients = clients_result.scalars().all()
        groups_result = await session.execute(
            _sel(ClientGroup).order_by(ClientGroup.group_key)
        )
        groups = groups_result.scalars().all()
        # Build group lookup
        group_map = {g.id: g for g in groups}

    return templates.TemplateResponse(
        "clients.html",
        {
            "request": request,
            "clients": [
                {
                    "id": c.id,
                    "client_key": c.client_key,
                    "plan": c.plan,
                    "is_active": c.is_active,
                    "group_id": c.client_group_id,
                    "group_key": (
                        group_map[c.client_group_id].group_key
                        if c.client_group_id and c.client_group_id in group_map
                        else "default"
                    ),
                    "tokens_today": c.tokens_used_today,
                    "tokens_month": c.tokens_used_this_month,
                    "registered_at": (
                        c.registered_at.strftime("%Y-%m-%d %H:%M")
                        if c.registered_at else "—"
                    ),
                }
                for c in clients
            ],
            "groups": [
                {"id": g.id, "group_key": g.group_key, "name": g.name}
                for g in groups
            ],
        },
    )


@router.post("/clients/create")
async def create_client(
    request: Request,
    admin=Depends(get_current_admin),
):
    """Create a new client with auto-generated key and free plan."""
    import uuid as _uuid
    from app.database import async_session
    from app.stats.models import Client

    client_key = f"cl_{_uuid.uuid4().hex[:16]}"

    async with async_session() as session:
        from app.stats.models import ClientGroup
        from sqlalchemy import select as _sel2
        default_group = (await session.execute(
            _sel2(ClientGroup).where(ClientGroup.group_key == "default")
        )).scalar_one_or_none()

        client = Client(
            client_key=client_key,
            plan="free",
            is_active=True,
            client_group_id=default_group.id if default_group else None,
        )
        session.add(client)
        await session.commit()

    return RedirectResponse(url="/admin/clients", status_code=303)


@router.post("/clients/{client_id}/toggle")
async def toggle_client(
    request: Request,
    client_id: int,
    admin=Depends(get_current_admin),
):
    """Toggle client active status."""
    from app.database import async_session
    from app.stats.models import Client
    from sqlalchemy import select as _sel, update

    async with async_session() as session:
        result = await session.execute(_sel(Client).where(Client.id == client_id))
        client = result.scalar_one()
        client.is_active = not client.is_active
        await session.commit()

    return RedirectResponse(url="/admin/clients", status_code=303)


@router.post("/clients/{client_id}/group")
async def reassign_client_group(
    request: Request,
    client_id: int,
    group_id: int = Form(...),
    admin=Depends(get_current_admin),
):
    """Reassign a client to a different group."""
    from app.database import async_session
    from app.stats.models import Client
    from sqlalchemy import select as _sel3

    async with async_session() as session:
        result = await session.execute(_sel3(Client).where(Client.id == client_id))
        client = result.scalar_one()
        client.client_group_id = group_id
        await session.commit()

    return RedirectResponse(url="/admin/clients", status_code=303)


# === Client Groups ===

@router.get("/groups", response_class=HTMLResponse)
async def groups_page(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """Manage client groups."""
    from app.database import async_session
    from app.stats.models import ClientGroup, RoutingPolicy
    from sqlalchemy import select as _sel

    async with async_session() as session:
        groups_result = await session.execute(
            _sel(ClientGroup).order_by(ClientGroup.group_key)
        )
        groups = groups_result.scalars().all()
        # Count clients per group
        from app.stats.models import Client
        from sqlalchemy import func
        counts_result = await session.execute(
            _sel(Client.client_group_id, func.count(Client.id))
            .where(Client.client_group_id.isnot(None))
            .group_by(Client.client_group_id)
        )
        client_counts = {row[0]: row[1] for row in counts_result}

    return templates.TemplateResponse(
        "groups.html",
        {
            "request": request,
            "groups": [
                {
                    "id": g.id,
                    "group_key": g.group_key,
                    "name": g.name,
                    "description": g.description or "",
                    "is_active": g.is_active,
                    "client_count": client_counts.get(g.id, 0),
                }
                for g in groups
            ],
        },
    )


@router.post("/groups/create")
async def create_group(
    request: Request,
    group_key: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    admin=Depends(get_current_admin),
):
    """Create a new client group."""
    from app.database import async_session
    from app.stats.models import ClientGroup

    async with async_session() as session:
        group = ClientGroup(
            group_key=group_key,
            name=name,
            description=description or None,
        )
        session.add(group)
        await session.commit()

    return RedirectResponse(url="/admin/groups", status_code=303)


# === Routing Policies ===

@router.get("/policies", response_class=HTMLResponse)
async def policies_page(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """Manage routing policies."""
    from app.database import async_session
    from app.stats.models import RoutingPolicy
    from sqlalchemy import select as _sel

    async with async_session() as session:
        policies_result = await session.execute(
            _sel(RoutingPolicy).order_by(RoutingPolicy.name)
        )
        policies = policies_result.scalars().all()

    return templates.TemplateResponse(
        "policies.html",
        {
            "request": request,
            "policies": [
                {
                    "id": p.id, "name": p.name, "description": p.description,
                    "is_active": p.is_active, "priority": p.priority,
                    "allowed_task_types": p.allowed_task_types,
                    "allowed_providers": p.allowed_providers,
                    "allowed_models": p.allowed_models,
                    "max_tokens_per_request": p.max_tokens_per_request,
                    "max_tokens_per_day": p.max_tokens_per_day,
                    "max_tokens_per_month": p.max_tokens_per_month,
                    "allow_image_input": p.allow_image_input,
                    "allow_image_output": p.allow_image_output,
                    "allow_image_edit": p.allow_image_edit,
                    "allow_streaming": p.allow_streaming,
                    "max_plan_tier": p.max_plan_tier,
                    "max_cost_class": p.max_cost_class,
                    "client_count": len(p.clients) if p.clients else 0,
                    "group_count": len(p.groups) if p.groups else 0,
                }
                for p in policies
            ],
        },
    )


@router.post("/policies/create")
async def create_policy(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    admin=Depends(get_current_admin),
):
    """Create a new routing policy with defaults."""
    from app.database import async_session
    from app.stats.models import RoutingPolicy

    async with async_session() as session:
        policy = RoutingPolicy(
            name=name,
            description=description or None,
        )
        session.add(policy)
        await session.commit()

    return RedirectResponse(url="/admin/policies", status_code=303)


@router.post("/policies/{policy_id}/toggle")
async def toggle_policy(
    request: Request,
    policy_id: int,
    admin=Depends(get_current_admin),
):
    """Toggle policy active status."""
    from app.database import async_session
    from app.stats.models import RoutingPolicy
    from sqlalchemy import select as _sel

    async with async_session() as session:
        result = await session.execute(
            _sel(RoutingPolicy).where(RoutingPolicy.id == policy_id)
        )
        policy = result.scalar_one()
        policy.is_active = not policy.is_active
        await session.commit()

    return RedirectResponse(url="/admin/policies", status_code=303)


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


# === API Playground ===

@router.get("/playground", response_class=HTMLResponse)
async def playground_page(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """API testing playground — build and send requests, see responses."""
    from app.models.registry import ModelRegistry

    registry = ModelRegistry()
    models = [{"name": n, "provider": c.provider} for n, c in registry.list_models().items()]

    from app.api.schemas import TaskType
    return templates.TemplateResponse(
        "playground.html",
        {
            "request": request,
            "task_types": [t.value for t in TaskType],
            "models": models,
            "public_key": request.app.state.key_manager.public_key_pem[:100] + "...",
        },
    )


@router.get("/playground/clients")
async def playground_clients(
    request: Request,
    admin=Depends(get_current_admin),
):
    """Return current client list as JSON for the playground dropdown."""
    from app.database import async_session
    from app.stats.models import Client, ClientGroup
    from sqlalchemy import select as _sel

    async with async_session() as session:
        clients = (await session.execute(
            _sel(Client).where(Client.is_active == True).order_by(Client.registered_at.desc()).limit(50)
        )).scalars().all()
        groups = (await session.execute(_sel(ClientGroup))).scalars().all()
        group_map = {g.id: g.group_key for g in groups}

    return [
        {
            "key": c.client_key,
            "plan": c.plan,
            "group": group_map.get(c.client_group_id, "default"),
        }
        for c in clients
    ]


@router.post("/playground/send")
async def playground_send(
    request: Request,
    admin=Depends(get_current_admin),
):
    """Proxy: encrypts a plain request, sends it to the API, decrypts the response.

    Accepts JSON body with: task_type, messages, client_id, and optional
    model, output_type, plan_tier, cost_class, preferred_provider, parameters.

    Returns: {original, encrypted_envelope, response}
    """
    import json as _json
    from app.security.encryption import encrypt_request
    from app.security.keys import KeyManager

    body = await request.json()
    payload = {
        "task_type": body.get("task_type", "chat_with_context"),
        "messages": body.get("messages", []),
        "client_id": body.get("client_id"),
    }
    for opt in ("model", "output_type", "plan_tier", "cost_class", "preferred_provider"):
        if body.get(opt):
            payload[opt] = body[opt]
    if body.get("parameters"):
        payload["parameters"] = body["parameters"]

    key_manager: KeyManager = request.app.state.key_manager

    # Encrypt with a known session key so we can decrypt the response
    import os as _os, base64 as _b64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    session_key = _os.urandom(32)
    nonce = _os.urandom(12)
    plaintext = _json.dumps(payload).encode("utf-8")
    aesgcm = AESGCM(session_key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    pubkey = serialization.load_pem_public_key(key_manager.public_key_pem.encode())
    encrypted_key = pubkey.encrypt(
        session_key,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )

    envelope = {
        "encrypted_key": _b64.b64encode(encrypted_key).decode(),
        "encrypted_payload": _b64.b64encode(ciphertext).decode(),
        "nonce": _b64.b64encode(nonce).decode(),
    }

    # Send to internal API
    import httpx
    decrypted = None
    async with httpx.AsyncClient() as client:
        api_resp = await client.post(
            "http://127.0.0.1:8000/api/v1/request",
            json=envelope,
            timeout=120.0,
        )
        if api_resp.status_code == 200:
            enc_resp = api_resp.json()
            resp_nonce = _b64.b64decode(enc_resp["nonce"])
            resp_ct = _b64.b64decode(enc_resp["encrypted_payload"])
            decrypted = _json.loads(aesgcm.decrypt(resp_nonce, resp_ct, None))
        else:
            decrypted = {"error": f"HTTP {api_resp.status_code}", "detail": api_resp.text}

    return {
        "original": payload,
        "encrypted_envelope": envelope,
        "response": decrypted,
    }
