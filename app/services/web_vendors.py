"""Context builders for the admin vendor pages.

Thin-wrapper rule: ``app/web`` may not touch the session directly
(``tests/architecture/test_thin_wrappers.py``), so every read the vendor pages
need is assembled here and handed to the route as a plain dict.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services import vendor_admin


def _as_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "on", "yes"}


def _active_filter(value: str | None) -> bool | None:
    """The list filter is tri-state: active / inactive / all."""
    choice = (value or "").strip().lower()
    if choice == "active":
        return True
    if choice == "inactive":
        return False
    return None


def _vendor_form_fields(
    *,
    name: str | None = None,
    code: str | None = None,
    contact_name: str | None = None,
    contact_email: str | None = None,
    contact_phone: str | None = None,
    license_number: str | None = None,
    service_area: str | None = None,
    notes: str | None = None,
    is_active: bool = True,
) -> dict[str, Any]:
    return {
        "name": (name or "").strip(),
        "code": (code or "").strip(),
        "contact_name": (contact_name or "").strip(),
        "contact_email": (contact_email or "").strip(),
        "contact_phone": (contact_phone or "").strip(),
        "license_number": (license_number or "").strip(),
        "service_area": (service_area or "").strip(),
        "notes": (notes or "").strip(),
        "is_active": is_active,
    }


def build_vendors_list_context(
    db: Session,
    *,
    search: str | None,
    status: str | None,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    is_active = _active_filter(status)
    offset = (page - 1) * per_page
    vendors = vendor_admin.list_vendors(
        db,
        search=search or None,
        is_active=is_active,
        limit=per_page,
        offset=offset,
    )
    total = vendor_admin.count(db, search=search or None, is_active=is_active)
    return {
        "vendors": vendors,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
        "search": search or "",
        "status": (status or "").strip(),
    }


def build_vendor_new_context() -> dict[str, Any]:
    return {
        "vendor": None,
        "vendor_form": _vendor_form_fields(),
        "form_title": "New Vendor",
        "submit_label": "Create Vendor",
        "action_url": "/admin/vendors",
        "error": None,
    }


def build_vendor_edit_context(db: Session, *, vendor_id: str) -> dict[str, Any]:
    vendor = vendor_admin.get(db, vendor_id)
    return {
        "vendor": vendor,
        "vendor_form": _vendor_form_fields(
            name=vendor.name,
            code=vendor.code,
            contact_name=vendor.contact_name,
            contact_email=vendor.contact_email,
            contact_phone=vendor.contact_phone,
            license_number=vendor.license_number,
            service_area=vendor.service_area,
            notes=vendor.notes,
            is_active=vendor.is_active,
        ),
        "form_title": "Edit Vendor",
        "submit_label": "Update Vendor",
        "action_url": f"/admin/vendors/{vendor_id}/edit",
        "error": None,
    }


def build_vendor_form_error_context(
    *, mode: str, vendor_id: str | None, is_active: str | None = None, **fields: Any
) -> dict[str, Any]:
    """Re-render the form with the operator's input intact after a failure.

    Takes the raw form values (``is_active`` still a string) so the route stays
    a thin pass-through and does not have to know the coercion rules.
    """
    editing = mode == "update"
    return {
        "vendor": None,
        "vendor_form": _vendor_form_fields(**fields, is_active=_as_bool(is_active)),
        "form_title": "Edit Vendor" if editing else "New Vendor",
        "submit_label": "Update Vendor" if editing else "Create Vendor",
        "action_url": (
            f"/admin/vendors/{vendor_id}/edit" if editing else "/admin/vendors"
        ),
    }


def build_vendor_detail_context(db: Session, *, vendor_id: str) -> dict[str, Any]:
    vendor = vendor_admin.get(db, vendor_id)
    field_vendor = vendor_admin.get_field_vendor(db, vendor)
    return {
        "vendor": vendor,
        # Surfaced so staff can see at a glance whether this vendor can
        # actually sign in -- portal auth resolves through the FieldVendor
        # twin, not the native row.
        "field_vendor": field_vendor,
        "portal_login_enabled": bool(field_vendor and field_vendor.is_active),
        "vendor_users": list(field_vendor.users) if field_vendor else [],
    }


def create_vendor_from_form(
    db: Session,
    *,
    name: str | None,
    code: str | None,
    contact_name: str | None,
    contact_email: str | None,
    contact_phone: str | None,
    license_number: str | None,
    service_area: str | None,
    notes: str | None,
    is_active: str | None,
) -> str:
    vendor = vendor_admin.create_committed(
        db,
        name=name,
        code=code,
        contact_name=contact_name,
        contact_email=contact_email,
        contact_phone=contact_phone,
        license_number=license_number,
        service_area=service_area,
        notes=notes,
        is_active=_as_bool(is_active) if is_active is not None else True,
    )
    return str(vendor.id)


def update_vendor_from_form(
    db: Session,
    *,
    vendor_id: str,
    name: str | None,
    code: str | None,
    contact_name: str | None,
    contact_email: str | None,
    contact_phone: str | None,
    license_number: str | None,
    service_area: str | None,
    notes: str | None,
    is_active: str | None,
) -> None:
    vendor_admin.update_committed(
        db,
        vendor_id,
        name=name,
        code=code,
        contact_name=contact_name,
        contact_email=contact_email,
        contact_phone=contact_phone,
        license_number=license_number,
        service_area=service_area,
        notes=notes,
        is_active=_as_bool(is_active),
    )


def deactivate_vendor(db: Session, vendor_id: str) -> None:
    vendor_admin.deactivate_committed(db, vendor_id)
