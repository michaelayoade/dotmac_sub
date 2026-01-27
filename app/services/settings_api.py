from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as settings_service
from app.services import settings_spec
from app.services.response import list_response

_GIS_SETTING_KEYS = {
    "sync_enabled",
    "sync_interval_minutes",
    "sync_pop_sites",
    "sync_addresses",
    "sync_deactivate_missing",
}

_AUTH_SETTING_KEYS = {
    "jwt_secret",
    "jwt_algorithm",
    "jwt_access_ttl_minutes",
    "jwt_refresh_ttl_days",
    "refresh_cookie_name",
    "refresh_cookie_secure",
    "refresh_cookie_samesite",
    "refresh_cookie_domain",
    "refresh_cookie_path",
    "totp_issuer",
    "totp_encryption_key",
    "api_key_rate_window_seconds",
    "api_key_rate_max",
}

_AUTH_SETTING_INT_KEYS = {
    "jwt_access_ttl_minutes",
    "jwt_refresh_ttl_days",
    "api_key_rate_window_seconds",
    "api_key_rate_max",
}
_AUTH_SETTING_BOOL_KEYS = {"refresh_cookie_secure"}
_AUTH_SETTING_SECRET_KEYS = {"jwt_secret", "totp_encryption_key"}

_AUDIT_SETTING_KEYS = {
    "enabled",
    "methods",
    "skip_paths",
    "read_trigger_header",
    "read_trigger_query",
}
_AUDIT_SETTING_BOOL_KEYS = {"enabled"}
_AUDIT_SETTING_LIST_KEYS = {"methods", "skip_paths"}

_IMPORTS_SETTING_KEYS = {"max_file_bytes", "max_rows"}

_NOTIFICATION_SETTING_KEYS = {
    "alert_notifications_enabled",
    "alert_notifications_default_channel",
    "alert_notifications_default_recipient",
    "alert_notifications_default_template_id",
    "alert_notifications_default_rotation_id",
    "alert_notifications_default_delay_minutes",
}
_NOTIFICATION_SETTING_INT_KEYS = {"alert_notifications_default_delay_minutes"}
_NOTIFICATION_SETTING_BOOL_KEYS = {"alert_notifications_enabled"}

_SCHEDULER_SETTING_KEYS = {
    "broker_url",
    "result_backend",
    "timezone",
    "beat_max_loop_interval",
    "beat_refresh_seconds",
    "refresh_minutes",
}

_GEOCODING_SETTING_KEYS = {
    "enabled",
    "provider",
    "base_url",
    "user_agent",
    "email",
    "timeout_sec",
}
_GEOCODING_SETTING_INT_KEYS = {"timeout_sec"}
_GEOCODING_SETTING_BOOL_KEYS = {"enabled"}

_RADIUS_SETTING_KEYS = {
    "auth_server_id",
    "auth_shared_secret",
    "auth_dictionary_path",
    "auth_timeout_sec",
    "default_auth_port",
    "default_acct_port",
    "default_sync_users",
    "default_sync_nas_clients",
    "default_sync_status",
}
_RADIUS_SETTING_INT_KEYS = {"auth_timeout_sec", "default_auth_port", "default_acct_port"}
_RADIUS_SETTING_SECRET_KEYS = {"auth_shared_secret"}
_RADIUS_SETTING_BOOL_KEYS = {"default_sync_users", "default_sync_nas_clients"}
_RADIUS_SETTING_STATUS_KEYS = {"default_sync_status"}


def _domain_allowed_keys(domain: SettingDomain) -> str:
    specs = settings_spec.list_specs(domain)
    return ", ".join(sorted(spec.key for spec in specs))


def _normalize_spec_setting(
    domain: SettingDomain, key: str, payload: DomainSettingUpdate
) -> DomainSettingUpdate:
    spec = settings_spec.get_spec(domain, key)
    if not spec:
        allowed = _domain_allowed_keys(domain)
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    value = payload.value_text if payload.value_text is not None else payload.value_json
    if value is None:
        raise HTTPException(status_code=400, detail="Value required")
    coerced, error = settings_spec.coerce_value(spec, value)
    if error:
        raise HTTPException(status_code=400, detail=error)
    if spec.allowed and coerced not in spec.allowed:
        allowed = ", ".join(sorted(spec.allowed))
        raise HTTPException(
            status_code=400, detail=f"Value must be one of: {allowed}"
        )
    if spec.value_type == SettingValueType.integer:
        try:
            parsed = int(coerced)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Value must be an integer") from exc
        if spec.min_value is not None and parsed < spec.min_value:
            raise HTTPException(
                status_code=400, detail=f"Value must be >= {spec.min_value}"
            )
        if spec.max_value is not None and parsed > spec.max_value:
            raise HTTPException(
                status_code=400, detail=f"Value must be <= {spec.max_value}"
            )
        coerced = parsed
    value_text, value_json = settings_spec.normalize_for_db(spec, coerced)
    data = payload.model_dump(exclude_unset=True)
    data["value_type"] = spec.value_type
    data["value_text"] = value_text
    data["value_json"] = value_json
    if spec.is_secret:
        data["is_secret"] = True
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
    items = _list_domain_settings(db, domain, is_active, order_by, order_dir, limit, offset)
    return list_response(items, limit, offset)


def _upsert_domain_setting(
    db: Session, domain: SettingDomain, key: str, payload: DomainSettingUpdate
):
    normalized_payload = _normalize_spec_setting(domain, key, payload)
    service = settings_spec.DOMAIN_SETTINGS_SERVICE.get(domain)
    if not service:
        raise HTTPException(status_code=400, detail="Unknown settings domain")
    return service.upsert_by_key(db, key, normalized_payload)


def _get_domain_setting(db: Session, domain: SettingDomain, key: str):
    spec = settings_spec.get_spec(domain, key)
    if not spec:
        allowed = _domain_allowed_keys(domain)
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    service = settings_spec.DOMAIN_SETTINGS_SERVICE.get(domain)
    if not service:
        raise HTTPException(status_code=400, detail="Unknown settings domain")
    return service.get_by_key(db, key)


def list_gis_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return settings_service.gis_settings.list(
        db, None, is_active, order_by, order_dir, limit, offset
    )


def list_gis_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    items = list_gis_settings(db, is_active, order_by, order_dir, limit, offset)
    return list_response(items, limit, offset)


def _normalize_gis_setting(key: str, payload: DomainSettingUpdate) -> DomainSettingUpdate:
    if key not in _GIS_SETTING_KEYS:
        allowed = ", ".join(sorted(_GIS_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    value = payload.value_text if payload.value_text is not None else payload.value_json
    if value is None:
        raise HTTPException(status_code=400, detail="Value required")
    data = payload.model_dump(exclude_unset=True)
    if key == "sync_interval_minutes":
        try:
            minutes = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Value must be an integer") from exc
        if minutes < 1:
            raise HTTPException(status_code=400, detail="Value must be >= 1")
        data["value_type"] = SettingValueType.integer
        data["value_text"] = str(minutes)
        data["value_json"] = None
    else:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                bool_value = True
            elif normalized in {"0", "false", "no", "off"}:
                bool_value = False
            else:
                raise HTTPException(status_code=400, detail="Value must be boolean")
        elif isinstance(value, bool):
            bool_value = value
        else:
            raise HTTPException(status_code=400, detail="Value must be boolean")
        data["value_type"] = SettingValueType.boolean
        data["value_text"] = "true" if bool_value else "false"
        data["value_json"] = bool_value
    return DomainSettingUpdate(**data)


def upsert_gis_setting(db: Session, key: str, payload: DomainSettingUpdate):
    normalized_payload = _normalize_gis_setting(key, payload)
    return settings_service.gis_settings.upsert_by_key(db, key, normalized_payload)


def get_gis_setting(db: Session, key: str):
    if key not in _GIS_SETTING_KEYS:
        allowed = ", ".join(sorted(_GIS_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    return settings_service.gis_settings.get_by_key(db, key)


def list_geocoding_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return settings_service.geocoding_settings.list(
        db, None, is_active, order_by, order_dir, limit, offset
    )


def list_geocoding_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    items = list_geocoding_settings(db, is_active, order_by, order_dir, limit, offset)
    return list_response(items, limit, offset)


def _normalize_geocoding_setting(
    key: str, payload: DomainSettingUpdate
) -> DomainSettingUpdate:
    if key not in _GEOCODING_SETTING_KEYS:
        allowed = ", ".join(sorted(_GEOCODING_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    value = payload.value_text if payload.value_text is not None else payload.value_json
    if value is None:
        raise HTTPException(status_code=400, detail="Value required")
    data = payload.model_dump(exclude_unset=True)
    if key in _GEOCODING_SETTING_INT_KEYS:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Value must be an integer") from exc
        if parsed < 1:
            raise HTTPException(status_code=400, detail="Value must be >= 1")
        data["value_type"] = SettingValueType.integer
        data["value_text"] = str(parsed)
        data["value_json"] = None
    elif key in _GEOCODING_SETTING_BOOL_KEYS:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                bool_value = True
            elif normalized in {"0", "false", "no", "off"}:
                bool_value = False
            else:
                raise HTTPException(status_code=400, detail="Value must be boolean")
        elif isinstance(value, bool):
            bool_value = value
        else:
            raise HTTPException(status_code=400, detail="Value must be boolean")
        data["value_type"] = SettingValueType.boolean
        data["value_text"] = "true" if bool_value else "false"
        data["value_json"] = bool_value
    else:
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail="Value must be a string")
        data["value_type"] = SettingValueType.string
        data["value_text"] = value
        data["value_json"] = None
    return DomainSettingUpdate(**data)


def upsert_geocoding_setting(db: Session, key: str, payload: DomainSettingUpdate):
    normalized_payload = _normalize_geocoding_setting(key, payload)
    return settings_service.geocoding_settings.upsert_by_key(db, key, normalized_payload)


def get_geocoding_setting(db: Session, key: str):
    if key not in _GEOCODING_SETTING_KEYS:
        allowed = ", ".join(sorted(_GEOCODING_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    return settings_service.geocoding_settings.get_by_key(db, key)


def list_radius_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return settings_service.radius_settings.list(
        db, None, is_active, order_by, order_dir, limit, offset
    )


def list_radius_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    items = list_radius_settings(db, is_active, order_by, order_dir, limit, offset)
    return list_response(items, limit, offset)


def _normalize_radius_setting(key: str, payload: DomainSettingUpdate) -> DomainSettingUpdate:
    if key not in _RADIUS_SETTING_KEYS:
        allowed = ", ".join(sorted(_RADIUS_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    value = payload.value_text if payload.value_text is not None else payload.value_json
    if value is None:
        raise HTTPException(status_code=400, detail="Value required")
    data = payload.model_dump(exclude_unset=True)
    if key in _RADIUS_SETTING_INT_KEYS:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Value must be an integer") from exc
        if parsed < 1:
            raise HTTPException(status_code=400, detail="Value must be >= 1")
        data["value_type"] = SettingValueType.integer
        data["value_text"] = str(parsed)
        data["value_json"] = None
    elif key in _RADIUS_SETTING_BOOL_KEYS:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                bool_value = True
            elif normalized in {"0", "false", "no", "off"}:
                bool_value = False
            else:
                raise HTTPException(status_code=400, detail="Value must be boolean")
        elif isinstance(value, bool):
            bool_value = value
        else:
            raise HTTPException(status_code=400, detail="Value must be boolean")
        data["value_type"] = SettingValueType.boolean
        data["value_text"] = "true" if bool_value else "false"
        data["value_json"] = bool_value
    elif key in _RADIUS_SETTING_STATUS_KEYS:
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail="Value must be a string")
        normalized = value.strip().lower()
        if normalized not in {"running", "success", "failed"}:
            raise HTTPException(
                status_code=400, detail="Value must be running, success, or failed"
            )
        data["value_type"] = SettingValueType.string
        data["value_text"] = normalized
        data["value_json"] = None
    else:
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail="Value must be a string")
        data["value_type"] = SettingValueType.string
        data["value_text"] = value
        data["value_json"] = None
    if key in _RADIUS_SETTING_SECRET_KEYS:
        data["is_secret"] = True
    return DomainSettingUpdate(**data)


def upsert_radius_setting(db: Session, key: str, payload: DomainSettingUpdate):
    normalized_payload = _normalize_radius_setting(key, payload)
    return settings_service.radius_settings.upsert_by_key(db, key, normalized_payload)


def get_radius_setting(db: Session, key: str):
    if key not in _RADIUS_SETTING_KEYS:
        allowed = ", ".join(sorted(_RADIUS_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    return settings_service.radius_settings.get_by_key(db, key)


def list_auth_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return settings_service.auth_settings.list(
        db, None, is_active, order_by, order_dir, limit, offset
    )


def list_auth_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    items = list_auth_settings(db, is_active, order_by, order_dir, limit, offset)
    return list_response(items, limit, offset)


def _normalize_auth_setting(key: str, payload: DomainSettingUpdate) -> DomainSettingUpdate:
    if key not in _AUTH_SETTING_KEYS:
        allowed = ", ".join(sorted(_AUTH_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    value = payload.value_text if payload.value_text is not None else payload.value_json
    if value is None:
        raise HTTPException(status_code=400, detail="Value required")
    data = payload.model_dump(exclude_unset=True)
    if key in _AUTH_SETTING_INT_KEYS:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Value must be an integer") from exc
        if parsed < 1:
            raise HTTPException(status_code=400, detail="Value must be >= 1")
        data["value_type"] = SettingValueType.integer
        data["value_text"] = str(parsed)
        data["value_json"] = None
    elif key in _AUTH_SETTING_BOOL_KEYS:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                bool_value = True
            elif normalized in {"0", "false", "no", "off"}:
                bool_value = False
            else:
                raise HTTPException(status_code=400, detail="Value must be boolean")
        elif isinstance(value, bool):
            bool_value = value
        else:
            raise HTTPException(status_code=400, detail="Value must be boolean")
        data["value_type"] = SettingValueType.boolean
        data["value_text"] = "true" if bool_value else "false"
        data["value_json"] = bool_value
    elif key == "refresh_cookie_samesite":
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail="Value must be a string")
        normalized = value.strip().lower()
        if normalized not in {"lax", "strict", "none"}:
            raise HTTPException(
                status_code=400, detail="Value must be lax, strict, or none"
            )
        data["value_type"] = SettingValueType.string
        data["value_text"] = normalized
        data["value_json"] = None
    else:
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail="Value must be a string")
        data["value_type"] = SettingValueType.string
        data["value_text"] = value
        data["value_json"] = None
    if key in _AUTH_SETTING_SECRET_KEYS:
        data["is_secret"] = True
    return DomainSettingUpdate(**data)


def upsert_auth_setting(db: Session, key: str, payload: DomainSettingUpdate):
    normalized_payload = _normalize_auth_setting(key, payload)
    return settings_service.auth_settings.upsert_by_key(db, key, normalized_payload)


def get_auth_setting(db: Session, key: str):
    if key not in _AUTH_SETTING_KEYS:
        allowed = ", ".join(sorted(_AUTH_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    return settings_service.auth_settings.get_by_key(db, key)


def list_audit_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return settings_service.audit_settings.list(
        db, None, is_active, order_by, order_dir, limit, offset
    )


def list_audit_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    items = list_audit_settings(db, is_active, order_by, order_dir, limit, offset)
    return list_response(items, limit, offset)


def _normalize_audit_setting(key: str, payload: DomainSettingUpdate) -> DomainSettingUpdate:
    if key not in _AUDIT_SETTING_KEYS:
        allowed = ", ".join(sorted(_AUDIT_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    value = payload.value_text if payload.value_text is not None else payload.value_json
    if value is None:
        raise HTTPException(status_code=400, detail="Value required")
    data = payload.model_dump(exclude_unset=True)
    if key in _AUDIT_SETTING_BOOL_KEYS:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                bool_value = True
            elif normalized in {"0", "false", "no", "off"}:
                bool_value = False
            else:
                raise HTTPException(status_code=400, detail="Value must be boolean")
        elif isinstance(value, bool):
            bool_value = value
        else:
            raise HTTPException(status_code=400, detail="Value must be boolean")
        data["value_type"] = SettingValueType.boolean
        data["value_text"] = "true" if bool_value else "false"
        data["value_json"] = bool_value
    elif key in _AUDIT_SETTING_LIST_KEYS:
        items: list[str]
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
        elif isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
        else:
            raise HTTPException(status_code=400, detail="Value must be a list or string")
        data["value_type"] = SettingValueType.json
        data["value_text"] = None
        data["value_json"] = items
    else:
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail="Value must be a string")
        data["value_type"] = SettingValueType.string
        data["value_text"] = value
        data["value_json"] = None
    return DomainSettingUpdate(**data)


def upsert_audit_setting(db: Session, key: str, payload: DomainSettingUpdate):
    normalized_payload = _normalize_audit_setting(key, payload)
    return settings_service.audit_settings.upsert_by_key(db, key, normalized_payload)


def get_audit_setting(db: Session, key: str):
    if key not in _AUDIT_SETTING_KEYS:
        allowed = ", ".join(sorted(_AUDIT_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    return settings_service.audit_settings.get_by_key(db, key)


def list_imports_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return settings_service.imports_settings.list(
        db, None, is_active, order_by, order_dir, limit, offset
    )


def list_imports_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    items = list_imports_settings(db, is_active, order_by, order_dir, limit, offset)
    return list_response(items, limit, offset)


def _normalize_imports_setting(
    key: str, payload: DomainSettingUpdate
) -> DomainSettingUpdate:
    if key not in _IMPORTS_SETTING_KEYS:
        allowed = ", ".join(sorted(_IMPORTS_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    value = payload.value_text if payload.value_text is not None else payload.value_json
    if value is None:
        raise HTTPException(status_code=400, detail="Value required")
    data = payload.model_dump(exclude_unset=True)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Value must be an integer") from exc
    if parsed < 1:
        raise HTTPException(status_code=400, detail="Value must be >= 1")
    data["value_type"] = SettingValueType.integer
    data["value_text"] = str(parsed)
    data["value_json"] = None
    return DomainSettingUpdate(**data)


def upsert_imports_setting(db: Session, key: str, payload: DomainSettingUpdate):
    normalized_payload = _normalize_imports_setting(key, payload)
    return settings_service.imports_settings.upsert_by_key(db, key, normalized_payload)


def get_imports_setting(db: Session, key: str):
    if key not in _IMPORTS_SETTING_KEYS:
        allowed = ", ".join(sorted(_IMPORTS_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    return settings_service.imports_settings.get_by_key(db, key)


def list_notification_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return settings_service.notification_settings.list(
        db, None, is_active, order_by, order_dir, limit, offset
    )


def list_notification_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    items = list_notification_settings(db, is_active, order_by, order_dir, limit, offset)
    return list_response(items, limit, offset)


def _normalize_notification_setting(
    key: str, payload: DomainSettingUpdate
) -> DomainSettingUpdate:
    if key not in _NOTIFICATION_SETTING_KEYS:
        allowed = ", ".join(sorted(_NOTIFICATION_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    value = payload.value_text if payload.value_text is not None else payload.value_json
    if value is None:
        raise HTTPException(status_code=400, detail="Value required")
    data = payload.model_dump(exclude_unset=True)
    if key in _NOTIFICATION_SETTING_INT_KEYS:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Value must be an integer") from exc
        if parsed < 0:
            raise HTTPException(status_code=400, detail="Value must be >= 0")
        data["value_type"] = SettingValueType.integer
        data["value_text"] = str(parsed)
        data["value_json"] = None
    elif key in _NOTIFICATION_SETTING_BOOL_KEYS:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                bool_value = True
            elif normalized in {"0", "false", "no", "off"}:
                bool_value = False
            else:
                raise HTTPException(status_code=400, detail="Value must be boolean")
        elif isinstance(value, bool):
            bool_value = value
        else:
            raise HTTPException(status_code=400, detail="Value must be boolean")
        data["value_type"] = SettingValueType.boolean
        data["value_text"] = "true" if bool_value else "false"
        data["value_json"] = bool_value
    else:
        data["value_type"] = SettingValueType.string
        data["value_text"] = str(value)
        data["value_json"] = None
    return DomainSettingUpdate(**data)


def upsert_notification_setting(db: Session, key: str, payload: DomainSettingUpdate):
    normalized_payload = _normalize_notification_setting(key, payload)
    return settings_service.notification_settings.upsert_by_key(
        db, key, normalized_payload
    )


def get_notification_setting(db: Session, key: str):
    if key not in _NOTIFICATION_SETTING_KEYS:
        allowed = ", ".join(sorted(_NOTIFICATION_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    return settings_service.notification_settings.get_by_key(db, key)


def list_scheduler_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return settings_service.scheduler_settings.list(
        db, None, is_active, order_by, order_dir, limit, offset
    )


def list_scheduler_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    items = list_scheduler_settings(db, is_active, order_by, order_dir, limit, offset)
    return list_response(items, limit, offset)


def _normalize_scheduler_setting(
    key: str, payload: DomainSettingUpdate
) -> DomainSettingUpdate:
    if key not in _SCHEDULER_SETTING_KEYS:
        allowed = ", ".join(sorted(_SCHEDULER_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    value = payload.value_text if payload.value_text is not None else payload.value_json
    if value is None:
        raise HTTPException(status_code=400, detail="Value required")
    data = payload.model_dump(exclude_unset=True)
    if key in {"beat_max_loop_interval", "beat_refresh_seconds"}:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Value must be an integer") from exc
        if parsed < 1:
            raise HTTPException(status_code=400, detail="Value must be >= 1")
        data["value_type"] = SettingValueType.integer
        data["value_text"] = str(parsed)
        data["value_json"] = None
        return DomainSettingUpdate(**data)
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="Value must be a string")
    data["value_type"] = SettingValueType.string
    data["value_text"] = value
    data["value_json"] = None
    return DomainSettingUpdate(**data)


def upsert_scheduler_setting(db: Session, key: str, payload: DomainSettingUpdate):
    normalized_payload = _normalize_scheduler_setting(key, payload)
    return settings_service.scheduler_settings.upsert_by_key(db, key, normalized_payload)


def get_scheduler_setting(db: Session, key: str):
    if key not in _SCHEDULER_SETTING_KEYS:
        allowed = ", ".join(sorted(_SCHEDULER_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    return settings_service.scheduler_settings.get_by_key(db, key)


def list_billing_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.billing, is_active, order_by, order_dir, limit, offset
    )


def list_billing_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.billing, is_active, order_by, order_dir, limit, offset
    )


def upsert_billing_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.billing, key, payload)


def get_billing_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.billing, key)


def list_catalog_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.catalog, is_active, order_by, order_dir, limit, offset
    )


def list_catalog_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.catalog, is_active, order_by, order_dir, limit, offset
    )


def upsert_catalog_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.catalog, key, payload)


def get_catalog_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.catalog, key)


def list_subscriber_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.subscriber, is_active, order_by, order_dir, limit, offset
    )


def list_subscriber_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.subscriber, is_active, order_by, order_dir, limit, offset
    )


def upsert_subscriber_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.subscriber, key, payload)


def get_subscriber_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.subscriber, key)


def list_usage_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.usage, is_active, order_by, order_dir, limit, offset
    )


def list_usage_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.usage, is_active, order_by, order_dir, limit, offset
    )


def upsert_usage_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.usage, key, payload)


def get_usage_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.usage, key)


def list_collections_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.collections, is_active, order_by, order_dir, limit, offset
    )


def list_collections_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.collections, is_active, order_by, order_dir, limit, offset
    )


def upsert_collections_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.collections, key, payload)


def get_collections_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.collections, key)


def list_provisioning_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.provisioning, is_active, order_by, order_dir, limit, offset
    )


def list_provisioning_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.provisioning, is_active, order_by, order_dir, limit, offset
    )


def upsert_provisioning_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.provisioning, key, payload)


def get_provisioning_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.provisioning, key)


def list_network_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.network, is_active, order_by, order_dir, limit, offset
    )


def list_network_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.network, is_active, order_by, order_dir, limit, offset
    )


def upsert_network_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.network, key, payload)


def get_network_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.network, key)


def list_inventory_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.inventory, is_active, order_by, order_dir, limit, offset
    )


def list_inventory_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.inventory, is_active, order_by, order_dir, limit, offset
    )


def upsert_inventory_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.inventory, key, payload)


def get_inventory_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.inventory, key)


def list_lifecycle_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.lifecycle, is_active, order_by, order_dir, limit, offset
    )


def list_lifecycle_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.lifecycle, is_active, order_by, order_dir, limit, offset
    )


def upsert_lifecycle_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.lifecycle, key, payload)


def get_lifecycle_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.lifecycle, key)


def list_comms_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.comms, is_active, order_by, order_dir, limit, offset
    )


def list_comms_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.comms, is_active, order_by, order_dir, limit, offset
    )


def upsert_comms_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.comms, key, payload)


def get_comms_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.comms, key)
