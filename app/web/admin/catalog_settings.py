"""Admin catalog settings management web routes."""

import csv
from io import StringIO

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session
from typing import Optional

from app.csrf import get_csrf_token
from app.db import SessionLocal
from app.schemas.catalog import (
    AddOnCreate,
    AddOnPriceCreate,
    AddOnUpdate,
    PolicyDunningStepCreate,
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

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/catalog/settings", tags=["web-admin-catalog-settings"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _base_context(request: Request, db: Session, active_page: str, settings_tab: str = ""):
    from app.web.admin import get_sidebar_stats, get_current_user
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
def catalog_settings_index(request: Request, db: Session = Depends(get_db)):
    """Settings overview with cards linking to each section."""
    # Get counts for each entity type
    region_zones = catalog_service.region_zones.list(
        db=db, is_active=None, order_by="name", order_dir="asc", limit=1000, offset=0
    )
    usage_allowances = catalog_service.usage_allowances.list(
        db=db, is_active=None, order_by="name", order_dir="asc", limit=1000, offset=0
    )
    sla_profiles = catalog_service.sla_profiles.list(
        db=db, is_active=None, order_by="name", order_dir="asc", limit=1000, offset=0
    )
    policy_sets = catalog_service.policy_sets.list(
        db=db, is_active=None, order_by="name", order_dir="asc", limit=1000, offset=0
    )
    add_ons = catalog_service.add_ons.list(
        db=db, is_active=None, addon_type=None, order_by="name", order_dir="asc", limit=1000, offset=0
    )

    context = _base_context(request, db, active_page="catalog-settings")
    context.update({
        "region_zones_count": len(region_zones),
        "region_zones_active": len([z for z in region_zones if z.is_active]),
        "usage_allowances_count": len(usage_allowances),
        "usage_allowances_active": len([a for a in usage_allowances if a.is_active]),
        "sla_profiles_count": len(sla_profiles),
        "sla_profiles_active": len([p for p in sla_profiles if p.is_active]),
        "policy_sets_count": len(policy_sets),
        "policy_sets_active": len([p for p in policy_sets if p.is_active]),
        "add_ons_count": len(add_ons),
        "add_ons_active": len([a for a in add_ons if a.is_active]),
    })
    return templates.TemplateResponse("admin/catalog/settings/index.html", context)


# =============================================================================
# REGION ZONES
# =============================================================================


@router.get("/region-zones", response_class=HTMLResponse)
def region_zones_list(
    request: Request,
    status: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List region zones."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    zones = catalog_service.region_zones.list(
        db=db, is_active=is_active, order_by="name", order_dir="asc", limit=1000, offset=0
    )

    # Filter by search
    if search:
        search_lower = search.lower()
        zones = [z for z in zones if search_lower in z.name.lower() or (z.code and search_lower in z.code.lower())]

    # Pagination
    total = len(zones)
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    zones = zones[offset:offset + per_page]

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="region-zones")
    context.update({
        "zones": zones,
        "status": status,
        "search": search,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    })
    return templates.TemplateResponse("admin/catalog/settings/region_zones.html", context)


@router.get("/region-zones/new", response_class=HTMLResponse)
def region_zone_new(request: Request, db: Session = Depends(get_db)):
    """Create region zone form."""
    zone = {"name": "", "code": "", "description": "", "is_active": True}
    context = _base_context(request, db, active_page="catalog-settings", settings_tab="region-zones")
    context.update({"zone": zone, "action_url": "/admin/catalog/settings/region-zones"})
    return templates.TemplateResponse("admin/catalog/settings/region_zone_form.html", context)


@router.post("/region-zones", response_class=HTMLResponse)
async def region_zone_create(request: Request, db: Session = Depends(get_db)):
    """Create region zone."""
    form = await request.form()
    zone = {
        "name": (form.get("name") or "").strip(),
        "code": (form.get("code") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "is_active": form.get("is_active") == "true",
    }

    try:
        payload = RegionZoneCreate(
            name=zone["name"],
            code=zone["code"] or None,
            description=zone["description"] or None,
            is_active=zone["is_active"],
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
def region_zone_edit(request: Request, zone_id: str, db: Session = Depends(get_db)):
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
async def region_zone_update(request: Request, zone_id: str, db: Session = Depends(get_db)):
    """Update region zone."""
    form = await request.form()
    zone = {
        "id": zone_id,
        "name": (form.get("name") or "").strip(),
        "code": (form.get("code") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "is_active": form.get("is_active") == "true",
    }

    try:
        payload = RegionZoneUpdate(
            name=zone["name"],
            code=zone["code"] or None,
            description=zone["description"] or None,
            is_active=zone["is_active"],
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
async def region_zone_delete(request: Request, zone_id: str, db: Session = Depends(get_db)):
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
    status: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List usage allowances."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    allowances = catalog_service.usage_allowances.list(
        db=db, is_active=is_active, order_by="name", order_dir="asc", limit=1000, offset=0
    )

    if search:
        search_lower = search.lower()
        allowances = [a for a in allowances if search_lower in a.name.lower()]

    total = len(allowances)
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    allowances = allowances[offset:offset + per_page]

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="usage-allowances")
    context.update({
        "allowances": allowances,
        "status": status,
        "search": search,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    })
    return templates.TemplateResponse("admin/catalog/settings/usage_allowances.html", context)


@router.get("/usage-allowances/new", response_class=HTMLResponse)
def usage_allowance_new(request: Request, db: Session = Depends(get_db)):
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
async def usage_allowance_create(request: Request, db: Session = Depends(get_db)):
    """Create usage allowance."""
    form = await request.form()
    allowance = {
        "name": (form.get("name") or "").strip(),
        "included_gb": (form.get("included_gb") or "").strip(),
        "overage_rate": (form.get("overage_rate") or "").strip(),
        "overage_cap_gb": (form.get("overage_cap_gb") or "").strip(),
        "throttle_rate_mbps": (form.get("throttle_rate_mbps") or "").strip(),
        "is_active": form.get("is_active") == "true",
    }

    try:
        payload = UsageAllowanceCreate(
            name=allowance["name"],
            included_gb=int(allowance["included_gb"]) if allowance["included_gb"] else None,
            overage_rate=allowance["overage_rate"] if allowance["overage_rate"] else None,
            overage_cap_gb=int(allowance["overage_cap_gb"]) if allowance["overage_cap_gb"] else None,
            throttle_rate_mbps=int(allowance["throttle_rate_mbps"]) if allowance["throttle_rate_mbps"] else None,
            is_active=allowance["is_active"],
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
def usage_allowance_edit(request: Request, allowance_id: str, db: Session = Depends(get_db)):
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
async def usage_allowance_update(request: Request, allowance_id: str, db: Session = Depends(get_db)):
    """Update usage allowance."""
    form = await request.form()
    allowance = {
        "id": allowance_id,
        "name": (form.get("name") or "").strip(),
        "included_gb": (form.get("included_gb") or "").strip(),
        "overage_rate": (form.get("overage_rate") or "").strip(),
        "overage_cap_gb": (form.get("overage_cap_gb") or "").strip(),
        "throttle_rate_mbps": (form.get("throttle_rate_mbps") or "").strip(),
        "is_active": form.get("is_active") == "true",
    }

    try:
        payload = UsageAllowanceUpdate(
            name=allowance["name"],
            included_gb=int(allowance["included_gb"]) if allowance["included_gb"] else None,
            overage_rate=allowance["overage_rate"] if allowance["overage_rate"] else None,
            overage_cap_gb=int(allowance["overage_cap_gb"]) if allowance["overage_cap_gb"] else None,
            throttle_rate_mbps=int(allowance["throttle_rate_mbps"]) if allowance["throttle_rate_mbps"] else None,
            is_active=allowance["is_active"],
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
async def usage_allowance_delete(request: Request, allowance_id: str, db: Session = Depends(get_db)):
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
    status: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List SLA profiles."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    profiles = catalog_service.sla_profiles.list(
        db=db, is_active=is_active, order_by="name", order_dir="asc", limit=1000, offset=0
    )

    if search:
        search_lower = search.lower()
        profiles = [p for p in profiles if search_lower in p.name.lower()]

    total = len(profiles)
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    profiles = profiles[offset:offset + per_page]

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="sla-profiles")
    context.update({
        "profiles": profiles,
        "status": status,
        "search": search,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    })
    return templates.TemplateResponse("admin/catalog/settings/sla_profiles.html", context)


@router.get("/sla-profiles/new", response_class=HTMLResponse)
def sla_profile_new(request: Request, db: Session = Depends(get_db)):
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
async def sla_profile_create(request: Request, db: Session = Depends(get_db)):
    """Create SLA profile."""
    form = await request.form()
    profile = {
        "name": (form.get("name") or "").strip(),
        "uptime_percent": (form.get("uptime_percent") or "").strip(),
        "response_time_hours": (form.get("response_time_hours") or "").strip(),
        "resolution_time_hours": (form.get("resolution_time_hours") or "").strip(),
        "credit_percent": (form.get("credit_percent") or "").strip(),
        "notes": (form.get("notes") or "").strip(),
        "is_active": form.get("is_active") == "true",
    }

    try:
        payload = SlaProfileCreate(
            name=profile["name"],
            uptime_percent=profile["uptime_percent"] if profile["uptime_percent"] else None,
            response_time_hours=int(profile["response_time_hours"]) if profile["response_time_hours"] else None,
            resolution_time_hours=int(profile["resolution_time_hours"]) if profile["resolution_time_hours"] else None,
            credit_percent=profile["credit_percent"] if profile["credit_percent"] else None,
            notes=profile["notes"] or None,
            is_active=profile["is_active"],
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
def sla_profile_edit(request: Request, profile_id: str, db: Session = Depends(get_db)):
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
async def sla_profile_update(request: Request, profile_id: str, db: Session = Depends(get_db)):
    """Update SLA profile."""
    form = await request.form()
    profile = {
        "id": profile_id,
        "name": (form.get("name") or "").strip(),
        "uptime_percent": (form.get("uptime_percent") or "").strip(),
        "response_time_hours": (form.get("response_time_hours") or "").strip(),
        "resolution_time_hours": (form.get("resolution_time_hours") or "").strip(),
        "credit_percent": (form.get("credit_percent") or "").strip(),
        "notes": (form.get("notes") or "").strip(),
        "is_active": form.get("is_active") == "true",
    }

    try:
        payload = SlaProfileUpdate(
            name=profile["name"],
            uptime_percent=profile["uptime_percent"] if profile["uptime_percent"] else None,
            response_time_hours=int(profile["response_time_hours"]) if profile["response_time_hours"] else None,
            resolution_time_hours=int(profile["resolution_time_hours"]) if profile["resolution_time_hours"] else None,
            credit_percent=profile["credit_percent"] if profile["credit_percent"] else None,
            notes=profile["notes"] or None,
            is_active=profile["is_active"],
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
async def sla_profile_delete(request: Request, profile_id: str, db: Session = Depends(get_db)):
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
    status: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List policy sets."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    policies = catalog_service.policy_sets.list(
        db=db, is_active=is_active, order_by="name", order_dir="asc", limit=1000, offset=0
    )

    if search:
        search_lower = search.lower()
        policies = [p for p in policies if search_lower in p.name.lower()]

    total = len(policies)
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    policies = policies[offset:offset + per_page]

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="policy-sets")
    context.update({
        "policies": policies,
        "status": status,
        "search": search,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    })
    return templates.TemplateResponse("admin/catalog/settings/policy_sets.html", context)


def _policy_form_context(request: Request, db: Session, policy: dict, error: str | None = None):
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
def policy_set_new(request: Request, db: Session = Depends(get_db)):
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
async def policy_set_create(request: Request, db: Session = Depends(get_db)):
    """Create policy set with dunning steps."""
    form = await request.form()

    # Parse dunning steps from form
    dunning_steps = []
    i = 0
    while True:
        day_offset = form.get(f"dunning_steps[{i}][day_offset]")
        if day_offset is None:
            break
        action = form.get(f"dunning_steps[{i}][action]", "").strip()
        note = form.get(f"dunning_steps[{i}][note]", "").strip()
        if day_offset.strip() and action:
            dunning_steps.append({
                "day_offset": day_offset.strip(),
                "action": action,
                "note": note,
            })
        i += 1

    policy = {
        "name": (form.get("name") or "").strip(),
        "proration_policy": (form.get("proration_policy") or "immediate").strip(),
        "downgrade_policy": (form.get("downgrade_policy") or "next_cycle").strip(),
        "trial_days": (form.get("trial_days") or "").strip(),
        "trial_card_required": form.get("trial_card_required") == "true",
        "grace_days": (form.get("grace_days") or "").strip(),
        "suspension_action": (form.get("suspension_action") or "suspend").strip(),
        "refund_policy": (form.get("refund_policy") or "none").strip(),
        "refund_window_days": (form.get("refund_window_days") or "").strip(),
        "is_active": form.get("is_active") == "true",
        "dunning_steps": dunning_steps,
    }

    try:
        payload = PolicySetCreate(
            name=policy["name"],
            proration_policy=ProrationPolicy(policy["proration_policy"]),
            downgrade_policy=ProrationPolicy(policy["downgrade_policy"]),
            trial_days=int(policy["trial_days"]) if policy["trial_days"] else None,
            trial_card_required=policy["trial_card_required"],
            grace_days=int(policy["grace_days"]) if policy["grace_days"] else None,
            suspension_action=SuspensionAction(policy["suspension_action"]),
            refund_policy=RefundPolicy(policy["refund_policy"]),
            refund_window_days=int(policy["refund_window_days"]) if policy["refund_window_days"] else None,
            is_active=policy["is_active"],
        )
        created = catalog_service.policy_sets.create(db=db, payload=payload)

        # Create dunning steps
        for step in dunning_steps:
            step_payload = PolicyDunningStepCreate(
                policy_set_id=created.id,
                day_offset=int(step["day_offset"]),
                action=DunningAction(step["action"]),
                note=step["note"] or None,
            )
            catalog_service.policy_dunning_steps.create(db=db, payload=step_payload)

        return RedirectResponse("/admin/catalog/settings/policy-sets", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _policy_form_context(request, db, policy, error)
    context["action_url"] = "/admin/catalog/settings/policy-sets"
    return templates.TemplateResponse("admin/catalog/settings/policy_set_form.html", context)


@router.get("/policy-sets/{policy_id}/edit", response_class=HTMLResponse)
def policy_set_edit(request: Request, policy_id: str, db: Session = Depends(get_db)):
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
async def policy_set_update(request: Request, policy_id: str, db: Session = Depends(get_db)):
    """Update policy set with dunning steps."""
    form = await request.form()

    # Parse dunning steps from form
    dunning_steps = []
    i = 0
    while True:
        day_offset = form.get(f"dunning_steps[{i}][day_offset]")
        if day_offset is None:
            break
        step_id = form.get(f"dunning_steps[{i}][id]", "").strip()
        action = form.get(f"dunning_steps[{i}][action]", "").strip()
        note = form.get(f"dunning_steps[{i}][note]", "").strip()
        if day_offset.strip() and action:
            dunning_steps.append({
                "id": step_id,
                "day_offset": day_offset.strip(),
                "action": action,
                "note": note,
            })
        i += 1

    policy = {
        "id": policy_id,
        "name": (form.get("name") or "").strip(),
        "proration_policy": (form.get("proration_policy") or "immediate").strip(),
        "downgrade_policy": (form.get("downgrade_policy") or "next_cycle").strip(),
        "trial_days": (form.get("trial_days") or "").strip(),
        "trial_card_required": form.get("trial_card_required") == "true",
        "grace_days": (form.get("grace_days") or "").strip(),
        "suspension_action": (form.get("suspension_action") or "suspend").strip(),
        "refund_policy": (form.get("refund_policy") or "none").strip(),
        "refund_window_days": (form.get("refund_window_days") or "").strip(),
        "is_active": form.get("is_active") == "true",
        "dunning_steps": dunning_steps,
    }

    try:
        payload = PolicySetUpdate(
            name=policy["name"],
            proration_policy=ProrationPolicy(policy["proration_policy"]),
            downgrade_policy=ProrationPolicy(policy["downgrade_policy"]),
            trial_days=int(policy["trial_days"]) if policy["trial_days"] else None,
            trial_card_required=policy["trial_card_required"],
            grace_days=int(policy["grace_days"]) if policy["grace_days"] else None,
            suspension_action=SuspensionAction(policy["suspension_action"]),
            refund_policy=RefundPolicy(policy["refund_policy"]),
            refund_window_days=int(policy["refund_window_days"]) if policy["refund_window_days"] else None,
            is_active=policy["is_active"],
        )
        catalog_service.policy_sets.update(db=db, policy_id=policy_id, payload=payload)

        # Get existing steps
        existing_steps = catalog_service.policy_dunning_steps.list(
            db=db, policy_set_id=policy_id, order_by="day_offset", order_dir="asc", limit=100, offset=0
        )
        existing_ids = {str(s.id) for s in existing_steps}
        submitted_ids = {s["id"] for s in dunning_steps if s["id"]}

        # Delete removed steps
        for step in existing_steps:
            if str(step.id) not in submitted_ids:
                catalog_service.policy_dunning_steps.delete(db=db, step_id=str(step.id))

        # Update or create steps
        for step in dunning_steps:
            from app.schemas.catalog import PolicyDunningStepUpdate
            if step["id"] and step["id"] in existing_ids:
                catalog_service.policy_dunning_steps.update(
                    db=db,
                    step_id=step["id"],
                    payload=PolicyDunningStepUpdate(
                        day_offset=int(step["day_offset"]),
                        action=DunningAction(step["action"]),
                        note=step["note"] or None,
                    ),
                )
            else:
                catalog_service.policy_dunning_steps.create(
                    db=db,
                    payload=PolicyDunningStepCreate(
                        policy_set_id=policy_id,
                        day_offset=int(step["day_offset"]),
                        action=DunningAction(step["action"]),
                        note=step["note"] or None,
                    ),
                )

        return RedirectResponse("/admin/catalog/settings/policy-sets", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _policy_form_context(request, db, policy, error)
    context["action_url"] = f"/admin/catalog/settings/policy-sets/{policy_id}/edit"
    return templates.TemplateResponse("admin/catalog/settings/policy_set_form.html", context)


@router.post("/policy-sets/{policy_id}/delete", response_class=HTMLResponse)
async def policy_set_delete(request: Request, policy_id: str, db: Session = Depends(get_db)):
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
    status: Optional[str] = None,
    addon_type: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List add-ons."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    add_ons = catalog_service.add_ons.list(
        db=db,
        is_active=is_active,
        addon_type=addon_type,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )

    if search:
        search_lower = search.lower()
        add_ons = [a for a in add_ons if search_lower in a.name.lower()]

    total = len(add_ons)
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    add_ons = add_ons[offset:offset + per_page]

    context = _base_context(request, db, active_page="catalog-settings", settings_tab="add-ons")
    context.update({
        "add_ons": add_ons,
        "status": status,
        "addon_type": addon_type,
        "addon_types": [item.value for item in AddOnType],
        "search": search,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    })
    return templates.TemplateResponse("admin/catalog/settings/add_ons.html", context)


def _addon_form_context(request: Request, db: Session, addon: dict, error: str | None = None):
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
def add_on_new(request: Request, db: Session = Depends(get_db)):
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
async def add_on_create(request: Request, db: Session = Depends(get_db)):
    """Create add-on with prices."""
    form = await request.form()

    # Parse prices from form
    prices = []
    i = 0
    while True:
        amount = form.get(f"prices[{i}][amount]")
        if amount is None:
            break
        price_type = form.get(f"prices[{i}][price_type]", "").strip()
        currency = form.get(f"prices[{i}][currency]", "NGN").strip()
        billing_cycle = form.get(f"prices[{i}][billing_cycle]", "").strip()
        unit = form.get(f"prices[{i}][unit]", "").strip()
        description = form.get(f"prices[{i}][description]", "").strip()
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
        "name": (form.get("name") or "").strip(),
        "addon_type": (form.get("addon_type") or "custom").strip(),
        "description": (form.get("description") or "").strip(),
        "is_active": form.get("is_active") == "true",
        "prices": prices,
    }

    try:
        payload = AddOnCreate(
            name=addon["name"],
            addon_type=AddOnType(addon["addon_type"]),
            description=addon["description"] or None,
            is_active=addon["is_active"],
        )
        created = catalog_service.add_ons.create(db=db, payload=payload)

        # Create prices
        for price in prices:
            price_payload = AddOnPriceCreate(
                add_on_id=created.id,
                price_type=PriceType(price["price_type"]),
                amount=price["amount"],
                currency=price["currency"] or "NGN",
                billing_cycle=BillingCycle(price["billing_cycle"]) if price["billing_cycle"] else None,
                unit=PriceUnit(price["unit"]) if price["unit"] else None,
                description=price["description"] or None,
            )
            catalog_service.add_on_prices.create(db=db, payload=price_payload)

        return RedirectResponse("/admin/catalog/settings/add-ons", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _addon_form_context(request, db, addon, error)
    context["action_url"] = "/admin/catalog/settings/add-ons"
    return templates.TemplateResponse("admin/catalog/settings/add_on_form.html", context)


@router.get("/add-ons/{addon_id}/edit", response_class=HTMLResponse)
def add_on_edit(request: Request, addon_id: str, db: Session = Depends(get_db)):
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
async def add_on_update(request: Request, addon_id: str, db: Session = Depends(get_db)):
    """Update add-on with prices."""
    form = await request.form()

    # Parse prices from form
    prices = []
    i = 0
    while True:
        amount = form.get(f"prices[{i}][amount]")
        if amount is None:
            break
        price_id = form.get(f"prices[{i}][id]", "").strip()
        price_type = form.get(f"prices[{i}][price_type]", "").strip()
        currency = form.get(f"prices[{i}][currency]", "NGN").strip()
        billing_cycle = form.get(f"prices[{i}][billing_cycle]", "").strip()
        unit = form.get(f"prices[{i}][unit]", "").strip()
        description = form.get(f"prices[{i}][description]", "").strip()
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
        "name": (form.get("name") or "").strip(),
        "addon_type": (form.get("addon_type") or "custom").strip(),
        "description": (form.get("description") or "").strip(),
        "is_active": form.get("is_active") == "true",
        "prices": prices,
    }

    try:
        payload = AddOnUpdate(
            name=addon["name"],
            addon_type=AddOnType(addon["addon_type"]),
            description=addon["description"] or None,
            is_active=addon["is_active"],
        )
        catalog_service.add_ons.update(db=db, add_on_id=addon_id, payload=payload)

        # Get existing prices
        existing_prices = catalog_service.add_on_prices.list(
            db=db, add_on_id=addon_id, is_active=None, order_by="created_at", order_dir="asc", limit=100, offset=0
        )
        existing_ids = {str(p.id) for p in existing_prices}
        submitted_ids = {p["id"] for p in prices if p["id"]}

        # Delete removed prices
        for price in existing_prices:
            if str(price.id) not in submitted_ids:
                catalog_service.add_on_prices.delete(db=db, price_id=str(price.id))

        # Update or create prices
        from app.schemas.catalog import AddOnPriceUpdate
        for price in prices:
            if price["id"] and price["id"] in existing_ids:
                catalog_service.add_on_prices.update(
                    db=db,
                    price_id=price["id"],
                    payload=AddOnPriceUpdate(
                        price_type=PriceType(price["price_type"]),
                        amount=price["amount"],
                        currency=price["currency"] or "NGN",
                        billing_cycle=BillingCycle(price["billing_cycle"]) if price["billing_cycle"] else None,
                        unit=PriceUnit(price["unit"]) if price["unit"] else None,
                        description=price["description"] or None,
                    ),
                )
            else:
                catalog_service.add_on_prices.create(
                    db=db,
                    payload=AddOnPriceCreate(
                        add_on_id=addon_id,
                        price_type=PriceType(price["price_type"]),
                        amount=price["amount"],
                        currency=price["currency"] or "NGN",
                        billing_cycle=BillingCycle(price["billing_cycle"]) if price["billing_cycle"] else None,
                        unit=PriceUnit(price["unit"]) if price["unit"] else None,
                        description=price["description"] or None,
                    ),
                )

        return RedirectResponse("/admin/catalog/settings/add-ons", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _addon_form_context(request, db, addon, error)
    context["action_url"] = f"/admin/catalog/settings/add-ons/{addon_id}/edit"
    return templates.TemplateResponse("admin/catalog/settings/add_on_form.html", context)


@router.post("/add-ons/{addon_id}/delete", response_class=HTMLResponse)
async def add_on_delete(request: Request, addon_id: str, db: Session = Depends(get_db)):
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
async def bulk_delete_region_zones(request: Request, db: Session = Depends(get_db)):
    """Bulk delete (deactivate) region zones."""
    form = await request.form()
    ids = form.getlist("ids")
    for zone_id in ids:
        try:
            catalog_service.region_zones.delete(db=db, zone_id=zone_id)
        except Exception:
            pass
    return RedirectResponse("/admin/catalog/settings/region-zones", status_code=303)


@router.post("/usage-allowances/bulk-delete", response_class=HTMLResponse)
async def bulk_delete_usage_allowances(request: Request, db: Session = Depends(get_db)):
    """Bulk delete (deactivate) usage allowances."""
    form = await request.form()
    ids = form.getlist("ids")
    for allowance_id in ids:
        try:
            catalog_service.usage_allowances.delete(db=db, allowance_id=allowance_id)
        except Exception:
            pass
    return RedirectResponse("/admin/catalog/settings/usage-allowances", status_code=303)


@router.post("/sla-profiles/bulk-delete", response_class=HTMLResponse)
async def bulk_delete_sla_profiles(request: Request, db: Session = Depends(get_db)):
    """Bulk delete (deactivate) SLA profiles."""
    form = await request.form()
    ids = form.getlist("ids")
    for profile_id in ids:
        try:
            catalog_service.sla_profiles.delete(db=db, profile_id=profile_id)
        except Exception:
            pass
    return RedirectResponse("/admin/catalog/settings/sla-profiles", status_code=303)


@router.post("/policy-sets/bulk-delete", response_class=HTMLResponse)
async def bulk_delete_policy_sets(request: Request, db: Session = Depends(get_db)):
    """Bulk delete (deactivate) policy sets."""
    form = await request.form()
    ids = form.getlist("ids")
    for policy_id in ids:
        try:
            catalog_service.policy_sets.delete(db=db, policy_set_id=policy_id)
        except Exception:
            pass
    return RedirectResponse("/admin/catalog/settings/policy-sets", status_code=303)


@router.post("/add-ons/bulk-delete", response_class=HTMLResponse)
async def bulk_delete_add_ons(request: Request, db: Session = Depends(get_db)):
    """Bulk delete (deactivate) add-ons."""
    form = await request.form()
    ids = form.getlist("ids")
    for addon_id in ids:
        try:
            catalog_service.add_ons.delete(db=db, add_on_id=addon_id)
        except Exception:
            pass
    return RedirectResponse("/admin/catalog/settings/add-ons", status_code=303)


# =============================================================================
# CSV EXPORT OPERATIONS
# =============================================================================


@router.get("/region-zones/export")
def export_region_zones(db: Session = Depends(get_db)):
    """Export region zones to CSV."""
    zones = catalog_service.region_zones.list(
        db=db, is_active=None, order_by="name", order_dir="asc", limit=10000, offset=0
    )
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Code", "Description", "Active"])
    for z in zones:
        writer.writerow([str(z.id), z.name, z.code or "", z.description or "", "Yes" if z.is_active else "No"])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=region_zones.csv"}
    )


@router.get("/usage-allowances/export")
def export_usage_allowances(db: Session = Depends(get_db)):
    """Export usage allowances to CSV."""
    allowances = catalog_service.usage_allowances.list(
        db=db, is_active=None, order_by="name", order_dir="asc", limit=10000, offset=0
    )
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Included GB", "Overage Rate", "Overage Cap GB", "Throttle Rate Mbps", "Active"])
    for a in allowances:
        writer.writerow([
            str(a.id), a.name, a.included_gb or "", a.overage_rate or "",
            a.overage_cap_gb or "", a.throttle_rate_mbps or "", "Yes" if a.is_active else "No"
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=usage_allowances.csv"}
    )


@router.get("/sla-profiles/export")
def export_sla_profiles(db: Session = Depends(get_db)):
    """Export SLA profiles to CSV."""
    profiles = catalog_service.sla_profiles.list(
        db=db, is_active=None, order_by="name", order_dir="asc", limit=10000, offset=0
    )
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Uptime %", "Response Hours", "Resolution Hours", "Credit %", "Notes", "Active"])
    for p in profiles:
        writer.writerow([
            str(p.id), p.name, p.uptime_percent or "", p.response_time_hours or "",
            p.resolution_time_hours or "", p.credit_percent or "", p.notes or "", "Yes" if p.is_active else "No"
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sla_profiles.csv"}
    )


@router.get("/policy-sets/export")
def export_policy_sets(db: Session = Depends(get_db)):
    """Export policy sets to CSV."""
    policies = catalog_service.policy_sets.list(
        db=db, is_active=None, order_by="name", order_dir="asc", limit=10000, offset=0
    )
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Name", "Proration Policy", "Downgrade Policy", "Trial Days",
        "Trial Card Required", "Grace Days", "Suspension Action", "Refund Policy", "Refund Window Days", "Active"
    ])
    for p in policies:
        writer.writerow([
            str(p.id), p.name,
            p.proration_policy.value if p.proration_policy else "",
            p.downgrade_policy.value if p.downgrade_policy else "",
            p.trial_days or "",
            "Yes" if p.trial_card_required else "No",
            p.grace_days or "",
            p.suspension_action.value if p.suspension_action else "",
            p.refund_policy.value if p.refund_policy else "",
            p.refund_window_days or "",
            "Yes" if p.is_active else "No"
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=policy_sets.csv"}
    )


@router.get("/add-ons/export")
def export_add_ons(db: Session = Depends(get_db)):
    """Export add-ons to CSV."""
    add_ons = catalog_service.add_ons.list(
        db=db, is_active=None, addon_type=None, order_by="name", order_dir="asc", limit=10000, offset=0
    )
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Type", "Description", "Active"])
    for a in add_ons:
        writer.writerow([
            str(a.id), a.name, a.addon_type.value if a.addon_type else "", a.description or "", "Yes" if a.is_active else "No"
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=add_ons.csv"}
    )
