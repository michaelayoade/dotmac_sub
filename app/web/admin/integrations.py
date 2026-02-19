"""Admin integrations routes."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import connector as connector_service
from app.services import integration as integration_service
from app.services import web_integrations as web_integrations_service
from app.services.audit_helpers import recent_activity_for_paths

router = APIRouter(prefix="/integrations", tags=["web-admin-integrations"])
templates = Jinja2Templates(directory="templates")


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


# ==================== Connectors ====================

@router.get("/connectors", response_class=HTMLResponse)
def connectors_list(request: Request, db: Session = Depends(get_db)):
    """List all connector configurations."""
    state = web_integrations_service.build_connectors_list_data(db)

    context = _base_context(request, db, active_page="connectors")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/connectors/index.html", context)


@router.get("/connectors/new", response_class=HTMLResponse)
def connector_new(request: Request, db: Session = Depends(get_db)):
    """New connector form."""
    context = _base_context(request, db, active_page="connectors")
    context.update(web_integrations_service.connector_form_options())
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
        connector = web_integrations_service.create_connector(
            db,
            name=name,
            connector_type=connector_type,
            auth_type=auth_type,
            base_url=base_url,
            timeout_sec=timeout_sec,
            auth_config=auth_config,
            headers=headers,
            retry_policy=retry_policy,
            metadata=metadata,
            notes=notes,
            is_active=is_active,
        )
    except Exception as exc:
        context = _base_context(request, db, active_page="connectors")
        context.update(
            {
                **web_integrations_service.connector_error_state(
                    name=name,
                    connector_type=connector_type,
                    auth_type=auth_type,
                    base_url=base_url,
                    timeout_sec=timeout_sec,
                    auth_config=auth_config,
                    headers=headers,
                    retry_policy=retry_policy,
                    metadata=metadata,
                    notes=notes,
                    is_active=is_active,
                ),
                "error": str(exc),
            }
        )
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
    state = web_integrations_service.build_targets_list_data(db)

    context = _base_context(request, db, active_page="targets")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/targets/index.html", context)


@router.get("/targets/new", response_class=HTMLResponse)
def target_new(request: Request, db: Session = Depends(get_db)):
    """New target form."""
    context = _base_context(request, db, active_page="targets")
    context.update(web_integrations_service.target_form_options(db))
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
        target = web_integrations_service.create_target(
            db,
            name=name,
            target_type=target_type,
            connector_config_id=connector_config_id,
            notes=notes,
            is_active=is_active,
        )
    except Exception as exc:
        context = _base_context(request, db, active_page="targets")
        context.update(
            {
                **web_integrations_service.target_error_state(
                    db,
                    name=name,
                    target_type=target_type,
                    connector_config_id=connector_config_id,
                    notes=notes,
                    is_active=is_active,
                ),
                "error": str(exc),
            }
        )
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
    state = web_integrations_service.build_jobs_list_data(db)

    context = _base_context(request, db, active_page="jobs")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/jobs/index.html", context)


@router.get("/jobs/new", response_class=HTMLResponse)
def job_new(request: Request, db: Session = Depends(get_db)):
    """New job form."""
    context = _base_context(request, db, active_page="jobs")
    context.update(web_integrations_service.job_form_options(db))
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
        job = web_integrations_service.create_job(
            db,
            target_id=target_id,
            name=name,
            job_type=job_type,
            schedule_type=schedule_type,
            interval_minutes=interval_minutes,
            notes=notes,
            is_active=is_active,
        )
    except Exception as exc:
        context = _base_context(request, db, active_page="jobs")
        context.update(
            {
                **web_integrations_service.job_error_state(
                    db,
                    target_id=target_id,
                    name=name,
                    job_type=job_type,
                    schedule_type=schedule_type,
                    interval_minutes=interval_minutes,
                    notes=notes,
                    is_active=is_active,
                ),
                "error": str(exc),
            }
        )
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
    state = web_integrations_service.build_webhooks_list_data(db)

    context = _base_context(request, db, active_page="webhooks")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/webhooks/index.html", context)


@router.get("/webhooks/new", response_class=HTMLResponse)
def webhook_new(request: Request, db: Session = Depends(get_db)):
    """New webhook endpoint form."""
    context = _base_context(request, db, active_page="webhooks")
    context.update(web_integrations_service.webhook_form_options(db))
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
        endpoint = web_integrations_service.create_webhook_endpoint(
            db,
            name=name,
            url=url,
            connector_config_id=connector_config_id,
            secret=secret,
            event_types=event_types,
            is_active=is_active,
        )
    except Exception as exc:
        context = _base_context(request, db, active_page="webhooks")
        context.update(
            {
                **web_integrations_service.webhook_error_state(
                    db,
                    name=name,
                    url=url,
                    connector_config_id=connector_config_id,
                    secret=secret,
                    event_types=event_types,
                    is_active=is_active,
                ),
                "error": str(exc),
            }
        )
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
        state = web_integrations_service.build_webhook_detail_data(
            db,
            endpoint_id=endpoint_id,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="webhooks")
        context["message"] = "The webhook endpoint you are looking for does not exist."
        return templates.TemplateResponse("admin/errors/404.html", context, status_code=404)

    context = _base_context(request, db, active_page="webhooks")
    context.update(state)
    return templates.TemplateResponse("admin/integrations/webhooks/detail.html", context)


# ==================== Payment Providers ====================

@router.get("/providers", response_class=HTMLResponse)
def providers_list(request: Request, db: Session = Depends(get_db)):
    """List all payment providers."""
    state = web_integrations_service.build_providers_list_data(db)

    context = _base_context(request, db, active_page="providers")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/providers/index.html", context)


@router.get("/providers/new", response_class=HTMLResponse)
def provider_new(request: Request, db: Session = Depends(get_db)):
    """New payment provider form."""
    context = _base_context(request, db, active_page="providers")
    context.update(web_integrations_service.provider_form_options(db))
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
        provider = web_integrations_service.create_provider(
            db,
            name=name,
            provider_type=provider_type,
            connector_config_id=connector_config_id,
            webhook_secret_ref=webhook_secret_ref,
            notes=notes,
            is_active=is_active,
        )
    except Exception as exc:
        context = _base_context(request, db, active_page="providers")
        context.update(
            {
                **web_integrations_service.provider_error_state(
                    db,
                    name=name,
                    provider_type=provider_type,
                    connector_config_id=connector_config_id,
                    webhook_secret_ref=webhook_secret_ref,
                    notes=notes,
                    is_active=is_active,
                ),
                "error": str(exc),
            }
        )
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
        state = web_integrations_service.build_provider_detail_data(
            db,
            provider_id=provider_id,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="providers")
        context["message"] = "The payment provider you are looking for does not exist."
        return templates.TemplateResponse("admin/errors/404.html", context, status_code=404)

    context = _base_context(request, db, active_page="providers")
    context.update(state)
    return templates.TemplateResponse("admin/integrations/providers/detail.html", context)
