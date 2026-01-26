from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.schemas.common import ListResponse
from app.api.response import list_response

from app.db import SessionLocal
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingRead, DomainSettingUpdate
from app.services import domain_settings as settings_service

router = APIRouter(prefix="/settings", tags=["settings"])

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
_SCHEDULER_SETTING_KEYS = {"broker_url", "result_backend", "timezone"}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/gis", response_model=ListResponse[DomainSettingRead], tags=["settings-gis"])
def list_gis_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = settings_service.gis_settings.list(
        db, None, is_active, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.put(
    "/gis/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-gis"],
)
def upsert_gis_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
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
    normalized_payload = DomainSettingUpdate(**data)
    return settings_service.gis_settings.upsert_by_key(db, key, normalized_payload)


def _normalize_auth_setting(key: str, payload: DomainSettingUpdate) -> DomainSettingUpdate:
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


def _normalize_audit_setting(key: str, payload: DomainSettingUpdate) -> DomainSettingUpdate:
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


                def _normalize_imports_setting(
                key: str, payload: DomainSettingUpdate
                ) -> DomainSettingUpdate:
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


                def _normalize_scheduler_setting(
                key: str, payload: DomainSettingUpdate
                ) -> DomainSettingUpdate:
                value = payload.value_text if payload.value_text is not None else payload.value_json
                if value is None:
                raise HTTPException(status_code=400, detail="Value required")
                if not isinstance(value, str):
                raise HTTPException(status_code=400, detail="Value must be a string")
                data = payload.model_dump(exclude_unset=True)
                data["value_type"] = SettingValueType.string
                data["value_text"] = value
                data["value_json"] = None
                return DomainSettingUpdate(**data)


                @router.get(
                "/gis/{key}",
                response_model=DomainSettingRead,
                tags=["settings-gis"],
            )
                def get_gis_setting(key: str, db: Session = Depends(get_db)):
                if key not in _GIS_SETTING_KEYS:
                allowed = ", ".join(sorted(_GIS_SETTING_KEYS))
                raise HTTPException(
                status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
            )
                return settings_service.gis_settings.get_by_key(db, key)


                @router.get("/auth", response_model=ListResponse[DomainSettingRead], tags=["settings-auth"])
                def list_auth_settings(
                is_active: bool | None = None,
                order_by: str = Query(default="created_at"),
                order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
                limit: int = Query(default=200, ge=1, le=500),
                offset: int = Query(default=0, ge=0),
                db: Session = Depends(get_db),
                ):
                items = settings_service.auth_settings.list(
                db, None, is_active, order_by, order_dir, limit, offset
            )
    return list_response(items, limit, offset)


@router.put(
    "/auth/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-auth"],
)
def upsert_auth_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    if key not in _AUTH_SETTING_KEYS:
        allowed = ", ".join(sorted(_AUTH_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    normalized_payload = _normalize_auth_setting(key, payload)
    return settings_service.auth_settings.upsert_by_key(db, key, normalized_payload)


@router.get(
    "/auth/{key}",
    response_model=DomainSettingRead,
    tags=["settings-auth"],
)
def get_auth_setting(key: str, db: Session = Depends(get_db)):
    if key not in _AUTH_SETTING_KEYS:
        allowed = ", ".join(sorted(_AUTH_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    return settings_service.auth_settings.get_by_key(db, key)


@router.get("/audit", response_model=ListResponse[DomainSettingRead], tags=["settings-audit"])
def list_audit_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = settings_service.audit_settings.list(
        db, None, is_active, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.put(
    "/audit/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-audit"],
)
def upsert_audit_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    if key not in _AUDIT_SETTING_KEYS:
        allowed = ", ".join(sorted(_AUDIT_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    normalized_payload = _normalize_audit_setting(key, payload)
    return settings_service.audit_settings.upsert_by_key(db, key, normalized_payload)


@router.get(
    "/audit/{key}",
    response_model=DomainSettingRead,
    tags=["settings-audit"],
)
def get_audit_setting(key: str, db: Session = Depends(get_db)):
    if key not in _AUDIT_SETTING_KEYS:
        allowed = ", ".join(sorted(_AUDIT_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    return settings_service.audit_settings.get_by_key(db, key)


@router.get("/imports", response_model=ListResponse[DomainSettingRead], tags=["settings-imports"])
def list_imports_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = settings_service.imports_settings.list(
        db, None, is_active, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.put(
    "/imports/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-imports"],
)
def upsert_imports_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    if key not in _IMPORTS_SETTING_KEYS:
        allowed = ", ".join(sorted(_IMPORTS_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    normalized_payload = _normalize_imports_setting(key, payload)
    return settings_service.imports_settings.upsert_by_key(db, key, normalized_payload)


@router.get(
    "/imports/{key}",
    response_model=DomainSettingRead,
    tags=["settings-imports"],
)
def get_imports_setting(key: str, db: Session = Depends(get_db)):
    if key not in _IMPORTS_SETTING_KEYS:
        allowed = ", ".join(sorted(_IMPORTS_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    return settings_service.imports_settings.get_by_key(db, key)


@router.get(
    "/scheduler", response_model=ListResponse[DomainSettingRead], tags=["settings-scheduler"]
)
def list_scheduler_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = settings_service.scheduler_settings.list(
        db, None, is_active, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.put(
    "/scheduler/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-scheduler"],
)
def upsert_scheduler_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    if key not in _SCHEDULER_SETTING_KEYS:
        allowed = ", ".join(sorted(_SCHEDULER_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    normalized_payload = _normalize_scheduler_setting(key, payload)
    return settings_service.scheduler_settings.upsert_by_key(db, key, normalized_payload)


@router.get(
    "/scheduler/{key}",
    response_model=DomainSettingRead,
    tags=["settings-scheduler"],
)
def get_scheduler_setting(key: str, db: Session = Depends(get_db)):
    if key not in _SCHEDULER_SETTING_KEYS:
        allowed = ", ".join(sorted(_SCHEDULER_SETTING_KEYS))
        raise HTTPException(
            status_code=400, detail=f"Invalid setting key. Allowed: {allowed}"
        )
    return settings_service.scheduler_settings.get_by_key(db, key)
