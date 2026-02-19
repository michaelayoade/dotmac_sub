"""Admin catalog settings management web routes."""

from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.csrf import get_csrf_token
from app.db import get_db
from app.models.catalog import (
    AddOnType,
    BillingCycle,
    DunningAction,
    PriceType,
    PriceUnit,
    ProrationPolicy,
    RefundPolicy,
    SuspensionAction,
)
from app.schemas.catalog import (
    AddOnCreate,
    AddOnUpdate,
    PolicySetCreate,
    PolicySetUpdate,
    RegionZoneCreate,
    RegionZoneUpdate,
    SlaProfileCreate,
    SlaProfileUpdate,
    UsageAllowanceCreate,
    UsageAllowanceUpdate,
)
from app.services import catalog as catalog_service
from app.services import web_catalog_settings as settings_svc
from app.web.request_parsing import parse_form_data

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/catalog/settings", tags=["web-admin-catalog-settings"])

def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


def _form_optional_str(form: FormData, key: str) -> str | None:
    value = form.get(key)
    return value if isinstance(value, str) else None


def _form_getlist_str(form: FormData, key: str) -> list[str]:
    return [value for value in form.getlist(key) if isinstance(value, str)]


def _base_context(request: Request, db: Session, active_page: str, settings_tab: str = "") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "catalog",
        "settings_tab": settings_tab,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "csrf_token": get_csrf_token(request),
    }


# =============================================================================
# SETTINGS OVERVIEW
# =============================================================================


@router.get("", response_class=HTMLResponse)
def catalog_settings_index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Settings overview with cards linking to each section."""
    counts = settings_svc.settings_overview_counts(db)
    context = _base_context(request, db, active_page="catalog-settings")
    context.update(counts)
    return templates.TemplateResponse("admin/catalog/settings/index.html", context)


# =============================================================================
# REGION ZONES
# =============================================================================


@router.get("/region-zones", response_class=HTMLResponse)
def region_zones_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List region zones."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    result = settings_svc.list_region_zones_paginated(
        db, is_active=is_active, search=search, page=page, per_page=per_page,
    )

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="region-zones")
    context.update({
        "zones": result.items,
        "status": status,
        "search": search,
        "page": page,
        "per_page": per_page,
        "total": result.total,
        "total_pages": result.total_pages,
    })
    return templates.TemplateResponse("admin/catalog/settings/region_zones.html", context)


@router.get("/region-zones/new", response_class=HTMLResponse)
def region_zone_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Create region zone form."""
    zone = {"name": "", "code": "", "description": "", "is_active": True}
    context = _base_context(request, db, active_page="catalog-settings", settings_tab="region-zones")
    context.update({"zone": zone, "action_url": "/admin/catalog/settings/region-zones"})
    return templates.TemplateResponse("admin/catalog/settings/region_zone_form.html", context)


@router.post("/region-zones", response_class=HTMLResponse)
def region_zone_create(request: Request, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> Response:
    """Create region zone."""
    zone = {
        "name": _form_str(form, "name").strip(),
        "code": _form_str(form, "code").strip(),
        "description": _form_str(form, "description").strip(),
        "is_active": _form_str(form, "is_active") == "true",
    }

    try:
        payload = RegionZoneCreate.model_validate(
            {
                "name": zone["name"],
                "code": zone["code"] or None,
                "description": zone["description"] or None,
                "is_active": zone["is_active"],
            }
        )
        catalog_service.region_zones.create(db=db, payload=payload)
        return RedirectResponse("/admin/catalog/settings/region-zones", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="region-zones")
    context.update({"zone": zone, "error": error, "action_url": "/admin/catalog/settings/region-zones"})
    return templates.TemplateResponse("admin/catalog/settings/region_zone_form.html", context)


@router.get("/region-zones/{zone_id}/edit", response_class=HTMLResponse)
def region_zone_edit(request: Request, zone_id: str, db: Session = Depends(get_db)) -> Response:
    """Edit region zone form."""
    try:
        zone_obj = catalog_service.region_zones.get(db=db, zone_id=zone_id)
    except Exception:
        return RedirectResponse("/admin/catalog/settings/region-zones", status_code=303)

    zone = {
        "id": str(zone_obj.id),
        "name": zone_obj.name,
        "code": zone_obj.code or "",
        "description": zone_obj.description or "",
        "is_active": zone_obj.is_active,
    }
    context = _base_context(request, db, active_page="catalog-settings", settings_tab="region-zones")
    context.update({"zone": zone, "action_url": f"/admin/catalog/settings/region-zones/{zone_id}/edit"})
    return templates.TemplateResponse("admin/catalog/settings/region_zone_form.html", context)


@router.post("/region-zones/{zone_id}/edit", response_class=HTMLResponse)
def region_zone_update(request: Request, zone_id: str, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> Response:
    """Update region zone."""
    zone = {
        "id": zone_id,
        "name": _form_str(form, "name").strip(),
        "code": _form_str(form, "code").strip(),
        "description": _form_str(form, "description").strip(),
        "is_active": _form_str(form, "is_active") == "true",
    }

    try:
        payload = RegionZoneUpdate.model_validate(
            {
                "name": zone["name"],
                "code": zone["code"] or None,
                "description": zone["description"] or None,
                "is_active": zone["is_active"],
            }
        )
        catalog_service.region_zones.update(db=db, zone_id=zone_id, payload=payload)
        return RedirectResponse("/admin/catalog/settings/region-zones", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="region-zones")
    context.update({"zone": zone, "error": error, "action_url": f"/admin/catalog/settings/region-zones/{zone_id}/edit"})
    return templates.TemplateResponse("admin/catalog/settings/region_zone_form.html", context)


@router.post("/region-zones/{zone_id}/delete", response_class=HTMLResponse)
def region_zone_delete(request: Request, zone_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    """Delete (deactivate) region zone."""
    try:
        catalog_service.region_zones.delete(db=db, zone_id=zone_id)
    except Exception:
        pass
    return RedirectResponse("/admin/catalog/settings/region-zones", status_code=303)


# =============================================================================
# USAGE ALLOWANCES
# =============================================================================


@router.get("/usage-allowances", response_class=HTMLResponse)
def usage_allowances_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List usage allowances."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    result = settings_svc.list_usage_allowances_paginated(
        db, is_active=is_active, search=search, page=page, per_page=per_page,
    )

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="usage-allowances")
    context.update({
        "allowances": result.items,
        "status": status,
        "search": search,
        "page": page,
        "per_page": per_page,
        "total": result.total,
        "total_pages": result.total_pages,
    })
    return templates.TemplateResponse("admin/catalog/settings/usage_allowances.html", context)


@router.get("/usage-allowances/new", response_class=HTMLResponse)
def usage_allowance_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Create usage allowance form."""
    allowance = {
        "name": "",
        "included_gb": "",
        "overage_rate": "",
        "overage_cap_gb": "",
        "throttle_rate_mbps": "",
        "is_active": True,
    }
    context = _base_context(request, db, active_page="catalog-settings", settings_tab="usage-allowances")
    context.update({"allowance": allowance, "action_url": "/admin/catalog/settings/usage-allowances"})
    return templates.TemplateResponse("admin/catalog/settings/usage_allowance_form.html", context)


@router.post("/usage-allowances", response_class=HTMLResponse)
def usage_allowance_create(request: Request, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> Response:
    """Create usage allowance."""
    allowance = {
        "name": _form_str(form, "name").strip(),
        "included_gb": _form_str(form, "included_gb").strip(),
        "overage_rate": _form_str(form, "overage_rate").strip(),
        "overage_cap_gb": _form_str(form, "overage_cap_gb").strip(),
        "throttle_rate_mbps": _form_str(form, "throttle_rate_mbps").strip(),
        "is_active": _form_str(form, "is_active") == "true",
    }

    try:
        included_gb_s = cast(str, allowance["included_gb"])
        overage_cap_gb_s = cast(str, allowance["overage_cap_gb"])
        throttle_rate_mbps_s = cast(str, allowance["throttle_rate_mbps"])
        payload = UsageAllowanceCreate.model_validate(
            {
                "name": allowance["name"],
                "included_gb": int(included_gb_s) if included_gb_s else None,
                "overage_rate": allowance["overage_rate"] if allowance["overage_rate"] else None,
                "overage_cap_gb": int(overage_cap_gb_s) if overage_cap_gb_s else None,
                "throttle_rate_mbps": int(throttle_rate_mbps_s) if throttle_rate_mbps_s else None,
                "is_active": allowance["is_active"],
            }
        )
        catalog_service.usage_allowances.create(db=db, payload=payload)
        return RedirectResponse("/admin/catalog/settings/usage-allowances", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="usage-allowances")
    context.update({"allowance": allowance, "error": error, "action_url": "/admin/catalog/settings/usage-allowances"})
    return templates.TemplateResponse("admin/catalog/settings/usage_allowance_form.html", context)


@router.get("/usage-allowances/{allowance_id}/edit", response_class=HTMLResponse)
def usage_allowance_edit(request: Request, allowance_id: str, db: Session = Depends(get_db)) -> Response:
    """Edit usage allowance form."""
    try:
        obj = catalog_service.usage_allowances.get(db=db, allowance_id=allowance_id)
    except Exception:
        return RedirectResponse("/admin/catalog/settings/usage-allowances", status_code=303)

    allowance = {
        "id": str(obj.id),
        "name": obj.name,
        "included_gb": obj.included_gb or "",
        "overage_rate": obj.overage_rate or "",
        "overage_cap_gb": obj.overage_cap_gb or "",
        "throttle_rate_mbps": obj.throttle_rate_mbps or "",
        "is_active": obj.is_active,
    }
    context = _base_context(request, db, active_page="catalog-settings", settings_tab="usage-allowances")
    context.update({"allowance": allowance, "action_url": f"/admin/catalog/settings/usage-allowances/{allowance_id}/edit"})
    return templates.TemplateResponse("admin/catalog/settings/usage_allowance_form.html", context)


@router.post("/usage-allowances/{allowance_id}/edit", response_class=HTMLResponse)
def usage_allowance_update(request: Request, allowance_id: str, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> Response:
    """Update usage allowance."""
    allowance = {
        "id": allowance_id,
        "name": _form_str(form, "name").strip(),
        "included_gb": _form_str(form, "included_gb").strip(),
        "overage_rate": _form_str(form, "overage_rate").strip(),
        "overage_cap_gb": _form_str(form, "overage_cap_gb").strip(),
        "throttle_rate_mbps": _form_str(form, "throttle_rate_mbps").strip(),
        "is_active": _form_str(form, "is_active") == "true",
    }

    try:
        included_gb_s = cast(str, allowance["included_gb"])
        overage_cap_gb_s = cast(str, allowance["overage_cap_gb"])
        throttle_rate_mbps_s = cast(str, allowance["throttle_rate_mbps"])
        payload = UsageAllowanceUpdate.model_validate(
            {
                "name": allowance["name"],
                "included_gb": int(included_gb_s) if included_gb_s else None,
                "overage_rate": allowance["overage_rate"] if allowance["overage_rate"] else None,
                "overage_cap_gb": int(overage_cap_gb_s) if overage_cap_gb_s else None,
                "throttle_rate_mbps": int(throttle_rate_mbps_s) if throttle_rate_mbps_s else None,
                "is_active": allowance["is_active"],
            }
        )
        catalog_service.usage_allowances.update(db=db, allowance_id=allowance_id, payload=payload)
        return RedirectResponse("/admin/catalog/settings/usage-allowances", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="usage-allowances")
    context.update({"allowance": allowance, "error": error, "action_url": f"/admin/catalog/settings/usage-allowances/{allowance_id}/edit"})
    return templates.TemplateResponse("admin/catalog/settings/usage_allowance_form.html", context)


@router.post("/usage-allowances/{allowance_id}/delete", response_class=HTMLResponse)
def usage_allowance_delete(request: Request, allowance_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    """Delete (deactivate) usage allowance."""
    try:
        catalog_service.usage_allowances.delete(db=db, allowance_id=allowance_id)
    except Exception:
        pass
    return RedirectResponse("/admin/catalog/settings/usage-allowances", status_code=303)


# =============================================================================
# SLA PROFILES
# =============================================================================


@router.get("/sla-profiles", response_class=HTMLResponse)
def sla_profiles_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List SLA profiles."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    result = settings_svc.list_sla_profiles_paginated(
        db, is_active=is_active, search=search, page=page, per_page=per_page,
    )

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="sla-profiles")
    context.update({
        "profiles": result.items,
        "status": status,
        "search": search,
        "page": page,
        "per_page": per_page,
        "total": result.total,
        "total_pages": result.total_pages,
    })
    return templates.TemplateResponse("admin/catalog/settings/sla_profiles.html", context)


@router.get("/sla-profiles/new", response_class=HTMLResponse)
def sla_profile_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Create SLA profile form."""
    profile = {
        "name": "",
        "uptime_percent": "",
        "response_time_hours": "",
        "resolution_time_hours": "",
        "credit_percent": "",
        "notes": "",
        "is_active": True,
    }
    context = _base_context(request, db, active_page="catalog-settings", settings_tab="sla-profiles")
    context.update({"profile": profile, "action_url": "/admin/catalog/settings/sla-profiles"})
    return templates.TemplateResponse("admin/catalog/settings/sla_profile_form.html", context)


@router.post("/sla-profiles", response_class=HTMLResponse)
def sla_profile_create(request: Request, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> Response:
    """Create SLA profile."""
    profile = {
        "name": _form_str(form, "name").strip(),
        "uptime_percent": _form_str(form, "uptime_percent").strip(),
        "response_time_hours": _form_str(form, "response_time_hours").strip(),
        "resolution_time_hours": _form_str(form, "resolution_time_hours").strip(),
        "credit_percent": _form_str(form, "credit_percent").strip(),
        "notes": _form_str(form, "notes").strip(),
        "is_active": _form_str(form, "is_active") == "true",
    }

    try:
        response_time_hours_s = cast(str, profile["response_time_hours"])
        resolution_time_hours_s = cast(str, profile["resolution_time_hours"])
        payload = SlaProfileCreate.model_validate(
            {
                "name": profile["name"],
                "uptime_percent": profile["uptime_percent"] if profile["uptime_percent"] else None,
                "response_time_hours": int(response_time_hours_s) if response_time_hours_s else None,
                "resolution_time_hours": int(resolution_time_hours_s) if resolution_time_hours_s else None,
                "credit_percent": profile["credit_percent"] if profile["credit_percent"] else None,
                "notes": profile["notes"] or None,
                "is_active": profile["is_active"],
            }
        )
        catalog_service.sla_profiles.create(db=db, payload=payload)
        return RedirectResponse("/admin/catalog/settings/sla-profiles", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="sla-profiles")
    context.update({"profile": profile, "error": error, "action_url": "/admin/catalog/settings/sla-profiles"})
    return templates.TemplateResponse("admin/catalog/settings/sla_profile_form.html", context)


@router.get("/sla-profiles/{profile_id}/edit", response_class=HTMLResponse)
def sla_profile_edit(request: Request, profile_id: str, db: Session = Depends(get_db)) -> Response:
    """Edit SLA profile form."""
    try:
        obj = catalog_service.sla_profiles.get(db=db, profile_id=profile_id)
    except Exception:
        return RedirectResponse("/admin/catalog/settings/sla-profiles", status_code=303)

    profile = {
        "id": str(obj.id),
        "name": obj.name,
        "uptime_percent": obj.uptime_percent or "",
        "response_time_hours": obj.response_time_hours or "",
        "resolution_time_hours": obj.resolution_time_hours or "",
        "credit_percent": obj.credit_percent or "",
        "notes": obj.notes or "",
        "is_active": obj.is_active,
    }
    context = _base_context(request, db, active_page="catalog-settings", settings_tab="sla-profiles")
    context.update({"profile": profile, "action_url": f"/admin/catalog/settings/sla-profiles/{profile_id}/edit"})
    return templates.TemplateResponse("admin/catalog/settings/sla_profile_form.html", context)


@router.post("/sla-profiles/{profile_id}/edit", response_class=HTMLResponse)
def sla_profile_update(request: Request, profile_id: str, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> Response:
    """Update SLA profile."""
    profile = {
        "id": profile_id,
        "name": _form_str(form, "name").strip(),
        "uptime_percent": _form_str(form, "uptime_percent").strip(),
        "response_time_hours": _form_str(form, "response_time_hours").strip(),
        "resolution_time_hours": _form_str(form, "resolution_time_hours").strip(),
        "credit_percent": _form_str(form, "credit_percent").strip(),
        "notes": _form_str(form, "notes").strip(),
        "is_active": _form_str(form, "is_active") == "true",
    }

    try:
        response_time_hours_s = cast(str, profile["response_time_hours"])
        resolution_time_hours_s = cast(str, profile["resolution_time_hours"])
        payload = SlaProfileUpdate.model_validate(
            {
                "name": profile["name"],
                "uptime_percent": profile["uptime_percent"] if profile["uptime_percent"] else None,
                "response_time_hours": int(response_time_hours_s) if response_time_hours_s else None,
                "resolution_time_hours": int(resolution_time_hours_s) if resolution_time_hours_s else None,
                "credit_percent": profile["credit_percent"] if profile["credit_percent"] else None,
                "notes": profile["notes"] or None,
                "is_active": profile["is_active"],
            }
        )
        catalog_service.sla_profiles.update(db=db, profile_id=profile_id, payload=payload)
        return RedirectResponse("/admin/catalog/settings/sla-profiles", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="sla-profiles")
    context.update({"profile": profile, "error": error, "action_url": f"/admin/catalog/settings/sla-profiles/{profile_id}/edit"})
    return templates.TemplateResponse("admin/catalog/settings/sla_profile_form.html", context)


@router.post("/sla-profiles/{profile_id}/delete", response_class=HTMLResponse)
def sla_profile_delete(request: Request, profile_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    """Delete (deactivate) SLA profile."""
    try:
        catalog_service.sla_profiles.delete(db=db, profile_id=profile_id)
    except Exception:
        pass
    return RedirectResponse("/admin/catalog/settings/sla-profiles", status_code=303)


# =============================================================================
# POLICY SETS (with nested dunning steps)
# =============================================================================


@router.get("/policy-sets", response_class=HTMLResponse)
def policy_sets_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List policy sets."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    result = settings_svc.list_policy_sets_paginated(
        db, is_active=is_active, search=search, page=page, per_page=per_page,
    )

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="policy-sets")
    context.update({
        "policies": result.items,
        "status": status,
        "search": search,
        "page": page,
        "per_page": per_page,
        "total": result.total,
        "total_pages": result.total_pages,
    })
    return templates.TemplateResponse("admin/catalog/settings/policy_sets.html", context)


def _policy_form_context(request: Request, db: Session, policy: dict, error: str | None = None) -> dict:
    context = _base_context(request, db, active_page="catalog-settings", settings_tab="policy-sets")
    context.update({
        "policy": policy,
        "proration_policies": [item.value for item in ProrationPolicy],
        "suspension_actions": [item.value for item in SuspensionAction],
        "refund_policies": [item.value for item in RefundPolicy],
        "dunning_actions": [item.value for item in DunningAction],
    })
    if error:
        context["error"] = error
    return context


@router.get("/policy-sets/new", response_class=HTMLResponse)
def policy_set_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Create policy set form."""
    policy = {
        "name": "",
        "proration_policy": "immediate",
        "downgrade_policy": "next_cycle",
        "trial_days": "",
        "trial_card_required": False,
        "grace_days": "",
        "suspension_action": "suspend",
        "refund_policy": "none",
        "refund_window_days": "",
        "is_active": True,
        "dunning_steps": [],
    }
    context = _policy_form_context(request, db, policy)
    context["action_url"] = "/admin/catalog/settings/policy-sets"
    return templates.TemplateResponse("admin/catalog/settings/policy_set_form.html", context)


@router.post("/policy-sets", response_class=HTMLResponse)
def policy_set_create(request: Request, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> Response:
    """Create policy set with dunning steps."""

    # Parse dunning steps from form
    dunning_steps: list[dict[str, str]] = []
    i = 0
    while True:
        day_offset = _form_optional_str(form, f"dunning_steps[{i}][day_offset]")
        if day_offset is None:
            break
        action = _form_str(form, f"dunning_steps[{i}][action]").strip()
        note = _form_str(form, f"dunning_steps[{i}][note]").strip()
        if day_offset.strip() and action:
            dunning_steps.append({
                "day_offset": day_offset.strip(),
                "action": action,
                "note": note,
            })
        i += 1

    name = _form_str(form, "name").strip()
    proration_policy = _form_str(form, "proration_policy", "immediate").strip()
    downgrade_policy = _form_str(form, "downgrade_policy", "next_cycle").strip()
    trial_days_s = _form_str(form, "trial_days").strip()
    trial_card_required = _form_str(form, "trial_card_required") == "true"
    grace_days_s = _form_str(form, "grace_days").strip()
    suspension_action = _form_str(form, "suspension_action", "suspend").strip()
    refund_policy = _form_str(form, "refund_policy", "none").strip()
    refund_window_days_s = _form_str(form, "refund_window_days").strip()
    is_active = _form_str(form, "is_active") == "true"
    policy: dict[str, Any] = {
        "name": name,
        "proration_policy": proration_policy,
        "downgrade_policy": downgrade_policy,
        "trial_days": trial_days_s,
        "trial_card_required": trial_card_required,
        "grace_days": grace_days_s,
        "suspension_action": suspension_action,
        "refund_policy": refund_policy,
        "refund_window_days": refund_window_days_s,
        "is_active": is_active,
        "dunning_steps": dunning_steps,
    }

    try:
        payload = PolicySetCreate(
            name=name,
            proration_policy=ProrationPolicy(proration_policy),
            downgrade_policy=ProrationPolicy(downgrade_policy),
            trial_days=int(trial_days_s) if trial_days_s else None,
            trial_card_required=trial_card_required,
            grace_days=int(grace_days_s) if grace_days_s else None,
            suspension_action=SuspensionAction(suspension_action),
            refund_policy=RefundPolicy(refund_policy),
            refund_window_days=int(refund_window_days_s) if refund_window_days_s else None,
            is_active=is_active,
        )
        created = catalog_service.policy_sets.create(db=db, payload=payload)
        settings_svc.create_dunning_steps(db, str(created.id), dunning_steps)
        return RedirectResponse("/admin/catalog/settings/policy-sets", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _policy_form_context(request, db, policy, error)
    context["action_url"] = "/admin/catalog/settings/policy-sets"
    return templates.TemplateResponse("admin/catalog/settings/policy_set_form.html", context)


@router.get("/policy-sets/{policy_id}/edit", response_class=HTMLResponse)
def policy_set_edit(request: Request, policy_id: str, db: Session = Depends(get_db)) -> Response:
    """Edit policy set form."""
    try:
        obj = catalog_service.policy_sets.get(db=db, policy_id=policy_id)
    except Exception:
        return RedirectResponse("/admin/catalog/settings/policy-sets", status_code=303)

    # Get dunning steps
    steps = catalog_service.policy_dunning_steps.list(
        db=db, policy_set_id=policy_id, order_by="day_offset", order_dir="asc", limit=100, offset=0
    )

    policy = {
        "id": str(obj.id),
        "name": obj.name,
        "proration_policy": obj.proration_policy.value if obj.proration_policy else "immediate",
        "downgrade_policy": obj.downgrade_policy.value if obj.downgrade_policy else "next_cycle",
        "trial_days": obj.trial_days or "",
        "trial_card_required": obj.trial_card_required,
        "grace_days": obj.grace_days or "",
        "suspension_action": obj.suspension_action.value if obj.suspension_action else "suspend",
        "refund_policy": obj.refund_policy.value if obj.refund_policy else "none",
        "refund_window_days": obj.refund_window_days or "",
        "is_active": obj.is_active,
        "dunning_steps": [
            {"id": str(s.id), "day_offset": s.day_offset, "action": s.action.value, "note": s.note or ""}
            for s in steps
        ],
    }
    context = _policy_form_context(request, db, policy)
    context["action_url"] = f"/admin/catalog/settings/policy-sets/{policy_id}/edit"
    return templates.TemplateResponse("admin/catalog/settings/policy_set_form.html", context)


@router.post("/policy-sets/{policy_id}/edit", response_class=HTMLResponse)
def policy_set_update(request: Request, policy_id: str, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> Response:
    """Update policy set with dunning steps."""

    # Parse dunning steps from form
    dunning_steps: list[dict[str, str]] = []
    i = 0
    while True:
        day_offset = _form_optional_str(form, f"dunning_steps[{i}][day_offset]")
        if day_offset is None:
            break
        step_id = _form_str(form, f"dunning_steps[{i}][id]").strip()
        action = _form_str(form, f"dunning_steps[{i}][action]").strip()
        note = _form_str(form, f"dunning_steps[{i}][note]").strip()
        if day_offset.strip() and action:
            dunning_steps.append({
                "id": step_id,
                "day_offset": day_offset.strip(),
                "action": action,
                "note": note,
            })
        i += 1

    name = _form_str(form, "name").strip()
    proration_policy = _form_str(form, "proration_policy", "immediate").strip()
    downgrade_policy = _form_str(form, "downgrade_policy", "next_cycle").strip()
    trial_days_s = _form_str(form, "trial_days").strip()
    trial_card_required = _form_str(form, "trial_card_required") == "true"
    grace_days_s = _form_str(form, "grace_days").strip()
    suspension_action = _form_str(form, "suspension_action", "suspend").strip()
    refund_policy = _form_str(form, "refund_policy", "none").strip()
    refund_window_days_s = _form_str(form, "refund_window_days").strip()
    is_active = _form_str(form, "is_active") == "true"
    policy: dict[str, Any] = {
        "id": policy_id,
        "name": name,
        "proration_policy": proration_policy,
        "downgrade_policy": downgrade_policy,
        "trial_days": trial_days_s,
        "trial_card_required": trial_card_required,
        "grace_days": grace_days_s,
        "suspension_action": suspension_action,
        "refund_policy": refund_policy,
        "refund_window_days": refund_window_days_s,
        "is_active": is_active,
        "dunning_steps": dunning_steps,
    }

    try:
        payload = PolicySetUpdate(
            name=name,
            proration_policy=ProrationPolicy(proration_policy),
            downgrade_policy=ProrationPolicy(downgrade_policy),
            trial_days=int(trial_days_s) if trial_days_s else None,
            trial_card_required=trial_card_required,
            grace_days=int(grace_days_s) if grace_days_s else None,
            suspension_action=SuspensionAction(suspension_action),
            refund_policy=RefundPolicy(refund_policy),
            refund_window_days=int(refund_window_days_s) if refund_window_days_s else None,
            is_active=is_active,
        )
        catalog_service.policy_sets.update(db=db, policy_id=policy_id, payload=payload)
        settings_svc.sync_dunning_steps(db, policy_id, dunning_steps)
        return RedirectResponse("/admin/catalog/settings/policy-sets", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _policy_form_context(request, db, policy, error)
    context["action_url"] = f"/admin/catalog/settings/policy-sets/{policy_id}/edit"
    return templates.TemplateResponse("admin/catalog/settings/policy_set_form.html", context)


@router.post("/policy-sets/{policy_id}/delete", response_class=HTMLResponse)
def policy_set_delete(request: Request, policy_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    """Delete (deactivate) policy set."""
    try:
        catalog_service.policy_sets.delete(db=db, policy_id=policy_id)
    except Exception:
        pass
    return RedirectResponse("/admin/catalog/settings/policy-sets", status_code=303)


# =============================================================================
# ADD-ONS (with nested prices)
# =============================================================================


@router.get("/add-ons", response_class=HTMLResponse)
def add_ons_list(
    request: Request,
    status: str | None = None,
    addon_type: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List add-ons."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    result = settings_svc.list_add_ons_paginated(
        db, is_active=is_active, addon_type=addon_type, search=search,
        page=page, per_page=per_page,
    )

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="add-ons")
    context.update({
        "add_ons": result.items,
        "status": status,
        "addon_type": addon_type,
        "addon_types": [item.value for item in AddOnType],
        "search": search,
        "page": page,
        "per_page": per_page,
        "total": result.total,
        "total_pages": result.total_pages,
    })
    return templates.TemplateResponse("admin/catalog/settings/add_ons.html", context)


def _addon_form_context(request: Request, db: Session, addon: dict, error: str | None = None) -> dict:
    context = _base_context(request, db, active_page="catalog-settings", settings_tab="add-ons")
    context.update({
        "addon": addon,
        "addon_types": [item.value for item in AddOnType],
        "price_types": [item.value for item in PriceType],
        "billing_cycles": [BillingCycle.monthly.value, BillingCycle.annual.value],
        "price_units": [item.value for item in PriceUnit],
    })
    if error:
        context["error"] = error
    return context


@router.get("/add-ons/new", response_class=HTMLResponse)
def add_on_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Create add-on form."""
    addon = {
        "name": "",
        "addon_type": "custom",
        "description": "",
        "is_active": True,
        "prices": [],
    }
    context = _addon_form_context(request, db, addon)
    context["action_url"] = "/admin/catalog/settings/add-ons"
    return templates.TemplateResponse("admin/catalog/settings/add_on_form.html", context)


@router.post("/add-ons", response_class=HTMLResponse)
def add_on_create(request: Request, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> Response:
    """Create add-on with prices."""

    # Parse prices from form
    prices: list[dict[str, str]] = []
    i = 0
    while True:
        amount = _form_optional_str(form, f"prices[{i}][amount]")
        if amount is None:
            break
        price_type = _form_str(form, f"prices[{i}][price_type]").strip()
        currency = _form_str(form, f"prices[{i}][currency]", "NGN").strip()
        billing_cycle = _form_str(form, f"prices[{i}][billing_cycle]").strip()
        unit = _form_str(form, f"prices[{i}][unit]").strip()
        description = _form_str(form, f"prices[{i}][description]").strip()
        if amount.strip() and price_type:
            prices.append({
                "price_type": price_type,
                "amount": amount.strip(),
                "currency": currency,
                "billing_cycle": billing_cycle,
                "unit": unit,
                "description": description,
            })
        i += 1

    addon = {
        "name": _form_str(form, "name").strip(),
        "addon_type": _form_str(form, "addon_type", "custom").strip(),
        "description": _form_str(form, "description").strip(),
        "is_active": _form_str(form, "is_active") == "true",
        "prices": prices,
    }

    try:
        payload = AddOnCreate.model_validate(
            {
                "name": addon["name"],
                "addon_type": AddOnType(str(addon["addon_type"])),
                "description": addon["description"] or None,
                "is_active": addon["is_active"],
            }
        )
        created = catalog_service.add_ons.create(db=db, payload=payload)
        settings_svc.create_addon_prices(db, str(created.id), prices)
        return RedirectResponse("/admin/catalog/settings/add-ons", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _addon_form_context(request, db, addon, error)
    context["action_url"] = "/admin/catalog/settings/add-ons"
    return templates.TemplateResponse("admin/catalog/settings/add_on_form.html", context)


@router.get("/add-ons/{addon_id}/edit", response_class=HTMLResponse)
def add_on_edit(request: Request, addon_id: str, db: Session = Depends(get_db)) -> Response:
    """Edit add-on form."""
    try:
        obj = catalog_service.add_ons.get(db=db, add_on_id=addon_id)
    except Exception:
        return RedirectResponse("/admin/catalog/settings/add-ons", status_code=303)

    # Get prices
    prices_list = catalog_service.add_on_prices.list(
        db=db, add_on_id=addon_id, is_active=None, order_by="created_at", order_dir="asc", limit=100, offset=0
    )

    addon = {
        "id": str(obj.id),
        "name": obj.name,
        "addon_type": obj.addon_type.value if obj.addon_type else "custom",
        "description": obj.description or "",
        "is_active": obj.is_active,
        "prices": [
            {
                "id": str(p.id),
                "price_type": p.price_type.value,
                "amount": str(p.amount),
                "currency": p.currency,
                "billing_cycle": p.billing_cycle.value if p.billing_cycle else "",
                "unit": p.unit.value if p.unit else "",
                "description": p.description or "",
            }
            for p in prices_list
        ],
    }
    context = _addon_form_context(request, db, addon)
    context["action_url"] = f"/admin/catalog/settings/add-ons/{addon_id}/edit"
    return templates.TemplateResponse("admin/catalog/settings/add_on_form.html", context)


@router.post("/add-ons/{addon_id}/edit", response_class=HTMLResponse)
def add_on_update(request: Request, addon_id: str, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> Response:
    """Update add-on with prices."""

    # Parse prices from form
    prices: list[dict[str, str]] = []
    i = 0
    while True:
        amount = _form_optional_str(form, f"prices[{i}][amount]")
        if amount is None:
            break
        price_id = _form_str(form, f"prices[{i}][id]").strip()
        price_type = _form_str(form, f"prices[{i}][price_type]").strip()
        currency = _form_str(form, f"prices[{i}][currency]", "NGN").strip()
        billing_cycle = _form_str(form, f"prices[{i}][billing_cycle]").strip()
        unit = _form_str(form, f"prices[{i}][unit]").strip()
        description = _form_str(form, f"prices[{i}][description]").strip()
        if amount.strip() and price_type:
            prices.append({
                "id": price_id,
                "price_type": price_type,
                "amount": amount.strip(),
                "currency": currency,
                "billing_cycle": billing_cycle,
                "unit": unit,
                "description": description,
            })
        i += 1

    addon = {
        "id": addon_id,
        "name": _form_str(form, "name").strip(),
        "addon_type": _form_str(form, "addon_type", "custom").strip(),
        "description": _form_str(form, "description").strip(),
        "is_active": _form_str(form, "is_active") == "true",
        "prices": prices,
    }

    try:
        payload = AddOnUpdate.model_validate(
            {
                "name": addon["name"],
                "addon_type": AddOnType(str(addon["addon_type"])),
                "description": addon["description"] or None,
                "is_active": addon["is_active"],
            }
        )
        catalog_service.add_ons.update(db=db, add_on_id=addon_id, payload=payload)
        settings_svc.sync_addon_prices(db, addon_id, prices)
        return RedirectResponse("/admin/catalog/settings/add-ons", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _addon_form_context(request, db, addon, error)
    context["action_url"] = f"/admin/catalog/settings/add-ons/{addon_id}/edit"
    return templates.TemplateResponse("admin/catalog/settings/add_on_form.html", context)


@router.post("/add-ons/{addon_id}/delete", response_class=HTMLResponse)
def add_on_delete(request: Request, addon_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    """Delete (deactivate) add-on."""
    try:
        catalog_service.add_ons.delete(db=db, add_on_id=addon_id)
    except Exception:
        pass
    return RedirectResponse("/admin/catalog/settings/add-ons", status_code=303)


# =============================================================================
# BULK DELETE OPERATIONS
# =============================================================================


@router.post("/region-zones/bulk-delete", response_class=HTMLResponse)
def bulk_delete_region_zones(request: Request, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> RedirectResponse:
    """Bulk delete (deactivate) region zones."""
    ids = _form_getlist_str(form, "ids")
    settings_svc.bulk_delete_region_zones(db, ids)
    return RedirectResponse("/admin/catalog/settings/region-zones", status_code=303)


@router.post("/usage-allowances/bulk-delete", response_class=HTMLResponse)
def bulk_delete_usage_allowances(request: Request, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> RedirectResponse:
    """Bulk delete (deactivate) usage allowances."""
    ids = _form_getlist_str(form, "ids")
    settings_svc.bulk_delete_usage_allowances(db, ids)
    return RedirectResponse("/admin/catalog/settings/usage-allowances", status_code=303)


@router.post("/sla-profiles/bulk-delete", response_class=HTMLResponse)
def bulk_delete_sla_profiles(request: Request, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> RedirectResponse:
    """Bulk delete (deactivate) SLA profiles."""
    ids = _form_getlist_str(form, "ids")
    settings_svc.bulk_delete_sla_profiles(db, ids)
    return RedirectResponse("/admin/catalog/settings/sla-profiles", status_code=303)


@router.post("/policy-sets/bulk-delete", response_class=HTMLResponse)
def bulk_delete_policy_sets(request: Request, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> RedirectResponse:
    """Bulk delete (deactivate) policy sets."""
    ids = _form_getlist_str(form, "ids")
    settings_svc.bulk_delete_policy_sets(db, ids)
    return RedirectResponse("/admin/catalog/settings/policy-sets", status_code=303)


@router.post("/add-ons/bulk-delete", response_class=HTMLResponse)
def bulk_delete_add_ons(request: Request, form: FormData = Depends(parse_form_data), db: Session = Depends(get_db)) -> RedirectResponse:
    """Bulk delete (deactivate) add-ons."""
    ids = _form_getlist_str(form, "ids")
    settings_svc.bulk_delete_add_ons(db, ids)
    return RedirectResponse("/admin/catalog/settings/add-ons", status_code=303)


# =============================================================================
# CSV EXPORT OPERATIONS
# =============================================================================


@router.get("/region-zones/export")
def export_region_zones(db: Session = Depends(get_db)) -> StreamingResponse:
    """Export region zones to CSV."""
    csv_content = settings_svc.export_region_zones_csv(db)
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=region_zones.csv"},
    )


@router.get("/usage-allowances/export")
def export_usage_allowances(db: Session = Depends(get_db)) -> StreamingResponse:
    """Export usage allowances to CSV."""
    csv_content = settings_svc.export_usage_allowances_csv(db)
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=usage_allowances.csv"},
    )


@router.get("/sla-profiles/export")
def export_sla_profiles(db: Session = Depends(get_db)) -> StreamingResponse:
    """Export SLA profiles to CSV."""
    csv_content = settings_svc.export_sla_profiles_csv(db)
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sla_profiles.csv"},
    )


@router.get("/policy-sets/export")
def export_policy_sets(db: Session = Depends(get_db)) -> StreamingResponse:
    """Export policy sets to CSV."""
    csv_content = settings_svc.export_policy_sets_csv(db)
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=policy_sets.csv"},
    )


@router.get("/add-ons/export")
def export_add_ons(db: Session = Depends(get_db)) -> StreamingResponse:
    """Export add-ons to CSV."""
    csv_content = settings_svc.export_add_ons_csv(db)
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=add_ons.csv"},
    )
