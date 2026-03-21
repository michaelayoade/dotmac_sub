"""Customer portal web routes."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.db import SessionLocal, get_db
from app.models.bandwidth import BandwidthSample
from app.models.catalog import Subscription
from app.services import crm_portal, customer_portal
from app.services import web_network_speedtests as web_network_speedtests_service
from app.services.bandwidth import bandwidth_samples
from app.services.customer_portal_context import (
    get_restricted_dashboard_context,
    is_subscriber_restricted,
)
from app.services.metrics_store import get_metrics_store
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])

logger = __import__("logging").getLogger(__name__)


def _emit_customer_event(db: Session, event_name: str, payload: dict) -> None:
    """Emit a customer portal event (non-blocking)."""
    try:
        from app.services.events import emit_event
        from app.services.events.types import EventType

        event_type = getattr(EventType, event_name, None)
        if event_type:
            emit_event(db, event_type, payload, actor="customer")
    except Exception as e:
        logger.warning("Failed to emit customer event %s: %s", event_name, e)


def _resolve_customer_subscription(db: Session, customer: dict) -> Subscription | None:
    account_id, session_subscription_id = customer_portal.resolve_customer_account(customer, db)
    account_id_str = str(account_id) if account_id else None

    if session_subscription_id:
        subscription = db.get(Subscription, session_subscription_id)
        if subscription and (
            not account_id_str or str(subscription.subscriber_id) == account_id_str
        ):
            return subscription

    if not account_id_str:
        return None

    try:
        return bandwidth_samples.get_user_active_subscription(db, {"account_id": account_id_str})
    except HTTPException:
        return None


def _resolve_subscriber_id(customer: dict) -> str:
    """Extract subscriber_id from customer session for CRM lookups."""
    return str(customer.get("subscriber_id") or customer.get("session", {}).get("subscriber_id", ""))


def _resolve_allowed_subscriber_ids(customer: dict, db: Session) -> list[str]:
    """Resolve all customer-visible subscriber/account IDs for CRM access checks."""
    allowed = customer_portal.get_allowed_account_ids(customer, db)
    if allowed:
        return [str(item) for item in allowed if item]
    fallback = _resolve_subscriber_id(customer)
    return [fallback] if fallback else []


def _render_dashboard(request: Request, db: Session, customer: dict, next_url: str) -> Response:
    """Render full or restricted dashboard based on subscriber status."""
    subscriber_id = customer.get("subscriber_id")
    if subscriber_id and is_subscriber_restricted(db, subscriber_id):
        ctx = get_restricted_dashboard_context(db, customer)
        return templates.TemplateResponse(
            "customer/dashboard/restricted.html",
            {"request": request, "customer": customer, **ctx, "active_page": "dashboard"},
        )
    dashboard_context = customer_portal.get_dashboard_context(db, customer)
    return templates.TemplateResponse(
        "customer/dashboard/index.html",
        {"request": request, "customer": customer, **dashboard_context, "active_page": "dashboard"},
    )

@router.get("", response_class=HTMLResponse)
def portal_home(request: Request, db: Session = Depends(get_db)) -> Response:
    """Customer portal dashboard."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal", status_code=303)
    return _render_dashboard(request, db, customer, "/portal")


@router.get("/dashboard", response_class=HTMLResponse)
def customer_dashboard(request: Request, db: Session = Depends(get_db)) -> Response:
    """Customer dashboard with account overview."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/dashboard", status_code=303)
    return _render_dashboard(request, db, customer, "/portal/dashboard")


@router.get("/support", response_class=HTMLResponse)
def customer_support(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Customer support tickets (CRM-backed)."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/support", status_code=303)

    subscriber_ids = _resolve_allowed_subscriber_ids(customer, db)
    context = crm_portal.tickets_list_context(request, db, customer, subscriber_ids)
    return templates.TemplateResponse("customer/support/index.html", context)


@router.get("/support/new", response_class=HTMLResponse)
def customer_support_new(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/support/new", status_code=303)
    context = crm_portal.ticket_create_context(request, customer)
    return templates.TemplateResponse("customer/support/new.html", context)


@router.post("/support/new", response_class=HTMLResponse)
def customer_support_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    priority: str = Form("normal"),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    subscriber_id, _subscription_id = customer_portal.resolve_customer_account(customer, db)
    subscriber_lookup = str(subscriber_id or _resolve_subscriber_id(customer) or "")
    result = crm_portal.handle_ticket_create(
        db,
        customer,
        subscriber_lookup,
        title,
        description,
        priority,
    )
    if result["success"]:
        ticket = result["ticket"]
        ticket_id = ticket.get("id", "")
        _emit_customer_event(db, "customer_ticket_created", {
            "ticket_id": str(ticket_id),
            "subscriber_id": subscriber_lookup,
        })
        return RedirectResponse(url=f"/portal/support/{ticket_id}", status_code=303)
    context = crm_portal.ticket_create_context(request, customer)
    context["crm_error"] = True
    context["crm_error_message"] = result.get("error") or "Unable to create ticket."
    context["form_values"] = {
        "title": title,
        "description": description,
        "priority": priority,
    }
    return templates.TemplateResponse(
        "customer/support/new.html",
        context,
        status_code=400,
    )


@router.get("/support/{ticket_id}", response_class=HTMLResponse)
def customer_support_detail(
    request: Request,
    ticket_id: str,
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/support", status_code=303)

    subscriber_ids = _resolve_allowed_subscriber_ids(customer, db)
    context = crm_portal.ticket_detail_context(
        request, db, customer, subscriber_ids, ticket_id
    )
    return templates.TemplateResponse("customer/support/detail.html", context)


@router.post("/support/{ticket_id}/comment", response_class=HTMLResponse)
def customer_support_add_comment(
    request: Request,
    ticket_id: str,
    body: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    subscriber_ids = _resolve_allowed_subscriber_ids(customer, db)
    result = crm_portal.handle_ticket_comment(db, customer, subscriber_ids, ticket_id, body)
    if not result.get("success"):
        context = crm_portal.ticket_detail_context(
            request, db, customer, subscriber_ids, ticket_id
        )
        context["crm_error"] = True
        context["crm_error_message"] = result.get("error") or "Unable to add comment."
        return templates.TemplateResponse(
            "customer/support/detail.html",
            context,
            status_code=400,
        )
    return RedirectResponse(url=f"/portal/support/{ticket_id}", status_code=303)


# ── Work Orders (CRM-backed) ─────────────────────────────────────────────


@router.get("/work-orders", response_class=HTMLResponse)
def customer_work_orders(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Customer work orders list (CRM-backed)."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/work-orders", status_code=303)
    subscriber_ids = _resolve_allowed_subscriber_ids(customer, db)
    context = crm_portal.work_orders_list_context(request, db, customer, subscriber_ids)
    return templates.TemplateResponse("customer/work-orders/index.html", context)


@router.get("/work-orders/{work_order_id}", response_class=HTMLResponse)
def customer_work_order_detail(
    request: Request,
    work_order_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Customer work order detail (CRM-backed)."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/work-orders", status_code=303)
    subscriber_ids = _resolve_allowed_subscriber_ids(customer, db)
    context = crm_portal.work_order_detail_context(
        request, db, customer, subscriber_ids, work_order_id
    )
    return templates.TemplateResponse("customer/work-orders/detail.html", context)


@router.get("/billing", response_class=HTMLResponse)
def customer_billing(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """Customer billing history."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/billing", status_code=303
        )

    billing_data = customer_portal.get_billing_page(
        db, customer, status=status, page=page, per_page=per_page
    )

    from datetime import UTC, datetime

    return templates.TemplateResponse(
        "customer/billing/index.html",
        {
            "request": request,
            "customer": customer,
            **billing_data,
            "active_page": "billing",
            "now": datetime.now(UTC),
        },
    )


@router.get("/billing/invoices", response_class=HTMLResponse)
def customer_billing_invoices_redirect(request: Request) -> RedirectResponse:
    target = "/portal/billing"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/billing/invoices/{invoice_id}", response_class=HTMLResponse)
def customer_invoice_detail(
    request: Request,
    invoice_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """View invoice details."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    detail = customer_portal.get_invoice_detail(db, customer, str(invoice_id))
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/billing/invoice.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "billing",
        },
    )


@router.get("/billing/invoices/{invoice_id}/pdf", response_class=HTMLResponse)
def customer_invoice_pdf(
    request: Request,
    invoice_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Download invoice PDF — triggers generation if needed."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    # Verify access
    detail = customer_portal.get_invoice_detail(db, customer, str(invoice_id))
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )

    from app.models.billing import Invoice
    from app.services import billing_invoice_pdf as billing_invoice_pdf_service
    from app.services.file_storage import build_content_disposition

    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )

    # Check for existing completed export
    latest_export = billing_invoice_pdf_service.get_latest_export(db, str(invoice_id))
    latest_export = billing_invoice_pdf_service.maybe_finalize_stalled_export(db, latest_export)

    if billing_invoice_pdf_service.is_export_cache_valid(db, invoice, latest_export):
        try:
            from starlette.responses import StreamingResponse

            if latest_export is None:
                raise ValueError("Missing invoice export")
            stream = billing_invoice_pdf_service.stream_export(db, latest_export)
            headers = {
                "Content-Disposition": build_content_disposition(
                    billing_invoice_pdf_service.download_filename(invoice)
                ),
            }
            if stream.content_length is not None:
                headers["Content-Length"] = str(stream.content_length)
            return StreamingResponse(
                stream.chunks,
                media_type=stream.content_type or "application/pdf",
                headers=headers,
            )
        except Exception:
            pass

    # Queue generation if not ready
    subscriber_id = customer.get("subscriber_id") or customer.get("session", {}).get("subscriber_id")
    billing_invoice_pdf_service.queue_export(
        db, str(invoice_id), requested_by_id=subscriber_id,
    )
    # Redirect back with notice
    return RedirectResponse(
        url=f"/portal/billing/invoices/{invoice_id}?pdf_notice=generating",
        status_code=303,
    )


@router.get("/usage", response_class=HTMLResponse)
def customer_usage(
    request: Request,
    period: str = Query("current", pattern="^(current|last)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> Response:
    """Customer usage dashboard."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/usage", status_code=303
        )
    usage_data = customer_portal.get_usage_page(
        db, customer, period=period, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/usage/index.html",
        {
            "request": request,
            "customer": customer,
            **usage_data,
            "active_page": "usage",
        },
    )


@router.get("/bandwidth/my/series")
def customer_bandwidth_series(
    request: Request,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    interval: str = Query(default="auto", pattern="^(auto|1m|5m|1h)$"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    subscription = _resolve_customer_subscription(db, customer)
    if not subscription:
        return JSONResponse({"data": [], "total": 0, "source": "postgres"})

    result = anyio.from_thread.run(
        bandwidth_samples.get_bandwidth_series,
        db,
        subscription.id,
        start_at,
        end_at,
        interval,
    )
    return JSONResponse(content=jsonable_encoder(result))


@router.get("/bandwidth/my/stats")
def customer_bandwidth_stats(
    request: Request,
    period: str = Query(default="24h", pattern="^(1h|24h|7d|30d)$"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    subscription = _resolve_customer_subscription(db, customer)
    if not subscription:
        return JSONResponse(
            {
                "current_rx_bps": 0,
                "current_tx_bps": 0,
                "peak_rx_bps": 0,
                "peak_tx_bps": 0,
                "total_rx_bytes": 0,
                "total_tx_bytes": 0,
                "sample_count": 0,
            }
        )

    stats = anyio.from_thread.run(
        bandwidth_samples.get_bandwidth_stats,
        db,
        subscription.id,
        period,
    )
    return JSONResponse(stats)


@router.get("/bandwidth/my/live")
def customer_bandwidth_live(
    request: Request,
    db: Session = Depends(get_db),
):
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    subscription = _resolve_customer_subscription(db, customer)
    if not subscription:
        return JSONResponse({"detail": "No active subscription found"}, status_code=404)

    subscription_id = subscription.id

    async def event_generator():
        metrics_store = get_metrics_store()

        while True:
            if await request.is_disconnected():
                break

            current = {"rx_bps": 0.0, "tx_bps": 0.0}
            try:
                current = await metrics_store.get_current_bandwidth(str(subscription_id))
            except Exception:
                pass

            try:
                if current.get("rx_bps", 0) <= 0 and current.get("tx_bps", 0) <= 0:
                    sse_db = SessionLocal()
                    try:
                        cutoff = datetime.now(UTC) - timedelta(minutes=2)
                        latest_sample = (
                            sse_db.query(BandwidthSample)
                            .filter(
                                BandwidthSample.subscription_id == subscription_id,
                                BandwidthSample.sample_at >= cutoff,
                            )
                            .order_by(BandwidthSample.sample_at.desc())
                            .first()
                        )
                        if latest_sample:
                            current = {
                                "rx_bps": float(latest_sample.rx_bps or 0),
                                "tx_bps": float(latest_sample.tx_bps or 0),
                            }
                    finally:
                        sse_db.close()
            except Exception:
                pass

            now = datetime.now(UTC)
            yield {
                "event": "bandwidth",
                "data": json.dumps(
                    {
                        "timestamp": now.isoformat(),
                        "rx_bps": float(current.get("rx_bps", 0) or 0),
                        "tx_bps": float(current.get("tx_bps", 0) or 0),
                    }
                ),
            }
            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())


@router.get("/speedtest", response_class=HTMLResponse)
def customer_speedtest(
    request: Request,
    saved: str | None = None,
    subscription_id: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Customer portal speed test."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/speedtest", status_code=303
        )
    account_id, resolved_subscription_id = customer_portal.resolve_customer_account(
        customer, db
    )
    subscriber_id = account_id or (str(customer.get("subscriber_id")) if customer.get("subscriber_id") else None)
    if not subscriber_id:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Subscriber account not found"},
            status_code=404,
        )
    selected_subscription = subscription_id or resolved_subscription_id
    page_data = web_network_speedtests_service.portal_page_data(
        db,
        subscriber_id=subscriber_id,
        subscription_id=selected_subscription,
    )

    return templates.TemplateResponse(
        "customer/services/speedtest.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "saved": bool(saved),
            "active_page": "speedtest",
        },
    )


@router.post("/speedtest", response_class=HTMLResponse)
def customer_speedtest_submit(
    request: Request,
    download_mbps: float = Form(...),
    upload_mbps: float = Form(...),
    latency_ms: float | None = Form(None),
    jitter_ms: float | None = Form(None),
    server_name: str | None = Form(None),
    subscription_id: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    account_id, resolved_subscription_id = customer_portal.resolve_customer_account(
        customer, db
    )
    subscriber_id = account_id or (str(customer.get("subscriber_id")) if customer.get("subscriber_id") else None)
    if not subscriber_id:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Subscriber account not found"},
            status_code=404,
        )

    web_network_speedtests_service.create_customer_speedtest(
        db,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id or resolved_subscription_id,
        download_mbps=download_mbps,
        upload_mbps=upload_mbps,
        latency_ms=latency_ms,
        jitter_ms=jitter_ms,
        server_name=server_name,
        user_agent=request.headers.get("user-agent"),
    )
    return RedirectResponse("/portal/speedtest?saved=1", status_code=303)


@router.get("/speedtest/probe-download")
def customer_speedtest_probe_download(
    request: Request,
    size_mb: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
) -> Response:
    """Download payload for browser speed test probing."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    payload = (b"dotmac-speedtest-" * 4096) * size_mb
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


_PROBE_UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # 25 MB


@router.post("/speedtest/probe-upload")
def customer_speedtest_probe_upload(
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Upload probe endpoint for browser speed test."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    content_length = int(request.headers.get("content-length", 0))
    if content_length > _PROBE_UPLOAD_MAX_BYTES:
        return JSONResponse(
            {"detail": f"Payload too large (max {_PROBE_UPLOAD_MAX_BYTES} bytes)"},
            status_code=413,
        )
    return JSONResponse({"bytes_received": content_length})


@router.get("/account", response_class=HTMLResponse)
def customer_account_root_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/portal/profile", status_code=303)


@router.get("/account/{path:path}", response_class=HTMLResponse)
def customer_account_path_redirect(request: Request, path: str) -> RedirectResponse:
    target = f"/portal/{path}" if path else "/portal/profile"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/services", response_class=HTMLResponse)
def customer_services(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """Customer active services."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/services", status_code=303
        )
    services_data = customer_portal.get_services_page(
        db, customer, status=status, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/services/index.html",
        {
            "request": request,
            "customer": customer,
            **services_data,
            "active_page": "services",
        },
    )


@router.get("/services/{subscription_id}", response_class=HTMLResponse)
def customer_service_detail(
    request: Request,
    subscription_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Customer service detail page."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    detail = customer_portal.get_service_detail(db, customer, str(subscription_id))
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Subscription not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/services/detail.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "services",
        },
    )


@router.get("/installations", response_class=HTMLResponse)
def customer_installations(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """Customer installation appointments."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/installations", status_code=303
        )
    appt_data = customer_portal.get_customer_appointments(
        db=db,
        customer=customer,
        status=status,
        page=page,
        per_page=per_page,
    )

    return templates.TemplateResponse(
        "customer/installations/index.html",
        {
            "request": request,
            "customer": customer,
            "appointments": appt_data["appointments"],
            "status": status,
            "page": page,
            "per_page": per_page,
            "total": appt_data["total"],
            "total_pages": appt_data["total_pages"],
            "active_page": "installations",
        },
    )


@router.get("/service-orders", response_class=HTMLResponse)
def customer_service_orders(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """Customer service orders."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/service-orders", status_code=303
        )
    orders_data = customer_portal.get_service_orders_page(
        db, customer, status=status, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/service-orders/index.html",
        {
            "request": request,
            "customer": customer,
            **orders_data,
            "active_page": "service-orders",
        },
    )


@router.get("/installations/{appointment_id}", response_class=HTMLResponse)
def customer_installation_detail(
    request: Request,
    appointment_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Customer installation appointment detail view."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    detail = customer_portal.get_installation_detail(db, customer, str(appointment_id))
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Installation not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/installations/detail.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "installations",
        },
    )


@router.get("/service-orders/{service_order_id}", response_class=HTMLResponse)
def customer_service_order_detail(
    request: Request,
    service_order_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Customer service order detail view."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    detail = customer_portal.get_service_order_detail(
        db, customer, str(service_order_id)
    )
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Service order not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/service-orders/detail.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "service-orders",
        },
    )


@router.get("/notifications", response_class=HTMLResponse)
def customer_notifications(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=5, le=100),
    db: Session = Depends(get_db),
) -> Response:
    """Customer notification inbox."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/notifications", status_code=303)
    return templates.TemplateResponse(
        "customer/notifications/index.html",
        {
            "request": request,
            "customer": customer,
            **customer_portal.get_notifications_page(
                db,
                customer,
                page=page,
                per_page=per_page,
            ),
            "active_page": "notifications",
        },
    )


@router.get("/profile", response_class=HTMLResponse)
def customer_profile(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Customer profile settings."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/profile", status_code=303
        )
    return templates.TemplateResponse(
        "customer/profile/index.html",
        {
            "request": request,
            "customer": customer,
            "active_page": "profile",
        },
    )


@router.post("/profile", response_class=HTMLResponse)
def customer_update_profile(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Update customer profile."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    subscriber_id = customer.get("subscriber_id")
    if subscriber_id:
        from app.services.web_customer_actions import update_customer_profile

        update_customer_profile(
            db, subscriber_id=subscriber_id, name=name, email=email, phone=phone
        )
        customer = get_current_customer_from_request(request, db)

    return templates.TemplateResponse(
        "customer/profile/index.html",
        {
            "request": request,
            "customer": customer,
            "success": "Profile updated successfully",
            "active_page": "profile",
        },
    )


# =============================================================================
# Online Payment (Paystack / Flutterwave)
# =============================================================================


@router.get("/billing/pay", response_class=HTMLResponse)
def customer_pay_invoice(
    request: Request,
    invoice: str = Query(...),
    db: Session = Depends(get_db),
) -> Response:
    """Show payment page for an invoice."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    page_data = customer_portal.get_payment_page(db, customer, invoice)
    if not page_data:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Invoice not found or already paid"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/billing/pay.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "active_page": "billing",
        },
    )


@router.get("/billing/pay/verify", response_class=HTMLResponse)
def customer_verify_payment(
    request: Request,
    reference: str = Query(...),
    provider: str | None = Query(None),
    db: Session = Depends(get_db),
) -> Response:
    """Verify online payment and record it."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    try:
        result = customer_portal.verify_and_record_payment(
            db, customer, reference, provider=provider
        )
        return templates.TemplateResponse(
            "customer/billing/pay_success.html",
            {
                "request": request,
                "customer": customer,
                "payment": result["payment"],
                "invoice": result["invoice"],
                "amount": result["amount"],
                "reference": result["reference"],
                "active_page": "billing",
            },
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": str(exc)},
            status_code=400,
        )


# =============================================================================
# Plan Change Self-Service
# =============================================================================


@router.get("/services/{subscription_id}/change", response_class=HTMLResponse)
def customer_change_plan(
    request: Request,
    subscription_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Show available plans for changing subscription."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    page_data = customer_portal.get_change_plan_page(db, customer, str(subscription_id))
    if not page_data:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Subscription not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/services/change_plan.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "active_page": "services",
        },
    )


@router.post("/services/{subscription_id}/change", response_class=HTMLResponse)
def customer_submit_change_plan(
    request: Request,
    subscription_id: UUID,
    offer_id: str = Form(...),
    notes: str = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Instantly apply a plan change."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    try:
        customer_portal.apply_instant_plan_change(
            db=db,
            customer=customer,
            subscription_id=str(subscription_id),
            offer_id=offer_id,
            notes=notes,
        )
        return RedirectResponse(
            url=f"/portal/services/{subscription_id}?plan_changed=true",
            status_code=303,
        )
    except ValueError as exc:
        error_ctx = customer_portal.get_change_plan_error_context(
            db, str(subscription_id)
        )
        return templates.TemplateResponse(
            "customer/services/change_plan.html",
            {
                "request": request,
                "customer": customer,
                **error_ctx,
                "error": str(exc),
                "active_page": "services",
            },
            status_code=400,
        )
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception("Plan change error for %s", subscription_id)
        raise


@router.get("/change-requests", response_class=HTMLResponse)
def customer_change_requests(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """List pending plan change requests."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/change-requests", status_code=303
        )
    change_data = customer_portal.get_change_requests_page(
        db, customer, status=status, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/services/change_requests.html",
        {
            "request": request,
            "customer": customer,
            **change_data,
            "active_page": "services",
        },
    )


# =============================================================================
# Payment Arrangements Self-Service
# =============================================================================


@router.get("/billing/arrangements", response_class=HTMLResponse)
def customer_payment_arrangements(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """List payment arrangements for the customer."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/billing/arrangements", status_code=303
        )

    arrangements_data = customer_portal.get_payment_arrangements_page(
        db, customer, status=status, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/billing/arrangements.html",
        {
            "request": request,
            "customer": customer,
            **arrangements_data,
            "active_page": "billing",
        },
    )


@router.get("/billing/arrangements/new", response_class=HTMLResponse)
def customer_new_payment_arrangement(
    request: Request,
    invoice_id: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Show form to request a new payment arrangement."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    page_data = customer_portal.get_new_arrangement_page(
        db, customer, invoice_id=invoice_id
    )

    return templates.TemplateResponse(
        "customer/billing/arrangement_form.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "active_page": "billing",
        },
    )


@router.post("/billing/arrangements", response_class=HTMLResponse)
def customer_submit_payment_arrangement(
    request: Request,
    total_amount: str = Form(...),
    installments: int = Form(...),
    frequency: str = Form(...),
    start_date: str = Form(...),
    invoice_id: str = Form(None),
    notes: str = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Submit a payment arrangement request."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    try:
        customer_portal.submit_payment_arrangement(
            db=db,
            customer=customer,
            total_amount=total_amount,
            installments=installments,
            frequency=frequency,
            start_date=start_date,
            invoice_id=invoice_id,
            notes=notes,
        )
        return RedirectResponse(
            url="/portal/billing/arrangements?submitted=true",
            status_code=303,
        )
    except ValueError as exc:
        account_id = customer.get("account_id")
        account_id_str = str(account_id) if account_id else None
        error_ctx = customer_portal.get_arrangement_error_context(db, account_id_str)
        return templates.TemplateResponse(
            "customer/billing/arrangement_form.html",
            {
                "request": request,
                "customer": customer,
                **error_ctx,
                "error": str(exc),
                "active_page": "billing",
            },
            status_code=400,
        )
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception("Payment arrangement error")
        raise


@router.post("/billing/arrangements/{arrangement_id}/cancel", response_class=HTMLResponse)
def customer_cancel_payment_arrangement(
    request: Request,
    arrangement_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Cancel a payment arrangement."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    from app.services import payment_arrangements as arrangement_service

    account_id = customer.get("account_id")
    try:
        arrangement = arrangement_service.payment_arrangements.get(db, str(arrangement_id))
    except HTTPException:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Payment arrangement not found"},
            status_code=404,
        )

    if not account_id or str(arrangement.subscriber_id) != str(account_id):
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Payment arrangement not found"},
            status_code=404,
        )

    try:
        arrangement_service.payment_arrangements.cancel(
            db, str(arrangement_id), notes="Canceled by customer via portal"
        )
    except HTTPException:
        return RedirectResponse(
            url=f"/portal/billing/arrangements/{arrangement_id}",
            status_code=303,
        )

    return RedirectResponse(url="/portal/billing/arrangements", status_code=303)


@router.get("/billing/arrangements/{arrangement_id}", response_class=HTMLResponse)
def customer_payment_arrangement_detail(
    request: Request,
    arrangement_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """View payment arrangement details."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    detail = customer_portal.get_payment_arrangement_detail(
        db, customer, str(arrangement_id)
    )
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Payment arrangement not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/billing/arrangement_detail.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "billing",
        },
    )
