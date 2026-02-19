"""Admin reporting web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_reports as web_reports_service
from app.services.audit_helpers import recent_activity_for_paths

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/reports", tags=["web-admin-reports"])


def _base_context(request: Request, db: Session, active_page: str, heading: str, description: str):
    from app.web.admin import get_current_user, get_sidebar_stats
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "page_title": heading,
        "heading": heading,
        "description": description,
        "empty_title": "No reports yet",
        "empty_message": "Report data will appear once analytics are configured.",
    }


@router.get("/revenue", response_class=HTMLResponse)
def reports_revenue(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    report_data = web_reports_service.get_revenue_report_data(db)

    context = {
        "request": request,
        "active_page": "reports-revenue",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "total_revenue": report_data["total_revenue"],
        "revenue_growth": report_data["revenue_growth"],
        "recurring_revenue": report_data["recurring_revenue"],
        "outstanding_amount": report_data["outstanding_amount"],
        "outstanding_count": report_data["outstanding_count"],
        "collection_rate": report_data["collection_rate"],
        "recent_payments": report_data["recent_payments"],
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/revenue.html", context)


@router.get("/revenue/export")
def reports_revenue_export(days: int | None = None, db: Session = Depends(get_db)):
    content = web_reports_service.build_revenue_export_csv(db=db, days=days)
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=revenue-payments.csv"},
    )


@router.get("/subscribers", response_class=HTMLResponse)
def reports_subscribers(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    report_data = web_reports_service.get_subscribers_report_data(db)

    context = {
        "request": request,
        "active_page": "reports-subscribers",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "total_subscribers": report_data["total_subscribers"],
        "subscriber_growth": report_data["subscriber_growth"],
        "new_this_month": report_data["new_this_month"],
        "active_subscribers": report_data["active_subscribers"],
        "suspended_subscribers": report_data["suspended_subscribers"],
        "active_rate": report_data["active_rate"],
        "status_breakdown": report_data["status_breakdown"],
        "recent_subscribers": report_data["recent_subscribers"],
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/subscribers.html", context)


@router.get("/subscribers/export")
def reports_subscribers_export(days: int | None = None, db: Session = Depends(get_db)):
    content = web_reports_service.build_subscribers_export_csv(db=db, days=days)
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=subscribers.csv"},
    )


@router.get("/churn", response_class=HTMLResponse)
def reports_churn(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    report_data = web_reports_service.get_churn_report_data(db)

    context = {
        "request": request,
        "active_page": "reports-churn",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "churn_rate": report_data["churn_rate"],
        "retention_rate": report_data["retention_rate"],
        "cancelled_count": report_data["cancelled_count"],
        "at_risk_count": report_data["at_risk_count"],
        "churn_reasons": report_data["churn_reasons"],
        "recent_cancellations": report_data["recent_cancellations"],
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/churn.html", context)


@router.get("/churn/export")
def reports_churn_export(days: int | None = None, db: Session = Depends(get_db)):
    content = web_reports_service.build_churn_export_csv(db=db, days=days)
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=churn-report.csv"},
    )


@router.get("/network", response_class=HTMLResponse)
def reports_network(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    report_data = web_reports_service.get_network_report_data(db=db)

    context = {
        "request": request,
        "active_page": "reports-network",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "total_olts": report_data["total_olts"],
        "active_olts": report_data["active_olts"],
        "total_onts": report_data["total_onts"],
        "connected_onts": report_data["connected_onts"],
        "ip_pool_usage": report_data["ip_pool_usage"],
        "used_ips": report_data["used_ips"],
        "total_ips": report_data["total_ips"],
        "active_vlans": report_data["active_vlans"],
        "olts": report_data["olts"],
        "ip_pools": report_data["pool_data"],
        "recent_ont_activity": report_data["recent_ont_activity"],
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/network.html", context)


@router.get("/network/export")
def reports_network_export(hours: int | None = None, db: Session = Depends(get_db)):
    report_data = web_reports_service.get_network_report_data(db=db, hours=hours)
    content = web_reports_service.build_network_export_csv(report_data, hours=hours)
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=network-usage.csv"},
    )


@router.get("/technician", response_class=HTMLResponse)
def reports_technician(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats
    report_data = web_reports_service.get_technician_report_data(db)

    context = {
        "request": request,
        "active_page": "reports-technician",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "total_technicians": report_data["total_technicians"],
        "jobs_completed": report_data["jobs_completed"],
        "avg_completion_hours": report_data["avg_completion_hours"],
        "first_visit_rate": report_data["first_visit_rate"],
        "technician_stats": report_data["technician_stats"],
        "job_type_breakdown": report_data["job_type_breakdown"],
        "recent_completions": report_data["recent_completions"],
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/technician.html", context)


@router.get("/technician/export")
def reports_technician_export(days: int | None = None, db: Session = Depends(get_db)):
    content = web_reports_service.build_technician_export_csv(db=db, days=days)
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=technician-performance.csv"},
    )
