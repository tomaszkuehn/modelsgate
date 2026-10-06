"""Access management — clients and groups."""

from app.admin.routes_pkg._shared import (
    router, templates, settings,
    get_current_admin,
)

from fastapi import Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
# === Clients ===

@router.get("/clients", response_class=HTMLResponse)
async def clients_page(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """Manage API clients."""
    from app.database import async_session
    from app.stats.models import Client, ClientGroup, UsageLog
    from sqlalchemy import select as _sel, func, case

    async with async_session() as session:
        clients_result = await session.execute(
            _sel(Client).order_by(Client.registered_at.desc())
        )
        clients = clients_result.scalars().all()
        groups_result = await session.execute(
            _sel(ClientGroup).order_by(ClientGroup.group_key)
        )
        groups = groups_result.scalars().all()
        group_map = {g.id: g for g in groups}
        client_counts = {}
        for c in clients:
            if c.client_group_id is not None:
                client_counts[c.client_group_id] = client_counts.get(c.client_group_id, 0) + 1

        # Per-client usage stats (excluding registrations)
        usage_q = (
            _sel(
                UsageLog.client_id,
                func.count(UsageLog.id).label("total"),
                func.sum(case((UsageLog.status == "error", 1), else_=0)).label("errors"),
            )
            .where(
                UsageLog.client_id.isnot(None),
                UsageLog.task_type != "register",
            )
            .group_by(UsageLog.client_id)
        )
        usage_result = await session.execute(usage_q)
        usage_by_client = {row.client_id: {"total": row.total, "errors": row.errors or 0} for row in usage_result}

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
                    "requests": usage_by_client.get(c.client_key, {}).get("total", 0),
                    "errors": usage_by_client.get(c.client_key, {}).get("errors", 0),
                }
                for c in clients
            ],
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

    return RedirectResponse(url="/admin/clients", status_code=303)

