"""Shared admin router, templates, and auth imports for all route modules."""

from pathlib import Path

from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.admin.auth import (
    get_current_admin, hash_password, verify_password,
)
from app.stats.tracker import (
    get_stats_summary,
    get_usage_by_model,
    get_usage_by_day,
    get_usage_by_task_type,
    get_recent_requests,
    get_distinct_task_types,
    get_client_stats,
    get_routing_failures,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))