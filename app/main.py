"""FastAPI application entry point."""

import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# ── Logging: timestamps on every line ────────────────────────────────────
# Configure root logger directly (basicConfig is a no-op if uvicorn got there first).
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
root = logging.getLogger()
root.handlers.clear()  # remove uvicorn's default handler
root.addHandler(handler)
root.setLevel(logging.INFO)

# Access logger — one line per HTTP request (mirrors nginx access log).
access_logger = logging.getLogger("app.access")

from app.config import settings
from app.database import init_db
from app.api.routes import router as api_router
from app.admin.routes import router as admin_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    # Ensure data directories exist
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.keys_dir).mkdir(parents=True, exist_ok=True)

    # Initialize database tables
    await init_db()

    # Initialize encryption keys
    from app.security.keys import KeyManager
    app.state.key_manager = KeyManager(settings.keys_dir)

    # Initialize model registry and router
    from app.models.registry import ModelRegistry
    from app.models.router import ModelRouter
    registry = ModelRegistry()
    await registry.initialize()
    app.state.registry = registry
    app.state.router = ModelRouter(registry.get_all_configs())

    # Seed default admin user
    from app.admin.auth import seed_admin_user
    await seed_admin_user()

    # Seed default client group
    from app.database import async_session
    from app.stats.models import ClientGroup
    from sqlalchemy import select as _sel
    async with async_session() as s:
        existing = (await s.execute(
            _sel(ClientGroup).where(ClientGroup.group_key == "default")
        )).scalar_one_or_none()
        if not existing:
            s.add(ClientGroup(group_key="default", name="Default Group"))
            await s.commit()

    yield


app = FastAPI(
    title="AI Model Backend",
    description="Unified API gateway for AI model providers with application-layer encryption",
    version="1.0.0",
    lifespan=lifespan,
)

# Session middleware (required for admin panel auth)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)

# ── Access log: one line per request (mirrors nginx access log) ──────────
# Ensures no client traffic is silent in the app log — covers /api/v1/public-key,
# /api/v1/request, /, /admin/*, /api/v1/jobs/*, and every status code.
@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    """Log every HTTP request so no client request is missing from the app log."""
    # Skip static assets — not client/API traffic.
    path = request.url.path
    if path.startswith("/admin/static/") or path == "/favicon.ico":
        return await call_next(request)

    start = time.time()
    response = await call_next(request)
    duration_ms = int((time.time() - start) * 1000)

    # Real client IP when behind nginx (X-Forwarded-For), else socket peer.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
    elif request.client:
        client_ip = request.client.host
    else:
        client_ip = "-"

    user_agent = request.headers.get("user-agent", "-")
    content_length = response.headers.get("content-length", "-")
    http_version = request.scope.get("http_version", "-")

    access_logger.info(
        f'ACCESS {client_ip} "{request.method} {path} '
        f'{http_version}" {response.status_code} {content_length} '
        f'{duration_ms}ms "{user_agent}"'
    )
    return response

# Mount routers
app.include_router(api_router)
app.include_router(admin_router)

# Static files for admin panel
static_dir = Path(__file__).parent / "admin" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/admin/static", StaticFiles(directory=str(static_dir)), name="admin_static")


@app.get("/")
async def root():
    """Health check endpoint."""
    return {"status": "ok", "service": "AI Model Backend", "version": "1.0.0"}
