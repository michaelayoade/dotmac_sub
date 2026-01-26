"""Admin integrations routes."""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.schemas.billing import PaymentProviderCreate
from app.schemas.connector import ConnectorConfigCreate
from app.schemas.integration import IntegrationJobCreate, IntegrationTargetCreate
from app.schemas.webhook import WebhookEndpointCreate, WebhookSubscriptionCreate
from app.services import billing as billing_service
from app.services import connector as connector_service
from app.services import integration as integration_service
from app.services import webhook as webhook_service
from app.services.audit_helpers import recent_activity_for_paths

router = APIRouter(prefix="/integrations", tags=["web-admin-integrations"])
templates = Jinja2Templates(directory="templates")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "integrations") -> dict:
    """Build base template context."""
    from app.web.admin import get_current_user, get_sidebar_stats
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _parse_uuid(value: str | None, field: str, required: bool = True) -> UUID | None:
    if not value:
        if required:
            raise ValueError(f"{field} is required")
        return None
    return UUID(value)


def _parse_json(value: str | None, field: str) -> dict | None:
    if not value or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must be a JSON object")
    return parsed


# ==================== Connectors ====================

@router.get("/connectors", response_class=HTMLResponse)
def connectors_list(request: Request, db: Session = Depends(get_db)):
    """List all connector configurations."""
    connectors = connector_service.connector_configs.list_all(
        db=db,
        connector_type=None,
        auth_type=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    stats = {
        "total": len(connectors),
        "active": sum(1 for c in connectors if c.is_active),
        "by_type": {},
    }

    for c in connectors:
        t = c.connector_type.value if hasattr(c.connector_type, "value") else str(c.connector_type or "custom")
        stats["by_type"][t] = stats["by_type"].get(t, 0) + 1

    context = _base_context(request, db, active_page="connectors")
    context.update(
        {
            "connectors": connectors,
            "stats": stats,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/connectors/index.html", context)


@router.get("/connectors/new", response_class=HTMLResponse)
def connector_new(request: Request, db: Session = Depends(get_db)):
    """New connector form."""
    from app.models.connector import ConnectorType, ConnectorAuthType

    context = _base_context(request, db, active_page="connectors")
    context.update({
        "connector_types": [t.value for t in ConnectorType],
        "auth_types": [t.value for t in ConnectorAuthType],
    })
    return templates.TemplateResponse("admin/integrations/connectors/new.html", context)


@router.post("/connectors", response_class=HTMLResponse)
def connector_create(
    request: Request,
    name: str = Form(...),
    connector_type: str = Form("custom"),
    auth_type: str = Form("none"),
    base_url: str | None = Form(None),
    timeout_sec: str | None = Form(None),
    auth_config: str | None = Form(None),
    headers: str | None = Form(None),
    retry_policy: str | None = Form(None),
    metadata: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        payload = ConnectorConfigCreate(
            name=name.strip(),
            connector_type=connector_type,
            auth_type=auth_type,
            base_url=base_url.strip() if base_url else None,
            timeout_sec=int(timeout_sec) if timeout_sec else None,
            auth_config=_parse_json(auth_config, "auth_config"),
            headers=_parse_json(headers, "headers"),
            retry_policy=_parse_json(retry_policy, "retry_policy"),
            metadata_=_parse_json(metadata, "metadata"),
            notes=notes.strip() if notes else None,
            is_active=is_active,
        )
        connector = connector_service.connector_configs.create(db, payload)
    except Exception as exc:
        from app.models.connector import ConnectorType, ConnectorAuthType

        context = _base_context(request, db, active_page="connectors")
        context.update({
            "connector_types": [t.value for t in ConnectorType],
            "auth_types": [t.value for t in ConnectorAuthType],
            "error": str(exc),
            "form": {
                "name": name,
                "connector_type": connector_type,
                "auth_type": auth_type,
                "base_url": base_url or "",
                "timeout_sec": timeout_sec or "",
                "auth_config": auth_config or "",
                "headers": headers or "",
                "retry_policy": retry_policy or "",
                "metadata": metadata or "",
                "notes": notes or "",
                "is_active": is_active,
            },
        })
        return templates.TemplateResponse(
            "admin/integrations/connectors/new.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/integrations/connectors/{connector.id}", status_code=303
    )


@router.get("/connectors/{connector_id}", response_class=HTMLResponse)
def connector_detail(request: Request, connector_id: str, db: Session = Depends(get_db)):
    """Connector detail view."""
    try:
        connector = connector_service.connector_configs.get(db, connector_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="connectors")
        context["message"] = "The connector you are looking for does not exist."
        return templates.TemplateResponse("admin/errors/404.html", context, status_code=404)

    context = _base_context(request, db, active_page="connectors")
    context.update({"connector": connector})
    return templates.TemplateResponse("admin/integrations/connectors/detail.html", context)


# ==================== Integration Targets ====================

@router.get("/targets", response_class=HTMLResponse)
def targets_list(request: Request, db: Session = Depends(get_db)):
    """List all integration targets."""
    targets = integration_service.integration_targets.list_all(
        db=db,
        target_type=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    stats = {
        "total": len(targets),
        "active": sum(1 for t in targets if t.is_active),
        "by_type": {},
    }

    for t in targets:
        tt = t.target_type.value if hasattr(t.target_type, "value") else str(t.target_type or "custom")
        stats["by_type"][tt] = stats["by_type"].get(tt, 0) + 1

    context = _base_context(request, db, active_page="targets")
    context.update(
        {
            "targets": targets,
            "stats": stats,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/targets/index.html", context)


@router.get("/targets/new", response_class=HTMLResponse)
def target_new(request: Request, db: Session = Depends(get_db)):
    """New target form."""
    from app.models.integration import IntegrationTargetType

    connectors = connector_service.connector_configs.list(
        db=db,
        connector_type=None,
        auth_type=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )

    context = _base_context(request, db, active_page="targets")
    context.update({
        "target_types": [t.value for t in IntegrationTargetType],
        "connectors": connectors,
    })
    return templates.TemplateResponse("admin/integrations/targets/new.html", context)


@router.post("/targets", response_class=HTMLResponse)
def target_create(
    request: Request,
    name: str = Form(...),
    target_type: str = Form("custom"),
    connector_config_id: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        payload = IntegrationTargetCreate(
            name=name.strip(),
            target_type=target_type,
            connector_config_id=_parse_uuid(connector_config_id, "connector_config_id", required=False),
            notes=notes.strip() if notes else None,
            is_active=is_active,
        )
        target = integration_service.integration_targets.create(db, payload)
    except Exception as exc:
        from app.models.integration import IntegrationTargetType

        connectors = connector_service.connector_configs.list(
            db=db,
            connector_type=None,
            auth_type=None,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=1000,
            offset=0,
        )
        context = _base_context(request, db, active_page="targets")
        context.update({
            "target_types": [t.value for t in IntegrationTargetType],
            "connectors": connectors,
            "error": str(exc),
            "form": {
                "name": name,
                "target_type": target_type,
                "connector_config_id": connector_config_id or "",
                "notes": notes or "",
                "is_active": is_active,
            },
        })
        return templates.TemplateResponse(
            "admin/integrations/targets/new.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/integrations/targets/{target.id}", status_code=303
    )


@router.get("/targets/{target_id}", response_class=HTMLResponse)
def target_detail(request: Request, target_id: str, db: Session = Depends(get_db)):
    """Target detail view."""
    try:
        target = integration_service.integration_targets.get(db, target_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="targets")
        context["message"] = "The integration target you are looking for does not exist."
        return templates.TemplateResponse("admin/errors/404.html", context, status_code=404)

    context = _base_context(request, db, active_page="targets")
    context.update({"target": target})
    return templates.TemplateResponse("admin/integrations/targets/detail.html", context)


# ==================== Integration Jobs ====================

@router.get("/jobs", response_class=HTMLResponse)
def jobs_list(request: Request, db: Session = Depends(get_db)):
    """List all integration jobs."""
    jobs = integration_service.integration_jobs.list_all(
        db=db,
        target_id=None,
        job_type=None,
        schedule_type=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    # Get recent runs for each job
    job_runs = {}
    for job in jobs:
        recent_runs = integration_service.integration_runs.list(
            db=db,
            job_id=str(job.id),
            status=None,
            order_by="started_at",
            order_dir="desc",
            limit=5,
            offset=0,
        )
        job_runs[str(job.id)] = recent_runs

    def _schedule_value(item):
        schedule = getattr(item, "schedule_type", None)
        if hasattr(schedule, "value"):
            return schedule.value
        return str(schedule) if schedule else None

    stats = {
        "total": len(jobs),
        "active": sum(1 for j in jobs if j.is_active),
        "manual": sum(1 for j in jobs if _schedule_value(j) == "manual"),
        "scheduled": sum(1 for j in jobs if _schedule_value(j) == "interval"),
    }

    context = _base_context(request, db, active_page="jobs")
    context.update(
        {
            "jobs": jobs,
            "job_runs": job_runs,
            "stats": stats,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/jobs/index.html", context)


@router.get("/jobs/new", response_class=HTMLResponse)
def job_new(request: Request, db: Session = Depends(get_db)):
    """New job form."""
    from app.models.integration import IntegrationJobType, IntegrationScheduleType

    targets = integration_service.integration_targets.list(
        db=db,
        target_type=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )

    context = _base_context(request, db, active_page="jobs")
    context.update({
        "job_types": [t.value for t in IntegrationJobType],
        "schedule_types": [t.value for t in IntegrationScheduleType],
        "targets": targets,
    })
    return templates.TemplateResponse("admin/integrations/jobs/new.html", context)


@router.post("/jobs", response_class=HTMLResponse)
def job_create(
    request: Request,
    target_id: str = Form(...),
    name: str = Form(...),
    job_type: str = Form("sync"),
    schedule_type: str = Form("manual"),
    interval_minutes: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        interval_value = int(interval_minutes) if interval_minutes else None
        if schedule_type == "interval" and not interval_value:
            raise ValueError("interval_minutes is required for interval schedules")
        payload = IntegrationJobCreate(
            target_id=_parse_uuid(target_id, "target_id"),
            name=name.strip(),
            job_type=job_type,
            schedule_type=schedule_type,
            interval_minutes=interval_value,
            notes=notes.strip() if notes else None,
            is_active=is_active,
        )
        job = integration_service.integration_jobs.create(db, payload)
    except Exception as exc:
        from app.models.integration import IntegrationJobType, IntegrationScheduleType

        targets = integration_service.integration_targets.list(
            db=db,
            target_type=None,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=1000,
            offset=0,
        )
        context = _base_context(request, db, active_page="jobs")
        context.update({
            "job_types": [t.value for t in IntegrationJobType],
            "schedule_types": [t.value for t in IntegrationScheduleType],
            "targets": targets,
            "error": str(exc),
            "form": {
                "target_id": target_id,
                "name": name,
                "job_type": job_type,
                "schedule_type": schedule_type,
                "interval_minutes": interval_minutes or "",
                "notes": notes or "",
                "is_active": is_active,
            },
        })
        return templates.TemplateResponse(
            "admin/integrations/jobs/new.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/integrations/jobs/{job.id}", status_code=303
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Job detail view with run history."""
    try:
        job = integration_service.integration_jobs.get(db, job_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="jobs")
        context["message"] = "The integration job you are looking for does not exist."
        return templates.TemplateResponse("admin/errors/404.html", context, status_code=404)

    runs = integration_service.integration_runs.list(
        db=db,
        job_id=str(job.id),
        status=None,
        order_by="started_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )

    context = _base_context(request, db, active_page="jobs")
    context.update({"job": job, "runs": runs})
    return templates.TemplateResponse("admin/integrations/jobs/detail.html", context)


# ==================== Webhooks ====================

@router.get("/webhooks", response_class=HTMLResponse)
def webhooks_list(request: Request, db: Session = Depends(get_db)):
    """List all webhook endpoints."""
    endpoints = webhook_service.webhook_endpoints.list_all(
        db=db,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    # Get subscriptions and delivery counts
    endpoint_stats = {}
    for endpoint in endpoints:
        subs = webhook_service.webhook_subscriptions.list_all(
            db=db,
            endpoint_id=str(endpoint.id),
            event_type=None,
            order_by="created_at",
            order_dir="desc",
            limit=1000,
            offset=0,
        )
        pending = webhook_service.webhook_deliveries.list(
            db=db,
            endpoint_id=str(endpoint.id),
            subscription_id=None,
            event_type=None,
            status="pending",
            order_by="created_at",
            order_dir="desc",
            limit=1000,
            offset=0,
        )
        failed = webhook_service.webhook_deliveries.list(
            db=db,
            endpoint_id=str(endpoint.id),
            subscription_id=None,
            event_type=None,
            status="failed",
            order_by="created_at",
            order_dir="desc",
            limit=1000,
            offset=0,
        )
        endpoint_stats[str(endpoint.id)] = {
            "subscriptions": len(subs),
            "pending": len(pending),
            "failed": len(failed),
        }

    stats = {
        "total": len(endpoints),
        "active": sum(1 for e in endpoints if e.is_active),
    }

    context = _base_context(request, db, active_page="webhooks")
    context.update(
        {
            "endpoints": endpoints,
            "endpoint_stats": endpoint_stats,
            "stats": stats,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/webhooks/index.html", context)


@router.get("/webhooks/new", response_class=HTMLResponse)
def webhook_new(request: Request, db: Session = Depends(get_db)):
    """New webhook endpoint form."""
    from app.models.webhook import WebhookEventType

    connectors = connector_service.connector_configs.list(
        db=db,
        connector_type=None,
        auth_type=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )

    context = _base_context(request, db, active_page="webhooks")
    context.update({
        "event_types": [t.value for t in WebhookEventType],
        "connectors": connectors,
    })
    return templates.TemplateResponse("admin/integrations/webhooks/new.html", context)


@router.post("/webhooks", response_class=HTMLResponse)
def webhook_create(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    connector_config_id: str | None = Form(None),
    secret: str | None = Form(None),
    event_types: list[str] | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        payload = WebhookEndpointCreate(
            name=name.strip(),
            url=url.strip(),
            connector_config_id=_parse_uuid(connector_config_id, "connector_config_id", required=False),
            secret=secret.strip() if secret else None,
            is_active=is_active,
        )
        endpoint = webhook_service.webhook_endpoints.create(db, payload)
        for event_type in event_types or []:
            subscription_payload = WebhookSubscriptionCreate(
                endpoint_id=endpoint.id,
                event_type=event_type,
                is_active=True,
            )
            webhook_service.webhook_subscriptions.create(db, subscription_payload)
    except Exception as exc:
        from app.models.webhook import WebhookEventType

        connectors = connector_service.connector_configs.list(
            db=db,
            connector_type=None,
            auth_type=None,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=1000,
            offset=0,
        )
        context = _base_context(request, db, active_page="webhooks")
        context.update({
            "event_types": [t.value for t in WebhookEventType],
            "connectors": connectors,
            "error": str(exc),
            "form": {
                "name": name,
                "url": url,
                "connector_config_id": connector_config_id or "",
                "secret": secret or "",
                "event_types": event_types or [],
                "is_active": is_active,
            },
        })
        return templates.TemplateResponse(
            "admin/integrations/webhooks/new.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/integrations/webhooks/{endpoint.id}", status_code=303
    )


@router.get("/webhooks/{endpoint_id}", response_class=HTMLResponse)
def webhook_detail(request: Request, endpoint_id: str, db: Session = Depends(get_db)):
    """Webhook endpoint detail view."""
    try:
        endpoint = webhook_service.webhook_endpoints.get(db, endpoint_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="webhooks")
        context["message"] = "The webhook endpoint you are looking for does not exist."
        return templates.TemplateResponse("admin/errors/404.html", context, status_code=404)

    subscriptions = webhook_service.webhook_subscriptions.list_all(
        db=db,
        endpoint_id=str(endpoint.id),
        event_type=None,
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )

    deliveries = webhook_service.webhook_deliveries.list(
        db=db,
        endpoint_id=str(endpoint.id),
        subscription_id=None,
        event_type=None,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )

    context = _base_context(request, db, active_page="webhooks")
    context.update({
        "endpoint": endpoint,
        "subscriptions": subscriptions,
        "deliveries": deliveries,
    })
    return templates.TemplateResponse("admin/integrations/webhooks/detail.html", context)


# ==================== Payment Providers ====================

@router.get("/providers", response_class=HTMLResponse)
def providers_list(request: Request, db: Session = Depends(get_db)):
    """List all payment providers."""
    providers = billing_service.payment_providers.list_all(
        db=db,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    stats = {
        "total": len(providers),
        "active": sum(1 for p in providers if p.is_active),
        "by_type": {},
    }

    for p in providers:
        pt = p.provider_type.value if hasattr(p.provider_type, "value") else str(p.provider_type or "manual")
        stats["by_type"][pt] = stats["by_type"].get(pt, 0) + 1

    context = _base_context(request, db, active_page="providers")
    context.update(
        {
            "providers": providers,
            "stats": stats,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/providers/index.html", context)


@router.get("/providers/new", response_class=HTMLResponse)
def provider_new(request: Request, db: Session = Depends(get_db)):
    """New payment provider form."""
    from app.models.billing import PaymentProviderType

    connectors = connector_service.connector_configs.list(
        db=db,
        connector_type=None,
        auth_type=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )

    context = _base_context(request, db, active_page="providers")
    context.update({
        "provider_types": [t.value for t in PaymentProviderType],
        "connectors": connectors,
    })
    return templates.TemplateResponse("admin/integrations/providers/new.html", context)


@router.post("/providers", response_class=HTMLResponse)
def provider_create(
    request: Request,
    name: str = Form(...),
    provider_type: str = Form("custom"),
    connector_config_id: str | None = Form(None),
    webhook_secret_ref: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        payload = PaymentProviderCreate(
            name=name.strip(),
            provider_type=provider_type,
            connector_config_id=_parse_uuid(connector_config_id, "connector_config_id", required=False),
            webhook_secret_ref=webhook_secret_ref.strip() if webhook_secret_ref else None,
            notes=notes.strip() if notes else None,
            is_active=is_active,
        )
        provider = billing_service.payment_providers.create(db, payload)
    except Exception as exc:
        from app.models.billing import PaymentProviderType

        connectors = connector_service.connector_configs.list(
            db=db,
            connector_type=None,
            auth_type=None,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=1000,
            offset=0,
        )
        context = _base_context(request, db, active_page="providers")
        context.update({
            "provider_types": [t.value for t in PaymentProviderType],
            "connectors": connectors,
            "error": str(exc),
            "form": {
                "name": name,
                "provider_type": provider_type,
                "connector_config_id": connector_config_id or "",
                "webhook_secret_ref": webhook_secret_ref or "",
                "notes": notes or "",
                "is_active": is_active,
            },
        })
        return templates.TemplateResponse(
            "admin/integrations/providers/new.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/integrations/providers/{provider.id}", status_code=303
    )


@router.get("/providers/{provider_id}", response_class=HTMLResponse)
def provider_detail(request: Request, provider_id: str, db: Session = Depends(get_db)):
    """Payment provider detail view."""
    try:
        provider = billing_service.payment_providers.get(db, provider_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="providers")
        context["message"] = "The payment provider you are looking for does not exist."
        return templates.TemplateResponse("admin/errors/404.html", context, status_code=404)

    events = billing_service.payment_provider_events.list(
        db=db,
        provider_id=str(provider.id),
        payment_id=None,
        invoice_id=None,
        status=None,
        order_by="received_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )

    context = _base_context(request, db, active_page="providers")
    context.update({"provider": provider, "events": events})
    return templates.TemplateResponse("admin/integrations/providers/detail.html", context)
