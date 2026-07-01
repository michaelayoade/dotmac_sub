"""Admin integrations routes."""

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import connector as connector_service
from app.services import integration as integration_service
from app.services import integration_hooks as integration_hooks_service
from app.services import web_integration_syncs as web_integration_syncs_service
from app.services import web_integrations as web_integrations_service
from app.services import web_integrations_whatsapp as web_integrations_whatsapp_service
from app.services.audit_helpers import recent_activity_for_paths
from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/integrations", tags=["web-admin-integrations"])
templates = Jinja2Templates(directory="templates")


def _base_context(
    request: Request, db: Session, active_page: str, active_menu: str = "integrations"
) -> dict:
    """Build base template context."""
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


# ==================== Overview ====================


@router.get(
    "/",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def integrations_overview(request: Request, db: Session = Depends(get_db)):
    """Main integrations page with connector inventory and integration actions."""
    state = web_integrations_service.build_connectors_list_data(db)

    from app.services import crm_sync_failures

    redrive_status = request.query_params.get("crm_redrive", "")
    crm_redrive_message = ""
    if redrive_status == "not_found":
        crm_redrive_message = "CRM dead-letter was not found or is already resolved."
    elif redrive_status == "skipped":
        crm_redrive_message = "CRM dead-letter has no payload to re-drive."
    elif redrive_status.isdigit():
        crm_redrive_message = f"Re-drove {redrive_status} CRM dead-letter(s)."

    context = _base_context(request, db, active_page="integrations")
    context.update(
        {
            **state,
            "page_title": "Integrations",
            "page_subtitle": "Manage integrations, connectors, syncs, and external system access",
            "table_title": "Connectors",
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
            "crm_dead_letter_count": crm_sync_failures.unresolved_count(db),
            "crm_dead_letters": crm_sync_failures.list_failures(db, limit=25),
            "crm_redrive_message": crm_redrive_message,
        }
    )
    return templates.TemplateResponse(
        "admin/integrations/connectors/index.html", context
    )


@router.post(
    "/crm-dead-letters/redrive",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def crm_dead_letters_redrive(failure_id: str = Form(""), db: Session = Depends(get_db)):
    """Re-enqueue CRM push dead-letters — one by id, or all unresolved."""
    from fastapi.responses import RedirectResponse

    from app.services import crm_sync_failures

    if failure_id.strip():
        redriven = crm_sync_failures.redrive(db, failure_id.strip())
        status = "1" if redriven else "not_found"
    else:
        result = crm_sync_failures.redrive_all(db)
        status = str(result.get("redriven", 0))
    return RedirectResponse(
        url=f"/admin/integrations/?crm_redrive={status}",
        status_code=303,
    )


# ==================== Syncs ====================


@router.get(
    "/syncs",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def syncs_list(request: Request, db: Session = Depends(get_db)):
    """Generic sync profiles across external systems."""
    state = web_integration_syncs_service.build_syncs_index_data(db)
    context = _base_context(request, db, active_page="syncs")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(
                db, ["/admin/integrations/syncs"]
            ),
        }
    )
    return templates.TemplateResponse("admin/integrations/syncs/index.html", context)


@router.get(
    "/syncs/{job_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def sync_detail(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Sync profile detail and run history."""
    try:
        state = web_integration_syncs_service.build_sync_detail_data(db, job_id)
    except ValueError:
        context = _base_context(request, db, active_page="syncs")
        context["message"] = "The sync profile you are looking for does not exist."
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )
    context = _base_context(request, db, active_page="syncs")
    context.update(state)
    return templates.TemplateResponse("admin/integrations/syncs/detail.html", context)


@router.post(
    "/syncs/{job_id}/run",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def sync_run(job_id: str):
    """Queue a manual sync run."""
    web_integration_syncs_service.trigger_sync_job(job_id)
    return RedirectResponse(
        url=f"/admin/integrations/syncs/{job_id}?queued=1", status_code=303
    )


@router.post(
    "/syncs/{job_id}/configure",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def sync_configure(
    job_id: str,
    schedule_type: str = Form("manual"),
    interval_minutes: str | None = Form(None),
    trigger_mode: str | None = Form(None),
    mapping_config: str | None = Form(None),
    filter_config: str | None = Form(None),
    page_size: str | None = Form(None),
    max_pages: str | None = Form(None),
    sync_comments: bool = Form(False),
    mapping_primary: str | None = Form(None),
    mapping_fallback: str | None = Form(None),
    mapping_ambiguous: str | None = Form(None),
    conflict_policy: str | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    web_integration_syncs_service.update_sync_profile(
        db,
        job_id,
        schedule_type=schedule_type,
        interval_minutes=interval_minutes,
        trigger_mode=trigger_mode,
        mapping_config=mapping_config,
        filter_config=filter_config,
        page_size=page_size,
        max_pages=max_pages,
        sync_comments=sync_comments,
        mapping_primary=mapping_primary,
        mapping_fallback=mapping_fallback,
        mapping_ambiguous=mapping_ambiguous,
        conflict_policy=conflict_policy,
        is_active=is_active,
    )
    return RedirectResponse(
        url=f"/admin/integrations/syncs/{job_id}?saved=1", status_code=303
    )


@router.post(
    "/syncs/{job_id}/backfill-crm-history",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def sync_backfill_crm_history(job_id: str, db: Session = Depends(get_db)):
    web_integration_syncs_service.backfill_crm_ticket_import_history(db, job_id)
    return RedirectResponse(
        url=f"/admin/integrations/syncs/{job_id}?backfilled=1", status_code=303
    )


@router.get(
    "/connectors",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
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
    return templates.TemplateResponse(
        "admin/integrations/connectors/index.html", context
    )


@router.get("/marketplace", response_class=HTMLResponse)
def integrations_marketplace(request: Request, db: Session = Depends(get_db)):
    """Integrations marketplace with discovered and installed connectors."""
    state = web_integrations_service.build_marketplace_data(db)
    context = _base_context(request, db, active_page="connectors")
    context.update(
        {
            **state,
            "updates_checked": request.query_params.get("checked") == "1",
        }
    )
    return templates.TemplateResponse("admin/integrations/marketplace.html", context)


@router.post(
    "/marketplace/check-updates",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def integrations_marketplace_check_updates():
    return RedirectResponse(
        "/admin/integrations/marketplace?checked=1", status_code=303
    )


@router.get("/installed", response_class=HTMLResponse)
def integrations_installed(request: Request, db: Session = Depends(get_db)):
    state = web_integrations_service.build_installed_integrations_data(db)
    context = _base_context(request, db, active_page="connectors")
    context.update(
        {
            **state,
            "saved": request.query_params.get("saved") == "1",
        }
    )
    return templates.TemplateResponse("admin/integrations/installed.html", context)


@router.post(
    "/installed/bulk",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def integrations_installed_bulk(
    connector_ids: list[str] = Form(default=[]),
    action: str = Form(...),
    db: Session = Depends(get_db),
):
    if connector_ids:
        web_integrations_service.bulk_set_integrations_enabled(
            db,
            connector_ids=connector_ids,
            enabled=(action == "enable"),
        )
    return RedirectResponse("/admin/integrations/installed?saved=1", status_code=303)


@router.post(
    "/installed/{connector_id}/relay",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def integrations_installed_relay_toggle(
    connector_id: str,
    relay_to_portal: bool = Form(False),
    db: Session = Depends(get_db),
):
    web_integrations_service.set_relay_to_portal(
        db,
        connector_id=connector_id,
        relay=relay_to_portal,
    )
    return RedirectResponse("/admin/integrations/installed?saved=1", status_code=303)


@router.post(
    "/installed/{connector_id}/toggle",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def integrations_installed_toggle(
    connector_id: str,
    enabled: bool = Form(False),
    db: Session = Depends(get_db),
):
    web_integrations_service.bulk_set_integrations_enabled(
        db,
        connector_ids=[connector_id],
        enabled=enabled,
    )
    return RedirectResponse("/admin/integrations/installed?saved=1", status_code=303)


@router.post(
    "/installed/{connector_id}/uninstall",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def integrations_installed_uninstall(connector_id: str, db: Session = Depends(get_db)):
    web_integrations_service.uninstall_integration(db, connector_id)
    return RedirectResponse("/admin/integrations/installed?saved=1", status_code=303)


@router.get("/connectors/new", response_class=HTMLResponse)
def connector_new(request: Request, db: Session = Depends(get_db)):
    """New connector form."""
    context = _base_context(request, db, active_page="connectors")
    context.update(web_integrations_service.connector_form_options())
    return templates.TemplateResponse("admin/integrations/connectors/new.html", context)


@router.get("/register", response_class=HTMLResponse)
def integration_register_page(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="connectors")
    context.update(web_integrations_service.integration_registration_form_options())
    return templates.TemplateResponse("admin/integrations/register.html", context)


@router.post(
    "/register",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def integration_register_create(
    request: Request,
    name: str = Form(...),
    display_title: str = Form(...),
    integration_type: str = Form("simple"),
    root_section: str = Form("integrations"),
    icon: str = Form("puzzle-piece"),
    db: Session = Depends(get_db),
):
    try:
        connector = web_integrations_service.create_registered_integration(
            db,
            name=name,
            display_title=display_title,
            integration_type=integration_type,
            root_section=root_section,
            icon=icon,
        )
    except Exception as exc:
        context = _base_context(request, db, active_page="connectors")
        context.update(
            {
                **web_integrations_service.integration_registration_form_options(),
                "error": str(exc),
                "form": {
                    "name": name,
                    "display_title": display_title,
                    "integration_type": integration_type,
                    "root_section": root_section,
                    "icon": icon,
                },
            }
        )
        return templates.TemplateResponse(
            "admin/integrations/register.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/integrations/register/{connector.id}/configure", status_code=303
    )


@router.get("/register/{connector_id}/configure", response_class=HTMLResponse)
def integration_register_configure_page(
    request: Request, connector_id: str, db: Session = Depends(get_db)
):
    context = _base_context(request, db, active_page="connectors")
    context.update(
        web_integrations_service.registered_integration_config_state(db, connector_id)
    )
    return templates.TemplateResponse(
        "admin/integrations/register_configure.html", context
    )


@router.post(
    "/register/{connector_id}/configure",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def integration_register_configure_save(
    request: Request,
    connector_id: str,
    custom_fields_json: str | None = Form("{}"),
    webhook_endpoint: str | None = Form(None),
    auth_method: str | None = Form(None),
    data_mapping_json: str | None = Form("{}"),
    external_url: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        connector = web_integrations_service.update_registered_integration_config(
            db,
            connector_id=connector_id,
            custom_fields_json=custom_fields_json,
            webhook_endpoint=webhook_endpoint,
            auth_method=auth_method,
            data_mapping_json=data_mapping_json,
            external_url=external_url,
        )
    except Exception as exc:
        context = _base_context(request, db, active_page="connectors")
        context.update(
            web_integrations_service.registered_integration_config_state(
                db, connector_id
            )
        )
        context["error"] = str(exc)
        return templates.TemplateResponse(
            "admin/integrations/register_configure.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/integrations/connectors/{connector.id}", status_code=303
    )


@router.post(
    "/connectors",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
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
def connector_detail(
    request: Request, connector_id: str, db: Session = Depends(get_db)
):
    """Connector detail view."""
    try:
        connector = connector_service.connector_configs.get(db, connector_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="connectors")
        context["message"] = "The connector you are looking for does not exist."
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )

    context = _base_context(request, db, active_page="connectors")
    context.update(
        {
            "connector": connector,
            # Secret-keyed header/metadata values are masked for display; the
            # update path restores them unless overwritten (see web_integrations).
            "headers_display": web_integrations_service.mask_secret_values(
                connector.headers
            ),
            "metadata_display": web_integrations_service.mask_secret_values(
                connector.metadata_
            ),
            **web_integrations_service.connector_form_options(),
        }
    )
    return templates.TemplateResponse(
        "admin/integrations/connectors/detail.html", context
    )


@router.post(
    "/connectors/{connector_id}",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def connector_update(
    connector_id: str,
    base_url: str | None = Form(None),
    auth_type: str = Form("none"),
    timeout_sec: str | None = Form(None),
    auth_config: str | None = Form(None),
    headers: str | None = Form(None),
    retry_policy: str | None = Form(None),
    metadata: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    web_integrations_service.update_connector_config(
        db,
        connector_id,
        base_url=base_url,
        auth_type=auth_type,
        timeout_sec=timeout_sec,
        auth_config=auth_config,
        headers=headers,
        retry_policy=retry_policy,
        metadata=metadata,
        notes=notes,
        is_active=is_active,
    )
    return RedirectResponse(
        url=f"/admin/integrations/connectors/{connector_id}?saved=1", status_code=303
    )


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
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )

    context = _base_context(request, db, active_page="connectors")
    context.update(state)
    return templates.TemplateResponse(
        "admin/integrations/connectors/embed.html", context
    )


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


@router.post(
    "/targets",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
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
        context["message"] = (
            "The integration target you are looking for does not exist."
        )
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )

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


@router.post(
    "/jobs",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def job_create(
    request: Request,
    target_id: str = Form(...),
    name: str = Form(...),
    job_type: str = Form("sync"),
    schedule_type: str = Form("manual"),
    interval_minutes: str | None = Form(None),
    adapter_key: str | None = Form(None),
    action: str | None = Form(None),
    entity_type: str | None = Form(None),
    direction: str | None = Form(None),
    trigger_mode: str | None = Form(None),
    mapping_config: str | None = Form(None),
    filter_config: str | None = Form(None),
    conflict_policy: str | None = Form(None),
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
            adapter_key=adapter_key,
            action=action,
            entity_type=entity_type,
            direction=direction,
            trigger_mode=trigger_mode,
            mapping_config=mapping_config,
            filter_config=filter_config,
            conflict_policy=conflict_policy,
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
                    adapter_key=adapter_key,
                    action=action,
                    entity_type=entity_type,
                    direction=direction,
                    trigger_mode=trigger_mode,
                    mapping_config=mapping_config,
                    filter_config=filter_config,
                    conflict_policy=conflict_policy,
                    notes=notes,
                    is_active=is_active,
                ),
                "error": str(exc),
            }
        )
        return templates.TemplateResponse(
            "admin/integrations/jobs/new.html", context, status_code=400
        )
    return RedirectResponse(url=f"/admin/integrations/jobs/{job.id}", status_code=303)


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
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )

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


@router.post(
    "/jobs/{job_id}/run",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def job_run(job_id: str):
    """Queue a manual integration job run."""
    web_integration_syncs_service.trigger_sync_job(job_id)
    return RedirectResponse(
        url=f"/admin/integrations/jobs/{job_id}?queued=1", status_code=303
    )


# ==================== Hooks ====================


@router.get("/hooks", response_class=HTMLResponse)
def hooks_list(request: Request, db: Session = Depends(get_db)):
    state = integration_hooks_service.build_hooks_page_state(db)
    context = _base_context(request, db, active_page="hooks")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(
                db, ["/admin/integrations/hooks"]
            ),
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


@router.post(
    "/hooks",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
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
    timeout_seconds: int | None = Form(None),
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
            timeout_seconds=timeout_seconds,
            notes=notes,
            is_enabled=is_enabled,
        )
        return RedirectResponse(
            url=f"/admin/integrations/hooks/{hook.id}/edit", status_code=303
        )
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
                    "auth_bearer_token": "",
                    "auth_basic_username": auth_basic_username or "",
                    "auth_basic_password": "",
                    "auth_hmac_secret": "",
                    "auth_config_json": auth_config_json or "",
                    "event_filters_csv": event_filters_csv or "",
                    "retry_max": retry_max,
                    "retry_backoff_ms": retry_backoff_ms,
                    "timeout_seconds": timeout_seconds or "",
                    "notes": notes or "",
                    "is_enabled": is_enabled,
                },
                "hook_templates": integration_hooks_service.list_hook_templates(),
                "selected_template_id": "",
            }
        )
        return templates.TemplateResponse(
            "admin/integrations/hooks/form.html", context, status_code=400
        )


@router.get("/hooks/{hook_id}/edit", response_class=HTMLResponse)
def hooks_edit(request: Request, hook_id: str, db: Session = Depends(get_db)):
    try:
        hook = integration_hooks_service.get_hook(db, hook_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="hooks")
        context["message"] = "The hook you are looking for does not exist."
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )

    form = {
        "title": hook.title,
        "hook_type": hook.hook_type.value,
        "command": hook.command or "",
        "url": hook.url or "",
        "http_method": hook.http_method,
        "auth_type": hook.auth_type.value,
        "auth_bearer_token": "",
        "auth_basic_username": _auth_value(hook.auth_config, "username"),
        "auth_basic_password": "",
        "auth_hmac_secret": "",
        "auth_config_json": json.dumps(
            integration_hooks_service.public_hook_auth_config(hook.auth_config),
            indent=2,
        ),
        "event_filters_csv": ", ".join(hook.event_filters or []),
        "retry_max": hook.retry_max,
        "retry_backoff_ms": hook.retry_backoff_ms,
        "timeout_seconds": hook.timeout_seconds or "",
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


@router.post(
    "/hooks/{hook_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
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
    timeout_seconds: int | None = Form(None),
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
                existing_auth_config=integration_hooks_service.get_hook(
                    db, hook_id
                ).auth_config,
                preserve_blank_secrets=True,
            ),
            event_filters=_split_csv(event_filters_csv),
            retry_max=retry_max,
            retry_backoff_ms=retry_backoff_ms,
            timeout_seconds=timeout_seconds,
            notes=notes,
            is_enabled=is_enabled,
        )
        return RedirectResponse(
            url="/admin/integrations/hooks?saved=1", status_code=303
        )
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
                    "auth_bearer_token": "",
                    "auth_basic_username": auth_basic_username or "",
                    "auth_basic_password": "",
                    "auth_hmac_secret": "",
                    "auth_config_json": auth_config_json or "",
                    "event_filters_csv": event_filters_csv or "",
                    "retry_max": retry_max,
                    "retry_backoff_ms": retry_backoff_ms,
                    "timeout_seconds": timeout_seconds or "",
                    "notes": notes or "",
                    "is_enabled": is_enabled,
                },
                "hook_templates": integration_hooks_service.list_hook_templates(),
                "selected_template_id": "",
            }
        )
        return templates.TemplateResponse(
            "admin/integrations/hooks/form.html", context, status_code=400
        )


@router.post(
    "/hooks/{hook_id}/duplicate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def hooks_duplicate(hook_id: str, db: Session = Depends(get_db)):
    copy = integration_hooks_service.duplicate_hook(db, hook_id=hook_id)
    return RedirectResponse(
        url=f"/admin/integrations/hooks/{copy.id}/edit", status_code=303
    )


@router.post(
    "/hooks/{hook_id}/toggle",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def hooks_toggle(
    hook_id: str, enabled: bool = Form(False), db: Session = Depends(get_db)
):
    integration_hooks_service.set_enabled(db, hook_id=hook_id, is_enabled=enabled)
    return RedirectResponse(url="/admin/integrations/hooks", status_code=303)


@router.post(
    "/hooks/{hook_id}/test",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def hooks_test(hook_id: str, db: Session = Depends(get_db)):
    integration_hooks_service.trigger_test(db, hook_id=hook_id)
    return RedirectResponse(
        url=f"/admin/integrations/hooks/{hook_id}/edit?tested=1", status_code=303
    )


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
    context.update(
        {
            "action_url": "/admin/integrations/webhooks",
            "submit_label": "Create Webhook",
        }
    )
    return templates.TemplateResponse("admin/integrations/webhooks/new.html", context)


@router.post(
    "/webhooks",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def webhook_create(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    connector_config_id: str | None = Form(None),
    secret: str | None = Form(None),
    event_types: list[str] | None = Form(None),
    delivery_timeout_seconds: str | None = Form(None),
    max_retries: str | None = Form(None),
    retry_backoff_seconds: str | None = Form(None),
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
            delivery_timeout_seconds=delivery_timeout_seconds,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
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
                    delivery_timeout_seconds=delivery_timeout_seconds,
                    max_retries=max_retries,
                    retry_backoff_seconds=retry_backoff_seconds,
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


@router.get("/webhooks/{endpoint_id}/edit", response_class=HTMLResponse)
def webhook_edit(request: Request, endpoint_id: str, db: Session = Depends(get_db)):
    """Edit webhook endpoint form."""
    try:
        state = web_integrations_service.build_webhook_edit_data(
            db,
            endpoint_id=endpoint_id,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="webhooks")
        context["message"] = "The webhook endpoint you are looking for does not exist."
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )
    context = _base_context(request, db, active_page="webhooks")
    context.update(state)
    return templates.TemplateResponse("admin/integrations/webhooks/new.html", context)


@router.post(
    "/webhooks/{endpoint_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def webhook_update(
    request: Request,
    endpoint_id: str,
    name: str = Form(...),
    url: str = Form(...),
    connector_config_id: str | None = Form(None),
    secret: str | None = Form(None),
    event_types: list[str] | None = Form(None),
    delivery_timeout_seconds: str | None = Form(None),
    max_retries: str | None = Form(None),
    retry_backoff_seconds: str | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        endpoint = web_integrations_service.update_webhook_endpoint(
            db,
            endpoint_id=endpoint_id,
            name=name,
            url=url,
            connector_config_id=connector_config_id,
            secret=secret,
            event_types=event_types,
            delivery_timeout_seconds=delivery_timeout_seconds,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
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
                    secret=None,
                    event_types=event_types,
                    delivery_timeout_seconds=delivery_timeout_seconds,
                    max_retries=max_retries,
                    retry_backoff_seconds=retry_backoff_seconds,
                    is_active=is_active,
                ),
                "endpoint": None,
                "action_url": f"/admin/integrations/webhooks/{endpoint_id}",
                "submit_label": "Save Webhook",
                "error": str(exc),
            }
        )
        return templates.TemplateResponse(
            "admin/integrations/webhooks/new.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/integrations/webhooks/{endpoint.id}?saved=1", status_code=303
    )


@router.post(
    "/webhooks/{endpoint_id}/enable",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def webhook_enable(endpoint_id: str, db: Session = Depends(get_db)):
    web_integrations_service.set_webhook_endpoint_active(
        db, endpoint_id=endpoint_id, is_active=True
    )
    return RedirectResponse(
        url=f"/admin/integrations/webhooks/{endpoint_id}?saved=1", status_code=303
    )


@router.post(
    "/webhooks/{endpoint_id}/disable",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def webhook_disable(endpoint_id: str, db: Session = Depends(get_db)):
    web_integrations_service.set_webhook_endpoint_active(
        db, endpoint_id=endpoint_id, is_active=False
    )
    return RedirectResponse(
        url=f"/admin/integrations/webhooks/{endpoint_id}?saved=1", status_code=303
    )


@router.post(
    "/webhooks/{endpoint_id}/rotate-secret",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def webhook_rotate_secret(endpoint_id: str, db: Session = Depends(get_db)):
    web_integrations_service.rotate_webhook_endpoint_secret(db, endpoint_id=endpoint_id)
    return RedirectResponse(
        url=f"/admin/integrations/webhooks/{endpoint_id}?secret=rotated",
        status_code=303,
    )


@router.post(
    "/webhooks/{endpoint_id}/test",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def webhook_test(endpoint_id: str, db: Session = Depends(get_db)):
    try:
        web_integrations_service.queue_webhook_test_delivery(
            db, endpoint_id=endpoint_id
        )
        query = "test=queued"
    except Exception:
        query = "test=failed"
    return RedirectResponse(
        url=f"/admin/integrations/webhooks/{endpoint_id}?{query}", status_code=303
    )


@router.post(
    "/webhooks/{endpoint_id}/delete",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def webhook_delete(endpoint_id: str, db: Session = Depends(get_db)):
    web_integrations_service.delete_webhook_endpoint(db, endpoint_id=endpoint_id)
    return RedirectResponse(
        url="/admin/integrations/webhooks?deleted=1", status_code=303
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
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )

    context = _base_context(request, db, active_page="webhooks")
    context.update(state)
    return templates.TemplateResponse(
        "admin/integrations/webhooks/detail.html", context
    )


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
    return templates.TemplateResponse(
        "admin/integrations/providers/index.html", context
    )


@router.get("/providers/new", response_class=HTMLResponse)
def provider_new(request: Request, db: Session = Depends(get_db)):
    """New payment provider form."""
    context = _base_context(request, db, active_page="providers")
    context.update(web_integrations_service.provider_form_options(db))
    return templates.TemplateResponse("admin/integrations/providers/new.html", context)


@router.post(
    "/providers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:provider:write"))],
)
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
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )

    context = _base_context(request, db, active_page="providers")
    context.update(state)
    return templates.TemplateResponse(
        "admin/integrations/providers/detail.html", context
    )


# ==================== WhatsApp Config ====================


@router.get("/whatsapp/config", response_class=HTMLResponse)
def whatsapp_config_page(request: Request, db: Session = Depends(get_db)):
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
    return templates.TemplateResponse(
        "admin/integrations/whatsapp/config.html", context
    )


@router.post(
    "/whatsapp/config",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
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
        return RedirectResponse(
            url="/admin/integrations/whatsapp/config?saved=1", status_code=303
        )
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


@router.post(
    "/whatsapp/config/test-send",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
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
    return templates.TemplateResponse(
        "admin/integrations/whatsapp/config.html", context
    )


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
    existing_auth_config: object | None = None,
    preserve_blank_secrets: bool = False,
) -> dict | None:
    auth_type_value = (auth_type or "none").strip().lower()
    existing = existing_auth_config if isinstance(existing_auth_config, dict) else {}
    result: dict[str, str] = {}
    if auth_type_value == "bearer":
        if auth_bearer_token and auth_bearer_token.strip():
            result["token"] = auth_bearer_token.strip()
        elif preserve_blank_secrets and existing.get("token"):
            result["token"] = str(existing["token"])
    elif auth_type_value == "basic":
        if auth_basic_username and auth_basic_username.strip():
            result["username"] = auth_basic_username.strip()
        if auth_basic_password and auth_basic_password.strip():
            result["password"] = auth_basic_password.strip()
        elif preserve_blank_secrets and existing.get("password"):
            result["password"] = str(existing["password"])
    elif auth_type_value == "hmac":
        if auth_hmac_secret and auth_hmac_secret.strip():
            result["secret"] = auth_hmac_secret.strip()
        elif preserve_blank_secrets and existing.get("secret"):
            result["secret"] = str(existing["secret"])
    extra = _parse_json_object(auth_config_json) or {}
    result.update(extra)
    return result or None


def _auth_value(auth_config: object, key: str) -> str:
    """Edit-form value for a hook auth_config key, decrypted for display.

    Secret values are encrypted at rest; the edit form shows the plaintext (its
    long-standing behaviour) so the operator can see/change it. They are
    re-encrypted on save.
    """
    if not isinstance(auth_config, dict):
        return ""
    raw = auth_config.get(key)
    if raw is None:
        return ""
    from app.models.integration_hook import SECRET_AUTH_CONFIG_KEYS

    if key in SECRET_AUTH_CONFIG_KEYS:
        from app.services.credential_crypto import decrypt_credential

        return str(decrypt_credential(str(raw)) or "")
    return str(raw)


def _decrypted_auth_config(auth_config: object) -> dict:
    """A copy of ``auth_config`` with secret values decrypted, for display."""
    if not isinstance(auth_config, dict):
        return {}
    return {key: _auth_value(auth_config, key) for key in auth_config}


def _hook_form_defaults(
    *, template: dict[str, object] | None = None
) -> dict[str, object]:
    defaults = {
        "title": "",
        "hook_type": "web",
        "command": "",
        "url": "",
        "http_method": "POST",
        "auth_type": "none",
        "auth_bearer_token": "",
        "auth_basic_username": "",
        "auth_basic_password": "",  # nosec
        "auth_hmac_secret": "",  # nosec
        "auth_config_json": "",
        "event_filters_csv": "",
        "retry_max": 3,
        "retry_backoff_ms": 500,
        "timeout_seconds": "",
        "notes": "",
        "is_enabled": True,
    }
    if template:
        defaults.update(
            {
                "title": str(template.get("title") or defaults["title"]),
                "hook_type": str(template.get("hook_type") or defaults["hook_type"]),
                "url": str(template.get("url") or defaults["url"]),
                "http_method": str(
                    template.get("http_method") or defaults["http_method"]
                ),
                "auth_type": str(template.get("auth_type") or defaults["auth_type"]),
                "event_filters_csv": str(
                    template.get("event_filters_csv") or defaults["event_filters_csv"]
                ),
                "retry_max": int(template.get("retry_max") or defaults["retry_max"]),
                "retry_backoff_ms": int(
                    template.get("retry_backoff_ms") or defaults["retry_backoff_ms"]
                ),
                "timeout_seconds": int(template.get("timeout_seconds") or 10),
            }
        )
    return defaults
