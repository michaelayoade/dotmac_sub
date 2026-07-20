"""Admin page contract for firmware catalog management.

Audience/job: network administrators register and maintain verified OLT/ONT
artifacts. The list supports choosing an available artifact by identity, usage,
and catalog state; the editor supports the create/update transition. The
``network.firmware_catalog`` service owns reads, validation, usage-based action
eligibility, and commands. The UI exposes one add action and one edit row action,
shows checksum evidence at work depth, and never initiates a device upgrade.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.requests import Request

from app.services import web_admin
from app.services.auth_dependencies import has_permission
from app.services.network import firmware_catalog


def _base_context(request: Request, db: Session) -> dict[str, object]:
    auth = getattr(request.state, "auth", None) or {}
    return {
        "request": request,
        "active_page": "firmware-images",
        "active_menu": "network",
        "current_user": web_admin.get_current_user(request),
        "sidebar_stats": web_admin.get_sidebar_stats(db),
        "can_manage_firmware": bool(auth)
        and has_permission(auth, db, "network:vendor_capability:write"),
    }


def parse_form(form: FormData) -> dict[str, object]:
    return {
        "kind": str(form.get("kind") or ""),
        "vendor": str(form.get("vendor") or ""),
        "model": str(form.get("model") or ""),
        "version": str(form.get("version") or ""),
        "file_url": str(form.get("file_url") or ""),
        "filename": str(form.get("filename") or ""),
        "checksum": str(form.get("checksum") or ""),
        "file_size_bytes": str(form.get("file_size_bytes") or ""),
        "release_notes": str(form.get("release_notes") or ""),
        "notes": str(form.get("notes") or ""),
        "is_active": str(form.get("is_active") or "").lower() in {"1", "true", "on"},
    }


def list_context(
    request: Request,
    db: Session,
    *,
    kind: str,
    search: str | None,
    vendor: str | None,
    status: str,
    page: int,
    per_page: int,
    feedback: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    context = _base_context(request, db)
    context.update(
        firmware_catalog.list_images(
            db,
            kind=kind,
            search=search,
            vendor=vendor,
            status=status,
            page=page,
            per_page=per_page,
        )
    )
    context.update(
        {
            "filters": {
                "kind": kind,
                "search": search or "",
                "vendor": vendor or "",
                "status": status,
                "per_page": per_page,
            },
            "feedback": feedback,
            "error": error,
        }
    )
    return context


def form_context(
    request: Request,
    db: Session,
    *,
    kind: str,
    image_id: str | None = None,
    values: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    image = firmware_catalog.get_image(db, kind, image_id) if image_id else None
    usage = (
        firmware_catalog.image_usage(db, image.id)
        if image is not None
        else firmware_catalog.FirmwareUsage()
    )
    context = _base_context(request, db)
    context.update(
        {
            "kind": kind,
            "image": image,
            "usage": usage,
            "form_values": values,
            "error": error,
            "action_url": (
                f"/admin/network/firmware-images/{kind}/{image.id}/edit"
                if image is not None
                else "/admin/network/firmware-images/create"
            ),
        }
    )
    return context
