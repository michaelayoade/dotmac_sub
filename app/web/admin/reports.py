"""Admin reporting web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_reports as web_reports_service
from app.services import web_reports_extended as web_reports_ext_service
from app.services.audit_helpers import recent_activity_for_paths
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/reports", tags=["web-admin-reports"])

REPORT_HUB_SECTIONS: list[dict] = [
    {
        "id": "core",
        "name": "Core Reports",
        "description": "Primary business and operations reporting views.",
        "color": "teal",
        "links": [
            {
                "name": "Revenue",
                "url": "/admin/reports/revenue",
                "description": "Revenue metrics and recent payments",
            },
            {
                "name": "Subscribers",
                "url": "/admin/reports/subscribers",
                "description": "Subscriber growth and status breakdown",
            },
            {
                "name": "Churn",
                "url": "/admin/reports/churn",
                "description": "Retention, churn reasons, and cancellations",
            },
            {
                "name": "Network Usage",
                "url": "/admin/reports/network",
                "description": "Network utilization and infrastructure stats",
            },
            {
                "name": "Technician",
                "url": "/admin/reports/technician",
                "description": "Technician performance and jobs",
            },
        ],
    },
    {
        "id": "billing",
        "name": "Billing & Finance",
        "description": "Financial and invoice analytics.",
        "color": "emerald",
        "links": [
            {
                "name": "Usage by Plan",
                "url": "/admin/reports/usage-by-plan",
                "description": "Subscriber distribution across plans",
            },
            {
                "name": "Revenue per Plan",
                "url": "/admin/reports/revenue-per-plan",
                "description": "Revenue split by plan",
            },
            {
                "name": "Invoice Report",
                "url": "/admin/reports/invoices",
                "description": "Invoice listing and tax details",
            },
            {
                "name": "Statements",
                "url": "/admin/reports/statements",
                "description": "Customer financial summaries",
            },
            {
                "name": "Tax Report",
                "url": "/admin/reports/tax",
                "description": "Tax totals and per-invoice tax values",
            },
            {
                "name": "MRR Net Change",
                "url": "/admin/reports/mrr",
                "description": "Monthly recurring revenue movement",
            },
            {
                "name": "New Services",
                "url": "/admin/reports/new-services",
                "description": "Recently activated subscriptions",
            },
            {
                "name": "Upcoming Charges",
                "url": "/admin/reports/upcoming-charges",
                "description": "Subscriptions with upcoming billing",
            },
        ],
    },
    {
        "id": "extended",
        "name": "Extended Reports",
        "description": "Specialized and advanced analytics.",
        "color": "indigo",
        "links": [
            {
                "name": "Subscriber Growth (Trend)",
                "url": "/admin/reports/subscriber-growth",
                "description": "Time-series subscriber growth trend",
            },
            {
                "name": "Referrals",
                "url": "/admin/reports/referrals",
                "description": "Referral performance and conversion",
                "coming_soon": True,
            },
            {
                "name": "Voucher Statistics",
                "url": "/admin/reports/vouchers",
                "description": "Voucher inventory and redemptions",
                "coming_soon": True,
            },
            {
                "name": "DNS Threat Archive",
                "url": "/admin/reports/dns-threats",
                "description": "Security-related DNS events",
                "coming_soon": True,
            },
            {
                "name": "Custom Pricing & Discounts",
                "url": "/admin/reports/custom-pricing",
                "description": "Custom pricing overrides",
            },
            {
                "name": "Revenue by Category",
                "url": "/admin/reports/revenue-categories",
                "description": "Revenue segmented by category",
            },
            {
                "name": "Bandwidth & Usage",
                "url": "/admin/reports/bandwidth",
                "description": "Network usage analytics and top consumers",
            },
        ],
    },
]


def _base_context(
    request: Request, db: Session, active_page: str, heading: str, description: str
):
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


@router.get("/hub", response_class=HTMLResponse)
def reports_hub(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    context = {
        "request": request,
        "active_page": "reports-hub",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "sections": REPORT_HUB_SECTIONS,
    }
    return templates.TemplateResponse("admin/reports/hub.html", context)


@router.get(
    "/revenue",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
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


@router.get(
    "/subscribers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
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


@router.get(
    "/network",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
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


@router.get(
    "/technician",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
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
        headers={
            "Content-Disposition": "attachment; filename=technician-performance.csv"
        },
    )


# ===================================================================
# Extended Reports (04_administration features)
# ===================================================================


@router.get("/subscriber-growth", response_class=HTMLResponse)
def reports_subscriber_growth(
    request: Request,
    days: int = 30,
    db: Session = Depends(get_db),
):
    data = web_reports_ext_service.get_subscriber_growth_data(db, days=days)
    ctx = _base_context(
        request,
        db,
        "reports-subscriber-growth",
        "Subscriber Growth",
        "Customer growth trend over time",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/subscriber_growth.html", ctx)


@router.get("/usage-by-plan", response_class=HTMLResponse)
def reports_usage_by_plan(request: Request, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_usage_by_plan_data(db)
    ctx = _base_context(
        request,
        db,
        "reports-usage-plan",
        "Usage by Plan",
        "Subscriber distribution across plans",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/usage_by_plan.html", ctx)


@router.get("/upcoming-charges", response_class=HTMLResponse)
def reports_upcoming_charges(request: Request, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_upcoming_charges_data(db)
    ctx = _base_context(
        request,
        db,
        "reports-upcoming-charges",
        "Upcoming Charges",
        "Active subscriptions with upcoming billing",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/upcoming_charges.html", ctx)


@router.get("/revenue-per-plan", response_class=HTMLResponse)
def reports_revenue_per_plan(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    data = web_reports_ext_service.get_revenue_per_plan_data(
        db, date_from=date_from, date_to=date_to
    )
    ctx = _base_context(
        request,
        db,
        "reports-revenue-plan",
        "Revenue per Plan",
        "Revenue aggregated by service plan",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/revenue_per_plan.html", ctx)


@router.get("/invoices", response_class=HTMLResponse)
def reports_invoices(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    data = web_reports_ext_service.get_invoice_report_data(
        db, date_from=date_from, date_to=date_to, status=status
    )
    ctx = _base_context(
        request,
        db,
        "reports-invoices",
        "Invoice Report",
        "Detailed invoice listing with tax breakdown",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/invoices.html", ctx)


@router.get("/statements", response_class=HTMLResponse)
def reports_statements(request: Request, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_statements_data(db)
    ctx = _base_context(
        request, db, "reports-statements", "Statements", "Customer financial summaries"
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/statements.html", ctx)


@router.get("/tax", response_class=HTMLResponse)
def reports_tax(request: Request, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_tax_report_data(db)
    ctx = _base_context(
        request, db, "reports-tax", "Tax Report", "Per-invoice tax details and totals"
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/tax.html", ctx)


@router.get("/mrr", response_class=HTMLResponse)
def reports_mrr(
    request: Request,
    year: int | None = None,
    db: Session = Depends(get_db),
):
    data = web_reports_ext_service.get_mrr_data(db, year=year)
    ctx = _base_context(
        request,
        db,
        "reports-mrr",
        "MRR Net Change",
        "Monthly recurring revenue movement",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/mrr.html", ctx)


@router.get("/new-services", response_class=HTMLResponse)
def reports_new_services(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    data = web_reports_ext_service.get_new_services_data(
        db, date_from=date_from, date_to=date_to
    )
    ctx = _base_context(
        request,
        db,
        "reports-new-services",
        "New Services",
        "Recently activated subscriptions",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/new_services.html", ctx)


@router.get("/referrals", response_class=HTMLResponse)
def reports_referrals(request: Request, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_referrals_data(db)
    ctx = _base_context(
        request, db, "reports-referrals", "Referrals", "Referral program tracking"
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/referrals.html", ctx)


@router.get("/vouchers", response_class=HTMLResponse)
def reports_vouchers(request: Request, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_vouchers_data(db)
    ctx = _base_context(
        request,
        db,
        "reports-vouchers",
        "Voucher Statistics",
        "Prepaid voucher inventory and redemption",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/vouchers.html", ctx)


@router.get("/dns-threats", response_class=HTMLResponse)
def reports_dns_threats(request: Request, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_dns_threats_data(db)
    ctx = _base_context(
        request,
        db,
        "reports-dns-threats",
        "DNS Threat Archive",
        "DNS-based threat detection events",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/dns_threats.html", ctx)


@router.get("/custom-pricing", response_class=HTMLResponse)
def reports_custom_pricing(request: Request, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_custom_pricing_data(db)
    ctx = _base_context(
        request,
        db,
        "reports-custom-pricing",
        "Custom Pricing & Discounts",
        "Non-standard pricing overrides",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/custom_pricing.html", ctx)


@router.get("/revenue-categories", response_class=HTMLResponse)
def reports_revenue_categories(request: Request, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_revenue_categories_data(db)
    ctx = _base_context(
        request,
        db,
        "reports-revenue-categories",
        "Revenue by Category",
        "Income breakdown by service type",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/revenue_categories.html", ctx)


@router.get("/bandwidth", response_class=HTMLResponse)
def reports_bandwidth(request: Request, days: int = 30, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_bandwidth_report_data(db, days=days)
    ctx = _base_context(
        request,
        db,
        "reports-bandwidth",
        "Bandwidth & Usage",
        "Network usage analytics and top consumers",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/bandwidth.html", ctx)
