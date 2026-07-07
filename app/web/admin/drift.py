"""Admin cross-app drift findings — a read-only operational table.

Answers, for each drift condition: what is broken, how bad is it, who owns it,
what should they do, and what evidence proves it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import cross_app_drift
from app.services.auth_dependencies import require_any_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/drift", tags=["web-admin-drift"])

_DRIFT_ACCESS = Depends(
    require_any_permission("system:read", "system:settings:read", "monitoring:read")
)


@router.get("", response_class=HTMLResponse, dependencies=[_DRIFT_ACCESS])
def drift_index(
    request: Request,
    status: str | None = Query("open"),
    severity: str | None = Query(None),
    check: str | None = Query(None),
    entity_type: str | None = Query(None),
    owner: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/drift/index.html",
        {
            "request": request,
            **cross_app_drift.drift_findings_context(
                db,
                status=status,
                severity=severity,
                check=check,
                entity_type=entity_type,
                owner=owner,
                page=page,
                per_page=per_page,
            ),
            "active_page": "admin-drift",
            "active_menu": "dashboard",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )
