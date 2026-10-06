"""Auth routes — login, logout."""

from app.admin.routes_pkg._shared import (
    router, templates, settings,
    get_current_admin,
    get_stats_summary, get_usage_by_model, get_usage_by_day,
    get_usage_by_task_type, get_recent_requests,
    get_distinct_task_types, get_client_stats, get_routing_failures,
)

from fastapi import Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.auth import check_ip_rate_limit, check_account_locked, record_failed_attempt, reset_failed_attempts, _rate_limiter
from app.admin.auth import authenticate_user
# === Auth routes ===

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Show login form; already-authenticated users go straight to the dashboard."""
    username = request.session.get("admin_username")
    if username:
        from app.database import async_session
        from app.stats.models import AdminUser
        from sqlalchemy import select
        async with async_session() as session:
            result = await session.execute(
                select(AdminUser).where(AdminUser.username == username)
            )
            if result.scalar_one_or_none() is not None:
                return RedirectResponse(url="/admin/dashboard", status_code=303)
        request.session.clear()
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """Authenticate admin user with brute-force protection."""
    from app.database import async_session

    # Trust the reverse proxy (nginx sets X-Real-IP) — never client-supplied XFF
    client_ip = request.headers.get("x-real-ip") or (
        request.client.host if request.client else "unknown"
    )

    # 1 — IP-level rate limit (don't even hit the DB if flooding)
    try:
        check_ip_rate_limit(client_ip)
    except HTTPException as e:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": e.detail},
            status_code=429,
        )

    async with async_session() as session:
        # 2 — Check account lockout
        try:
            user = await check_account_locked(session, username)
        except HTTPException as e:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": e.detail},
                status_code=429,
            )

        # 3 — Verify password
        authed = await authenticate_user(session, username, password)

        if authed is None:
            # Record the failed attempt for both existing and non-existent users
            await record_failed_attempt(session, username)
            _rate_limiter.record(client_ip)
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "Invalid username or password"},
                status_code=401,
            )

        # 4 — Success — reset counters
        await reset_failed_attempts(session, authed)

    request.session["admin_username"] = authed.username
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    """Clear admin session."""
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)

