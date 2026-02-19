"""Admin dashboard web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_admin_dashboard as web_admin_dashboard_service
router = APIRouter(tags=["web-admin-dashboard"])


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    """Admin dashboard overview page."""
    return web_admin_dashboard_service.dashboard(request, db)


@router.get("/dashboard/stats", response_class=HTMLResponse)
def dashboard_stats_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial for dashboard stats cards."""
    return web_admin_dashboard_service.dashboard_stats_partial(request, db)


@router.get("/dashboard/activity", response_class=HTMLResponse)
def dashboard_activity_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial for recent activity feed."""
    return web_admin_dashboard_service.dashboard_activity_partial(request, db)


@router.get("/dashboard/server-health", response_class=HTMLResponse)
def dashboard_server_health_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial for server health widget."""
    return web_admin_dashboard_service.dashboard_server_health_partial(request, db)
