"""Admin web routes for OLT profile and TR-069 profile management."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_admin as web_admin_service
from app.services import web_network_olt_profiles as web_network_olt_profiles_service
from app.services.auth_dependencies import require_permission
from app.services.network import olt_operations as olt_operations_service
from app.services.network import olt_tr069_admin as olt_tr069_admin_service
from app.services.network.olt_inventory import get_olt_or_none

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network-olt-profiles"])


def _base_context(request: Request, db: Session, active_page: str) -> dict:
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "network",
        "current_user": web_admin_service.get_current_user(request),
        "sidebar_stats": web_admin_service.get_sidebar_stats(db),
    }


@router.api_route(
    "/olts/{olt_id}/tr069-profiles",
    methods=["GET", "POST"],
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_tr069_profiles_ssh(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Read TR-069 server profiles from OLT via SSH and return partial."""
    ok, message, profiles, extra = olt_tr069_admin_service.get_tr069_profiles_context(
        db, olt_id
    )
    return templates.TemplateResponse(
        "admin/network/olts/_tr069_profiles.html",
        {
            "request": request,
            "olt_id": olt_id,
            "tr069_ok": ok,
            "tr069_message": message,
            "tr069_profiles": profiles,
            "tr069_onts": extra.get("onts", []),
            "acs_prefill": extra.get("acs_prefill", {}),
        },
    )


@router.post(
    "/olts/{olt_id}/tr069-profiles/create",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_tr069_profile_create(
    request: Request,
    olt_id: str,
    profile_name: str = Form(""),
    acs_url: str = Form(""),
    acs_username: str = Form(""),
    acs_password: str = Form(""),
    inform_interval: int = Form(300),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Create a TR-069 server profile on the OLT via SSH."""
    ok, message = olt_tr069_admin_service.handle_create_tr069_profile_audited(
        db,
        olt_id,
        profile_name=profile_name.strip(),
        acs_url=acs_url.strip(),
        username=acs_username.strip(),
        password=acs_password.strip(),
        inform_interval=inform_interval,
        request=request,
    )
    return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 400)


@router.post(
    "/olts/{olt_id}/tr069-profiles/rebind",
    dependencies=[Depends(require_permission("network:write"))],
)
async def olt_tr069_rebind(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Rebind selected ONTs to a TR-069 server profile."""
    form = await request.form()
    target_profile_raw = form.get("target_profile_id", "0")
    target_profile_value = (
        target_profile_raw if isinstance(target_profile_raw, str) else "0"
    )
    target_profile_id = int(target_profile_value)
    ont_ids = [value for value in form.getlist("ont_ids") if isinstance(value, str)]
    if not ont_ids or not target_profile_id:
        return JSONResponse(
            {"ok": False, "message": "Missing ONT selection or target profile"},
            status_code=400,
        )

    stats = olt_tr069_admin_service.handle_rebind_tr069_profiles_audited(
        db,
        olt_id,
        list(ont_ids),
        target_profile_id,
        request=request,
    )
    rebound_raw = stats.get("rebound", 0)
    failed_raw = stats.get("failed", 0)
    rebound = rebound_raw if isinstance(rebound_raw, int) else 0
    failed = failed_raw if isinstance(failed_raw, int) else 0
    errors = stats.get("errors", [])
    message = f"Rebound {rebound} ONT(s) to profile {target_profile_id}"
    if failed:
        message += f", {failed} failed"
    ok = rebound > 0

    return JSONResponse(
        {
            "ok": ok,
            "message": message,
            "rebound": rebound,
            "failed": failed,
            "errors": errors,
        },
        status_code=200 if ok else 400,
    )


@router.post(
    "/olts/{olt_id}/init-tr069",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_init_tr069(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Create or verify the linked ACS TR-069 profile on the OLT."""
    ok, msg, _profile_id = olt_tr069_admin_service.ensure_tr069_profile_for_olt_audited(
        db, olt_id, request=request
    )

    status = "notice" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?{status}={quote_plus(msg)}", status_code=303
    )


@router.post(
    "/olts/{olt_id}/propagate-acs",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_propagate_acs(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Propagate OLT's ACS server to all its unbound ONTs."""
    status_code, payload = web_network_olt_profiles_service.propagate_acs_to_onts(
        db, olt_id, request=request
    )
    return JSONResponse(payload, status_code=status_code)


@router.post(
    "/olts/{olt_id}/enforce-provisioning",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_enforce_provisioning(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Run provisioning enforcement on all ONTs for this OLT."""
    status_code, payload = web_network_olt_profiles_service.enforce_provisioning(
        db, olt_id, request=request
    )
    return JSONResponse(payload, status_code=status_code)


@router.post(
    "/olts/{olt_id}/backfill-pon-ports",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_backfill_pon_ports(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Create missing PON ports from ONT board/port data and link assignments."""
    status_code, payload = web_network_olt_profiles_service.backfill_pon_ports(
        db, olt_id, request=request
    )
    return JSONResponse(payload, status_code=status_code)


@router.get(
    "/olts/{olt_id}/profiles/line",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_line_profiles(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: OLT line and service profiles."""
    data = web_network_olt_profiles_service.line_profiles_context(db, olt_id)
    context = _base_context(request, db, active_page="olts")
    context.update(data)
    return templates.TemplateResponse("admin/network/olts/_profiles_tab.html", context)


@router.get(
    "/olts/{olt_id}/profiles/tr069",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_tr069_profiles(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: OLT TR-069 server profiles."""
    data = web_network_olt_profiles_service.tr069_profiles_context(db, olt_id)
    context = _base_context(request, db, active_page="olts")
    context.update(data)
    return templates.TemplateResponse("admin/network/olts/_profiles_tab.html", context)


@router.post(
    "/olts/{olt_id}/firmware-upgrade",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_firmware_upgrade(
    request: Request,
    olt_id: str,
    firmware_image_id: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Trigger firmware upgrade on OLT via SSH."""
    if not firmware_image_id:
        msg = quote_plus("No firmware image selected")
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?sync_status=error&sync_message={msg}",
            status_code=303,
        )

    ok, message = olt_operations_service.trigger_olt_firmware_upgrade(
        db, olt_id, firmware_image_id, request=request
    )
    status = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status={status}&sync_message={quote_plus(message)}",
        status_code=303,
    )


@router.get(
    "/olts/{olt_id}/backups",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_backups_list(
    request: Request,
    olt_id: str,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    test_status: str | None = None,
    test_message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    backups = olt_operations_service.list_olt_backups(
        db,
        olt_id=olt_id,
        start_at=start_at,
        end_at=end_at,
    )
    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            "olt": olt,
            "backups": backups,
            "start_at": start_at,
            "end_at": end_at,
            "test_status": test_status,
            "test_message": test_message,
        }
    )
    return templates.TemplateResponse("admin/network/olts/backups.html", context)


@router.get(
    "/olts/backups/{backup_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_backup_detail(
    request: Request, backup_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    backup = olt_operations_service.get_olt_backup_or_none(db, backup_id)
    if not backup:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Backup not found"},
            status_code=404,
        )
    olt = get_olt_or_none(db, str(backup.olt_device_id))
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    preview = olt_operations_service.read_backup_preview(backup)
    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            "olt": olt,
            "backup": backup,
            "preview": preview,
        }
    )
    return templates.TemplateResponse("admin/network/olts/backup_detail.html", context)


@router.get(
    "/olts/backups/{backup_id}/download",
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_backup_download(backup_id: str, db: Session = Depends(get_db)) -> FileResponse:
    backup = olt_operations_service.get_olt_backup_or_none(db, backup_id)
    if not backup:
        raise HTTPException(status_code=404, detail="Backup not found")
    path = olt_operations_service.backup_file_path(backup)
    filename = path.name
    return FileResponse(path=path, filename=filename, media_type="text/plain")


@router.post(
    "/olts/{olt_id}/backups/test-connection",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_backup_test_connection(
    olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    ok, message = olt_operations_service.test_olt_connection(db, olt_id)
    status = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}/backups?test_status={status}&test_message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/backups/test-backup",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_backup_test_backup(
    olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    backup, message = olt_operations_service.run_test_backup(db, olt_id)
    status = "success" if backup is not None else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}/backups?test_status={status}&test_message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/backups/ssh-backup",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_backup_ssh(olt_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    """Fetch full running config via SSH and save as backup."""
    backup, message = olt_operations_service.backup_running_config_ssh(db, olt_id)
    status = "success" if backup is not None else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}/backups?test_status={status}&test_message={quote_plus(message)}",
        status_code=303,
    )


@router.get(
    "/olts/backups/compare",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_backup_compare(
    request: Request,
    backup_id_1: str,
    backup_id_2: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        backup1, backup2, diff = olt_operations_service.compare_olt_backups(
            db, backup_id_1, backup_id_2
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": str(exc.detail)},
            status_code=exc.status_code,
        )

    olt = get_olt_or_none(db, str(backup1.olt_device_id))
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="olts")
    context.update({"olt": olt, "backup1": backup1, "backup2": backup2, "diff": diff})
    return templates.TemplateResponse("admin/network/olts/backup_compare.html", context)
