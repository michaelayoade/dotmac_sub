"""Admin system management web routes."""

import json
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import Optional
from uuid import UUID

from app.db import SessionLocal
from app.models.auth import ApiKey, MFAMethod, UserCredential, Session as AuthSession
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber
from app.models.rbac import (
    Permission,
    SubscriberPermission as PersonPermission,
    SubscriberRole as PersonRole,
    Role,
    RolePermission,
)
from app.models.subscriber import Subscriber
from app.models.webhook import WebhookDelivery, WebhookDeliveryStatus, WebhookEndpoint, WebhookSubscription
from app.schemas.auth import UserCredentialCreate
from app.schemas.settings import DomainSettingUpdate
from app.schemas.billing import BankAccountCreate, BankAccountUpdate
from app.schemas.rbac import (
    PermissionCreate,
    PermissionUpdate,
    PersonRoleCreate,
    RoleCreate,
    RolePermissionCreate,
    RoleUpdate,
)
from app.services import (
    audit as audit_service,
    auth as auth_service,
    auth_flow as auth_flow_service,
    email as email_service,
    rbac as rbac_service,
    settings_api as settings_service,
    scheduler as scheduler_service,
    billing as billing_service,
)
from app.services import settings_spec
from app.services.auth_flow import hash_password
from app.services.auth_dependencies import require_permission
from app.services.common import coerce_uuid

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/system", tags=["web-admin-system"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _is_admin_request(request: Request) -> bool:
    auth = getattr(request.state, "auth", {}) or {}
    roles = auth.get("roles") or []
    return any(str(role).lower() == "admin" for role in roles)


def _placeholder_context(request: Request, db: Session, title: str, active_page: str):
    from app.web.admin import get_sidebar_stats, get_current_user
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
    from app.models.domain_settings import SettingDomain
    from app.services import system_health as system_health_service, settings_spec
    from app.web.admin import get_sidebar_stats, get_current_user

    health = system_health_service.get_system_health()
    thresholds = {
        "disk_warn_pct": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_disk_warn_pct"
        ),
        "disk_crit_pct": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_disk_crit_pct"
        ),
        "mem_warn_pct": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_mem_warn_pct"
        ),
        "mem_crit_pct": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_mem_crit_pct"
        ),
        "load_warn": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_load_warn"
        ),
        "load_crit": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_load_crit"
        ),
    }
    for key, value in thresholds.items():
        try:
            thresholds[key] = float(value) if value is not None else None
        except (TypeError, ValueError):
            thresholds[key] = None
    health_status = system_health_service.evaluate_health(health, thresholds)
    context = {
        "request": request,
        "active_page": "system-health",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "health": health,
        "health_status": health_status,
    }
    return templates.TemplateResponse("admin/system/health.html", context)


def _form_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _linked_user_labels(db: Session, subscriber_id) -> list[str]:
    """Check for linked data that would prevent user deletion."""
    # Since Person and Subscriber are now unified, users ARE subscribers
    # No additional linked data checks needed
    return []


def _blocked_delete_response(request: Request, linked: list[str], detail: str | None = None):
    if detail is None:
        if linked:
            detail = f"Cannot delete user. Linked to: {', '.join(linked)}."
        else:
            detail = "Cannot delete user. Linked records exist."
    if request.headers.get("HX-Request"):
        trigger = {
            "showToast": {
                "type": "error",
                "title": "Delete blocked",
                "message": detail,
            }
        }
        return Response(status_code=409, headers={"HX-Trigger": json.dumps(trigger)})
    raise HTTPException(status_code=409, detail=detail)


def _humanize_integrity_error(exc: IntegrityError) -> str:
    raw = str(getattr(exc, "orig", exc) or "").lower()
    if "user_credentials" in raw and "username" in raw and "already exists" in raw:
        return "Username already exists. Choose a different username or email."
    if "people" in raw and "email" in raw and "already exists" in raw:
        return "Email already exists. Use a different email address."
    if "unique" in raw and "username" in raw:
        return "Username already exists. Choose a different username or email."
    if "unique" in raw and "email" in raw:
        return "Email already exists. Use a different email address."
    return "Could not save this user because the record already exists."


def _error_banner(message: str, status_code: int = 409) -> HTMLResponse:
    return HTMLResponse(
        '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">'
        f"{message}"
        "</div>",
        status_code=status_code,
    )


ENFORCEMENT_DOMAIN = "enforcement"


def _settings_domains() -> list[dict]:
    domains = sorted(
        {spec.domain for spec in settings_spec.SETTINGS_SPECS},
        key=lambda domain: domain.value,
    )
    items = [
        {"value": domain.value, "label": domain.value.replace("_", " ").title()}
        for domain in domains
    ]
    items.insert(0, {"value": ENFORCEMENT_DOMAIN, "label": "Enforcement & FUP"})
    return items


# Domain groupings by business function
SETTINGS_DOMAIN_GROUPS = {
    "Enforcement": [ENFORCEMENT_DOMAIN],
    "Billing & Payments": ["billing", "collections", "usage"],
    "Notifications": ["notification", "comms"],
    "Services & Catalog": ["catalog", "subscriber", "provisioning", "lifecycle"],
    "Network": ["network", "network_monitoring", "radius", "bandwidth", "gis", "geocoding"],
    "Operations": ["workflow", "projects", "scheduler", "inventory"],
    "Security & System": ["auth", "audit", "imports"],
}


def _grouped_settings_domains() -> dict[str, list[dict]]:
    """Return settings domains grouped by business function."""
    all_domains = {d["value"]: d for d in _settings_domains()}
    grouped = {}
    used = set()

    for group_name, domain_values in SETTINGS_DOMAIN_GROUPS.items():
        group_domains = []
        for dv in domain_values:
            if dv in all_domains:
                group_domains.append(all_domains[dv])
                used.add(dv)
        if group_domains:
            grouped[group_name] = group_domains

    # Add any remaining domains to "Other"
    other = [d for v, d in all_domains.items() if v not in used]
    if other:
        grouped["Other"] = sorted(other, key=lambda x: x["value"])

    return grouped


def _resolve_settings_domain(value: str | None) -> SettingDomain:
    domains = _settings_domains()
    default_value = domains[0]["value"] if domains else SettingDomain.auth.value
    raw = value or default_value
    if raw == ENFORCEMENT_DOMAIN:
        return SettingDomain.auth
    try:
        return SettingDomain(raw)
    except ValueError:
        return SettingDomain(default_value)


def _enforcement_specs() -> list[settings_spec.SettingSpec]:
    ordered_keys = {
        SettingDomain.radius: [
            "coa_enabled",
            "coa_dictionary_path",
            "coa_timeout_sec",
            "coa_retries",
            "refresh_sessions_on_profile_change",
        ],
        SettingDomain.usage: [
            "usage_warning_enabled",
            "usage_warning_thresholds",
            "fup_action",
            "fup_throttle_radius_profile_id",
        ],
        SettingDomain.network: [
            "mikrotik_session_kill_enabled",
            "address_list_block_enabled",
            "default_mikrotik_address_list",
        ],
    }
    spec_map = {
        (spec.domain, spec.key): spec for spec in settings_spec.SETTINGS_SPECS
    }
    specs: list[settings_spec.SettingSpec] = []
    for domain, keys in ordered_keys.items():
        for key in keys:
            spec = spec_map.get((domain, key))
            if spec:
                specs.append(spec)
    return specs


def _build_settings_context(db: Session, domain_value: str | None) -> dict:
    if domain_value == ENFORCEMENT_DOMAIN:
        sections: list[dict] = []
        for domain, title in (
            (SettingDomain.radius, "RADIUS Enforcement"),
            (SettingDomain.usage, "Usage & FUP"),
            (SettingDomain.network, "Network Controls"),
        ):
            specs = [spec for spec in _enforcement_specs() if spec.domain == domain]
            service = settings_spec.DOMAIN_SETTINGS_SERVICE.get(domain)
            existing = {}
            if service:
                items = service.list(db, None, True, "key", "asc", 1000, 0)
                existing = {item.key: item for item in items}
            section_settings = []
            for spec in specs:
                setting = existing.get(spec.key)
                raw = settings_spec.extract_db_value(setting)
                if raw is None:
                    raw = spec.default
                value, error = settings_spec.coerce_value(spec, raw)
                if error:
                    value = spec.default
                section_settings.append(
                    {
                        "key": spec.key,
                        "label": spec.label or spec.key.replace("_", " ").title(),
                        "value": value if value is not None else "",
                        "value_type": spec.value_type.value,
                        "allowed": sorted(spec.allowed) if spec.allowed else None,
                        "min_value": spec.min_value,
                        "max_value": spec.max_value,
                        "is_secret": spec.is_secret,
                        "required": spec.required,
                    }
                )
            sections.append({"title": title, "settings": section_settings})
        return {
            "domain": ENFORCEMENT_DOMAIN,
            "domains": _settings_domains(),
            "grouped_domains": _grouped_settings_domains(),
            "settings": [],
            "sections": sections,
        }

    selected_domain = _resolve_settings_domain(domain_value)
    domain_specs = settings_spec.list_specs(selected_domain)
    service = settings_spec.DOMAIN_SETTINGS_SERVICE.get(selected_domain)
    existing = {}
    if service:
        items = service.list(db, None, True, "key", "asc", 1000, 0)
        existing = {item.key: item for item in items}
    settings = []
    for spec in domain_specs:
        setting = existing.get(spec.key)
        raw = settings_spec.extract_db_value(setting)
        if raw is None:
            raw = spec.default
        value, error = settings_spec.coerce_value(spec, raw)
        if error:
            value = spec.default
        settings.append(
            {
                "key": spec.key,
                "label": spec.label or spec.key.replace("_", " ").title(),
                "value": value if value is not None else "",
                "value_type": spec.value_type.value,
                "allowed": sorted(spec.allowed) if spec.allowed else None,
                "min_value": spec.min_value,
                "max_value": spec.max_value,
                "is_secret": spec.is_secret,
                "required": spec.required,
            }
        )
    return {
        "domain": selected_domain.value,
        "domains": _settings_domains(),
        "grouped_domains": _grouped_settings_domains(),
        "settings": settings,
    }


def _user_stats(db: Session) -> dict:
    total = db.query(Subscriber).count()
    active = db.query(Subscriber).filter(Subscriber.is_active.is_(True)).count()

    admin_role = (
        db.query(Role)
        .filter(Role.name.ilike("admin"))
        .filter(Role.is_active.is_(True))
        .first()
    )
    if admin_role:
        admins = (
            db.query(PersonRole.subscriber_id)
            .filter(PersonRole.role_id == admin_role.id)
            .distinct()
            .count()
        )
    else:
        admins = 0

    active_credential = (
        db.query(UserCredential.id)
        .filter(UserCredential.subscriber_id == Subscriber.id)
        .filter(UserCredential.is_active.is_(True))
        .exists()
    )
    pending_credential = (
        db.query(UserCredential.id)
        .filter(UserCredential.subscriber_id == Subscriber.id)
        .filter(UserCredential.is_active.is_(True))
        .filter(UserCredential.must_change_password.is_(True))
        .exists()
    )
    pending = db.query(Subscriber).filter(or_(~active_credential, pending_credential)).count()

    return {"total": total, "active": active, "admins": admins, "pending": pending}


def _build_users(
    db: Session,
    search: str | None,
    role_id: str | None,
    status: str | None,
    offset: int,
    limit: int,
):
    query = db.query(Subscriber)

    if search:
        search_value = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Subscriber.first_name.ilike(search_value),
                Subscriber.last_name.ilike(search_value),
                Subscriber.email.ilike(search_value),
                Subscriber.display_name.ilike(search_value),
            )
        )

    if role_id:
        query = query.join(PersonRole).filter(PersonRole.role_id == coerce_uuid(role_id)).distinct()

    if status:
        if status == "active":
            query = query.filter(Subscriber.is_active.is_(True))
        elif status == "inactive":
            query = query.filter(Subscriber.is_active.is_(False))
        elif status == "pending":
            active_credential = (
                db.query(UserCredential.id)
                .filter(UserCredential.subscriber_id == Subscriber.id)
                .filter(UserCredential.is_active.is_(True))
                .exists()
            )
            pending_credential = (
                db.query(UserCredential.id)
                .filter(UserCredential.subscriber_id == Subscriber.id)
                .filter(UserCredential.is_active.is_(True))
                .filter(UserCredential.must_change_password.is_(True))
                .exists()
            )
            query = query.filter(or_(~active_credential, pending_credential))

    total = query.count()
    people = (
        query.order_by(Subscriber.last_name.asc(), Subscriber.first_name.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    person_ids = [person.id for person in people]
    if not person_ids:
        return [], total

    credentials = (
        db.query(UserCredential)
        .filter(UserCredential.subscriber_id.in_(person_ids))
        .all()
    )
    credential_info: dict = {}
    for credential in credentials:
        info = credential_info.setdefault(
            credential.subscriber_id,
            {"last_login": None, "has_active": False, "must_change_password": False},
        )
        if credential.is_active:
            info["has_active"] = True
            if credential.must_change_password:
                info["must_change_password"] = True
        if credential.last_login_at:
            if info["last_login"] is None or credential.last_login_at > info["last_login"]:
                info["last_login"] = credential.last_login_at

    mfa_enabled = {
        method.subscriber_id
        for method in db.query(MFAMethod)
        .filter(MFAMethod.subscriber_id.in_(person_ids))
        .filter(MFAMethod.enabled.is_(True))
        .filter(MFAMethod.is_active.is_(True))
        .all()
    }

    roles_query = (
        db.query(PersonRole, Role)
        .join(Role, Role.id == PersonRole.role_id)
        .filter(PersonRole.subscriber_id.in_(person_ids))
        .order_by(PersonRole.assigned_at.desc())
        .all()
    )
    role_map: dict = {}
    for person_role, role in roles_query:
        if person_role.subscriber_id not in role_map:
            role_map[person_role.subscriber_id] = []
        role_map[person_role.subscriber_id].append({
            "id": str(role.id),
            "name": role.name,
            "is_active": role.is_active,
        })

    users = []
    for person in people:
        name = person.display_name or f"{person.first_name} {person.last_name}".strip()
        info = credential_info.get(person.id, {})
        users.append(
            {
                "id": str(person.id),
                "name": name,
                "email": person.email,
                "roles": role_map.get(person.id, []),
                "is_active": bool(person.is_active),
                "mfa_enabled": person.id in mfa_enabled,
                "last_login": info.get("last_login"),
            }
        )

    return users, total


def _workflow_context(request: Request, db: Session, error: str | None = None):
    """Build context for workflow page - simplified after CRM cleanup."""
    from app.web.admin import get_sidebar_stats, get_current_user
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
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/system/index.html",
        {
            "request": request,
            "active_page": "system",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/users", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:read"))])
def users_list(
    request: Request,
    search: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    offset: Optional[int] = Query(None, ge=0),
    limit: Optional[int] = Query(None, ge=5, le=100),
    db: Session = Depends(get_db),
):
    """List system users."""
    if limit is None:
        limit = per_page
    if offset is None:
        offset = (page - 1) * limit

    users, total = _build_users(db, search, role, status, offset, limit)
    roles = rbac_service.roles.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    stats = _user_stats(db)
    pagination = total > limit

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "admin/system/users/_table_rows.html",
            {"request": request, "users": users},
        )

    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/system/users/index.html",
        {
            "request": request,
            "users": users,
            "search": search,
            "role": role,
            "status": status,
            "stats": stats,
            "roles": roles,
            "pagination": pagination,
            "total": total,
            "offset": offset,
            "limit": limit,
            "htmx_url": "/admin/system/users/filter",
            "htmx_target": "users-table-body",
            "active_page": "users",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/users/search", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:read"))])
def users_search(
    request: Request,
    search: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=5, le=100),
    db: Session = Depends(get_db),
):
    users, _ = _build_users(db, search, role, status, offset, limit)
    return templates.TemplateResponse(
        "admin/system/users/_table_rows.html",
        {"request": request, "users": users},
    )


@router.get("/users/filter", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:read"))])
def users_filter(
    request: Request,
    search: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=5, le=100),
    db: Session = Depends(get_db),
):
    users, _ = _build_users(db, search, role, status, offset, limit)
    return templates.TemplateResponse(
        "admin/system/users/_table_rows.html",
        {"request": request, "users": users},
    )


@router.get("/users/profile", response_class=HTMLResponse)
def user_profile(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user

    current_user = get_current_user(request)

    # Get person record
    person = None
    credential = None
    mfa_enabled = False
    api_key_count = 0

    if current_user and current_user.get("subscriber_id"):
        person_id = current_user["person_id"]
        person = db.get(Subscriber, coerce_uuid(person_id))
        if person:
            # Get credential
            credential = db.query(UserCredential).filter(
                UserCredential.subscriber_id == person.id,
                UserCredential.is_active.is_(True)
            ).first()
            # Check MFA
            mfa_method = db.query(MFAMethod).filter(
                MFAMethod.subscriber_id == person.id,
                MFAMethod.enabled.is_(True)
            ).first()
            mfa_enabled = mfa_method is not None
            # Count API keys
            api_key_count = db.query(ApiKey).filter(
                ApiKey.subscriber_id == person.id,
                ApiKey.is_active.is_(True),
                ApiKey.revoked_at.is_(None)
            ).count()

    context = {
        "request": request,
        "active_page": "users",
        "active_menu": "system",
        "current_user": current_user,
        "sidebar_stats": get_sidebar_stats(db),
        "person": person,
        "credential": credential,
        "mfa_enabled": mfa_enabled,
        "api_key_count": api_key_count,
        "error": None,
        "success": None,
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
    from app.web.admin import get_sidebar_stats, get_current_user

    current_user = get_current_user(request)
    error = None
    success = None
    person = None

    if current_user and current_user.get("subscriber_id"):
        person_id = current_user["person_id"]
        subscriber = db.get(Subscriber, coerce_uuid(person_id))
        if subscriber:
            try:
                # Direct model update instead of person_service
                if first_name:
                    subscriber.first_name = first_name
                if last_name:
                    subscriber.last_name = last_name
                if email:
                    subscriber.email = email
                if phone:
                    subscriber.phone = phone
                db.commit()
                db.refresh(subscriber)
                person = subscriber  # Keep variable for template compatibility
                success = "Profile updated successfully."
            except Exception as e:
                db.rollback()
                error = str(e)
        else:
            person = None
    else:
        person = None

    # Get related data
    credential = None
    mfa_enabled = False
    api_key_count = 0
    if person:
        credential = db.query(UserCredential).filter(
            UserCredential.subscriber_id == person.id,
            UserCredential.is_active.is_(True)
        ).first()
        mfa_method = db.query(MFAMethod).filter(
            MFAMethod.subscriber_id == person.id,
            MFAMethod.enabled.is_(True)
        ).first()
        mfa_enabled = mfa_method is not None
        api_key_count = db.query(ApiKey).filter(
            ApiKey.subscriber_id == person.id,
            ApiKey.is_active.is_(True),
            ApiKey.revoked_at.is_(None)
        ).count()

    context = {
        "request": request,
        "active_page": "users",
        "active_menu": "system",
        "current_user": current_user,
        "sidebar_stats": get_sidebar_stats(db),
        "person": person,
        "credential": credential,
        "mfa_enabled": mfa_enabled,
        "api_key_count": api_key_count,
        "error": error,
        "success": success,
    }
    return templates.TemplateResponse("admin/system/profile.html", context)


@router.get("/users/{user_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:read"))])
def user_detail(request: Request, user_id: str, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user

    subscriber = db.get(Subscriber, coerce_uuid(user_id))
    if not subscriber:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "User not found"},
            status_code=404,
        )
    person = subscriber  # Keep for template compatibility

    # Get user's roles
    person_roles = (
        db.query(PersonRole)
        .filter(PersonRole.subscriber_id == subscriber.id)
        .all()
    )
    roles = []
    for pr in person_roles:
        role = db.get(Role, pr.role_id)
        if role and role.is_active:
            roles.append(role)

    # Get user's credential
    credential = (
        db.query(UserCredential)
        .filter(UserCredential.subscriber_id == person.id)
        .filter(UserCredential.is_active.is_(True))
        .first()
    )

    # Get MFA methods
    mfa_methods = (
        db.query(MFAMethod)
        .filter(MFAMethod.subscriber_id == person.id)
        .all()
    )

    return templates.TemplateResponse(
        "admin/system/users/detail.html",
        {
            "request": request,
            "user": person,
            "roles": roles,
            "credential": credential,
            "mfa_methods": mfa_methods,
            "active_page": "users",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/users/{user_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_edit(request: Request, user_id: str, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user

    subscriber = db.get(Subscriber, coerce_uuid(user_id))
    if not subscriber:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "User not found"},
            status_code=404,
        )
    person = subscriber  # Keep for template compatibility

    roles = rbac_service.roles.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )

    # Get all roles assigned to this user
    current_roles = (
        db.query(PersonRole)
        .filter(PersonRole.subscriber_id == person.id)
        .all()
    )
    current_role_ids = {str(pr.role_id) for pr in current_roles}

    # Get all permissions for direct assignment UI
    all_permissions = rbac_service.permissions.list(
        db=db,
        is_active=True,
        order_by="key",
        order_dir="asc",
        limit=1000,
        offset=0,
    )

    # Get direct permissions assigned to this user
    direct_permissions = rbac_service.person_permissions.list_for_person(db, str(person.id))
    direct_permission_ids = {str(pp.permission_id) for pp in direct_permissions}

    return templates.TemplateResponse(
        "admin/system/users/edit.html",
        {
            "request": request,
            "user": person,
            "roles": roles,
            "current_role_ids": current_role_ids,
            "all_permissions": all_permissions,
            "direct_permission_ids": direct_permission_ids,
            "can_update_password": _is_admin_request(request),
            "active_page": "users",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/users/{user_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
async def user_edit_submit(
    request: Request,
    user_id: str,
    db: Session = Depends(get_db),
):
    from app.web.admin import get_sidebar_stats, get_current_user

    subscriber = db.get(Subscriber, coerce_uuid(user_id))
    if not subscriber:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "User not found"},
            status_code=404,
        )
    person = subscriber  # Keep for template compatibility

    # Parse form data manually to handle multiple checkbox values
    form_data = await request.form()
    first_name = form_data.get("first_name", "")
    last_name = form_data.get("last_name", "")
    display_name = form_data.get("display_name")
    email = form_data.get("email", "")
    phone = form_data.get("phone")
    is_active = form_data.get("is_active")
    new_password = form_data.get("new_password")
    confirm_password = form_data.get("confirm_password")
    require_password_change = form_data.get("require_password_change")

    # Get multiple values for role_ids and direct_permission_ids
    role_ids = form_data.getlist("role_ids")
    direct_permission_ids = form_data.getlist("direct_permission_ids")

    roles = rbac_service.roles.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    all_permissions = rbac_service.permissions.list(
        db=db,
        is_active=True,
        order_by="key",
        order_dir="asc",
        limit=1000,
        offset=0,
    )

    try:
        # Direct model update instead of person_service
        subscriber.first_name = first_name.strip()
        subscriber.last_name = last_name.strip()
        subscriber.display_name = display_name.strip() if display_name else None
        subscriber.email = email.strip()
        subscriber.phone = phone.strip() if phone else None
        subscriber.is_active = _form_bool(is_active)
        subscriber.status = "active" if _form_bool(is_active) else "inactive"

        db.query(UserCredential).filter(
            UserCredential.subscriber_id == subscriber.id,
            UserCredential.is_active.is_(True),
        ).update({"username": email.strip()})

        # Sync roles - add new, remove deselected, keep existing
        desired_role_ids = set(role_ids)
        existing_roles = db.query(PersonRole).filter(PersonRole.subscriber_id == subscriber.id).all()
        existing_role_map = {str(pr.role_id): pr for pr in existing_roles}

        # Remove roles not in desired set
        for role_id_str, person_role in existing_role_map.items():
            if role_id_str not in desired_role_ids:
                db.delete(person_role)

        # Add new roles
        for role_id_str in desired_role_ids:
            if role_id_str not in existing_role_map:
                db.add(PersonRole(subscriber_id=subscriber.id, role_id=UUID(role_id_str)))

        # Sync direct permissions
        rbac_service.person_permissions.sync_for_person(
            db,
            str(subscriber.id),
            set(direct_permission_ids),
            granted_by=getattr(request.state, "actor_id", None),
        )

        if new_password or confirm_password:
            if not _is_admin_request(request):
                raise ValueError("Only admins can update passwords.")
            if not new_password or not confirm_password:
                raise ValueError("Password and confirmation are required.")
            if new_password != confirm_password:
                raise ValueError("Passwords do not match.")
            must_change = _form_bool(require_password_change)
            updated = db.query(UserCredential).filter(
                UserCredential.subscriber_id == subscriber.id,
                UserCredential.is_active.is_(True),
            ).update(
                {
                    "password_hash": hash_password(new_password),
                    "must_change_password": must_change,
                    "password_updated_at": datetime.now(timezone.utc),
                }
            )
            if not updated:
                auth_service.user_credentials.create(
                    db,
                    UserCredentialCreate(
                        subscriber_id=subscriber.id,
                        username=email.strip(),
                        password_hash=hash_password(new_password),
                        must_change_password=must_change,
                    ),
                )
        db.commit()
    except Exception as exc:
        db.rollback()
        current_roles = db.query(PersonRole).filter(PersonRole.subscriber_id == subscriber.id).all()
        current_role_ids = {str(pr.role_id) for pr in current_roles}
        direct_permissions = rbac_service.person_permissions.list_for_person(db, str(subscriber.id))
        direct_permission_ids_set = {str(pp.permission_id) for pp in direct_permissions}
        return templates.TemplateResponse(
            "admin/system/users/edit.html",
            {
                "request": request,
                "user": person,
                "roles": roles,
                "current_role_ids": current_role_ids,
                "all_permissions": all_permissions,
                "direct_permission_ids": direct_permission_ids_set,
                "can_update_password": _is_admin_request(request),
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
    subscriber = db.get(Subscriber, coerce_uuid(user_id))
    # Direct model update instead of person_service
    subscriber.is_active = True
    subscriber.status = "active"
    db.query(UserCredential).filter(
        UserCredential.subscriber_id == subscriber.id
    ).update({"is_active": True})
    db.commit()
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/deactivate", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_deactivate(request: Request, user_id: str, db: Session = Depends(get_db)):
    subscriber = db.get(Subscriber, coerce_uuid(user_id))
    # Direct model update instead of person_service
    subscriber.is_active = False
    subscriber.status = "inactive"
    db.query(UserCredential).filter(
        UserCredential.subscriber_id == subscriber.id
    ).update({"is_active": False})
    db.commit()
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/disable-mfa", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_disable_mfa(request: Request, user_id: str, db: Session = Depends(get_db)):
    subscriber = db.get(Subscriber, coerce_uuid(user_id))
    db.query(MFAMethod).filter(MFAMethod.subscriber_id == subscriber.id).update(
        {"enabled": False, "is_active": False}
    )
    db.commit()
    return Response(status_code=204)


@router.post("/users/{user_id}/reset-password", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_reset_password(request: Request, user_id: str, db: Session = Depends(get_db)):
    subscriber = db.get(Subscriber, coerce_uuid(user_id))
    temp_password = secrets.token_urlsafe(16)
    db.query(UserCredential).filter(
        UserCredential.subscriber_id == subscriber.id,
        UserCredential.is_active.is_(True),
    ).update(
        {
            "password_hash": hash_password(temp_password),
            "must_change_password": True,
            "password_updated_at": datetime.now(timezone.utc),
        }
    )
    db.commit()
    trigger = {
        "showToast": {
            "type": "success",
            "title": "Password reset",
            "message": f"Temporary password: {temp_password}",
            "duration": 12000,
        }
    }
    return Response(status_code=204, headers={"HX-Trigger": json.dumps(trigger)})


@router.post("/users", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_create(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    role_id: str = Form(...),
    send_invite: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    role = rbac_service.roles.get(db, role_id)

    try:
        # Direct model creation instead of person_service
        subscriber = Subscriber(
            first_name=first_name,
            last_name=last_name,
            display_name=f"{first_name} {last_name}".strip(),
            email=email,
        )
        db.add(subscriber)
        db.flush()  # Get the ID without committing

        rbac_service.person_roles.create(
            db,
            PersonRoleCreate(subscriber_id=subscriber.id, role_id=role.id),
        )

        temp_password = secrets.token_urlsafe(16)
        auth_service.user_credentials.create(
            db,
            UserCredentialCreate(
                subscriber_id=subscriber.id,
                username=email,
                password_hash=hash_password(temp_password),
                must_change_password=True,
            ),
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        return _error_banner(_humanize_integrity_error(exc))

    note = "User created. Ask the user to reset their password."
    if send_invite:
        reset = auth_flow_service.request_password_reset(db=db, email=email)
        if reset and reset.get("token"):
            sent = email_service.send_user_invite_email(
                db,
                to_email=email,
                reset_token=reset["token"],
                person_name=reset.get("person_name"),
            )
            if sent:
                note = "Invitation sent. Password reset email delivered."
            else:
                note = "User created, but the reset email could not be sent."
        else:
            note = "User created, but no reset token was generated."
    return HTMLResponse(
        '<div class="rounded-lg border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-700">'
        f"{note}"
        "</div>"
    )


@router.delete("/users/{user_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:assign"))])
def user_delete(request: Request, user_id: str, db: Session = Depends(get_db)):
    subscriber = db.get(Subscriber, coerce_uuid(user_id))
    if subscriber.is_active:
        return _blocked_delete_response(request, [], detail="Deactivate user before deleting.")
    linked = _linked_user_labels(db, subscriber.id)
    if linked:
        return _blocked_delete_response(request, linked)
    try:
        db.query(UserCredential).filter(UserCredential.subscriber_id == subscriber.id).delete(synchronize_session=False)
        db.query(MFAMethod).filter(MFAMethod.subscriber_id == subscriber.id).delete(synchronize_session=False)
        db.query(AuthSession).filter(AuthSession.subscriber_id == subscriber.id).delete(synchronize_session=False)
        db.query(ApiKey).filter(ApiKey.subscriber_id == subscriber.id).delete(synchronize_session=False)
        db.query(PersonRole).filter(PersonRole.subscriber_id == subscriber.id).delete(synchronize_session=False)
        db.query(PersonPermission).filter(PersonPermission.subscriber_id == subscriber.id).delete(synchronize_session=False)
        # ResellerUser was removed during model consolidation
        db.delete(subscriber)
        db.commit()
    except IntegrityError:
        db.rollback()
        linked = _linked_user_labels(db, subscriber.id)
        return _blocked_delete_response(request, linked)
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
    from sqlalchemy import func

    offset = (page - 1) * per_page

    roles = rbac_service.roles.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )

    all_roles = rbac_service.roles.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_roles)
    total_pages = (total + per_page - 1) // per_page

    # Get user counts per role
    user_counts_query = (
        db.query(PersonRole.role_id, func.count(PersonRole.subscriber_id.distinct()))
        .group_by(PersonRole.role_id)
        .all()
    )
    user_counts = {str(role_id): count for role_id, count in user_counts_query}

    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/system/roles.html",
        {
            "request": request,
            "roles": roles,
            "user_counts": user_counts,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "active_page": "roles",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/roles/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:roles:write"))])
def role_new(request: Request, db: Session = Depends(get_db)):
    permissions = rbac_service.permissions.list(
        db=db,
        is_active=None,
        order_by="key",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/system/roles_form.html",
        {
            "request": request,
            "role": None,
            "permissions": permissions,
            "selected_permission_ids": set(),
            "action_url": "/admin/system/roles",
            "form_title": "New Role",
            "submit_label": "Create Role",
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
    description_value = description.strip() if description else None
    payload = RoleCreate(
        name=name.strip(),
        description=description_value or None,
        is_active=_form_bool(is_active),
    )
    try:
        role = rbac_service.roles.create(db, payload)
        for permission_id in permission_ids:
            if not permission_id:
                continue
            rbac_service.role_permissions.create(
                db,
                RolePermissionCreate(
                    role_id=role.id,
                    permission_id=UUID(permission_id),
                ),
            )
    except Exception as exc:
        permissions = rbac_service.permissions.list(
            db=db,
            is_active=None,
            order_by="key",
            order_dir="asc",
            limit=1000,
            offset=0,
        )
        selected_permission_ids = set()
        for permission_id in permission_ids:
            try:
                selected_permission_ids.add(str(UUID(permission_id)))
            except ValueError:
                continue
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/system/roles_form.html",
            {
                "request": request,
                "role": payload.model_dump(),
                "permissions": permissions,
                "selected_permission_ids": selected_permission_ids,
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
        role = rbac_service.roles.get(db, role_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Role not found"},
            status_code=404,
        )
    permissions = rbac_service.permissions.list(
        db=db,
        is_active=None,
        order_by="key",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    role_permissions = (
        db.query(RolePermission)
        .filter(RolePermission.role_id == role.id)
        .all()
    )
    selected_permission_ids = {str(link.permission_id) for link in role_permissions}
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/system/roles_form.html",
        {
            "request": request,
            "role": role,
            "permissions": permissions,
            "selected_permission_ids": selected_permission_ids,
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
    description_value = description.strip() if description else None
    payload = RoleUpdate(
        name=name.strip(),
        description=description_value or None,
        is_active=_form_bool(is_active),
    )
    try:
        role = rbac_service.roles.update(db, role_id, payload)
        desired_ids: set[UUID] = set()
        for permission_id in permission_ids:
            if not permission_id:
                continue
            desired_ids.add(UUID(permission_id))
        if desired_ids:
            found_ids = {
                str(row[0])
                for row in db.query(Permission.id)
                .filter(Permission.id.in_(desired_ids))
                .all()
            }
            missing = {str(item) for item in desired_ids} - found_ids
            if missing:
                raise ValueError("One or more permissions were not found.")
        existing_links = (
            db.query(RolePermission)
            .filter(RolePermission.role_id == role.id)
            .all()
        )
        existing_ids = {link.permission_id: link for link in existing_links}
        for permission_id, link in existing_ids.items():
            if permission_id not in desired_ids:
                db.delete(link)
        for permission_id in desired_ids - set(existing_ids.keys()):
            db.add(RolePermission(role_id=role.id, permission_id=permission_id))
        db.commit()
    except Exception as exc:
        permissions = rbac_service.permissions.list(
            db=db,
            is_active=None,
            order_by="key",
            order_dir="asc",
            limit=1000,
            offset=0,
        )
        selected_permission_ids = set()
        for permission_id in permission_ids:
            try:
                selected_permission_ids.add(str(UUID(permission_id)))
            except ValueError:
                continue
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/system/roles_form.html",
            {
                "request": request,
                "role": {"id": role_id, **payload.model_dump()},
                "permissions": permissions,
                "selected_permission_ids": selected_permission_ids,
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
    offset = (page - 1) * per_page

    permissions = rbac_service.permissions.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )

    all_permissions = rbac_service.permissions.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_permissions)
    total_pages = (total + per_page - 1) // per_page

    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/system/permissions.html",
        {
            "request": request,
            "permissions": permissions,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "active_page": "roles",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/permissions/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("rbac:permissions:write"))])
def permission_new(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/system/permissions_form.html",
        {
            "request": request,
            "permission": None,
            "action_url": "/admin/system/permissions",
            "form_title": "New Permission",
            "submit_label": "Create Permission",
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
    description_value = description.strip() if description else None
    payload = PermissionCreate(
        key=key.strip(),
        description=description_value or None,
        is_active=_form_bool(is_active),
    )
    try:
        rbac_service.permissions.create(db, payload)
    except Exception as exc:
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/system/permissions_form.html",
            {
                "request": request,
                "permission": payload.model_dump(),
                "action_url": "/admin/system/permissions",
                "form_title": "New Permission",
                "submit_label": "Create Permission",
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
    try:
        permission = rbac_service.permissions.get(db, permission_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Permission not found"},
            status_code=404,
        )
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/system/permissions_form.html",
        {
            "request": request,
            "permission": permission,
            "action_url": f"/admin/system/permissions/{permission_id}",
            "form_title": "Edit Permission",
            "submit_label": "Save Changes",
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
    description_value = description.strip() if description else None
    payload = PermissionUpdate(
        key=key.strip(),
        description=description_value or None,
        is_active=_form_bool(is_active),
    )
    try:
        rbac_service.permissions.update(db, permission_id, payload)
    except Exception as exc:
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/system/permissions_form.html",
            {
                "request": request,
                "permission": {"id": permission_id, **payload.model_dump()},
                "action_url": f"/admin/system/permissions/{permission_id}",
                "form_title": "Edit Permission",
                "submit_label": "Save Changes",
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
def api_keys_list(request: Request, new_key: str = None, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user

    current_user = get_current_user(request)
    api_keys = []

    if current_user and current_user.get("subscriber_id"):
        person_id = current_user["person_id"]
        api_keys = db.query(ApiKey).filter(
            ApiKey.subscriber_id == coerce_uuid(person_id)
        ).order_by(ApiKey.created_at.desc()).all()

    context = {
        "request": request,
        "active_page": "api-keys",
        "active_menu": "system",
        "current_user": current_user,
        "sidebar_stats": get_sidebar_stats(db),
        "api_keys": api_keys,
        "new_key": new_key,
        "now": datetime.now(timezone.utc),
    }
    return templates.TemplateResponse("admin/system/api_keys.html", context)


@router.get("/api-keys/new", response_class=HTMLResponse)
def api_key_new(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user

    context = {
        "request": request,
        "active_page": "api-keys",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "error": None,
    }
    return templates.TemplateResponse("admin/system/api_key_form.html", context)


@router.post("/api-keys", response_class=HTMLResponse)
def api_key_create(
    request: Request,
    label: str = Form(...),
    expires_in: str = Form(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_sidebar_stats, get_current_user
    from datetime import timedelta

    current_user = get_current_user(request)

    if not current_user or not current_user.get("subscriber_id"):
        return RedirectResponse(url="/admin/system/api-keys", status_code=303)

    try:
        # Generate a random API key
        raw_key = secrets.token_urlsafe(32)
        key_hash = hash_password(raw_key)

        # Calculate expiration
        expires_at = None
        if expires_in:
            days = int(expires_in)
            expires_at = datetime.now(timezone.utc) + timedelta(days=days)

        # Create the API key
        api_key = ApiKey(
            subscriber_id=coerce_uuid(current_user["person_id"]),
            label=label,
            key_hash=key_hash,
            is_active=True,
            expires_at=expires_at,
        )
        db.add(api_key)
        db.commit()

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
    api_key = db.get(ApiKey, coerce_uuid(key_id))
    if api_key:
        api_key.revoked_at = datetime.now(timezone.utc)
        api_key.is_active = False
        db.commit()
    return RedirectResponse(url="/admin/system/api-keys", status_code=303)


@router.get("/webhooks", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:read"))])
def webhooks_list(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user
    from datetime import timedelta

    # Get all webhook endpoints
    endpoints = db.query(WebhookEndpoint).order_by(WebhookEndpoint.created_at.desc()).all()
    active_count = sum(1 for e in endpoints if e.is_active)

    # Get delivery stats for last 24 hours
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    deliveries_24h = db.query(WebhookDelivery).filter(
        WebhookDelivery.created_at >= cutoff
    ).all()
    delivery_count_24h = len(deliveries_24h)
    failed_count_24h = sum(1 for d in deliveries_24h if d.status == WebhookDeliveryStatus.failed)

    context = {
        "request": request,
        "active_page": "webhooks",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "endpoints": endpoints,
        "active_count": active_count,
        "delivery_count_24h": delivery_count_24h,
        "failed_count_24h": failed_count_24h,
    }
    return templates.TemplateResponse("admin/system/webhooks.html", context)


@router.get("/webhooks/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:write"))])
def webhook_new(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user

    context = {
        "request": request,
        "active_page": "webhooks",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "endpoint": None,
        "subscribed_events": [],
        "action_url": "/admin/system/webhooks",
        "error": None,
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
    from app.web.admin import get_sidebar_stats, get_current_user

    try:
        # Generate secret if not provided
        if not secret:
            secret = secrets.token_urlsafe(32)

        # Create endpoint
        endpoint = WebhookEndpoint(
            name=name,
            url=url,
            secret=secret,
            is_active=is_active == "true",
        )
        db.add(endpoint)
        db.commit()
        db.refresh(endpoint)

        return RedirectResponse(url="/admin/system/webhooks", status_code=303)
    except Exception as e:
        context = {
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
    from app.web.admin import get_sidebar_stats, get_current_user

    endpoint = db.get(WebhookEndpoint, coerce_uuid(endpoint_id))
    if not endpoint:
        return RedirectResponse(url="/admin/system/webhooks", status_code=303)

    # Get subscribed events
    subscribed_events = [sub.event_type.value for sub in endpoint.subscriptions if sub.is_active]

    context = {
        "request": request,
        "active_page": "webhooks",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "endpoint": endpoint,
        "subscribed_events": subscribed_events,
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
    from app.web.admin import get_sidebar_stats, get_current_user

    endpoint = db.get(WebhookEndpoint, coerce_uuid(endpoint_id))
    if not endpoint:
        return RedirectResponse(url="/admin/system/webhooks", status_code=303)

    try:
        endpoint.name = name
        endpoint.url = url
        if secret:
            endpoint.secret = secret
        endpoint.is_active = is_active == "true"
        db.commit()

        return RedirectResponse(url="/admin/system/webhooks", status_code=303)
    except Exception as e:
        subscribed_events = [sub.event_type.value for sub in endpoint.subscriptions if sub.is_active]
        context = {
            "request": request,
            "active_page": "webhooks",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "endpoint": endpoint,
            "subscribed_events": subscribed_events,
            "action_url": f"/admin/system/webhooks/{endpoint_id}",
            "error": str(e),
        }
        return templates.TemplateResponse("admin/system/webhook_form.html", context)


@router.get("/audit", response_class=HTMLResponse, dependencies=[Depends(require_permission("audit:read"))])
def audit_log(
    request: Request,
    actor_id: Optional[str] = None,
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """View audit log."""
    offset = (page - 1) * per_page

    events = audit_service.audit_events.list(
        db=db,
        actor_id=UUID(actor_id) if actor_id else None,
        actor_type=None,
        action=action if action else None,
        entity_type=entity_type if entity_type else None,
        entity_id=None,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )

    from app.models.subscriber import Subscriber
    from app.services.audit_helpers import (
        extract_changes,
        format_audit_datetime,
        format_changes,
        humanize_action,
        humanize_entity,
    )

    from app.models.audit import AuditActorType

    def _is_user_actor(actor_type) -> bool:
        return actor_type in {AuditActorType.user, AuditActorType.user.value, "user"}

    actor_ids = {
        event.actor_id
        for event in events
        if event.actor_id and _is_user_actor(getattr(event, "actor_type", None))
    }
    people = {}
    if actor_ids:
        try:
            people = {
                str(person.id): person
                for person in db.query(Subscriber).filter(Subscriber.id.in_(actor_ids)).all()
            }
        except Exception:
            people = {}

    event_views = []
    for event in events:
        actor_name = None
        is_user_actor = _is_user_actor(getattr(event, "actor_type", None))
        if event.actor_id and is_user_actor:
            actor = people.get(str(event.actor_id))
            if actor:
                actor_name = (
                    actor.display_name
                    or f"{actor.first_name} {actor.last_name}".strip()
                    or actor.email
                )
        if not actor_name:
            metadata = getattr(event, "metadata_", None) or {}
            if is_user_actor:
                actor_name = metadata.get("actor_email") or event.actor_id or "User"
            else:
                actor_name = (
                    metadata.get("actor_name")
                    or metadata.get("actor_email")
                    or event.actor_id
                    or "System"
                )
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes)
        action_label = humanize_action(event.action)
        entity_label = humanize_entity(event.entity_type, event.entity_id)
        event_views.append(
            {
                "occurred_at": event.occurred_at,
                "occurred_at_display": format_audit_datetime(
                    event.occurred_at, "%b %d, %Y %H:%M"
                ),
                "actor_name": actor_name,
                "actor_id": event.actor_id,
                "action": event.action,
                "action_label": action_label,
                "action_detail": change_summary,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "entity_label": entity_label,
                "is_success": event.is_success,
                "status_code": event.status_code,
            }
        )

    all_events = audit_service.audit_events.list(
        db=db,
        actor_id=UUID(actor_id) if actor_id else None,
        actor_type=None,
        action=action if action else None,
        entity_type=entity_type if entity_type else None,
        entity_id=None,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_events)
    total_pages = (total + per_page - 1) // per_page

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "admin/system/_audit_table.html",
            {
                "request": request,
                "events": event_views,
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
            },
        )

    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/system/audit.html",
        {
            "request": request,
            "events": event_views,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
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
    offset = (page - 1) * per_page

    tasks = scheduler_service.scheduled_tasks.list(
        db=db,
        enabled=None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )

    all_tasks = scheduler_service.scheduled_tasks.list(
        db=db,
        enabled=None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_tasks)
    total_pages = (total + per_page - 1) // per_page

    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/system/scheduler.html",
        {
            "request": request,
            "tasks": tasks,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "active_page": "scheduler",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/scheduler/{task_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:read"))])
def scheduler_task_detail(request: Request, task_id: str, db: Session = Depends(get_db)):
    """View scheduled task details."""
    from app.web.admin import get_sidebar_stats, get_current_user

    task = scheduler_service.scheduled_tasks.get(db, task_id)
    if not task:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Scheduled task not found"},
            status_code=404,
        )

    # Calculate next run time
    next_run = None
    if task.enabled and task.last_run_at:
        from datetime import timedelta
        next_run = task.last_run_at + timedelta(seconds=task.interval_seconds)

    return templates.TemplateResponse(
        "admin/system/scheduler_detail.html",
        {
            "request": request,
            "task": task,
            "next_run": next_run,
            "runs": [],  # Task run history would come from a task_runs table
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
    from app.schemas.scheduler import ScheduledTaskUpdate
    # Update last_run_at to indicate manual trigger (actual execution would be async)
    scheduler_service.scheduled_tasks.update(
        db, task_id, ScheduledTaskUpdate(last_run_at=datetime.now(timezone.utc))
    )
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
    domain: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """System settings management."""
    from app.web.admin import get_sidebar_stats, get_current_user
    settings_context = _build_settings_context(db, domain)
    base_url = str(request.base_url).rstrip("/")
    crm_meta_callback_url = base_url + "/webhooks/crm/meta"
    crm_meta_oauth_redirect_url = base_url + "/admin/crm/meta/callback"
    return templates.TemplateResponse(
        "admin/system/settings.html",
        {
            "request": request,
            **settings_context,
            "crm_meta_callback_url": crm_meta_callback_url,
            "crm_meta_oauth_redirect_url": crm_meta_oauth_redirect_url,
            "active_page": "settings",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/settings", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:settings:write"))])
async def settings_update(
    request: Request,
    domain: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Update system settings for a domain."""
    form = await request.form()
    domain_value = domain or form.get("domain")
    errors: list[str] = []
    if domain_value == ENFORCEMENT_DOMAIN:
        specs = _enforcement_specs()
        for spec in specs:
            service = settings_spec.DOMAIN_SETTINGS_SERVICE.get(spec.domain)
            if not service:
                errors.append(f"{spec.key}: Settings service not configured.")
                continue
            raw = form.get(spec.key)
            if spec.is_secret and (raw is None or raw == ""):
                continue
            if spec.value_type == settings_spec.SettingValueType.boolean:
                value = _form_bool(raw)
            elif spec.value_type == settings_spec.SettingValueType.integer:
                if raw in (None, ""):
                    value = spec.default
                else:
                    try:
                        value = int(raw)
                    except ValueError:
                        errors.append(f"{spec.key}: Value must be an integer.")
                        continue
            else:
                if raw in (None, ""):
                    if spec.value_type == settings_spec.SettingValueType.string:
                        value = spec.default if spec.default is not None else ""
                    elif spec.value_type == settings_spec.SettingValueType.json:
                        value = spec.default if spec.default is not None else {}
                    else:
                        value = spec.default
                else:
                    value = raw
            if spec.allowed and value is not None and value not in spec.allowed:
                errors.append(f"{spec.key}: Value must be one of {', '.join(sorted(spec.allowed))}.")
                continue
            if isinstance(value, int):
                if spec.min_value is not None and value < spec.min_value:
                    errors.append(f"{spec.key}: Minimum value is {spec.min_value}.")
                    continue
                if spec.max_value is not None and value > spec.max_value:
                    errors.append(f"{spec.key}: Maximum value is {spec.max_value}.")
                    continue
            if value is None:
                value_text, value_json = None, None
            else:
                value_text, value_json = settings_spec.normalize_for_db(spec, value)
            payload = DomainSettingUpdate(
                value_type=spec.value_type,
                value_text=value_text,
                value_json=value_json,
                is_secret=spec.is_secret,
                is_active=True,
            )
            service.upsert_by_key(db, spec.key, payload)
        settings_context = _build_settings_context(db, ENFORCEMENT_DOMAIN)
    else:
        selected_domain = _resolve_settings_domain(domain_value)
        specs = settings_spec.list_specs(selected_domain)
        service = settings_spec.DOMAIN_SETTINGS_SERVICE.get(selected_domain)
        if not service:
            errors.append("Settings service not configured for this domain.")
        else:
            for spec in specs:
                raw = form.get(spec.key)
                if spec.is_secret and (raw is None or raw == ""):
                    continue
                if spec.value_type == settings_spec.SettingValueType.boolean:
                    value = _form_bool(raw)
                elif spec.value_type == settings_spec.SettingValueType.integer:
                    if raw in (None, ""):
                        value = spec.default
                    else:
                        try:
                            value = int(raw)
                        except ValueError:
                            errors.append(f"{spec.key}: Value must be an integer.")
                            continue
                else:
                    if raw in (None, ""):
                        if spec.value_type == settings_spec.SettingValueType.string:
                            value = spec.default if spec.default is not None else ""
                        elif spec.value_type == settings_spec.SettingValueType.json:
                            value = spec.default if spec.default is not None else {}
                        else:
                            value = spec.default
                    else:
                        value = raw
                if spec.allowed and value is not None and value not in spec.allowed:
                    errors.append(f"{spec.key}: Value must be one of {', '.join(sorted(spec.allowed))}.")
                    continue
                if isinstance(value, int):
                    if spec.min_value is not None and value < spec.min_value:
                        errors.append(f"{spec.key}: Minimum value is {spec.min_value}.")
                        continue
                    if spec.max_value is not None and value > spec.max_value:
                        errors.append(f"{spec.key}: Maximum value is {spec.max_value}.")
                        continue
                if value is None:
                    value_text, value_json = None, None
                else:
                    value_text, value_json = settings_spec.normalize_for_db(spec, value)
                payload = DomainSettingUpdate(
                    value_type=spec.value_type,
                    value_text=value_text,
                    value_json=value_json,
                    is_secret=spec.is_secret,
                    is_active=True,
                )
                service.upsert_by_key(db, spec.key, payload)

        settings_context = _build_settings_context(db, selected_domain.value)
    base_url = str(request.base_url).rstrip("/")
    crm_meta_callback_url = base_url + "/webhooks/crm/meta"
    crm_meta_oauth_redirect_url = base_url + "/admin/crm/meta/callback"
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/system/settings.html",
        {
            "request": request,
            **settings_context,
            "crm_meta_callback_url": crm_meta_callback_url,
            "crm_meta_oauth_redirect_url": crm_meta_oauth_redirect_url,
            "active_page": "settings",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "errors": errors,
            "saved": not errors,
        },
    )


@router.post(
    "/settings/bank-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
async def settings_bank_account_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        account_id = (form.get("account_id") or "").strip()
        if not account_id:
            raise HTTPException(status_code=400, detail="Account is required.")
        account_type = (form.get("account_type") or "").strip() or None
        bank_name = (form.get("bank_name") or "").strip() or None
        account_last4 = (form.get("account_last4") or "").strip() or None
        routing_last4 = (form.get("routing_last4") or "").strip() or None
        token = (form.get("token") or "").strip() or None
        payload = BankAccountCreate(
            account_id=account_id,
            bank_name=bank_name,
            account_type=account_type,
            account_last4=account_last4,
            routing_last4=routing_last4,
            token=token,
            is_default=_form_bool(form.get("is_default")),
            is_active=_form_bool(form.get("is_active")) if form.get("is_active") is not None else True,
        )
        billing_service.bank_accounts.create(db, payload)
        return RedirectResponse(
            url="/admin/system/settings?domain=billing#bank-accounts", status_code=303
        )
    except Exception as exc:
        error = exc.detail if hasattr(exc, "detail") else str(exc)
        settings_context = _build_settings_context(db, "billing")
        base_url = str(request.base_url).rstrip("/")
        crm_meta_callback_url = base_url + "/webhooks/crm/meta"
        crm_meta_oauth_redirect_url = base_url + "/admin/crm/meta/callback"
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/system/settings.html",
            {
                "request": request,
                **settings_context,
                "crm_meta_callback_url": crm_meta_callback_url,
                "crm_meta_oauth_redirect_url": crm_meta_oauth_redirect_url,
                "active_page": "settings",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "bank_account_error": error or "Unable to create bank account.",
            },
            status_code=400,
        )


@router.post(
    "/settings/bank-accounts/{bank_account_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
async def settings_bank_account_update(
    request: Request, bank_account_id: UUID, db: Session = Depends(get_db)
):
    form = await request.form()
    try:
        data: dict[str, object] = {}
        if "bank_name" in form:
            data["bank_name"] = (form.get("bank_name") or "").strip() or None
        if "account_type" in form:
            account_type = (form.get("account_type") or "").strip()
            data["account_type"] = account_type or None
        if "account_last4" in form:
            data["account_last4"] = (form.get("account_last4") or "").strip() or None
        if "routing_last4" in form:
            data["routing_last4"] = (form.get("routing_last4") or "").strip() or None
        if "is_default" in form:
            data["is_default"] = _form_bool(form.get("is_default"))
        if "is_active" in form:
            data["is_active"] = _form_bool(form.get("is_active"))
        token = (form.get("token") or "").strip()
        if token:
            data["token"] = token
        payload = BankAccountUpdate(**data)
        billing_service.bank_accounts.update(db, str(bank_account_id), payload)
        return RedirectResponse(
            url="/admin/system/settings?domain=billing#bank-accounts", status_code=303
        )
    except Exception as exc:
        error = exc.detail if hasattr(exc, "detail") else str(exc)
        settings_context = _build_settings_context(db, "billing")
        base_url = str(request.base_url).rstrip("/")
        crm_meta_callback_url = base_url + "/webhooks/crm/meta"
        crm_meta_oauth_redirect_url = base_url + "/admin/crm/meta/callback"
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/system/settings.html",
            {
                "request": request,
                **settings_context,
                "crm_meta_callback_url": crm_meta_callback_url,
                "crm_meta_oauth_redirect_url": crm_meta_oauth_redirect_url,
                "active_page": "settings",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "bank_account_error": error or "Unable to update bank account.",
            },
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
