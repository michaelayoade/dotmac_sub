"""Admin reporting web routes."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import csv
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services import billing as billing_service
from app.services import subscriber as subscriber_service
from app.services import network as network_service
from app.services import provisioning as operations_service
from app.services.audit_helpers import recent_activity_for_paths
from app.models.billing import InvoiceStatus
from app.models.subscriber import AccountStatus

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/reports", tags=["web-admin-reports"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _base_context(request: Request, db: Session, active_page: str, heading: str, description: str):
    from app.web.admin import get_sidebar_stats, get_current_user
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


def _derive_subscriber_status(subscriber) -> AccountStatus:
    if subscriber.status:
        return subscriber.status
    return AccountStatus.active if subscriber.is_active else AccountStatus.canceled


def _ensure_aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@router.get("/revenue", response_class=HTMLResponse)
def reports_revenue(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user

    # Get payments for revenue calculation
    payments = billing_service.payments.list(
        db=db,
        account_id=None,
        invoice_id=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )
    recent_payments = billing_service.payments.list(
        db=db,
        account_id=None,
        invoice_id=None,
        status=None,
        is_active=None,
        order_by="paid_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )

    # Calculate total revenue
    total_revenue = sum(p.amount for p in payments if p.amount)

    # Fetch invoices for outstanding/collection calculations
    all_invoices = billing_service.invoices.list(
        db=db,
        account_id=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )
    outstanding_statuses = {InvoiceStatus.issued, InvoiceStatus.partially_paid, InvoiceStatus.overdue}
    outstanding_invoices = [inv for inv in all_invoices if inv.status in outstanding_statuses]
    def _invoice_amount_due(invoice):
        if hasattr(invoice, "balance_due"):
            return invoice.balance_due
        if hasattr(invoice, "amount_due"):
            return invoice.amount_due
        if hasattr(invoice, "total"):
            return invoice.total
        return 0

    outstanding_amount = sum(
        _invoice_amount_due(inv) for inv in outstanding_invoices if _invoice_amount_due(inv)
    )
    outstanding_count = len(outstanding_invoices)

    # Calculate collection rate (paid vs total invoiced)
    total_invoiced = sum(
        _invoice_amount_due(inv) for inv in all_invoices if _invoice_amount_due(inv)
    )
    collection_rate = (total_revenue / total_invoiced * 100) if total_invoiced > 0 else 0

    # Recurring revenue (placeholder - would need subscription calculation)
    recurring_revenue = total_revenue * Decimal("0.85")  # Estimate 85% is recurring

    context = {
        "request": request,
        "active_page": "reports-revenue",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "total_revenue": total_revenue,
        "revenue_growth": 12.5,  # Would calculate from historical data
        "recurring_revenue": recurring_revenue,
        "outstanding_amount": outstanding_amount,
        "outstanding_count": outstanding_count,
        "collection_rate": collection_rate,
        "recent_payments": recent_payments,
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/revenue.html", context)


def _account_display_name(account) -> str:
    if not account:
        return ""
    subscriber = getattr(account, "subscriber", None)
    if subscriber and getattr(subscriber, "person", None):
        person = subscriber.person
        return f"{person.first_name} {person.last_name}".strip()
    if subscriber and getattr(subscriber, "organization", None):
        return subscriber.organization.name or ""
    return getattr(account, "account_number", "") or str(getattr(account, "id", ""))


@router.get("/revenue/export")
def reports_revenue_export(days: int | None = None, db: Session = Depends(get_db)):
    payments = billing_service.payments.list(
        db=db,
        account_id=None,
        invoice_id=None,
        status=None,
        is_active=None,
        order_by="paid_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )

    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        payments = [p for p in payments if p.paid_at and p.paid_at >= cutoff]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "paid_at",
        "account",
        "account_id",
        "invoice_id",
        "amount",
        "currency",
        "status",
        "payment_method",
        "provider",
    ])
    for payment in payments:
        writer.writerow([
            payment.paid_at.isoformat() if payment.paid_at else "",
            _account_display_name(payment.account),
            str(payment.account_id) if payment.account_id else "",
            str(payment.invoice_id) if payment.invoice_id else "",
            str(payment.amount or ""),
            payment.currency or "",
            payment.status.value if payment.status else "",
            payment.payment_method.name if payment.payment_method else "",
            payment.provider.name if payment.provider else "",
        ])

    content = output.getvalue()
    output.close()

    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=revenue-payments.csv"},
    )


@router.get("/subscribers", response_class=HTMLResponse)
def reports_subscribers(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user

    # Get all subscribers
    all_subscribers = subscriber_service.subscribers.list(
        db=db,
        subscriber_type=None,
        organization_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )
    total_subscribers = len(all_subscribers)

    # Count by status
    status_breakdown = {}
    active_count = 0
    suspended_count = 0

    for sub in all_subscribers:
        status = _derive_subscriber_status(sub)
        sub.status = status
        status_name = status.value if status else "unknown"
        status_breakdown[status_name] = status_breakdown.get(status_name, 0) + 1
        if status == AccountStatus.active:
            active_count += 1
        elif status == AccountStatus.suspended:
            suspended_count += 1

    # Calculate rates
    active_rate = (active_count / total_subscribers * 100) if total_subscribers > 0 else 0

    # Get recent subscribers (sorted by created_at desc)
    recent_subscribers = sorted(
        all_subscribers,
        key=lambda x: x.created_at if x.created_at else datetime.min,
        reverse=True
    )[:10]

    # Count new this month
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    new_this_month = sum(
        1 for sub in all_subscribers
        if _ensure_aware_datetime(sub.created_at)
        and _ensure_aware_datetime(sub.created_at) >= month_start
    )

    context = {
        "request": request,
        "active_page": "reports-subscribers",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "total_subscribers": total_subscribers,
        "subscriber_growth": 8.3,  # Would calculate from historical data
        "new_this_month": new_this_month,
        "active_subscribers": active_count,
        "suspended_subscribers": suspended_count,
        "active_rate": active_rate,
        "status_breakdown": status_breakdown,
        "recent_subscribers": recent_subscribers,
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/subscribers.html", context)


@router.get("/subscribers/export")
def reports_subscribers_export(days: int | None = None, db: Session = Depends(get_db)):
    all_subscribers = subscriber_service.subscribers.list(
        db=db,
        subscriber_type=None,
        organization_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        all_subscribers = [
            sub
            for sub in all_subscribers
            if _ensure_aware_datetime(sub.created_at)
            and _ensure_aware_datetime(sub.created_at) >= cutoff
        ]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["subscriber_id", "name", "type", "status", "created_at"])
    for sub in all_subscribers:
        status = _derive_subscriber_status(sub)
        name = "Subscriber"
        if sub.person:
            name = f"{sub.person.first_name} {sub.person.last_name}".strip()
        elif sub.organization:
            name = sub.organization.name or "Subscriber"
        subscriber_type = "organization" if sub.person and sub.person.organization_id else "person"
        writer.writerow(
            [
                str(sub.id),
                name,
                subscriber_type or "",
                status.value if status else "",
                sub.created_at.isoformat() if sub.created_at else "",
            ]
        )

    content = output.getvalue()
    output.close()

    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=subscribers.csv"},
    )


@router.get("/churn", response_class=HTMLResponse)
def reports_churn(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user

    # Get all subscribers
    all_subscribers = subscriber_service.subscribers.list(
        db=db,
        subscriber_type=None,
        organization_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )
    total_subscribers = len(all_subscribers)
    for sub in all_subscribers:
        sub.status = _derive_subscriber_status(sub)

    # Get cancelled subscribers
    cancelled_subscribers = [
        s for s in all_subscribers
        if s.status == AccountStatus.canceled
    ]
    cancelled_count = len(cancelled_subscribers)

    # Get suspended/at-risk subscribers
    at_risk_subscribers = [
        s for s in all_subscribers
        if s.status == AccountStatus.suspended
    ]
    at_risk_count = len(at_risk_subscribers)

    # Calculate churn rate
    churn_rate = (cancelled_count / total_subscribers * 100) if total_subscribers > 0 else 0
    retention_rate = 100 - churn_rate

    # Recent cancellations (sorted by updated_at desc)
    recent_cancellations = sorted(
        cancelled_subscribers,
        key=lambda x: x.updated_at if x.updated_at else datetime.min,
        reverse=True
    )[:10]

    # Mock churn reasons (would come from actual data)
    churn_reasons = {
        "price": cancelled_count // 3 if cancelled_count > 0 else 0,
        "service_quality": cancelled_count // 4 if cancelled_count > 0 else 0,
        "moved": cancelled_count // 5 if cancelled_count > 0 else 0,
        "competitor": cancelled_count // 6 if cancelled_count > 0 else 0,
    }

    context = {
        "request": request,
        "active_page": "reports-churn",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "churn_rate": churn_rate,
        "retention_rate": retention_rate,
        "cancelled_count": cancelled_count,
        "at_risk_count": at_risk_count,
        "churn_reasons": churn_reasons,
        "recent_cancellations": recent_cancellations,
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/churn.html", context)


@router.get("/churn/export")
def reports_churn_export(days: int | None = None, db: Session = Depends(get_db)):
    all_subscribers = subscriber_service.subscribers.list(
        db=db,
        subscriber_type=None,
        organization_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )
    for sub in all_subscribers:
        sub.status = _derive_subscriber_status(sub)

    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        all_subscribers = [
            sub
            for sub in all_subscribers
            if _ensure_aware_datetime(sub.updated_at)
            and _ensure_aware_datetime(sub.updated_at) >= cutoff
        ]

    total_subscribers = len(all_subscribers)
    cancelled_subscribers = [
        sub for sub in all_subscribers if sub.status == AccountStatus.canceled
    ]
    at_risk_subscribers = [
        sub for sub in all_subscribers if sub.status == AccountStatus.suspended
    ]
    churn_rate = (
        (len(cancelled_subscribers) / total_subscribers * 100)
        if total_subscribers > 0
        else 0
    )
    retention_rate = 100 - churn_rate

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["metric", "value"])
    writer.writerow(["total_subscribers", total_subscribers])
    writer.writerow(["cancelled_count", len(cancelled_subscribers)])
    writer.writerow(["at_risk_count", len(at_risk_subscribers)])
    writer.writerow(["churn_rate_percent", f"{churn_rate:.2f}"])
    writer.writerow(["retention_rate_percent", f"{retention_rate:.2f}"])
    writer.writerow(["report_window_days", days or ""])
    writer.writerow([])
    writer.writerow(["subscriber_id", "name", "status", "updated_at"])
    for sub in cancelled_subscribers:
        name = "Subscriber"
        if sub.person:
            name = f"{sub.person.first_name} {sub.person.last_name}".strip()
        elif sub.organization:
            name = sub.organization.name or "Subscriber"
        writer.writerow(
            [
                str(sub.id),
                name,
                sub.status.value if sub.status else "",
                sub.updated_at.isoformat() if sub.updated_at else "",
            ]
        )

    content = output.getvalue()
    output.close()

    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=churn-report.csv"},
    )


@router.get("/network", response_class=HTMLResponse)
def reports_network(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user

    # Get OLTs
    olts = network_service.olt_devices.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )
    total_olts = len(olts)
    active_olts = sum(1 for olt in olts if olt.is_active)

    # Get ONTs
    onts = network_service.ont_units.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )
    total_onts = len(onts)
    connected_onts = sum(1 for ont in onts if ont.is_active)

    # Recent ONT activity (sorted by updated_at)
    recent_ont_activity = sorted(
        onts,
        key=lambda x: x.updated_at if x.updated_at else datetime.min,
        reverse=True
    )[:10]

    # Get IP pools
    ip_pools = network_service.ip_pools.list(
        db=db,
        ip_version=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )
    assignments = network_service.ip_assignments.list(
        db=db,
        subscriber_id=None,
        subscription_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )
    used_ips = 0
    total_ips = 0
    pool_data = []
    for pool in ip_pools:
        # Get blocks for this pool
        blocks = network_service.ip_blocks.list(
            db=db,
            pool_id=str(pool.id),
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        pool_used = 0
        pool_total = 0
        pool_ip_version = getattr(pool.ip_version, "value", pool.ip_version)
        if pool_ip_version == "ipv6":
            addresses = network_service.ipv6_addresses.list(
                db=db,
                pool_id=str(pool.id),
                is_reserved=None,
                order_by="created_at",
                order_dir="desc",
                limit=10000,
                offset=0,
            )
            address_ids = {str(address.id) for address in addresses}
            pool_used = sum(
                1 for assignment in assignments
                if assignment.ipv6_address_id and str(assignment.ipv6_address_id) in address_ids
            )
            pool_total = len(addresses)
        else:
            addresses = network_service.ipv4_addresses.list(
                db=db,
                pool_id=str(pool.id),
                is_reserved=None,
                order_by="created_at",
                order_dir="desc",
                limit=10000,
                offset=0,
            )
            address_ids = {str(address.id) for address in addresses}
            pool_used = sum(
                1 for assignment in assignments
                if assignment.ipv4_address_id and str(assignment.ipv4_address_id) in address_ids
            )
            pool_total = len(addresses)
        if pool_total == 0:
            for block in blocks:
                # Estimate total from CIDR (simplified)
                pool_total += 256  # Default estimate
        pool_data.append({
            "name": pool.name,
            "cidr": pool.cidr,
            "used_count": pool_used,
            "total_count": pool_total if pool_total > 0 else 256,
        })
        used_ips += pool_used
        total_ips += pool_total

    ip_pool_usage = (used_ips / total_ips * 100) if total_ips > 0 else 0

    # Get VLANs
    vlans = network_service.vlans.list(
        db=db,
        region_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )
    active_vlans = sum(1 for v in vlans if v.is_active)

    context = {
        "request": request,
        "active_page": "reports-network",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "total_olts": total_olts,
        "active_olts": active_olts,
        "total_onts": total_onts,
        "connected_onts": connected_onts,
        "ip_pool_usage": ip_pool_usage,
        "used_ips": used_ips,
        "total_ips": total_ips,
        "active_vlans": active_vlans,
        "olts": olts,
        "ip_pools": pool_data,
        "recent_ont_activity": recent_ont_activity,
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/network.html", context)


@router.get("/network/export")
def reports_network_export(hours: int | None = None, db: Session = Depends(get_db)):
    olts = network_service.olt_devices.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )
    total_olts = len(olts)
    active_olts = sum(1 for olt in olts if olt.is_active)

    onts = network_service.ont_units.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )
    if hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        onts = [
            ont
            for ont in onts
            if _ensure_aware_datetime(ont.updated_at)
            and _ensure_aware_datetime(ont.updated_at) >= cutoff
        ]
    total_onts = len(onts)
    connected_onts = sum(1 for ont in onts if ont.is_active)

    ip_pools = network_service.ip_pools.list(
        db=db,
        ip_version=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )
    assignments = network_service.ip_assignments.list(
        db=db,
        subscriber_id=None,
        subscription_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )
    used_ips = 0
    total_ips = 0
    pool_data = []
    for pool in ip_pools:
        blocks = network_service.ip_blocks.list(
            db=db,
            pool_id=str(pool.id),
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        pool_used = 0
        pool_total = 0
        pool_ip_version = getattr(pool.ip_version, "value", pool.ip_version)
        if pool_ip_version == "ipv6":
            addresses = network_service.ipv6_addresses.list(
                db=db,
                pool_id=str(pool.id),
                is_reserved=None,
                order_by="created_at",
                order_dir="desc",
                limit=10000,
                offset=0,
            )
            address_ids = {str(address.id) for address in addresses}
            pool_used = sum(
                1
                for assignment in assignments
                if assignment.ipv6_address_id
                and str(assignment.ipv6_address_id) in address_ids
            )
            pool_total = len(addresses)
        else:
            addresses = network_service.ipv4_addresses.list(
                db=db,
                pool_id=str(pool.id),
                is_reserved=None,
                order_by="created_at",
                order_dir="desc",
                limit=10000,
                offset=0,
            )
            address_ids = {str(address.id) for address in addresses}
            pool_used = sum(
                1
                for assignment in assignments
                if assignment.ipv4_address_id
                and str(assignment.ipv4_address_id) in address_ids
            )
            pool_total = len(addresses)
        if pool_total == 0:
            for _block in blocks:
                pool_total += 256
        pool_total = pool_total if pool_total > 0 else 256
        pool_data.append(
            {
                "name": pool.name,
                "cidr": pool.cidr,
                "used_count": pool_used,
                "total_count": pool_total,
            }
        )
        used_ips += pool_used
        total_ips += pool_total

    ip_pool_usage = (used_ips / total_ips * 100) if total_ips > 0 else 0

    vlans = network_service.vlans.list(
        db=db,
        region_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )
    active_vlans = sum(1 for v in vlans if v.is_active)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["metric", "value"])
    writer.writerow(["total_olts", total_olts])
    writer.writerow(["active_olts", active_olts])
    writer.writerow(["total_onts", total_onts])
    writer.writerow(["connected_onts", connected_onts])
    writer.writerow(["used_ips", used_ips])
    writer.writerow(["total_ips", total_ips])
    writer.writerow(["ip_pool_usage_percent", f"{ip_pool_usage:.2f}"])
    writer.writerow(["active_vlans", active_vlans])
    writer.writerow(["report_window_hours", hours or ""])
    writer.writerow([])
    writer.writerow(["pool_name", "cidr", "used_count", "total_count", "usage_percent"])
    for pool in pool_data:
        usage = (pool["used_count"] / pool["total_count"] * 100) if pool["total_count"] else 0
        writer.writerow(
            [
                pool["name"],
                pool["cidr"],
                pool["used_count"],
                pool["total_count"],
                f"{usage:.2f}",
            ]
        )

    content = output.getvalue()
    output.close()

    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=network-usage.csv"},
    )


@router.get("/technician", response_class=HTMLResponse)
def reports_technician(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user
    # from app.services import dispatch as dispatch_service
    from app.models.provisioning import ServiceOrderStatus

    # Get technicians
    # technicians = dispatch_service.technicians.list(
    #     db=db,
    #     person_id=None,
    #     region=None,
    #     is_active=True,
    #     order_by="created_at",
    #     order_dir="desc",
    #     limit=100,
    #     offset=0,
    # )
    # total_technicians = len(technicians)
    technicians = []
    total_technicians = 0

    # Get service orders
    all_orders = operations_service.service_orders.list(
        db=db,
        account_id=None,
        subscription_id=None,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )

    # Count completed jobs
    completed_orders = [
        o for o in all_orders
        if o.status == ServiceOrderStatus.active
    ]
    jobs_completed = len(completed_orders)

    # Recent completions
    recent_completions = sorted(
        completed_orders,
        key=lambda x: x.updated_at or datetime.min,
        reverse=True
    )[:10]

    # Calculate technician stats (based on technician profiles, not order assignments)
    technician_stats = []
    for tech in technicians:
        tech_name = f"{tech.person.first_name} {tech.person.last_name}" if tech.person else f"Technician #{str(tech.id)[:8]}"
        technician_stats.append({
            "name": tech_name,
            "total_jobs": 0,  # Would need proper assignment tracking
            "completed_jobs": 0,
            "avg_hours": 2.5,  # Would calculate from actual data
            "rating": 4,  # Would come from ratings
        })

    # Sort by name
    technician_stats.sort(key=lambda x: x["name"])

    # Job type breakdown by status
    job_type_breakdown = {}
    for order in all_orders:
        status_name = order.status.value if order.status else "unknown"
        job_type_breakdown[status_name] = job_type_breakdown.get(status_name, 0) + 1

    context = {
        "request": request,
        "active_page": "reports-technician",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "total_technicians": total_technicians,
        "jobs_completed": jobs_completed,
        "avg_completion_hours": 2.5,  # Would calculate from actual data
        "first_visit_rate": 85.0,  # Would calculate from actual data
        "technician_stats": technician_stats[:10],
        "job_type_breakdown": job_type_breakdown,
        "recent_completions": recent_completions,
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/technician.html", context)


@router.get("/technician/export")
def reports_technician_export(days: int | None = None, db: Session = Depends(get_db)):
    # from app.services import dispatch as dispatch_service
    from app.models.provisioning import ServiceOrderStatus

    # technicians = dispatch_service.technicians.list(
    #     db=db,
    #     person_id=None,
    #     region=None,
    #     is_active=True,
    #     order_by="created_at",
    #     order_dir="desc",
    #     limit=5000,
    #     offset=0,
    # )
    technicians = []
    all_orders = operations_service.service_orders.list(
        db=db,
        account_id=None,
        subscription_id=None,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        all_orders = [order for order in all_orders if order.created_at and order.created_at >= cutoff]

    completed_orders = [
        order for order in all_orders
        if order.status == ServiceOrderStatus.active
    ]

    technician_stats = []
    for tech in technicians:
        tech_name = (
            f"{tech.person.first_name} {tech.person.last_name}"
            if tech.person
            else f"Technician #{str(tech.id)[:8]}"
        )
        technician_stats.append(
            {
                "name": tech_name,
                "total_jobs": 0,
                "completed_jobs": 0,
                "avg_hours": 2.5,
                "rating": 4,
            }
        )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "technician",
            "total_jobs",
            "completed_jobs",
            "avg_completion_hours",
            "rating",
            "jobs_completed_total",
            "report_window_days",
        ]
    )
    jobs_completed = len(completed_orders)
    for tech in technician_stats:
        writer.writerow(
            [
                tech["name"],
                tech["total_jobs"],
                tech["completed_jobs"],
                tech["avg_hours"],
                tech["rating"],
                jobs_completed,
                days or "",
            ]
        )

    content = output.getvalue()
    output.close()

    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=technician-performance.csv"},
    )
