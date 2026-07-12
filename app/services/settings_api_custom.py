"""Settings API helpers backed by the canonical specification registry."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import settings_spec
from app.services.response import list_response


def _domain_allowed_keys(domain: SettingDomain) -> str:
    return ", ".join(sorted(spec.key for spec in settings_spec.list_specs(domain)))


def _normalize_spec_setting(
    domain: SettingDomain,
    key: str,
    payload: DomainSettingUpdate,
) -> DomainSettingUpdate:
    spec = settings_spec.get_spec(domain, key)
    if not spec:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid setting key. Allowed: {_domain_allowed_keys(domain)}",
        )

    value = payload.value_text if payload.value_text is not None else payload.value_json
    if value is None:
        raise HTTPException(status_code=400, detail="Value required")
    coerced, error = settings_spec.coerce_value(spec, value)
    if error:
        raise HTTPException(status_code=400, detail=error)
    if spec.allowed and coerced not in spec.allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Value must be one of: {', '.join(sorted(spec.allowed))}",
        )
    if spec.value_type == SettingValueType.integer:
        try:
            parsed = int(str(coerced))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="Value must be an integer",
            ) from exc
        if spec.min_value is not None and parsed < spec.min_value:
            raise HTTPException(
                status_code=400,
                detail=f"Value must be >= {spec.min_value}",
            )
        if spec.max_value is not None and parsed > spec.max_value:
            raise HTTPException(
                status_code=400,
                detail=f"Value must be <= {spec.max_value}",
            )
        coerced = parsed

    value_text, value_json = settings_spec.normalize_for_db(spec, coerced)
    data = payload.model_dump(exclude_unset=True)
    data.update(
        value_type=spec.value_type,
        value_text=value_text,
        value_json=value_json,
        is_secret=spec.is_secret,
    )
    return DomainSettingUpdate(**data)


def _list_domain_settings(
    db: Session,
    domain: SettingDomain,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    service = settings_spec.DOMAIN_SETTINGS_SERVICE.get(domain)
    if not service:
        raise HTTPException(status_code=400, detail="Unknown settings domain")
    return service.list(db, None, is_active, order_by, order_dir, limit, offset)


def _list_domain_settings_response(
    db: Session,
    domain: SettingDomain,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    items = _list_domain_settings(
        db,
        domain,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )
    return list_response(items, limit, offset)


def _upsert_domain_setting(
    db: Session,
    domain: SettingDomain,
    key: str,
    payload: DomainSettingUpdate,
):
    service = settings_spec.DOMAIN_SETTINGS_SERVICE.get(domain)
    if not service:
        raise HTTPException(status_code=400, detail="Unknown settings domain")
    return service.upsert_by_key(
        db,
        key,
        _normalize_spec_setting(domain, key, payload),
    )


def _get_domain_setting(db: Session, domain: SettingDomain, key: str):
    if not settings_spec.get_spec(domain, key):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid setting key. Allowed: {_domain_allowed_keys(domain)}",
        )
    service = settings_spec.DOMAIN_SETTINGS_SERVICE.get(domain)
    if not service:
        raise HTTPException(status_code=400, detail="Unknown settings domain")
    return service.get_by_key(db, key)


def _list(
    db: Session,
    domain: SettingDomain,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db,
        domain,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )


def _list_response(
    db: Session,
    domain: SettingDomain,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db,
        domain,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )


def list_gis_settings(db, is_active, order_by, order_dir, limit, offset):
    return _list(db, SettingDomain.gis, is_active, order_by, order_dir, limit, offset)


def list_gis_settings_response(db, is_active, order_by, order_dir, limit, offset):
    return _list_response(
        db, SettingDomain.gis, is_active, order_by, order_dir, limit, offset
    )


def upsert_gis_setting(db, key, payload):
    return _upsert_domain_setting(db, SettingDomain.gis, key, payload)


def get_gis_setting(db, key):
    return _get_domain_setting(db, SettingDomain.gis, key)


def list_geocoding_settings(db, is_active, order_by, order_dir, limit, offset):
    return _list(
        db, SettingDomain.geocoding, is_active, order_by, order_dir, limit, offset
    )


def list_geocoding_settings_response(
    db, is_active, order_by, order_dir, limit, offset
):
    return _list_response(
        db, SettingDomain.geocoding, is_active, order_by, order_dir, limit, offset
    )


def upsert_geocoding_setting(db, key, payload):
    return _upsert_domain_setting(db, SettingDomain.geocoding, key, payload)


def get_geocoding_setting(db, key):
    return _get_domain_setting(db, SettingDomain.geocoding, key)


def list_radius_settings(db, is_active, order_by, order_dir, limit, offset):
    return _list(
        db, SettingDomain.radius, is_active, order_by, order_dir, limit, offset
    )


def list_radius_settings_response(db, is_active, order_by, order_dir, limit, offset):
    return _list_response(
        db, SettingDomain.radius, is_active, order_by, order_dir, limit, offset
    )


def upsert_radius_setting(db, key, payload):
    return _upsert_domain_setting(db, SettingDomain.radius, key, payload)


def get_radius_setting(db, key):
    return _get_domain_setting(db, SettingDomain.radius, key)


def list_auth_settings(db, is_active, order_by, order_dir, limit, offset):
    return _list(db, SettingDomain.auth, is_active, order_by, order_dir, limit, offset)


def list_auth_settings_response(db, is_active, order_by, order_dir, limit, offset):
    return _list_response(
        db, SettingDomain.auth, is_active, order_by, order_dir, limit, offset
    )


def upsert_auth_setting(db, key, payload):
    return _upsert_domain_setting(db, SettingDomain.auth, key, payload)


def get_auth_setting(db, key):
    return _get_domain_setting(db, SettingDomain.auth, key)


def list_audit_settings(db, is_active, order_by, order_dir, limit, offset):
    return _list(db, SettingDomain.audit, is_active, order_by, order_dir, limit, offset)


def list_audit_settings_response(db, is_active, order_by, order_dir, limit, offset):
    return _list_response(
        db, SettingDomain.audit, is_active, order_by, order_dir, limit, offset
    )


def upsert_audit_setting(db, key, payload):
    return _upsert_domain_setting(db, SettingDomain.audit, key, payload)


def get_audit_setting(db, key):
    return _get_domain_setting(db, SettingDomain.audit, key)


def list_imports_settings(db, is_active, order_by, order_dir, limit, offset):
    return _list(
        db, SettingDomain.imports, is_active, order_by, order_dir, limit, offset
    )


def list_imports_settings_response(db, is_active, order_by, order_dir, limit, offset):
    return _list_response(
        db, SettingDomain.imports, is_active, order_by, order_dir, limit, offset
    )


def upsert_imports_setting(db, key, payload):
    return _upsert_domain_setting(db, SettingDomain.imports, key, payload)


def get_imports_setting(db, key):
    return _get_domain_setting(db, SettingDomain.imports, key)


def list_notification_settings(db, is_active, order_by, order_dir, limit, offset):
    return _list(
        db, SettingDomain.notification, is_active, order_by, order_dir, limit, offset
    )


def list_notification_settings_response(
    db, is_active, order_by, order_dir, limit, offset
):
    return _list_response(
        db, SettingDomain.notification, is_active, order_by, order_dir, limit, offset
    )


def upsert_notification_setting(db, key, payload):
    return _upsert_domain_setting(db, SettingDomain.notification, key, payload)


def get_notification_setting(db, key):
    return _get_domain_setting(db, SettingDomain.notification, key)


def list_scheduler_settings(db, is_active, order_by, order_dir, limit, offset):
    return _list(
        db, SettingDomain.scheduler, is_active, order_by, order_dir, limit, offset
    )


def list_scheduler_settings_response(
    db, is_active, order_by, order_dir, limit, offset
):
    return _list_response(
        db, SettingDomain.scheduler, is_active, order_by, order_dir, limit, offset
    )


def upsert_scheduler_setting(db, key, payload):
    return _upsert_domain_setting(db, SettingDomain.scheduler, key, payload)


def get_scheduler_setting(db, key):
    return _get_domain_setting(db, SettingDomain.scheduler, key)
