"""Admin panel routes, split by domain.

Each module registers endpoints on the shared ``router`` (prefix ``/admin``).
Import order matters only for readability; FastAPI picks up all decorated
routes when this package is imported.
"""
from app.admin.routes_pkg.auth import router, templates  # noqa: F401
from app.admin.routes_pkg.dashboard import router as dashboard_router  # noqa: F401
from app.admin.routes_pkg.models import router as models_router  # noqa: F401
from app.admin.routes_pkg.settings import router as settings_router  # noqa: F401
from app.admin.routes_pkg.analytics import router as analytics_router  # noqa: F401
from app.admin.routes_pkg.access import router as access_router  # noqa: F401
from app.admin.routes_pkg.grouprouting import router as grouprouting_router  # noqa: F401
from app.admin.routes_pkg.playground import router as playground_router  # noqa: F401