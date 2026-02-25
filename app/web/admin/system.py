"""Admin system management web routes."""

import json
from base64 import b64encode
from datetime import UTC, datetime
from typing import cast
from urllib.parse import quote_plus
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.audit import AuditActorType
from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import (
    billing as billing_service,
)
from app.services import branding_storage as branding_storage_service
from app.services import email as email_service
from app.services import file_upload as file_upload_service
from app.services import module_manager as module_manager_service
from app.services import (
    rbac as rbac_service,
)
from app.services import (
    scheduler as scheduler_service,
)
from app.services import settings_spec
from app.services import web_system_api_key_forms as web_system_api_key_forms_service
from app.services import (
    web_system_api_key_mutations as web_system_api_key_mutations_service,
)
from app.services import web_system_api_keys as web_system_api_keys_service
from app.services import web_system_audit as web_system_audit_service
from app.services import web_system_billing_forms as web_system_billing_forms_service
from app.services import web_system_common as web_system_common_service
from app.services import web_system_export_tool as web_system_export_tool_service
from app.services import web_system_form_views as web_system_form_views_service
from app.services import web_system_health as web_system_health_service
from app.services import web_system_import_wizard as web_system_import_wizard_service
from app.services import web_system_overview as web_system_overview_service
from app.services import (
    web_system_permission_forms as web_system_permission_forms_service,
)
from app.services import web_system_profiles as web_system_profiles_service
from app.services import web_system_role_forms as web_system_role_forms_service
from app.services import web_system_roles as web_system_roles_service
from app.services import web_system_scheduler as web_system_scheduler_service
from app.services import web_system_settings_forms as web_system_settings_forms_service
from app.services import web_system_settings_views as web_system_settings_views_service
from app.services import web_system_user_edit as web_system_user_edit_service
from app.services import web_system_user_mutations as web_system_user_mutations_service
from app.services import web_system_users as web_system_users_service
from app.services import web_system_webhook_forms as web_system_webhook_forms_service
from app.services import web_system_webhooks as web_system_webhooks_service
from app.services.auth_dependencies import require_permission
from app.tasks.imports import run_import_job
from app.tasks.exports import run_export_job
from app.web.request_parsing import (
    parse_form_data,
    parse_form_data_sync,
    parse_json_body,
)

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/system", tags=["web-admin-system"])


def _placeholder_context(request: Request, db: Session, title: str, active_page: str):
    from app.web.admin import get_current_user, get_sidebar_stats
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "page_title": title,
        "heading": title,
        "description": f"{title} configuration will appear here.",
        "empty_title": f"No {title.lower()} yet",
        "empty_message": "System configuration will appear once it is enabled.",
    }


@router.get("/health", response_class=HTMLResponse)
def system_health_page(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_system_health_service.build_health_data(db)
    context = {
        "request": request,
        "active_page": "system-health",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        **state,
    }
    return templates.TemplateResponse("admin/system/health.html", context)


def _workflow_context(request: Request, db: Session, error: str | None = None):
    """Build context for workflow page - simplified after CRM cleanup."""
    from app.web.admin import get_current_user, get_sidebar_stats
    context = {
        "request": request,
        "active_page": "workflow",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }
    if error:
        context["error"] = error
    return context

@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:read"))])
def system_overview(request: Request, db: Session = Depends(get_db)):
    """System settings overview."""
    from app.web.admin import get_current_user, get_sidebar_stats

    dashboard = web_system_overview_service.get_dashboard_stats(db)
    return templates.TemplateResponse(
        "admin/system/index.html",
        {
            "request": request,
            "active_page": "system",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            **dashboard,
        },
    )


@router.get("/modules", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:read"))])
def modules_manager_page(request: Request, db: Session = Depends(get_db)):
    """Main module manager with module and feature toggles."""
    from app.web.admin import get_current_user, get_sidebar_stats

    state = module_manager_service.module_manager_page_state(db)
    return templates.TemplateResponse(
        "admin/system/modules.html",
        {
            "request": request,
            "active_page": "system-modules",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "save_success": request.query_params.get("saved") == "1",
            **state,
        },
    )


@router.post("/modules", dependencies=[Depends(require_permission("system:settings:write"))])
def modules_manager_save(request: Request, db: Session = Depends(get_db)):
    """Persist module manager toggles."""
    form = parse_form_data_sync(request)
    module_payload: dict[str, bool] = {}
    feature_payload: dict[str, bool] = {}

    for module_name in module_manager_service.MODULE_KEY_MAP:
        module_payload[module_name] = str(form.get(f"module__{module_name}") or "").lower() == "true"

    for feature_map in module_manager_service.MODULE_FEATURE_MAP.values():
        for feature_name in feature_map:
            feature_payload[feature_name] = str(form.get(f"feature__{feature_name}") or "").lower() == "true"

    module_manager_service.update_module_flags(db, payload=module_payload)
    module_manager_service.update_feature_flags(db, payload=feature_payload)
    return RedirectResponse("/admin/system/modules?saved=1", status_code=303)


@router.get(
    "/import",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_import_wizard(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_system_import_wizard_service.build_page_state(db)
    rollback_notice = request.query_params.get("rollback_notice")
    rollback_error = request.query_params.get("rollback_error")
    job_notice = request.query_params.get("job_notice")
    active_job_id = request.query_params.get("job_id")
    active_job = web_system_import_wizard_service.get_job(db, active_job_id) if active_job_id else None
    return templates.TemplateResponse(
        "admin/system/import_wizard.html",
        {
            "request": request,
            "active_page": "system-import",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "rollback_notice": rollback_notice,
            "rollback_error": rollback_error,
            "job_notice": job_notice,
            "active_job_id": active_job_id,
            "active_job": active_job,
            **state,
        },
    )


@router.get(
    "/import/template.csv",
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_import_template_csv(
    module: str = Query(...),
):
    from fastapi.responses import StreamingResponse

    content = web_system_import_wizard_service.csv_template(module)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename=\"import_template_{module}.csv\"'},
    )


@router.post(
    "/import/mapping-preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_import_mapping_preview(
    request: Request,
    db: Session = Depends(get_db),
):
    form = parse_form_data_sync(request)
    module = str(form.get("module") or "subscribers").strip()
    data_format = str(form.get("data_format") or "csv").strip().lower()
    payload_text = str(form.get("payload_text") or "")
    csv_delimiter = str(form.get("csv_delimiter") or ",")
    payload_file = cast(UploadFile | None, form.get("payload_file"))
    file_bytes: bytes | None = None
    if payload_file is not None and payload_file.filename:
        file_bytes = payload_file.file.read()

    state = web_system_import_wizard_service.detect_columns_and_preview(
        data_format=data_format,
        raw_text=payload_text,
        csv_delimiter=csv_delimiter,
        file_bytes=file_bytes,
    )
    headers = web_system_import_wizard_service.module_headers(module)
    return templates.TemplateResponse(
        "admin/system/_import_mapping.html",
        {
            "request": request,
            "module": module,
            "target_headers": headers,
            "selected_delimiter": csv_delimiter,
            **state,
        },
    )


@router.post(
    "/import",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_import_wizard_submit(
    request: Request,
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats
    current_user = get_current_user(request)
    form = parse_form_data_sync(request)
    module = str(form.get("module") or "").strip()
    data_format = str(form.get("data_format") or "csv").strip().lower()
    csv_delimiter = str(form.get("csv_delimiter") or ",")
    payload_text = str(form.get("payload_text") or "")
    source_name = str(form.get("source_name") or "manual")
    payload_file = cast(UploadFile | None, form.get("payload_file"))
    file_bytes: bytes | None = None
    if payload_file is not None and payload_file.filename:
        source_name = source_name if source_name.strip() != "manual" else payload_file.filename
        file_bytes = payload_file.file.read()
    dry_run = form.get("dry_run")
    column_mapping: dict[str, str] = {}
    for key, value in form.items():
        if str(key).startswith("mapping__"):
            source_col = str(key).split("mapping__", 1)[1]
            target_col = str(value or "").strip()
            if source_col:
                column_mapping[source_col] = target_col

    try:
        parsed_preview = web_system_import_wizard_service.parse_payload(
            data_format=data_format,
            raw_text=payload_text,
            source_name=source_name,
            csv_delimiter=csv_delimiter,
            file_bytes=file_bytes,
        )
        total_rows = len(parsed_preview.rows)
        threshold = web_system_import_wizard_service.background_threshold_rows(db)
        if total_rows >= threshold:
            job_id = str(uuid4())
            web_system_import_wizard_service.upsert_job(
                db,
                {
                    "job_id": job_id,
                    "module": module,
                    "module_label": web_system_import_wizard_service.ENTITY_CONFIG.get(module, {}).get("label", module),
                    "source_name": source_name,
                    "status": "queued",
                    "queued_at": datetime.now(UTC).isoformat(),
                    "progress_percent": 0,
                    "row_count": total_rows,
                    "threshold_rows": threshold,
                    "requested_by": current_user.get("email") or "",
                    "result": None,
                    "error": None,
                },
            )
            run_import_job.delay(
                job_id=job_id,
                module=module,
                data_format=data_format,
                raw_text=payload_text,
                source_name=source_name,
                dry_run=dry_run is not None,
                column_mapping=column_mapping,
                csv_delimiter=csv_delimiter,
                file_bytes_b64=b64encode(file_bytes).decode("ascii") if file_bytes is not None else None,
                notify_email=(current_user.get("email") or "").strip() or None,
            )
            notice = quote_plus(
                f"Large import queued in background ({total_rows} rows). Track progress below."
            )
            return RedirectResponse(
                f"/admin/system/import?job_id={job_id}&job_notice={notice}",
                status_code=303,
            )

        result = web_system_import_wizard_service.execute_import(
            db,
            module=module,
            data_format=data_format,
            raw_text=payload_text,
            source_name=source_name,
            dry_run=dry_run is not None,
            column_mapping=column_mapping,
            csv_delimiter=csv_delimiter,
            file_bytes=file_bytes,
        )
        state = web_system_import_wizard_service.build_page_state(db)
        return templates.TemplateResponse(
            "admin/system/import_wizard.html",
            {
                "request": request,
                "active_page": "system-import",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "result": result,
                "form": {
                    "module": module,
                    "data_format": data_format,
                    "csv_delimiter": csv_delimiter,
                    "payload_text": payload_text,
                    "source_name": source_name,
                    "dry_run": dry_run is not None,
                },
                **state,
            },
            status_code=200 if result.get("status") in {"success", "dry_run", "partial"} else 400,
        )
    except Exception as exc:
        state = web_system_import_wizard_service.build_page_state(db)
        return templates.TemplateResponse(
            "admin/system/import_wizard.html",
            {
                "request": request,
                "active_page": "system-import",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "error": str(exc),
                "form": {
                    "module": module,
                    "data_format": data_format,
                    "csv_delimiter": csv_delimiter,
                    "payload_text": payload_text,
                    "source_name": source_name,
                    "dry_run": dry_run is not None,
                },
                **state,
            },
            status_code=400,
        )


@router.get(
    "/import/jobs/{job_id}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_import_job_status(
    request: Request,
    job_id: str,
    db: Session = Depends(get_db),
):
    job = web_system_import_wizard_service.get_job(db, job_id)
    return templates.TemplateResponse(
        "admin/system/_import_job_status.html",
        {
            "request": request,
            "job": job,
        },
    )


@router.get(
    "/export",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_export_tool(
    request: Request,
    module: str | None = Query(None),
    template: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    selected_module = (module or "subscribers").strip()
    selected_template_id = (template or "").strip() or None
    selected_fields: list[str] | None = None
    selected_delimiter = ","
    selected_export_format = "csv"
    selected_date_from: str | None = None
    selected_date_to: str | None = None
    selected_status: str | None = None
    selected_include_headers = True

    if selected_template_id:
        template_data = web_system_export_tool_service.get_export_template(db, selected_template_id)
        if template_data:
            config = template_data.get("config") if isinstance(template_data.get("config"), dict) else {}
            selected_module = str(config.get("module") or selected_module).strip()
            selected_fields = [str(field) for field in config.get("selected_fields") or []]
            selected_delimiter = str(config.get("delimiter") or ",")
            selected_export_format = str(config.get("export_format") or "csv")
            selected_date_from = str(config.get("date_from") or "").strip() or None
            selected_date_to = str(config.get("date_to") or "").strip() or None
            selected_status = str(config.get("status") or "").strip() or None
            selected_include_headers = bool(config.get("include_headers", True))

    try:
        available_fields = web_system_export_tool_service.module_fields(selected_module)
        status_options = web_system_export_tool_service.module_status_options(selected_module)
    except Exception:
        selected_module = "subscribers"
        available_fields = web_system_export_tool_service.module_fields(selected_module)
        status_options = web_system_export_tool_service.module_status_options(selected_module)

    return templates.TemplateResponse(
        "admin/system/export_tool.html",
        {
            "request": request,
            "active_page": "system-export",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "module_options": web_system_export_tool_service.module_options(),
            "delimiter_options": web_system_export_tool_service.DELIMITER_OPTIONS,
            "export_format_options": web_system_export_tool_service.EXPORT_FORMAT_OPTIONS,
            "export_templates": web_system_export_tool_service.list_export_templates(db),
            "selected_template_id": selected_template_id,
            "selected_module": selected_module,
            "available_fields": available_fields,
            "status_options": status_options,
            "frequency_options": web_system_export_tool_service.SCHEDULE_FREQUENCY_OPTIONS,
            "export_schedules": web_system_export_tool_service.list_export_schedules(db),
            "export_jobs": web_system_export_tool_service.list_export_jobs(db, limit=20),
            "selected_fields": selected_fields or available_fields,
            "selected_delimiter": selected_delimiter,
            "selected_export_format": selected_export_format,
            "selected_date_from": selected_date_from,
            "selected_date_to": selected_date_to,
            "selected_status": selected_status,
            "selected_include_headers": selected_include_headers,
            "error": request.query_params.get("error"),
            "notice": request.query_params.get("notice"),
        },
    )


@router.post(
    "/export/download",
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_export_download(request: Request, db: Session = Depends(get_db)):
    from fastapi.responses import StreamingResponse
    from app.web.admin import get_current_user

    form = parse_form_data_sync(request)
    module = str(form.get("module") or "subscribers").strip()
    selected_fields = [str(field).strip() for field in form.getlist("fields") if str(field).strip()]
    delimiter = str(form.get("delimiter") or ",")
    export_format = str(form.get("export_format") or "csv")
    date_from = str(form.get("date_from") or "").strip() or None
    date_to = str(form.get("date_to") or "").strip() or None
    status = str(form.get("status") or "").strip() or None
    include_headers = form.get("include_headers") is not None
    current_user = get_current_user(request)
    requester_email = str(current_user.get("email") or "").strip() or None
    actor_id = str(current_user.get("person_id") or "").strip() or None

    try:
        row_count = web_system_export_tool_service.count_rows(
            db,
            module=module,
            date_from=date_from,
            date_to=date_to,
            status=status,
        )
        if row_count > web_system_export_tool_service.EXPORT_BG_THRESHOLD_ROWS:
            job = web_system_export_tool_service.create_export_job(
                db,
                module=module,
                selected_fields=selected_fields,
                delimiter=delimiter,
                export_format=export_format,
                date_from=date_from,
                date_to=date_to,
                status=status,
                include_headers=include_headers,
                recipient_email=requester_email,
                requested_by_email=requester_email,
                row_count=row_count,
            )
            run_export_job.delay(job_id=str(job["id"]))
            web_system_export_tool_service.log_export_audit_event(
                db,
                action="export_job_queued",
                module=module,
                actor_id=actor_id,
                actor_type=AuditActorType.user if actor_id else AuditActorType.system,
                entity_type="export_job",
                entity_id=str(job["id"]),
                metadata={
                    "row_count": row_count,
                    "format": export_format,
                },
            )
            notice = quote_plus(
                f"Large export queued ({row_count} rows). Download link will be emailed when ready."
            )
            return RedirectResponse(f"/admin/system/export?module={quote_plus(module)}&notice={notice}", status_code=303)

        content, media_type, extension, row_count = web_system_export_tool_service.export_content(
            db,
            module=module,
            selected_fields=selected_fields,
            delimiter=delimiter,
            export_format=export_format,
            date_from=date_from,
            date_to=date_to,
            status=status,
            include_headers=include_headers,
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        filename = f"export_{module}_{row_count}_{timestamp}.{extension}"
        web_system_export_tool_service.log_export_audit_event(
            db,
            action="export_download",
            module=module,
            actor_id=actor_id,
            actor_type=AuditActorType.user if actor_id else AuditActorType.system,
            entity_type="system_export",
            entity_id=None,
            metadata={
                "row_count": row_count,
                "format": export_format,
                "filename": filename,
            },
        )
        return StreamingResponse(
            iter([content]),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        error = quote_plus(str(exc))
        module_q = quote_plus(module)
        return RedirectResponse(f"/admin/system/export?module={module_q}&error={error}", status_code=303)


@router.get(
    "/export/jobs/{job_id}/download",
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_export_job_download(request: Request, job_id: str, db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse
    from app.web.admin import get_current_user

    job = web_system_export_tool_service.get_export_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    if str(job.get("status") or "") != "completed":
        raise HTTPException(status_code=409, detail="Export job is not completed")
    file_path = str(job.get("file_path") or "").strip()
    if not file_path:
        raise HTTPException(status_code=404, detail="Export file not available")
    filename = str(job.get("filename") or f"export_{job_id}.dat")
    current_user = get_current_user(request)
    actor_id = str(current_user.get("person_id") or "").strip() or None
    web_system_export_tool_service.log_export_audit_event(
        db,
        action="export_job_download",
        module=str(job.get("module") or ""),
        actor_id=actor_id,
        actor_type=AuditActorType.user if actor_id else AuditActorType.system,
        entity_type="export_job",
        entity_id=job_id,
        metadata={"filename": filename},
    )
    return FileResponse(path=file_path, filename=filename, media_type="application/octet-stream")


@router.post(
    "/export/templates",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_export_create_template(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    module = str(form.get("module") or "subscribers").strip()
    template_name = str(form.get("template_name") or "").strip()
    selected_fields = [str(field).strip() for field in form.getlist("fields") if str(field).strip()]
    delimiter = str(form.get("delimiter") or ",")
    export_format = str(form.get("export_format") or "csv")
    date_from = str(form.get("date_from") or "").strip() or None
    date_to = str(form.get("date_to") or "").strip() or None
    status = str(form.get("status") or "").strip() or None
    include_headers = form.get("include_headers") is not None
    try:
        template_data = web_system_export_tool_service.create_export_template(
            db,
            name=template_name,
            module=module,
            selected_fields=selected_fields,
            delimiter=delimiter,
            export_format=export_format,
            date_from=date_from,
            date_to=date_to,
            status=status,
            include_headers=include_headers,
        )
        notice = quote_plus("Export template saved.")
        template_id = quote_plus(str(template_data.get("id") or ""))
        return RedirectResponse(
            f"/admin/system/export?module={quote_plus(module)}&template={template_id}&notice={notice}",
            status_code=303,
        )
    except Exception as exc:
        error = quote_plus(str(exc))
        return RedirectResponse(f"/admin/system/export?module={quote_plus(module)}&error={error}", status_code=303)


@router.post(
    "/export/templates/{template_id}/delete",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_export_delete_template(
    template_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    form = parse_form_data_sync(request)
    module = str(form.get("module") or "subscribers").strip()
    try:
        web_system_export_tool_service.delete_export_template(db, template_id=template_id)
        notice = quote_plus("Export template removed.")
        return RedirectResponse(f"/admin/system/export?module={quote_plus(module)}&notice={notice}", status_code=303)
    except Exception as exc:
        error = quote_plus(str(exc))
        return RedirectResponse(f"/admin/system/export?module={quote_plus(module)}&error={error}", status_code=303)


@router.post(
    "/export/schedules",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_export_create_schedule(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    module = str(form.get("module") or "subscribers").strip()
    selected_fields = [str(field).strip() for field in form.getlist("fields") if str(field).strip()]
    delimiter = str(form.get("delimiter") or ",")
    export_format = str(form.get("export_format") or "csv").strip().lower()
    date_from = str(form.get("date_from") or "").strip() or None
    date_to = str(form.get("date_to") or "").strip() or None
    status = str(form.get("status") or "").strip() or None
    include_headers = form.get("include_headers") is not None
    name = str(form.get("schedule_name") or "").strip()
    recipient_email = str(form.get("recipient_email") or "").strip()
    frequency = str(form.get("frequency") or "weekly").strip().lower()
    custom_interval_raw = str(form.get("custom_interval_hours") or "").strip()
    custom_interval_hours = int(custom_interval_raw) if custom_interval_raw.isdigit() else None

    try:
        web_system_export_tool_service.create_export_schedule(
            db,
            name=name,
            module=module,
            selected_fields=selected_fields,
            delimiter=delimiter,
            export_format=export_format,
            date_from=date_from,
            date_to=date_to,
            status=status,
            include_headers=include_headers,
            recipient_email=recipient_email,
            frequency=frequency,
            custom_interval_hours=custom_interval_hours,
        )
        notice = quote_plus("Scheduled export created.")
        return RedirectResponse(f"/admin/system/export?module={quote_plus(module)}&notice={notice}", status_code=303)
    except Exception as exc:
        error = quote_plus(str(exc))
        return RedirectResponse(f"/admin/system/export?module={quote_plus(module)}&error={error}", status_code=303)


@router.post(
    "/export/schedules/{schedule_id}/toggle",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_export_toggle_schedule(
    schedule_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    form = parse_form_data_sync(request)
    module = str(form.get("module") or "subscribers").strip()
    enabled = str(form.get("enabled") or "").strip() == "1"
    try:
        web_system_export_tool_service.set_export_schedule_enabled(
            db,
            schedule_id=schedule_id,
            enabled=enabled,
        )
        notice = quote_plus("Scheduled export updated.")
        return RedirectResponse(f"/admin/system/export?module={quote_plus(module)}&notice={notice}", status_code=303)
    except Exception as exc:
        error = quote_plus(str(exc))
        return RedirectResponse(f"/admin/system/export?module={quote_plus(module)}&error={error}", status_code=303)


@router.post(
    "/export/schedules/{schedule_id}/delete",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_export_delete_schedule(
    schedule_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    form = parse_form_data_sync(request)
    module = str(form.get("module") or "subscribers").strip()
    try:
        web_system_export_tool_service.delete_export_schedule(db, schedule_id=schedule_id)
        notice = quote_plus("Scheduled export removed.")
        return RedirectResponse(f"/admin/system/export?module={quote_plus(module)}&notice={notice}", status_code=303)
    except Exception as exc:
        error = quote_plus(str(exc))
        return RedirectResponse(f"/admin/system/export?module={quote_plus(module)}&error={error}", status_code=303)


@router.post(
    "/import/{import_id}/rollback",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_import_wizard_rollback(
    import_id: str,
    db: Session = Depends(get_db),
):
    try:
        result = web_system_import_wizard_service.rollback_import(db, import_id=import_id)
        notice = quote_plus(
            f"Import {import_id} rolled back: {result['rolled_back_rows']} rows removed."
        )
        return RedirectResponse(
            f"/admin/system/import?rollback_notice={notice}",
            status_code=303,
        )
    except Exception as exc:
        error = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/system/import?rollback_error={error}",
            status_code=303,
        )


@router.get("/users", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:read"))])
def users_list(
    request: Request,
    search: str | None = None,
    role: str | None = None,
    status: str | None = None,
    filters: str | None = None,
    order_by: str | None = Query("last_name"),
    order_dir: str | None = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(500, ge=10, le=500),
    offset: int | None = Query(None, ge=0),
    limit: int | None = Query(None, ge=5, le=500),
    db: Session = Depends(get_db),
):
    """List system users."""
    if limit is None:
        limit = per_page
    if offset is None:
        offset = (page - 1) * limit

    state = web_system_users_service.build_users_page_state(
        db,
        search=search,
        role=role,
        status=status,
        filters=filters,
        order_by=order_by,
        order_dir=order_dir,
        offset=offset,
        limit=limit,
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "admin/system/users/_table_content.html",
            {
                "request": request,
                **state,
                "htmx_url": "/admin/system/users/filter",
                "htmx_target": "users-table-content",
            },
        )

    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/system/users/index.html",
        {
            "request": request,
            **state,
            "htmx_url": "/admin/system/users/filter",
            "htmx_target": "users-table-content",
            "active_page": "users",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "user_type_options": state["user_type_options"],
        },
    )


@router.get("/users/search", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:read"))])
def users_search(
    request: Request,
    search: str | None = None,
    role: str | None = None,
    status: str | None = None,
    filters: str | None = None,
    order_by: str | None = Query("last_name"),
    order_dir: str | None = Query("asc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=5, le=500),
    db: Session = Depends(get_db),
):
    state = web_system_users_service.build_users_page_state(
        db,
        search=search,
        role=role,
        status=status,
        filters=filters,
        order_by=order_by,
        order_dir=order_dir,
        offset=offset,
        limit=limit,
    )
    return templates.TemplateResponse(
        "admin/system/users/_table_content.html",
        {
            "request": request,
            **state,
            "htmx_url": "/admin/system/users/filter",
            "htmx_target": "users-table-content",
        },
    )


@router.get("/users/filter", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:read"))])
def users_filter(
    request: Request,
    search: str | None = None,
    role: str | None = None,
    status: str | None = None,
    filters: str | None = None,
    order_by: str | None = Query("last_name"),
    order_dir: str | None = Query("asc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=5, le=500),
    db: Session = Depends(get_db),
):
    state = web_system_users_service.build_users_page_state(
        db,
        search=search,
        role=role,
        status=status,
        filters=filters,
        order_by=order_by,
        order_dir=order_dir,
        offset=offset,
        limit=limit,
    )
    return templates.TemplateResponse(
        "admin/system/users/_table_content.html",
        {
            "request": request,
            **state,
            "htmx_url": "/admin/system/users/filter",
            "htmx_target": "users-table-content",
        },
    )


@router.get("/users/profile", response_class=HTMLResponse)
def user_profile(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    current_user = get_current_user(request)
    state = web_system_profiles_service.build_profile_page_state(
        db,
        current_user=current_user,
    )

    context = {
        "request": request,
        "active_page": "users",
        "active_menu": "system",
        "current_user": current_user,
        "sidebar_stats": get_sidebar_stats(db),
        **state,
    }
    return templates.TemplateResponse("admin/system/profile.html", context)


@router.post("/users/profile", response_class=HTMLResponse)
def user_profile_update(
    request: Request,
    first_name: str = Form(None),
    last_name: str = Form(None),
    email: str = Form(None),
    phone: str = Form(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    current_user = get_current_user(request)
    error = None
    success = None
    updated_person_id = None

    if current_user and current_user.get("person_id"):
        person_id = current_user["person_id"]
        system_user = web_system_profiles_service.get_subscriber(db, person_id)
        if system_user:
            try:
                person = web_system_profiles_service.update_profile(
                    db,
                    person=system_user,
                    first_name=first_name,
                    last_name=last_name,
                    email=email,
                    phone=phone,
                )
                updated_person_id = person.id
                success = "Profile updated successfully."
            except Exception as e:
                db.rollback()
                error = str(e)
    state = web_system_profiles_service.build_profile_page_state(
        db,
        current_user=current_user,
        error=error,
        success=success,
        person_id=updated_person_id,
    )

    context = {
        "request": request,
        "active_page": "users",
        "active_menu": "system",
        "current_user": current_user,
        "sidebar_stats": get_sidebar_stats(db),
        **state,
    }
    return templates.TemplateResponse("admin/system/profile.html", context)


@router.post("/users/bulk/user-type", dependencies=[Depends(require_permission("rbac:assign"))])
def users_bulk_set_user_type(
    data: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    user_ids = data.get("user_ids", [])
    user_type = data.get("user_type")
    if not user_ids or not isinstance(user_ids, list):
        raise HTTPException(status_code=400, detail="user_ids is required")
    if not user_type or not isinstance(user_type, str):
        raise HTTPException(status_code=400, detail="user_type is required")

    updated = web_system_user_mutations_service.bulk_set_user_type(
        db,
        user_ids=[str(item) for item in user_ids],
        user_type=user_type,
    )
    return {
        "message": f"Updated user type for {updated} users.",
        "updated_count": updated,
    }


@router.post("/users/bulk/delete", dependencies=[Depends(require_permission("rbac:assign"))])
def users_bulk_delete(
    data: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    user_ids = data.get("user_ids", [])
    if not user_ids or not isinstance(user_ids, list):
        raise HTTPException(status_code=400, detail="user_ids is required")

    deleted, skipped = web_system_user_mutations_service.bulk_delete_user_records(
        db,
        user_ids=[str(item) for item in user_ids],
    )
    return {
        "message": f"Deleted {deleted} users. Skipped {skipped}.",
        "deleted_count": deleted,
        "skipped_count": skipped,
    }


@router.post("/users/bulk/invite", dependencies=[Depends(require_permission("rbac:assign"))])
def users_bulk_invite(
    data: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    user_ids = data.get("user_ids", [])
    if not user_ids or not isinstance(user_ids, list):
        raise HTTPException(status_code=400, detail="user_ids is required")

    sent, failed = web_system_user_mutations_service.bulk_send_user_invites(
        db,
        user_ids=[str(item) for item in user_ids],
    )
    return {
        "message": f"Sent {sent} invite(s). Failed {failed}.",
        "sent_count": sent,
        "failed_count": failed,
    }


@router.get("/users/{user_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:read"))])
def user_detail(request: Request, user_id: str, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    detail_data = web_system_profiles_service.get_user_detail_data(db, user_id)
    if not detail_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "User not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "admin/system/users/detail.html",
        {
            "request": request,
            "user": detail_data["user"],
            "roles": detail_data["roles"],
            "credential": detail_data["credential"],
            "mfa_methods": detail_data["mfa_methods"],
            "active_page": "users",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/users/{user_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_edit(request: Request, user_id: str, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    edit_data = web_system_profiles_service.get_user_edit_data(db, user_id)
    if not edit_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "User not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "admin/system/users/edit.html",
        {
            "request": request,
            "user": edit_data["user"],
            "roles": edit_data["roles"],
            "current_role_ids": edit_data["current_role_ids"],
            "all_permissions": edit_data["all_permissions"],
            "direct_permission_ids": edit_data["direct_permission_ids"],
            "user_type_options": web_system_users_service.USER_TYPE_OPTIONS,
            "can_update_password": web_system_common_service.is_admin_request(request),
            "active_page": "users",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/users/{user_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_edit_submit(
    request: Request,
    user_id: str,
    form_data = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    system_user = web_system_user_edit_service.get_subscriber_or_none(db, user_id)
    if not system_user:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "User not found"},
            status_code=404,
        )
    parsed = web_system_user_edit_service.parse_edit_form(form_data)
    display_name = cast(str | None, parsed["display_name"])
    phone = cast(str | None, parsed["phone"])
    user_type = cast(str | None, parsed["user_type"])
    is_active = cast(str | None, parsed["is_active"])
    role_ids = cast(list[str], parsed["role_ids"])
    direct_permission_ids = cast(list[str], parsed["direct_permission_ids"])
    new_password = cast(str | None, parsed["new_password"])
    confirm_password = cast(str | None, parsed["confirm_password"])
    require_password_change = cast(str | None, parsed["require_password_change"])

    try:
        web_system_user_edit_service.apply_user_edit(
            db,
            subscriber=system_user,
            first_name=str(parsed["first_name"]),
            last_name=str(parsed["last_name"]),
            display_name=display_name,
            email=str(parsed["email"]),
            phone=phone,
            user_type=user_type,
            is_active=web_system_common_service.form_bool(is_active),
            role_ids=role_ids,
            direct_permission_ids=direct_permission_ids,
            new_password=new_password,
            confirm_password=confirm_password,
            require_password_change=web_system_common_service.form_bool(require_password_change),
            is_admin=web_system_common_service.is_admin_request(request),
            actor_id=getattr(request.state, "actor_id", None),
        )
    except Exception as exc:
        db.rollback()
        edit_data = web_system_user_edit_service.build_edit_state(db, subscriber=system_user)
        return templates.TemplateResponse(
            "admin/system/users/edit.html",
            {
                "request": request,
                "user": edit_data["user"],
                "roles": edit_data["roles"],
                "current_role_ids": edit_data["current_role_ids"],
                "all_permissions": edit_data["all_permissions"],
                "direct_permission_ids": edit_data["direct_permission_ids"],
                "user_type_options": web_system_users_service.USER_TYPE_OPTIONS,
                "can_update_password": web_system_common_service.is_admin_request(request),
                "active_page": "users",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "error": str(exc),
            },
            status_code=400,
        )
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/activate", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_activate(request: Request, user_id: str, db: Session = Depends(get_db)):
    web_system_user_mutations_service.set_user_active(db, user_id=user_id, is_active=True)
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/deactivate", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_deactivate(request: Request, user_id: str, db: Session = Depends(get_db)):
    web_system_user_mutations_service.set_user_active(db, user_id=user_id, is_active=False)
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/disable-mfa", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_disable_mfa(request: Request, user_id: str, db: Session = Depends(get_db)):
    web_system_user_mutations_service.disable_user_mfa(db, user_id=user_id)
    return Response(status_code=204)


@router.post("/users/{user_id}/reset-password", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_reset_password(request: Request, user_id: str, db: Session = Depends(get_db)):
    try:
        note = web_system_user_mutations_service.send_password_reset_link_for_user(
            db,
            user_id=user_id,
        )
    except Exception as exc:
        note = str(exc)
    success = "success" if "sent" in note.lower() else "error"
    trigger = {
        "showToast": {
            "type": success,
            "title": "Password reset",
            "message": note,
            "duration": 8000,
        }
    }
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Trigger": json.dumps(trigger)})
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.get("/users/{user_id}/reset-password", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_reset_password_get_fallback(user_id: str):
    """Fallback for auth-refresh GET redirect on reset-password action URLs."""
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/invite", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_send_invite(request: Request, user_id: str, db: Session = Depends(get_db)):
    try:
        note = web_system_user_mutations_service.send_user_invite_for_user(
            db,
            user_id=user_id,
        )
    except Exception as exc:
        note = str(exc)
    success = "success" if "sent" in note.lower() else "error"
    trigger = {
        "showToast": {
            "type": success,
            "title": "User invite",
            "message": note,
            "duration": 8000,
        }
    }
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Trigger": json.dumps(trigger)})
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.get("/users/{user_id}/invite", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_send_invite_get_fallback(user_id: str):
    """Fallback for auth-refresh GET redirect on invite action URLs."""
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.post("/users", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_create(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    role_id: str = Form(...),
    user_type: str = Form("system_user"),
    send_invite: str | None = Form(None),
    db: Session = Depends(get_db),
):
    system_user = None
    try:
        system_user, _ = web_system_user_mutations_service.create_user_with_role_and_password(
            db,
            first_name=first_name,
            last_name=last_name,
            email=email,
            role_id=role_id,
            user_type="system_user",
        )
    except IntegrityError as exc:
        db.rollback()
        return web_system_common_service.error_banner(web_system_common_service.humanize_integrity_error(exc))

    note = "User created. Ask the user to reset their password."
    if send_invite and system_user is not None:
        note = web_system_user_mutations_service.send_user_invite_for_user(
            db,
            user_id=str(system_user.id),
        )
    return HTMLResponse(
        '<div class="rounded-lg border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-700">'
        f"{note}"
        "</div>"
    )


@router.delete("/users/{user_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_delete(request: Request, user_id: str, db: Session = Depends(get_db)):
    system_user = web_system_user_edit_service.get_subscriber_or_none(db, user_id)
    if not system_user:
        raise HTTPException(status_code=404, detail="User not found")
    if system_user.is_active:
        return web_system_common_service.blocked_delete_response(request, [], detail="Deactivate user before deleting.")
    try:
        web_system_user_mutations_service.delete_user_records(db, user_id=user_id)
    except IntegrityError:
        db.rollback()
        return web_system_common_service.blocked_delete_response(request, [], detail="User cannot be deleted due to linked records.")
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": "/admin/system/users"})
    return RedirectResponse(url="/admin/system/users", status_code=303)


@router.get("/roles", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:read"))])
def roles_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List roles and permissions."""
    page_data = web_system_roles_service.get_roles_page_data(
        db,
        page=page,
        per_page=per_page,
    )

    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/system/roles.html",
        {
            "request": request,
            **page_data,
            "active_page": "roles",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/roles/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:write"))])
def role_new(request: Request, db: Session = Depends(get_db)):
    form_context = web_system_form_views_service.get_role_new_form_context(db)
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/system/roles_form.html",
        {
            "request": request,
            **form_context,
            "active_page": "roles",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/roles", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:write"))])
def role_create(
    request: Request,
    name: str = Form(...),
    description: str | None = Form(None),
    is_active: str | None = Form(None),
    permission_ids: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    payload = web_system_role_forms_service.build_role_create_payload(
        name=name,
        description=description,
        is_active=web_system_common_service.form_bool(is_active),
    )
    try:
        web_system_role_forms_service.create_role_with_permissions(
            db,
            payload=payload,
            permission_ids=permission_ids,
        )
    except Exception as exc:
        error_state = web_system_role_forms_service.build_role_error_state(
            db,
            role=payload.model_dump(),
            permission_ids=permission_ids,
        )
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/system/roles_form.html",
            {
                "request": request,
                **error_state,
                "action_url": "/admin/system/roles",
                "form_title": "New Role",
                "submit_label": "Create Role",
                "error": str(exc),
                "active_page": "roles",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/system/roles", status_code=303)


@router.get("/roles/{role_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:write"))])
def role_edit(request: Request, role_id: str, db: Session = Depends(get_db)):
    try:
        form_data = web_system_role_forms_service.get_role_edit_data(db, role_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Role not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/system/roles_form.html",
        {
            "request": request,
            "role": form_data["role"],
            "permissions": form_data["permissions"],
            "selected_permission_ids": form_data["selected_permission_ids"],
            "action_url": f"/admin/system/roles/{role_id}",
            "form_title": "Edit Role",
            "submit_label": "Save Changes",
            "active_page": "roles",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/roles/{role_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:write"))])
def role_update(
    request: Request,
    role_id: str,
    name: str = Form(...),
    description: str | None = Form(None),
    is_active: str | None = Form(None),
    permission_ids: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    payload = web_system_role_forms_service.build_role_update_payload(
        name=name,
        description=description,
        is_active=web_system_common_service.form_bool(is_active),
    )
    try:
        web_system_role_forms_service.update_role_with_permissions(
            db,
            role_id=role_id,
            payload=payload,
            permission_ids=permission_ids,
        )
    except Exception as exc:
        error_state = web_system_role_forms_service.build_role_error_state(
            db=db,
            role={"id": role_id, **payload.model_dump()},
            permission_ids=permission_ids,
        )
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/system/roles_form.html",
            {
                "request": request,
                **error_state,
                "action_url": f"/admin/system/roles/{role_id}",
                "form_title": "Edit Role",
                "submit_label": "Save Changes",
                "error": str(exc),
                "active_page": "roles",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/system/roles", status_code=303)


@router.post("/roles/{role_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:delete"))])
def role_delete(request: Request, role_id: str, db: Session = Depends(get_db)):
    rbac_service.roles.delete(db, role_id)
    return RedirectResponse(url="/admin/system/roles", status_code=303)


@router.get("/permissions", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:permissions:read"))])
def permissions_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    page_data = web_system_roles_service.get_permissions_page_data(
        db,
        page=page,
        per_page=per_page,
    )

    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/system/permissions.html",
        {
            "request": request,
            **page_data,
            "active_page": "roles",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/permissions/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:permissions:write"))])
def permission_new(request: Request, db: Session = Depends(get_db)):
    form_context = web_system_form_views_service.get_permission_new_form_context()
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/system/permissions_form.html",
        {
            "request": request,
            **form_context,
            "active_page": "roles",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/permissions", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:permissions:write"))])
def permission_create(
    request: Request,
    key: str = Form(...),
    description: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    payload = web_system_permission_forms_service.build_permission_create_payload(
        key=key,
        description=description,
        is_active=web_system_common_service.form_bool(is_active),
    )
    try:
        rbac_service.permissions.create(db, payload)
    except Exception as exc:
        error_state = web_system_permission_forms_service.build_permission_error_state(
            permission=payload.model_dump(),
            action_url="/admin/system/permissions",
            form_title="New Permission",
            submit_label="Create Permission",
        )
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/system/permissions_form.html",
            {
                "request": request,
                **error_state,
                "error": str(exc),
                "active_page": "roles",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/system/permissions", status_code=303)


@router.get("/permissions/{permission_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:permissions:write"))])
def permission_edit(request: Request, permission_id: str, db: Session = Depends(get_db)):
    form_context = web_system_form_views_service.get_permission_edit_form_context(
        db,
        permission_id,
    )
    if form_context is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Permission not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/system/permissions_form.html",
        {
            "request": request,
            **form_context,
            "active_page": "roles",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/permissions/{permission_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:permissions:write"))])
def permission_update(
    request: Request,
    permission_id: str,
    key: str = Form(...),
    description: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    payload = web_system_permission_forms_service.build_permission_update_payload(
        key=key,
        description=description,
        is_active=web_system_common_service.form_bool(is_active),
    )
    try:
        rbac_service.permissions.update(db, permission_id, payload)
    except Exception as exc:
        error_state = web_system_permission_forms_service.build_permission_error_state(
            permission={"id": permission_id, **payload.model_dump()},
            action_url=f"/admin/system/permissions/{permission_id}",
            form_title="Edit Permission",
            submit_label="Save Changes",
        )
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/system/permissions_form.html",
            {
                "request": request,
                **error_state,
                "error": str(exc),
                "active_page": "roles",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/system/permissions", status_code=303)


@router.post("/permissions/{permission_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:permissions:delete"))])
def permission_delete(
    request: Request, permission_id: str, db: Session = Depends(get_db)
):
    rbac_service.permissions.delete(db, permission_id)
    return RedirectResponse(url="/admin/system/permissions", status_code=303)


@router.get("/api-keys", response_class=HTMLResponse)
def api_keys_list(request: Request, new_key: str | None = None, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    current_user = get_current_user(request)
    person_id = current_user.get("person_id") if current_user else None
    api_keys = web_system_api_keys_service.list_api_keys_for_subscriber(db, person_id)

    context = {
        "request": request,
        "active_page": "api-keys",
        "active_menu": "system",
        "current_user": current_user,
        "sidebar_stats": get_sidebar_stats(db),
        "api_keys": api_keys,
        "new_key": new_key,
        "now": datetime.now(UTC),
    }
    return templates.TemplateResponse("admin/system/api_keys.html", context)


@router.get("/api-keys/new", response_class=HTMLResponse)
def api_key_new(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    form_context = web_system_api_key_forms_service.get_api_key_new_form_context()
    context = {
        "request": request,
        "active_page": "api-keys",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        **form_context,
    }
    return templates.TemplateResponse("admin/system/api_key_form.html", context)


@router.post("/api-keys", response_class=HTMLResponse)
def api_key_create(
    request: Request,
    label: str = Form(...),
    expires_in: str = Form(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    current_user = get_current_user(request)

    if not current_user or not current_user.get("person_id"):
        return RedirectResponse(url="/admin/system/api-keys", status_code=303)

    try:
        raw_key = web_system_api_key_forms_service.create_api_key(
            db,
            subscriber_id=current_user["person_id"],
            label=label,
            expires_in=expires_in,
        )

        # Return to list with the new key shown
        return RedirectResponse(
            url=f"/admin/system/api-keys?new_key={raw_key}",
            status_code=303
        )
    except Exception as e:
        context = {
            "request": request,
            "active_page": "api-keys",
            "active_menu": "system",
            "current_user": current_user,
            "sidebar_stats": get_sidebar_stats(db),
            "error": str(e),
        }
        return templates.TemplateResponse("admin/system/api_key_form.html", context)


@router.post("/api-keys/{key_id}/revoke", response_class=HTMLResponse)
def api_key_revoke(request: Request, key_id: str, db: Session = Depends(get_db)):
    web_system_api_key_mutations_service.revoke_api_key(db, key_id=key_id)
    return RedirectResponse(url="/admin/system/api-keys", status_code=303)


@router.get("/webhooks", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:read"))])
def webhooks_list(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats
    page_data = web_system_webhooks_service.get_webhooks_list_data(db)

    context = {
        "request": request,
        "active_page": "webhooks",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        **page_data,
    }
    return templates.TemplateResponse("admin/system/webhooks.html", context)


@router.get("/webhooks/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:write"))])
def webhook_new(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    form_context = web_system_webhook_forms_service.get_webhook_new_form_context()
    context = {
        "request": request,
        "active_page": "webhooks",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        **form_context,
    }
    return templates.TemplateResponse("admin/system/webhook_form.html", context)


@router.post("/webhooks", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:write"))])
def webhook_create(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    secret: str = Form(None),
    is_active: str = Form(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    try:
        web_system_webhook_forms_service.create_webhook_endpoint(
            db,
            name=name,
            url=url,
            secret=secret,
            is_active=is_active == "true",
        )
        return RedirectResponse(url="/admin/system/webhooks", status_code=303)
    except Exception as e:
        context: dict[str, object] = {
            "request": request,
            "active_page": "webhooks",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "endpoint": None,
            "subscribed_events": [],
            "action_url": "/admin/system/webhooks",
            "error": str(e),
        }
        return templates.TemplateResponse("admin/system/webhook_form.html", context)


@router.get("/webhooks/{endpoint_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:write"))])
def webhook_edit(request: Request, endpoint_id: str, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    form_data = web_system_webhook_forms_service.get_webhook_form_data(db, endpoint_id)
    if not form_data:
        return RedirectResponse(url="/admin/system/webhooks", status_code=303)

    context = {
        "request": request,
        "active_page": "webhooks",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "endpoint": form_data["endpoint"],
        "subscribed_events": form_data["subscribed_events"],
        "action_url": f"/admin/system/webhooks/{endpoint_id}",
        "error": None,
    }
    return templates.TemplateResponse("admin/system/webhook_form.html", context)


@router.post("/webhooks/{endpoint_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:write"))])
def webhook_update(
    request: Request,
    endpoint_id: str,
    name: str = Form(...),
    url: str = Form(...),
    secret: str = Form(None),
    is_active: str = Form(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    try:
        endpoint = web_system_webhook_forms_service.update_webhook_endpoint(
            db,
            endpoint_id=endpoint_id,
            name=name,
            url=url,
            secret=secret,
            is_active=is_active == "true",
        )
        if endpoint is None:
            return RedirectResponse(url="/admin/system/webhooks", status_code=303)
        return RedirectResponse(url="/admin/system/webhooks", status_code=303)
    except Exception as e:
        form_data = web_system_webhook_forms_service.get_webhook_form_data(db, endpoint_id)
        context = {
            "request": request,
            "active_page": "webhooks",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "endpoint": form_data["endpoint"] if form_data else None,
            "subscribed_events": form_data["subscribed_events"] if form_data else [],
            "action_url": f"/admin/system/webhooks/{endpoint_id}",
            "error": str(e),
        }
        return templates.TemplateResponse("admin/system/webhook_form.html", context)


@router.get("/audit", response_class=HTMLResponse, dependencies=[Depends(require_permission("audit:read"))])
def audit_log(
    request: Request,
    actor_id: str | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """View audit log."""
    page_data = web_system_audit_service.get_audit_page_data(
        db,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        page=page,
        per_page=per_page,
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "admin/system/_audit_table.html",
            {
                "request": request,
                **page_data,
            },
        )

    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/system/audit.html",
        {
            "request": request,
            **page_data,
            "actor_id": actor_id,
            "action": action,
            "entity_type": entity_type,
            "active_page": "audit",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/scheduler", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:read"))])
def scheduler_overview(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """View scheduled tasks."""
    page_data = web_system_scheduler_service.get_scheduler_overview_data(
        db,
        page=page,
        per_page=per_page,
    )

    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/system/scheduler.html",
        {
            "request": request,
            **page_data,
            "active_page": "scheduler",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/scheduler/{task_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:read"))])
def scheduler_task_detail(request: Request, task_id: str, db: Session = Depends(get_db)):
    """View scheduled task details."""
    from app.web.admin import get_current_user, get_sidebar_stats

    detail_data = web_system_scheduler_service.get_scheduler_task_detail_data(db, task_id)
    if not detail_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Scheduled task not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "admin/system/scheduler_detail.html",
        {
            "request": request,
            "task": detail_data["task"],
            "next_run": detail_data["next_run"],
            "runs": detail_data["runs"],
            "active_page": "scheduler",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/scheduler/{task_id}/enable", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:write"))])
def scheduler_task_enable(request: Request, task_id: str, db: Session = Depends(get_db)):
    """Enable a scheduled task."""
    from app.schemas.scheduler import ScheduledTaskUpdate
    scheduler_service.scheduled_tasks.update(db, task_id, ScheduledTaskUpdate(enabled=True))
    return RedirectResponse(url=f"/admin/system/scheduler/{task_id}", status_code=303)


@router.post("/scheduler/{task_id}/disable", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:write"))])
def scheduler_task_disable(request: Request, task_id: str, db: Session = Depends(get_db)):
    """Disable a scheduled task."""
    from app.schemas.scheduler import ScheduledTaskUpdate
    scheduler_service.scheduled_tasks.update(db, task_id, ScheduledTaskUpdate(enabled=False))
    return RedirectResponse(url=f"/admin/system/scheduler/{task_id}", status_code=303)


@router.post("/scheduler/{task_id}/run", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:write"))])
def scheduler_task_run(request: Request, task_id: str, db: Session = Depends(get_db)):
    """Manually trigger a scheduled task."""
    scheduler_service.enqueue_by_id(db, task_id)
    return RedirectResponse(url=f"/admin/system/scheduler/{task_id}", status_code=303)


@router.post("/scheduler/{task_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:write"))])
def scheduler_task_delete(request: Request, task_id: str, db: Session = Depends(get_db)):
    """Delete a scheduled task."""
    scheduler_service.scheduled_tasks.delete(db, task_id)
    return RedirectResponse(url="/admin/system/scheduler", status_code=303)


@router.get("/workflow", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:read"))])
def workflow_overview(request: Request, db: Session = Depends(get_db)):
    """Workflow and SLA configuration overview."""
    context = _workflow_context(request, db)
    return templates.TemplateResponse("admin/system/workflow.html", context)


@router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:read"))])
def settings_overview(
    request: Request,
    domain: str | None = None,
    db: Session = Depends(get_db),
):
    """System settings management."""
    settings_context = web_system_settings_views_service.build_settings_context(db, domain)
    context = web_system_settings_views_service.build_settings_page_context(
        request,
        db,
        settings_context=settings_context,
    )
    return templates.TemplateResponse(
        "admin/system/settings.html",
        context,
    )


@router.post("/settings", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:write"))])
def settings_update(
    request: Request,
    domain: str | None = Form(None),
    form = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    """Update system settings for a domain."""
    domain_value = domain or form.get("domain")
    settings_context, errors = web_system_settings_forms_service.process_settings_update(
        db=db,
        domain_value=domain_value,
        form=form,
    )
    context = web_system_settings_views_service.build_settings_page_context(
        request,
        db,
        settings_context=settings_context,
        extra={"errors": errors, "saved": not errors},
    )
    return templates.TemplateResponse(
        "admin/system/settings.html",
        context,
    )


@router.post(
    "/settings/branding",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_branding_update(
    request: Request,
    main_logo_url: str | None = Form(None),
    dark_logo_url: str | None = Form(None),
    favicon_url: str | None = Form(None),
    remove_main_logo: str | None = Form(None),
    remove_dark_logo: str | None = Form(None),
    remove_favicon: str | None = Form(None),
    main_logo_file: UploadFile | None = File(None),
    dark_logo_file: UploadFile | None = File(None),
    favicon_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    """Update sidebar branding assets via URL or file upload."""
    domain = SettingDomain.comms
    from app.web.admin import get_current_user as get_admin_current_user

    def _is_local_branding_path(value: str) -> bool:
        return value.startswith("/static/branding/")

    def _validate_url(url_value: str) -> str:
        candidate = url_value.strip()
        if not candidate:
            return ""
        if (
            candidate.startswith("http://")
            or candidate.startswith("https://")
            or candidate.startswith("/static/")
            or candidate.startswith("/branding/assets/")
        ):
            return candidate
        raise ValueError(
            "Branding URLs must start with http://, https://, /static/, or /branding/assets/."
        )

    def _resolve_next_value(
        *,
        key: str,
        incoming_url: str | None,
        remove_requested: bool,
        incoming_file: UploadFile | None,
        subdir: str,
        prefix: str,
    ) -> tuple[str, str]:
        current_raw = settings_spec.resolve_value(db, domain, key)
        current_value = str(current_raw).strip() if current_raw else ""
        next_value = current_value

        if remove_requested:
            return current_value, ""

        if incoming_file and incoming_file.filename:
            file_bytes = incoming_file.file.read()
            if not file_bytes:
                raise ValueError("Uploaded file is empty.")
            file_record = branding_storage_service.upload_branding_asset(
                db=db,
                setting_key=key,
                file_data=file_bytes,
                content_type=incoming_file.content_type,
                filename=incoming_file.filename,
                uploaded_by=(get_admin_current_user(request) or {}).get("subscriber_id"),
            )
            next_value = branding_storage_service.branding_url_for_file(file_record.id)
        else:
            validated_url = _validate_url(incoming_url or "")
            if validated_url:
                next_value = validated_url

        return current_value, next_value

    def _persist_setting(key: str, value: str) -> None:
        payload = DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=value,
            value_json=None,
            is_secret=False,
            is_active=True,
        )
        settings_spec.DOMAIN_SETTINGS_SERVICE[domain].upsert_by_key(db, key, payload)

    try:
        assets = [
            (
                web_system_settings_views_service.SIDEBAR_LOGO_SETTING_KEY,
                main_logo_url,
                web_system_common_service.form_bool(remove_main_logo),
                main_logo_file,
                "sidebar",
                "main_logo",
            ),
            (
                web_system_settings_views_service.SIDEBAR_LOGO_DARK_SETTING_KEY,
                dark_logo_url,
                web_system_common_service.form_bool(remove_dark_logo),
                dark_logo_file,
                "sidebar",
                "dark_logo",
            ),
            (
                web_system_settings_views_service.FAVICON_SETTING_KEY,
                favicon_url,
                web_system_common_service.form_bool(remove_favicon),
                favicon_file,
                "favicon",
                "favicon",
            ),
        ]

        updates: list[tuple[str, str, str]] = []
        for key, url_value, remove_flag, file_value, subdir, prefix in assets:
            current_value, next_value = _resolve_next_value(
                key=key,
                incoming_url=url_value,
                remove_requested=remove_flag,
                incoming_file=file_value,
                subdir=subdir,
                prefix=prefix,
            )
            updates.append((key, current_value, next_value))

        for key, _current, next_value in updates:
            _persist_setting(key, next_value)

        for _key, current_value, next_value in updates:
            if not current_value or current_value == next_value:
                continue
            if branding_storage_service.is_managed_branding_url(current_value):
                branding_storage_service.delete_managed_branding_url(db, current_value)
                continue
            if _is_local_branding_path(current_value):
                upload = file_upload_service.get_branding_upload()
                upload.delete_by_url(current_value, "/static/branding/")

        return RedirectResponse(url="/admin/system/settings?domain=branding", status_code=303)
    except Exception as exc:
        settings_context = web_system_settings_views_service.build_settings_context(db, "branding")
        context = web_system_settings_views_service.build_settings_page_context(
            request,
            db,
            settings_context=settings_context,
            extra={"errors": [str(exc)]},
        )
        return templates.TemplateResponse("admin/system/settings.html", context, status_code=400)


@router.post(
    "/settings/smtp-senders",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_smtp_sender_upsert(
    request: Request,
    sender_key: str = Form(...),
    host: str = Form(...),
    port: int = Form(587),
    username: str | None = Form(None),
    password: str | None = Form(None),
    from_email: str = Form(...),
    from_name: str | None = Form(None),
    use_tls: str | None = Form(None),
    use_ssl: str | None = Form(None),
    is_active: str | None = Form(None),
    is_default: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        normalized_key = email_service.upsert_smtp_sender(
            db,
            sender_key=sender_key,
            host=host,
            port=port,
            username=username,
            password=password,
            from_email=from_email,
            from_name=from_name,
            use_tls=web_system_common_service.form_bool(use_tls),
            use_ssl=web_system_common_service.form_bool(use_ssl),
            is_active=web_system_common_service.form_bool(is_active),
        )
        if web_system_common_service.form_bool(is_default):
            email_service.set_default_smtp_sender_key(db, normalized_key)
        return RedirectResponse(
            url="/admin/system/settings?domain=notification#smtp-senders",
            status_code=303,
        )
    except Exception as exc:
        settings_context = web_system_settings_views_service.build_settings_context(db, "notification")
        context = web_system_settings_views_service.build_settings_page_context(
            request,
            db,
            settings_context=settings_context,
            extra={"errors": [str(exc)]},
        )
        return templates.TemplateResponse("admin/system/settings.html", context, status_code=400)


@router.post(
    "/settings/smtp-senders/default",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_smtp_sender_set_default(
    sender_key: str = Form(...),
    db: Session = Depends(get_db),
):
    email_service.set_default_smtp_sender_key(db, sender_key)
    return RedirectResponse(url="/admin/system/settings?domain=notification#smtp-senders", status_code=303)


@router.post(
    "/settings/smtp-senders/{sender_key}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_smtp_sender_delete(sender_key: str, db: Session = Depends(get_db)):
    normalized = sender_key.strip().lower()
    email_service.deactivate_smtp_sender(db, normalized)
    remaining = email_service.list_smtp_senders(db)
    current_default = email_service.get_default_smtp_sender_key(db)
    if current_default == normalized:
        if remaining:
            email_service.set_default_smtp_sender_key(db, str(remaining[0].get("sender_key", "default")))
        else:
            email_service.set_default_smtp_sender_key(db, "default")
    return RedirectResponse(url="/admin/system/settings?domain=notification#smtp-senders", status_code=303)


@router.post(
    "/settings/smtp-senders/{sender_key}/test",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_smtp_sender_test(
    request: Request,
    sender_key: str,
    db: Session = Depends(get_db),
):
    normalized = sender_key.strip().lower()
    config = email_service.get_smtp_config(db, sender_key=normalized)
    ok, error = email_service.test_smtp_connection(config, db=db)
    message = "SMTP connection successful." if ok else (error or "SMTP test failed.")
    settings_context = web_system_settings_views_service.build_settings_context(db, "notification")
    context = web_system_settings_views_service.build_settings_page_context(
        request,
        db,
        settings_context=settings_context,
        extra={
            "smtp_test_result": {
                "sender_key": normalized,
                "ok": ok,
                "message": message,
            }
        },
    )
    return templates.TemplateResponse("admin/system/settings.html", context)


@router.post(
    "/settings/smtp-senders/activities",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_smtp_sender_activities(
    form=Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    for activity_key, _ in email_service.SMTP_ACTIVITY_CHOICES:
        field_name = f"activity_{activity_key}"
        sender = form.get(field_name)
        email_service.upsert_smtp_activity_mapping(db, activity_key, sender)
    return RedirectResponse(url="/admin/system/settings?domain=notification#smtp-senders", status_code=303)


@router.post(
    "/settings/bank-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def settings_bank_account_create(
    request: Request,
    form = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    try:
        payload = web_system_billing_forms_service.build_bank_account_create_payload(form)
        billing_service.bank_accounts.create(db, payload)
        return RedirectResponse(
            url="/admin/system/settings?domain=billing#bank-accounts", status_code=303
        )
    except Exception as exc:
        error = exc.detail if hasattr(exc, "detail") else str(exc)
        context = web_system_billing_forms_service.build_bank_account_error_context(
            request,
            db,
            error=error,
            message="Unable to create bank account.",
        )
        return templates.TemplateResponse(
            "admin/system/settings.html",
            context,
            status_code=400,
        )


@router.post(
    "/settings/bank-accounts/{bank_account_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def settings_bank_account_update(
    request: Request,
    bank_account_id: UUID,
    form = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    try:
        payload = web_system_billing_forms_service.build_bank_account_update_payload(form)
        billing_service.bank_accounts.update(db, str(bank_account_id), payload)
        return RedirectResponse(
            url="/admin/system/settings?domain=billing#bank-accounts", status_code=303
        )
    except Exception as exc:
        error = exc.detail if hasattr(exc, "detail") else str(exc)
        context = web_system_billing_forms_service.build_bank_account_error_context(
            request,
            db,
            error=error,
            message="Unable to update bank account.",
        )
        return templates.TemplateResponse(
            "admin/system/settings.html",
            context,
            status_code=400,
        )


@router.post(
    "/settings/bank-accounts/{bank_account_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def settings_bank_account_deactivate(
    bank_account_id: UUID, db: Session = Depends(get_db)
):
    billing_service.bank_accounts.delete(db, str(bank_account_id))
    return RedirectResponse(
        url="/admin/system/settings?domain=billing#bank-accounts", status_code=303
    )
