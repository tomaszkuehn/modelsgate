"""Settings — key rotation, password change, system health, server info."""

from app.admin.routes_pkg._shared import (
    router, templates, settings,
    get_current_admin, hash_password, verify_password,
    get_stats_summary, get_usage_by_model, get_usage_by_day,
    get_usage_by_task_type, get_recent_requests,
    get_distinct_task_types, get_client_stats, get_routing_failures,
)

from fastapi import Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse


def _api_keys_context() -> dict:
    return {
        "openai": bool(settings.openai_api_key),
        "anthropic": bool(settings.anthropic_api_key),
        "gemini": bool(settings.gemini_api_key),
        "openrouter": bool(settings.openrouter_api_key),
        "alibaba": bool(settings.alibaba_api_key),
        "deepseek": bool(settings.deepseek_api_key),
        "ollama_url": settings.ollama_base_url,
    }


# === Settings ===

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """Server settings and key rotation page."""
    from app.stats.memory import memory_stats

    key_manager = request.app.state.key_manager

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "public_key": key_manager.public_key_pem[:200] + "..." if len(key_manager.public_key_pem) > 200 else key_manager.public_key_pem,
            "api_keys": _api_keys_context(),
            "mem": memory_stats(),
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
    from sqlalchemy import select

    async with async_session() as session:
        result = await session.execute(
            select(AdminUser).where(AdminUser.username == admin.username)
        )
        user = result.scalar_one()

        if not verify_password(current_password, user.password_hash):
            return templates.TemplateResponse(
                "settings.html",
                {
                    "request": request,
                    "error": "Current password is incorrect",
                    "public_key": request.app.state.key_manager.public_key_pem[:200] + "...",
                    "api_keys": _api_keys_context(),
                },
            )

        user.password_hash = hash_password(new_password)
        await session.commit()

    return RedirectResponse(url="/admin/settings?password_changed=1", status_code=303)

