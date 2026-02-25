"""Admin integrations routes."""

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import connector as connector_service
from app.services import integration_hooks as integration_hooks_service
from app.services import integration as integration_service
from app.services import web_integrations as web_integrations_service
from app.services import web_integrations_whatsapp as web_integrations_whatsapp_service
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


@router.get("/connectors/{connector_id}/embed", response_class=HTMLResponse)
def connector_embed(request: Request, connector_id: str, db: Session = Depends(get_db)):
    """Embedded connector frame view with reload/open controls."""
    perform_check = request.query_params.get("check") == "1"
    try:
        state = web_integrations_service.build_embedded_connector_data(
            db,
            connector_id=connector_id,
            perform_check=perform_check,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="connectors")
        context["message"] = "The connector you are trying to embed does not exist."
        return templates.TemplateResponse("admin/errors/404.html", context, status_code=404)

    context = _base_context(request, db, active_page="connectors")
    context.update(state)
    return templates.TemplateResponse("admin/integrations/connectors/embed.html", context)


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


# ==================== Hooks ====================

@router.get("/hooks", response_class=HTMLResponse)
def hooks_list(request: Request, db: Session = Depends(get_db)):
    state = integration_hooks_service.build_hooks_page_state(db)
    context = _base_context(request, db, active_page="hooks")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations/hooks"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/hooks/index.html", context)


@router.get("/hooks/new", response_class=HTMLResponse)
def hooks_new(request: Request, db: Session = Depends(get_db)):
    template_id = request.query_params.get("template")
    template = integration_hooks_service.get_hook_template(template_id)
    context = _base_context(request, db, active_page="hooks")
    context.update(
        {
            "form": _hook_form_defaults(template=template),
            "hook_templates": integration_hooks_service.list_hook_templates(),
            "selected_template_id": template_id or "",
        }
    )
    return templates.TemplateResponse("admin/integrations/hooks/form.html", context)


@router.post("/hooks", response_class=HTMLResponse)
def hooks_create(
    request: Request,
    title: str = Form(...),
    hook_type: str = Form("web"),
    command: str | None = Form(None),
    url: str | None = Form(None),
    http_method: str | None = Form("POST"),
    auth_type: str | None = Form("none"),
    auth_bearer_token: str | None = Form(None),
    auth_basic_username: str | None = Form(None),
    auth_basic_password: str | None = Form(None),
    auth_hmac_secret: str | None = Form(None),
    auth_config_json: str | None = Form(None),
    event_filters_csv: str | None = Form(None),
    retry_max: int = Form(3),
    retry_backoff_ms: int = Form(500),
    notes: str | None = Form(None),
    is_enabled: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        hook = integration_hooks_service.create_hook(
            db,
            title=title,
            hook_type=hook_type,
            command=command,
            url=url,
            http_method=http_method,
            auth_type=auth_type,
            auth_config=_build_hook_auth_config(
                auth_type=auth_type,
                auth_bearer_token=auth_bearer_token,
                auth_basic_username=auth_basic_username,
                auth_basic_password=auth_basic_password,
                auth_hmac_secret=auth_hmac_secret,
                auth_config_json=auth_config_json,
            ),
            event_filters=_split_csv(event_filters_csv),
            retry_max=retry_max,
            retry_backoff_ms=retry_backoff_ms,
            notes=notes,
            is_enabled=is_enabled,
        )
        return RedirectResponse(url=f"/admin/integrations/hooks/{hook.id}/edit", status_code=303)
    except Exception as exc:
        context = _base_context(request, db, active_page="hooks")
        context.update(
            {
                "error": str(exc),
                "form": {
                    "title": title,
                    "hook_type": hook_type,
                    "command": command or "",
                    "url": url or "",
                    "http_method": (http_method or "POST").upper(),
                    "auth_type": auth_type or "none",
                    "auth_bearer_token": auth_bearer_token or "",
                    "auth_basic_username": auth_basic_username or "",
                    "auth_basic_password": auth_basic_password or "",
                    "auth_hmac_secret": auth_hmac_secret or "",
                    "auth_config_json": auth_config_json or "",
                    "event_filters_csv": event_filters_csv or "",
                    "retry_max": retry_max,
                    "retry_backoff_ms": retry_backoff_ms,
                    "notes": notes or "",
                    "is_enabled": is_enabled,
                },
                "hook_templates": integration_hooks_service.list_hook_templates(),
                "selected_template_id": "",
            }
        )
        return templates.TemplateResponse("admin/integrations/hooks/form.html", context, status_code=400)


@router.get("/hooks/{hook_id}/edit", response_class=HTMLResponse)
def hooks_edit(request: Request, hook_id: str, db: Session = Depends(get_db)):
    try:
        hook = integration_hooks_service.get_hook(db, hook_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="hooks")
        context["message"] = "The hook you are looking for does not exist."
        return templates.TemplateResponse("admin/errors/404.html", context, status_code=404)

    form = {
        "title": hook.title,
        "hook_type": hook.hook_type.value,
        "command": hook.command or "",
        "url": hook.url or "",
        "http_method": hook.http_method,
        "auth_type": hook.auth_type.value,
        "auth_bearer_token": _auth_value(hook.auth_config, "token"),
        "auth_basic_username": _auth_value(hook.auth_config, "username"),
        "auth_basic_password": _auth_value(hook.auth_config, "password"),
        "auth_hmac_secret": _auth_value(hook.auth_config, "secret"),
        "auth_config_json": json.dumps(hook.auth_config or {}, indent=2),
        "event_filters_csv": ", ".join(hook.event_filters or []),
        "retry_max": hook.retry_max,
        "retry_backoff_ms": hook.retry_backoff_ms,
        "notes": hook.notes or "",
        "is_enabled": hook.is_enabled,
        "id": str(hook.id),
    }
    executions = integration_hooks_service.list_executions(
        db, hook_id=hook_id, limit=50, offset=0
    )
    context = _base_context(request, db, active_page="hooks")
    context.update(
        {
            "form": form,
            "executions": executions,
            "test_success": request.query_params.get("tested") == "1",
            "hook_templates": integration_hooks_service.list_hook_templates(),
            "selected_template_id": "",
        }
    )
    return templates.TemplateResponse("admin/integrations/hooks/form.html", context)


@router.post("/hooks/{hook_id}", response_class=HTMLResponse)
def hooks_update(
    request: Request,
    hook_id: str,
    title: str = Form(...),
    hook_type: str = Form("web"),
    command: str | None = Form(None),
    url: str | None = Form(None),
    http_method: str | None = Form("POST"),
    auth_type: str | None = Form("none"),
    auth_bearer_token: str | None = Form(None),
    auth_basic_username: str | None = Form(None),
    auth_basic_password: str | None = Form(None),
    auth_hmac_secret: str | None = Form(None),
    auth_config_json: str | None = Form(None),
    event_filters_csv: str | None = Form(None),
    retry_max: int = Form(3),
    retry_backoff_ms: int = Form(500),
    notes: str | None = Form(None),
    is_enabled: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        integration_hooks_service.update_hook(
            db,
            hook_id=hook_id,
            title=title,
            hook_type=hook_type,
            command=command,
            url=url,
            http_method=http_method,
            auth_type=auth_type,
            auth_config=_build_hook_auth_config(
                auth_type=auth_type,
                auth_bearer_token=auth_bearer_token,
                auth_basic_username=auth_basic_username,
                auth_basic_password=auth_basic_password,
                auth_hmac_secret=auth_hmac_secret,
                auth_config_json=auth_config_json,
            ),
            event_filters=_split_csv(event_filters_csv),
            retry_max=retry_max,
            retry_backoff_ms=retry_backoff_ms,
            notes=notes,
            is_enabled=is_enabled,
        )
        return RedirectResponse(url="/admin/integrations/hooks?saved=1", status_code=303)
    except Exception as exc:
        context = _base_context(request, db, active_page="hooks")
        context.update(
            {
                "error": str(exc),
                "form": {
                    "id": hook_id,
                    "title": title,
                    "hook_type": hook_type,
                    "command": command or "",
                    "url": url or "",
                    "http_method": (http_method or "POST").upper(),
                    "auth_type": auth_type or "none",
                    "auth_bearer_token": auth_bearer_token or "",
                    "auth_basic_username": auth_basic_username or "",
                    "auth_basic_password": auth_basic_password or "",
                    "auth_hmac_secret": auth_hmac_secret or "",
                    "auth_config_json": auth_config_json or "",
                    "event_filters_csv": event_filters_csv or "",
                    "retry_max": retry_max,
                    "retry_backoff_ms": retry_backoff_ms,
                    "notes": notes or "",
                    "is_enabled": is_enabled,
                },
                "hook_templates": integration_hooks_service.list_hook_templates(),
                "selected_template_id": "",
            }
        )
        return templates.TemplateResponse("admin/integrations/hooks/form.html", context, status_code=400)


@router.post("/hooks/{hook_id}/duplicate", response_class=HTMLResponse)
def hooks_duplicate(hook_id: str, db: Session = Depends(get_db)):
    copy = integration_hooks_service.duplicate_hook(db, hook_id=hook_id)
    return RedirectResponse(url=f"/admin/integrations/hooks/{copy.id}/edit", status_code=303)


@router.post("/hooks/{hook_id}/toggle", response_class=HTMLResponse)
def hooks_toggle(hook_id: str, enabled: bool = Form(False), db: Session = Depends(get_db)):
    integration_hooks_service.set_enabled(db, hook_id=hook_id, is_enabled=enabled)
    return RedirectResponse(url="/admin/integrations/hooks", status_code=303)


@router.post("/hooks/{hook_id}/test", response_class=HTMLResponse)
def hooks_test(hook_id: str, db: Session = Depends(get_db)):
    integration_hooks_service.trigger_test(db, hook_id=hook_id)
    return RedirectResponse(url=f"/admin/integrations/hooks/{hook_id}/edit?tested=1", status_code=303)


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


# ==================== WhatsApp Config ====================

@router.get("/whatsapp/config", response_class=HTMLResponse)
def whatsapp_config_page(request: Request, db: Session = Depends(get_db)):
    state = web_integrations_whatsapp_service.build_config_state(db)
    context = _base_context(request, db, active_page="whatsapp")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations/whatsapp"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/whatsapp/config.html", context)


@router.post("/whatsapp/config", response_class=HTMLResponse)
def whatsapp_config_save(
    request: Request,
    provider: str = Form("meta_cloud_api"),
    phone_number: str = Form(""),
    webhook_url: str = Form(""),
    api_key: str = Form(""),
    api_secret: str = Form(""),
    message_templates_json: str = Form("[]"),
    db: Session = Depends(get_db),
):
    try:
        web_integrations_whatsapp_service.save_config(
            db,
            provider=provider,
            phone_number=phone_number,
            webhook_url=webhook_url,
            api_key=api_key,
            api_secret=api_secret,
            message_templates_json=message_templates_json,
        )
        return RedirectResponse(url="/admin/integrations/whatsapp/config?saved=1", status_code=303)
    except Exception as exc:
        state = web_integrations_whatsapp_service.build_config_state(db)
        context = _base_context(request, db, active_page="whatsapp")
        context.update(
            {
                **state,
                "recent_activities": recent_activity_for_paths(
                    db, ["/admin/integrations/whatsapp"]
                ),
                "error": str(exc),
                "form": {
                    **state.get("form", {}),
                    "provider": provider,
                    "phone_number": phone_number,
                    "webhook_url": webhook_url,
                    "message_templates_json": message_templates_json,
                },
            }
        )
        return templates.TemplateResponse(
            "admin/integrations/whatsapp/config.html",
            context,
            status_code=400,
        )


@router.post("/whatsapp/config/test-send", response_class=HTMLResponse)
def whatsapp_config_test_send(
    request: Request,
    test_recipient: str = Form(""),
    test_template_name: str = Form(""),
    test_variables_json: str = Form("{}"),
    db: Session = Depends(get_db),
):
    state = web_integrations_whatsapp_service.build_config_state(db)
    context = _base_context(request, db, active_page="whatsapp")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(
                db, ["/admin/integrations/whatsapp"]
            ),
        }
    )
    try:
        result = web_integrations_whatsapp_service.run_test_send(
            db,
            recipient=test_recipient,
            template_name=test_template_name,
            variables_json=test_variables_json,
        )
        context["test_result"] = result
    except Exception as exc:
        context["error"] = str(exc)
    return templates.TemplateResponse("admin/integrations/whatsapp/config.html", context)


def _parse_json_object(raw: str | None) -> dict | None:
    if not raw or not raw.strip():
        return None
    parsed = json.loads(raw)
    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        raise ValueError("auth_config_json must be a JSON object")
    return parsed


def _split_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _build_hook_auth_config(
    *,
    auth_type: str | None,
    auth_bearer_token: str | None,
    auth_basic_username: str | None,
    auth_basic_password: str | None,
    auth_hmac_secret: str | None,
    auth_config_json: str | None,
) -> dict | None:
    auth_type_value = (auth_type or "none").strip().lower()
    result: dict[str, str] = {}
    if auth_type_value == "bearer" and auth_bearer_token and auth_bearer_token.strip():
        result["token"] = auth_bearer_token.strip()
    elif auth_type_value == "basic":
        if auth_basic_username and auth_basic_username.strip():
            result["username"] = auth_basic_username.strip()
        if auth_basic_password and auth_basic_password.strip():
            result["password"] = auth_basic_password.strip()
    elif auth_type_value == "hmac" and auth_hmac_secret and auth_hmac_secret.strip():
        result["secret"] = auth_hmac_secret.strip()
    extra = _parse_json_object(auth_config_json) or {}
    result.update(extra)
    return result or None


def _auth_value(auth_config: object, key: str) -> str:
    if isinstance(auth_config, dict):
        raw = auth_config.get(key)
        return str(raw) if raw is not None else ""
    return ""


def _hook_form_defaults(*, template: dict[str, object] | None = None) -> dict[str, object]:
    defaults = {
        "title": "",
        "hook_type": "web",
        "command": "",
        "url": "",
        "http_method": "POST",
        "auth_type": "none",
        "auth_bearer_token": "",
        "auth_basic_username": "",
        "auth_basic_password": "",
        "auth_hmac_secret": "",
        "auth_config_json": "",
        "event_filters_csv": "",
        "retry_max": 3,
        "retry_backoff_ms": 500,
        "notes": "",
        "is_enabled": True,
    }
    if template:
        defaults.update(
            {
                "title": str(template.get("title") or defaults["title"]),
                "hook_type": str(template.get("hook_type") or defaults["hook_type"]),
                "url": str(template.get("url") or defaults["url"]),
                "http_method": str(template.get("http_method") or defaults["http_method"]),
                "auth_type": str(template.get("auth_type") or defaults["auth_type"]),
                "event_filters_csv": str(
                    template.get("event_filters_csv") or defaults["event_filters_csv"]
                ),
                "retry_max": int(template.get("retry_max") or defaults["retry_max"]),
                "retry_backoff_ms": int(
                    template.get("retry_backoff_ms") or defaults["retry_backoff_ms"]
                ),
            }
        )
    return defaults
