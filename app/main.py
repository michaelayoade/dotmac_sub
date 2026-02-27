import logging
import secrets
from threading import Lock
from time import monotonic

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.api.analytics import router as analytics_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.auth_flow import limiter as auth_flow_limiter
from app.api.auth_flow import router as auth_flow_router
from app.api.bandwidth import router as bandwidth_router
from app.api.billing import router as billing_router
from app.api.catalog import router as catalog_router
from app.api.comms import router as comms_router
from app.api.connectors import router as connectors_router
from app.api.customers import router as customers_router
from app.api.defaults import router as defaults_router
from app.api.deps import require_role, require_user_auth
from app.api.domains import router as domains_router
from app.api.domains_monitoring import router as domains_monitoring_router
from app.api.domains_network_access import router as domains_network_access_router
from app.api.domains_network_fiber import router as domains_network_fiber_router
from app.api.domains_provisioning import router as domains_provisioning_router
from app.api.domains_usage import router as domains_usage_router
from app.api.external import router as external_router
from app.api.fiber_plant import router as fiber_plant_router
from app.api.files import router as files_router
from app.api.geocoding import router as geocoding_router
from app.api.gis import router as gis_router
from app.api.imports import router as imports_router
from app.api.integrations import router as integrations_router
from app.api.nas import router as nas_router
from app.api.nextcloud_talk import router as nextcloud_talk_router
from app.api.notifications import router as notifications_router
from app.api.provisioning import router as provisioning_api_router
from app.api.qualification import router as qualification_router
from app.api.rbac import router as rbac_router
from app.api.scheduler import router as scheduler_router
from app.api.search import router as search_router
from app.api.settings import router as settings_router
from app.api.subscribers import router as subscriber_router
from app.api.tables import router as tables_router
from app.api.validation import router as validation_router
from app.api.webhooks import router as webhooks_router
from app.api.wireguard import public_router as wireguard_public_router
from app.api.wireguard import router as wireguard_router
from app.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    generate_csrf_token,
    set_csrf_cookie,
)
from app.db import SessionLocal
from app.errors import register_error_handlers
from app.logging import configure_logging
from app.models.domain_settings import DomainSetting, SettingDomain
from app.observability import ObservabilityMiddleware
from app.services import audit as audit_service
from app.services.object_storage import ensure_storage_bucket
from app.services.settings_seed import (
    seed_audit_settings,
    seed_auth_policy_settings,
    seed_auth_settings,
    seed_billing_settings,
    seed_catalog_settings,
    seed_collections_policy_settings,
    seed_collections_settings,
    seed_comms_settings,
    seed_geocoding_settings,
    seed_gis_settings,
    seed_imports_settings,
    seed_lifecycle_settings,
    seed_network_monitoring_settings,
    seed_network_policy_settings,
    seed_network_settings,
    seed_notification_settings,
    seed_provisioning_settings,
    seed_radius_policy_settings,
    seed_radius_settings,
    seed_scheduler_settings,
    seed_subscriber_settings,
    seed_tr069_settings,
    seed_usage_policy_settings,
    seed_usage_settings,
    seed_wireguard_settings,
)
from app.telemetry import setup_otel
from app.web import router as web_router
from app.web_domains import router as web_domains_router
from app.web_home import router as web_home_router
from app.websocket.router import router as ws_router

app = FastAPI(title="dotmac_sm API")
logger = logging.getLogger(__name__)
app.state.limiter = auth_flow_limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_AUDIT_SETTINGS_CACHE: dict | None = None
_AUDIT_SETTINGS_CACHE_AT: float | None = None
_AUDIT_SETTINGS_CACHE_TTL_SECONDS = 30.0
_AUDIT_SETTINGS_LOCK = Lock()
configure_logging()
setup_otel(app)
app.add_middleware(ObservabilityMiddleware)
register_error_handlers(app)


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    response: Response
    path = request.url.path
    db = SessionLocal()
    try:
        audit_settings = _load_audit_settings(db)
    finally:
        db.close()
    if not audit_settings["enabled"]:
        return await call_next(request)
    track_read = request.method == "GET" and (
        request.headers.get(audit_settings["read_trigger_header"], "").lower() == "true"
        or request.query_params.get(audit_settings["read_trigger_query"]) == "true"
    )
    should_log = request.method in audit_settings["methods"] or track_read
    if _is_audit_path_skipped(path, audit_settings["skip_paths"]):
        should_log = False
    try:
        response = await call_next(request)
    except Exception:
        if should_log:
            db = SessionLocal()
            try:
                audit_service.audit_events.log_request(
                    db, request, Response(status_code=500)
                )
            finally:
                db.close()
        raise
    if should_log:
        db = SessionLocal()
        try:
            audit_service.audit_events.log_request(db, request, response)
        finally:
            db.close()
    return response


# CSRF Protection paths - protect all web portals and auth forms
_CSRF_PROTECTED_PATHS = ["/admin/", "/web/", "/portal/", "/reseller/", "/auth/"]
_CSRF_EXEMPT_PATHS = ["/api/", "/health", "/metrics", "/static/"]


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    """
    CSRF protection middleware using double-submit cookie pattern.

    For GET requests: Sets CSRF cookie if not present.
    For POST/PUT/DELETE on protected paths: Validates CSRF token.
    """
    path = request.url.path
    method = request.method.upper()

    # Skip CSRF for exempt paths
    if any(path.startswith(exempt) for exempt in _CSRF_EXEMPT_PATHS):
        return await call_next(request)

    # Check if path needs CSRF protection
    needs_protection = any(path.startswith(protected) for protected in _CSRF_PROTECTED_PATHS)

    if not needs_protection:
        return await call_next(request)

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    generated_token: str | None = None

    if not cookie_token:
        generated_token = generate_csrf_token()
        request.state.csrf_token = generated_token
    else:
        request.state.csrf_token = cookie_token

    # For state-changing methods, validate CSRF token
    if method in ("POST", "PUT", "DELETE", "PATCH"):
        from fastapi.responses import HTMLResponse

        def _csrf_forbidden(message: str) -> HTMLResponse:
            return HTMLResponse(
                content=f"<h1>403 Forbidden</h1><p>{message}</p>",
                status_code=403,
            )

        if not cookie_token:
            # No CSRF cookie - reject request
            return _csrf_forbidden("CSRF token missing. Please refresh the page and try again.")

        # Check header first (for HTMX/fetch requests)
        header_token = request.headers.get(CSRF_HEADER_NAME)
        if header_token:
            if not secrets.compare_digest(cookie_token, header_token):
                return _csrf_forbidden("CSRF token invalid. Please refresh the page and try again.")
        else:
            # For form submissions, check form data
            content_type = request.headers.get("content-type", "")
            if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
                # Read body and check token
                body = await request.body()

                # Parse form data to get CSRF token
                from urllib.parse import parse_qs
                form_token: str | None = None
                try:
                    if "multipart/form-data" in content_type:
                        # Parse multipart form data properly using email.parser
                        # Extract boundary from content-type header
                        import re
                        from email.parser import BytesParser
                        from email.policy import HTTP
                        boundary_match = re.search(r'boundary=([^\s;]+)', content_type)
                        if boundary_match:
                            boundary = boundary_match.group(1).strip('"')
                            # Construct a valid MIME message for parsing
                            mime_header = f"Content-Type: multipart/form-data; boundary={boundary}\r\n\r\n"
                            mime_message = mime_header.encode('utf-8') + body

                            parser = BytesParser(policy=HTTP)
                            msg = parser.parsebytes(mime_message)

                            # Walk through all parts to find CSRF token field
                            if msg.is_multipart():
                                for part in msg.iter_parts():
                                    content_disp = part.get("Content-Disposition", "")
                                    if (
                                        'name="_csrf_token"' in content_disp
                                        or "name=_csrf_token" in content_disp
                                    ):
                                        payload = part.get_payload(decode=True)
                                        if isinstance(payload, (bytes, bytearray)):
                                            form_token = payload.decode("utf-8", errors="ignore").strip()
                                            break
                                        if isinstance(payload, str) and payload.strip():
                                            form_token = payload.strip()
                                            break
                    else:
                        form_data = parse_qs(body.decode('utf-8'))
                        form_token = form_data.get("_csrf_token", [None])[0]

                    if not form_token:
                        return _csrf_forbidden("CSRF token missing. Please refresh the page and try again.")
                    if not secrets.compare_digest(cookie_token, form_token):
                        return _csrf_forbidden("CSRF token invalid. Please refresh the page and try again.")
                except Exception:
                    return _csrf_forbidden("CSRF token invalid. Please refresh the page and try again.")

                # Reconstruct request with body for downstream handlers
                async def receive():
                    return {"type": "http.request", "body": body}
                request = Request(scope=request.scope, receive=receive)
            else:
                # Non-form state-changing requests must use header token.
                return _csrf_forbidden("CSRF token missing. Please refresh the page and try again.")

    response = await call_next(request)

    # Set CSRF cookie on responses if not present
    if generated_token:
        set_csrf_cookie(response, generated_token, request)

    return response


def _load_audit_settings(db: Session):
    global _AUDIT_SETTINGS_CACHE, _AUDIT_SETTINGS_CACHE_AT
    now = monotonic()
    with _AUDIT_SETTINGS_LOCK:
        if (
            _AUDIT_SETTINGS_CACHE
            and _AUDIT_SETTINGS_CACHE_AT
            and now - _AUDIT_SETTINGS_CACHE_AT < _AUDIT_SETTINGS_CACHE_TTL_SECONDS
        ):
            return _AUDIT_SETTINGS_CACHE
    defaults = {
        "enabled": True,
        "methods": {"POST", "PUT", "PATCH", "DELETE"},
        "skip_paths": ["/static", "/web", "/health"],
        "read_trigger_header": "x-audit-read",
        "read_trigger_query": "audit",
    }
    rows = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.audit)
        .filter(DomainSetting.is_active.is_(True))
        .all()
    )
    values = {row.key: row for row in rows}
    if "enabled" in values:
        defaults["enabled"] = _to_bool(values["enabled"])
    if "methods" in values:
        defaults["methods"] = _to_list(values["methods"], upper=True)
    if "skip_paths" in values:
        defaults["skip_paths"] = _to_list(values["skip_paths"], upper=False)
    if "read_trigger_header" in values:
        defaults["read_trigger_header"] = _to_str(values["read_trigger_header"])
    if "read_trigger_query" in values:
        defaults["read_trigger_query"] = _to_str(values["read_trigger_query"])
    with _AUDIT_SETTINGS_LOCK:
        _AUDIT_SETTINGS_CACHE = defaults
        _AUDIT_SETTINGS_CACHE_AT = now
    return defaults


def _to_bool(setting: DomainSetting) -> bool:
    value = setting.value_json if setting.value_json is not None else setting.value_text
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _to_str(setting: DomainSetting) -> str:
    value = setting.value_text if setting.value_text is not None else setting.value_json
    if value is None:
        return ""
    return str(value)


def _to_list(setting: DomainSetting, upper: bool) -> set[str] | list[str]:
    value = setting.value_json if setting.value_json is not None else setting.value_text
    items: list[str]
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    else:
        items = []
    if upper:
        return {item.upper() for item in items}
    return items


def _is_audit_path_skipped(path: str, skip_paths: list[str]) -> bool:
    return any(path.startswith(prefix) for prefix in skip_paths)

def _include_api_router(router, dependencies=None):
    app.include_router(router, prefix="/api/v1", dependencies=dependencies)


_include_api_router(notifications_router, dependencies=[Depends(require_user_auth)])
_include_api_router(external_router, dependencies=[Depends(require_user_auth)])
_include_api_router(billing_router, dependencies=[Depends(require_user_auth)])
_include_api_router(files_router, dependencies=[Depends(require_user_auth)])
_include_api_router(catalog_router, dependencies=[Depends(require_user_auth)])
_include_api_router(auth_router, dependencies=[Depends(require_role("admin"))])
# Only include auth_flow at /api/v1 to avoid conflict with web /auth/login
app.include_router(auth_flow_router, prefix="/api/v1")
_include_api_router(rbac_router, dependencies=[Depends(require_user_auth)])
_include_api_router(customers_router, dependencies=[Depends(require_user_auth)])
_include_api_router(search_router, dependencies=[Depends(require_user_auth)])
_include_api_router(subscriber_router, dependencies=[Depends(require_user_auth)])
_include_api_router(tables_router, dependencies=[Depends(require_user_auth)])
_include_api_router(domains_router, dependencies=[Depends(require_user_auth)])
_include_api_router(domains_provisioning_router, dependencies=[Depends(require_user_auth)])
_include_api_router(domains_monitoring_router, dependencies=[Depends(require_user_auth)])
_include_api_router(domains_network_access_router, dependencies=[Depends(require_user_auth)])
_include_api_router(domains_network_fiber_router, dependencies=[Depends(require_user_auth)])
_include_api_router(domains_usage_router, dependencies=[Depends(require_user_auth)])
_include_api_router(imports_router, dependencies=[Depends(require_user_auth)])
_include_api_router(audit_router)
_include_api_router(gis_router, dependencies=[Depends(require_user_auth)])
_include_api_router(geocoding_router, dependencies=[Depends(require_user_auth)])
_include_api_router(qualification_router, dependencies=[Depends(require_user_auth)])
_include_api_router(settings_router, dependencies=[Depends(require_user_auth)])
_include_api_router(webhooks_router, dependencies=[Depends(require_user_auth)])
_include_api_router(connectors_router, dependencies=[Depends(require_user_auth)])
_include_api_router(integrations_router, dependencies=[Depends(require_user_auth)])
_include_api_router(scheduler_router, dependencies=[Depends(require_user_auth)])
_include_api_router(comms_router, dependencies=[Depends(require_user_auth)])
_include_api_router(analytics_router, dependencies=[Depends(require_user_auth)])
_include_api_router(fiber_plant_router, dependencies=[Depends(require_user_auth)])
_include_api_router(nextcloud_talk_router, dependencies=[Depends(require_user_auth)])
_include_api_router(wireguard_router, dependencies=[Depends(require_user_auth)])
_include_api_router(nas_router, dependencies=[Depends(require_user_auth)])
_include_api_router(provisioning_api_router, dependencies=[Depends(require_user_auth)])
_include_api_router(bandwidth_router, dependencies=[Depends(require_user_auth)])
_include_api_router(validation_router, dependencies=[Depends(require_user_auth)])
_include_api_router(defaults_router, dependencies=[Depends(require_user_auth)])
# WireGuard provisioning public endpoints - no auth required (token-based)
_include_api_router(wireguard_public_router)
app.include_router(web_home_router)
app.include_router(web_domains_router)
app.include_router(web_router)

app.include_router(ws_router)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


@app.on_event("startup")
def _start_jobs():
    try:
        ensure_storage_bucket()
    except Exception:
        logger.exception("Failed to ensure storage bucket during startup")
    db = SessionLocal()
    try:
        seed_auth_settings(db)
        seed_auth_policy_settings(db)
        seed_audit_settings(db)
        seed_billing_settings(db)
        seed_catalog_settings(db)
        seed_imports_settings(db)
        seed_gis_settings(db)
        seed_usage_settings(db)
        seed_usage_policy_settings(db)
        seed_notification_settings(db)
        seed_collections_settings(db)
        seed_collections_policy_settings(db)
        seed_geocoding_settings(db)
        seed_radius_settings(db)
        seed_radius_policy_settings(db)
        seed_scheduler_settings(db)
        seed_subscriber_settings(db)
        seed_provisioning_settings(db)
        seed_tr069_settings(db)
        seed_network_policy_settings(db)
        seed_network_settings(db)
        seed_network_monitoring_settings(db)
        seed_lifecycle_settings(db)
        seed_comms_settings(db)
        seed_wireguard_settings(db)
    finally:
        db.close()


@app.on_event("startup")
async def _start_websocket_manager():
    from app.websocket.manager import get_connection_manager
    manager = get_connection_manager()
    await manager.connect()


@app.on_event("shutdown")
async def _stop_websocket_manager():
    from app.websocket.manager import get_connection_manager
    manager = get_connection_manager()
    await manager.disconnect()
