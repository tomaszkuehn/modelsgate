"""FastAPI application entry point."""

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
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
