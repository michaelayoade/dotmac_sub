import asyncio
import logging
import os
import secrets
import warnings
from contextlib import asynccontextmanager
from importlib import import_module
from threading import Lock
from time import monotonic
from typing import TypedDict

warnings.filterwarnings(
    "ignore",
    category=SyntaxWarning,
    module=r"routeros_api\.sentence",
)

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    generate_csrf_token,
    set_csrf_cookie,
)
from app.errors import register_error_handlers
from app.logging import configure_logging
from app.models.domain_settings import DomainSetting, SettingDomain
from app.monitoring import setup_monitoring
from app.observability import ObservabilityMiddleware
from app.services import audit as audit_service
from app.services.db_session_adapter import db_session_adapter
from app.telemetry import setup_otel

logger = logging.getLogger(__name__)

_AUDIT_SETTINGS_CACHE: dict | None = None
_AUDIT_SETTINGS_CACHE_AT: float | None = None
_AUDIT_SETTINGS_CACHE_TTL_SECONDS = 30.0
_AUDIT_SETTINGS_LOCK = Lock()
_DEFERRED_ROUTER_TASK = None

_CORE_ROUTER_SPECS = [
    ("app.api.health", "router", "api", "none"),
    ("app.web.auth", "router", "web", "none"),
]

_DEFERRED_API_ROUTER_SPECS = [
    ("app.web.admin", "router", "web", "none"),
    ("app.web_home", "router", "web", "none"),
    ("app.web_domains", "router", "web", "none"),
    ("app.web.customer", "router", "web", "none"),
    ("app.web.reseller", "router", "web", "none"),
    ("app.web.public", "router", "web", "none"),
    ("app.web.admin.network_routers", "router", "admin", "none"),
    ("app.websocket.router", "router", "ws", "none"),
    ("app.api.notifications", "router", "api", "user"),
    ("app.api.external", "router", "api", "user"),
    ("app.api.billing", "router", "api", "user"),
    ("app.api.files", "router", "api", "user"),
    ("app.api.catalog", "router", "api", "user"),
    ("app.api.auth", "router", "api", "admin"),
    ("app.api.auth_flow", "router", "api", "none"),
    ("app.api.rbac", "router", "api", "user"),
    ("app.api.customers", "router", "api", "user"),
    ("app.api.search", "router", "api", "user"),
    ("app.api.subscribers", "router", "api", "user"),
    ("app.api.support", "router", "api", "user"),
    ("app.api.tables", "router", "api", "user"),
    ("app.api.domains_provisioning", "router", "api", "user"),
    ("app.api.domains_monitoring", "router", "api", "user"),
    ("app.api.domains_network_access", "router", "api", "user"),
    ("app.api.network_ont_ops", "router", "api", "user"),
    ("app.api.network_olt_ops", "router", "api", "user"),
    ("app.api.network_catalog", "router", "api", "user"),
    ("app.api.domains_network_fiber", "router", "api", "user"),
    ("app.api.domains_usage", "router", "api", "user"),
    ("app.api.imports", "router", "api", "user"),
    ("app.api.audit", "router", "api", "none"),
    ("app.api.gis", "router", "api", "user"),
    ("app.api.geocoding", "router", "api", "user"),
    ("app.api.qualification", "router", "api", "user"),
    ("app.api.settings", "router", "api", "user"),
    ("app.api.webhooks", "router", "api", "user"),
    ("app.api.connectors", "router", "api", "user"),
    ("app.api.integrations", "router", "api", "user"),
    ("app.api.scheduler", "router", "api", "user"),
    ("app.api.comms", "router", "api", "user"),
    ("app.api.analytics", "router", "api", "user"),
    ("app.api.fiber_plant", "router", "api", "user"),
    ("app.api.nextcloud_talk", "router", "api", "user"),
    ("app.api.wireguard", "router", "api", "user"),
    ("app.api.nas", "router", "api", "user"),
    ("app.api.router_management", "router", "api", "user"),
    ("app.api.router_management", "jump_host_router", "api", "user"),
    ("app.api.provisioning", "router", "api", "user"),
    ("app.api.bandwidth", "router", "api", "user"),
    ("app.api.validation", "router", "api", "user"),
    ("app.api.defaults", "router", "api", "user"),
    ("app.api.zabbix", "router", "api", "user"),
    ("app.api.zabbix_webhook", "router", "api", "none"),
    ("app.api.wireguard", "public_router", "api", "none"),
    ("app.api.tr069_inform", "router", "api", "none"),
]


def _get_release_metadata() -> dict[str, str | None]:
    return {
        "release": os.getenv("APP_RELEASE") or os.getenv("IMAGE_TAG") or os.getenv("GIT_SHA"),
        "git_sha": os.getenv("GIT_SHA") or os.getenv("COMMIT_SHA"),
        "environment": os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "unknown",
    }


def _log_release_metadata(component: str) -> None:
    metadata = _get_release_metadata()
    logger.info(
        "application_release",
        extra={
            "event": "application_release",
            "component": component,
            **metadata,
        },
    )


def _router_dependencies(mode: str):
    if mode == "user":
        from app.api.deps import require_user_auth

        return [Depends(require_user_auth)]
    if mode == "admin":
        from app.api.deps import require_role

        return [Depends(require_role("admin"))]
    return None


def _load_router_object(module_name: str, attr_name: str):
    module = import_module(module_name)
    return getattr(module, attr_name)


def _apply_router_spec(app: FastAPI, spec: tuple[str, str, str, str]) -> None:
    module_name, attr_name, mount_kind, dependency_mode = spec
    router = _load_router_object(module_name, attr_name)
    _mount_router(app, router, mount_kind, dependency_mode)


def _mount_router(
    app: FastAPI, router, mount_kind: str, dependency_mode: str
) -> None:
    dependencies = _router_dependencies(dependency_mode)

    if mount_kind == "api":
        app.include_router(router, prefix="/api/v1", dependencies=dependencies)
        return
    if mount_kind == "admin":
        app.include_router(router, prefix="/admin")
        return
    app.include_router(router)


def _include_core_routers(app: FastAPI) -> None:
    for spec in _CORE_ROUTER_SPECS:
        _apply_router_spec(app, spec)


async def _load_deferred_api_routers(app: FastAPI) -> None:
    logger.info(
        "deferred_api_router_load_begin",
        extra={
            "event": "deferred_api_router_load_begin",
            "router_count": len(_DEFERRED_API_ROUTER_SPECS),
        },
    )
    for spec in _DEFERRED_API_ROUTER_SPECS:
        module_name, attr_name, _mount_kind, _dependency_mode = spec
        try:
            router = await asyncio.to_thread(_load_router_object, module_name, attr_name)
            _mount_router(app, router, spec[2], spec[3])
            logger.info(
                "deferred_api_router_loaded",
                extra={
                    "event": "deferred_api_router_loaded",
                    "module": module_name,
                    "attr": attr_name,
                },
            )
        except Exception:
            logger.exception(
                "deferred_api_router_load_failed",
                extra={
                    "event": "deferred_api_router_load_failed",
                    "module": module_name,
                    "attr": attr_name,
                },
            )
            raise
        await asyncio.sleep(0)
    logger.info(
        "deferred_api_router_load_complete",
        extra={"event": "deferred_api_router_load_complete"},
    )


def _warn_on_scheduler_registry_drift() -> None:
    try:
        from app.celery_app import celery_app
        from app.services.scheduler_config import find_unregistered_scheduled_tasks

        drift = find_unregistered_scheduled_tasks(celery_app.tasks.keys())
    except Exception:
        logger.warning(
            "scheduler_registry_drift_check_failed",
            exc_info=True,
            extra={"event": "scheduler_registry_drift_check_failed"},
        )
        return

    if not drift:
        logger.info(
            "scheduler_registry_drift_check_clean",
            extra={"event": "scheduler_registry_drift_check_clean"},
        )
        return

    logger.warning(
        "scheduler_registry_drift_detected",
        extra={
            "event": "scheduler_registry_drift_detected",
            "unknown_task_count": len(drift),
            "unknown_tasks": [item["task_name"] for item in drift],
        },
    )


def _assert_required_schema() -> None:
    """Fail fast when required DB schema changes are missing."""
    db = db_session_adapter.create_session()
    try:
        inspector = sqlalchemy_inspect(db.get_bind())
        if not inspector.has_table("ont_units"):
            raise RuntimeError(
                "Database schema is incompatible: required table 'ont_units' is missing. "
                "Run `alembic upgrade head` before starting the app."
            )
        ont_columns = {column["name"] for column in inspector.get_columns("ont_units")}
        if "contact" not in ont_columns:
            raise RuntimeError(
                "Database schema is incompatible: required column 'ont_units.contact' is missing. "
                "Run `alembic upgrade head` before starting the app."
            )
    finally:
        db.close()


def _check_test_environment_leakage() -> None:
    """Warn if test environment variables are set in production.

    The PYTEST_CURRENT_TEST variable being set in production can cause
    test stubs/mocks to be used instead of real implementations, leading
    to errors like '_RedisStub' object has no attribute 'get'.
    """
    test_env_vars = [
        "PYTEST_CURRENT_TEST",
        "PYTEST_VERSION",
        "_PYTEST_RAISE",
    ]
    found = []
    for var in test_env_vars:
        if os.environ.get(var):
            found.append(var)

    if found:
        logger.error(
            "test_environment_variables_detected",
            extra={
                "event": "test_environment_variables_detected",
                "variables": found,
                "warning": "Test environment variables are set in production. "
                "This can cause test stubs to be used instead of real implementations.",
            },
        )


def _seed_startup_settings() -> None:
    started_at = monotonic()
    logger.info("startup_seed_begin", extra={"event": "startup_seed_begin"})

    # Check for test environment leakage (can cause _RedisStub errors)
    _check_test_environment_leakage()

    # Enforce credential encryption if configured (P0 security fix)
    from app.config import settings
    from app.services.credential_crypto import require_encryption_key
    from app.services.object_storage import (
        ObjectStorageConnectionError,
        ObjectStorageError,
        ensure_storage_bucket,
    )
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
        seed_notification_templates,
        seed_provisioning_settings,
        seed_provisioning_workflows,
        seed_radius_policy_settings,
        seed_radius_settings,
        seed_scheduler_settings,
        seed_subscriber_settings,
        seed_tr069_settings,
        seed_usage_policy_settings,
        seed_usage_settings,
        seed_wireguard_settings,
    )

    if settings.enforce_credential_encryption:
        require_encryption_key(enforce=True)
        logger.info(
            "Credential encryption enforcement enabled",
            extra={"event": "credential_encryption_enforced"},
        )

    _assert_required_schema()

    try:
        ensure_storage_bucket(raise_on_failure=False)
    except (ObjectStorageConnectionError, ObjectStorageError):
        logger.warning(
            "Storage bucket initialization deferred during startup",
            extra={"event": "storage_bucket_init_deferred"},
        )
    except Exception:
        logger.exception(
            "Failed to ensure storage bucket during startup",
            extra={"event": "storage_bucket_init_failed"},
        )
    db = db_session_adapter.create_session()
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
        seed_notification_templates(db)
        seed_collections_settings(db)
        seed_collections_policy_settings(db)
        seed_geocoding_settings(db)
        seed_radius_settings(db)
        seed_radius_policy_settings(db)
        seed_scheduler_settings(db)
        seed_subscriber_settings(db)
        seed_provisioning_settings(db)
        seed_provisioning_workflows(db)
        seed_tr069_settings(db)
        seed_network_policy_settings(db)
        seed_network_settings(db)
        seed_network_monitoring_settings(db)
        seed_lifecycle_settings(db)
        seed_comms_settings(db)
        seed_wireguard_settings(db)
    finally:
        db.close()
    logger.info(
        "startup_seed_complete",
        extra={
            "event": "startup_seed_complete",
            "duration_ms": round((monotonic() - started_at) * 1000.0, 2),
        },
    )


def _log_zabbix_startup_health() -> None:
    try:
        from app.services.zabbix import check_zabbix_availability

        health = check_zabbix_availability(timeout=3.0)
    except Exception:
        logger.warning(
            "zabbix_startup_health_failed",
            exc_info=True,
            extra={"event": "zabbix_startup_health_failed"},
        )
        return

    log = logger.info if health.get("available") else logger.warning
    log(
        "zabbix_startup_health",
        extra={
            "event": "zabbix_startup_health",
            "status": health.get("status"),
            "configured": health.get("configured"),
            "available": health.get("available"),
            "api_url": health.get("api_url"),
            "status_message": health.get("message"),
        },
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _DEFERRED_ROUTER_TASK
    logger.info("app_lifespan_start", extra={"event": "app_lifespan_start"})
    _log_release_metadata("api")
    _seed_startup_settings()
    _log_zabbix_startup_health()
    _warn_on_scheduler_registry_drift()
    from app.websocket.manager import get_connection_manager

    manager = get_connection_manager()
    logger.info(
        "websocket_manager_connect_begin",
        extra={"event": "websocket_manager_connect_begin"},
    )
    await manager.connect()
    logger.info(
        "websocket_manager_connect_complete",
        extra={"event": "websocket_manager_connect_complete"},
    )
    _DEFERRED_ROUTER_TASK = asyncio.create_task(_load_deferred_api_routers(app))
    try:
        yield
    finally:
        if _DEFERRED_ROUTER_TASK is not None:
            _DEFERRED_ROUTER_TASK.cancel()
            try:
                await _DEFERRED_ROUTER_TASK
            except asyncio.CancelledError:
                pass
            _DEFERRED_ROUTER_TASK = None
        logger.info(
            "websocket_manager_disconnect_begin",
            extra={"event": "websocket_manager_disconnect_begin"},
        )
        await manager.disconnect()
        logger.info(
            "websocket_manager_disconnect_complete",
            extra={"event": "websocket_manager_disconnect_complete"},
        )


app = FastAPI(title="dotmac_sm API", lifespan=lifespan)
configure_logging()
setup_monitoring(
    app_name="dotmac-sub",
    server=os.getenv("SERVER_NAME", "default"),
)
setup_otel(app)
app.add_middleware(ObservabilityMiddleware)
register_error_handlers(app)
_include_core_routers(app)


@app.post("/api/v1/alerts/grafana-webhook", include_in_schema=False)
async def grafana_webhook_sink(request: Request) -> Response:
    """Accept Grafana alert webhooks even when alert ingestion is not configured."""
    try:
        await request.body()
    except Exception:
        logger.debug("Failed to read Grafana webhook body", exc_info=True)
    return Response(status_code=204)


def _get_cached_audit_settings() -> dict | None:
    """Return cached audit settings if valid, else None."""
    with _AUDIT_SETTINGS_LOCK:
        if (
            _AUDIT_SETTINGS_CACHE
            and _AUDIT_SETTINGS_CACHE_AT
            and monotonic() - _AUDIT_SETTINGS_CACHE_AT
            < _AUDIT_SETTINGS_CACHE_TTL_SECONDS
        ):
            return _AUDIT_SETTINGS_CACHE
    return None


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    response: Response
    path = request.url.path
    # Check cache first to avoid unnecessary session creation
    audit_settings = _get_cached_audit_settings()
    if audit_settings is None:
        db = db_session_adapter.create_session()
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
            db = db_session_adapter.create_session()
            try:
                audit_service.audit_events.log_request(
                    db, request, Response(status_code=500)
                )
            finally:
                db.close()
        raise
    if should_log:
        db = db_session_adapter.create_session()
        try:
            audit_service.audit_events.log_request(db, request, response)
        finally:
            db.close()
    return response


# ---------------------------------------------------------------------------
# Domain-based portal routing
# ---------------------------------------------------------------------------
# Reads selfcare_domain from settings to redirect / → /portal/ on the
# selfcare host. Changes in the admin UI (System → Config → Customer Portal)
# take effect within 30 s (cache TTL).
class DomainRoutingCache(TypedDict):
    ts: float
    selfcare: str
    redirect: str


_domain_routing_cache: DomainRoutingCache = {
    "ts": 0.0,
    "selfcare": "",
    "redirect": "/portal/",
}


def _load_domain_routing(db: Session) -> dict[str, str]:
    """Return cached selfcare domain + redirect target."""
    now = monotonic()
    if now - _domain_routing_cache["ts"] < 30:
        return {
            "selfcare": _domain_routing_cache["selfcare"],
            "redirect": _domain_routing_cache["redirect"],
        }
    from sqlalchemy import select

    from app.models.domain_settings import DomainSetting, SettingDomain

    stmt = (
        select(DomainSetting)
        .where(DomainSetting.domain == SettingDomain.auth)
        .where(DomainSetting.key.in_(["selfcare_domain", "selfcare_redirect_root"]))
    )
    rows = {r.key: (r.value_text or "") for r in db.scalars(stmt).all()}
    _domain_routing_cache["selfcare"] = rows.get("selfcare_domain", "")
    _domain_routing_cache["redirect"] = rows.get("selfcare_redirect_root", "/portal/")
    _domain_routing_cache["ts"] = now
    return {
        "selfcare": _domain_routing_cache["selfcare"],
        "redirect": _domain_routing_cache["redirect"],
    }


def _get_cached_domain_routing(*, allow_stale: bool = False) -> dict[str, str] | None:
    """Return cached domain routing when still fresh, or stale if allowed."""
    cache_ts = _domain_routing_cache["ts"]
    if cache_ts <= 0:
        return None
    if allow_stale or monotonic() - cache_ts < 30:
        return {
            "selfcare": _domain_routing_cache["selfcare"],
            "redirect": _domain_routing_cache["redirect"],
        }
    return None


@app.middleware("http")
async def domain_routing_middleware(request: Request, call_next):
    """Apply lightweight host-aware routing for the selfcare domain."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if not host:
        return await call_next(request)

    # Check cache first to avoid unnecessary session creation
    routing = _get_cached_domain_routing()
    if routing is None:
        db = db_session_adapter.create_session()
        try:
            routing = _load_domain_routing(db)
        except SQLAlchemyError:
            routing = _get_cached_domain_routing(allow_stale=True) or {
                "selfcare": "",
                "redirect": "/portal/",
            }
            logger.warning(
                "domain_routing_refresh_failed",
                exc_info=True,
                extra={"event": "domain_routing_refresh_failed"},
            )
        finally:
            db.close()

    selfcare = str(routing.get("selfcare", "")).strip().lower()
    if not selfcare or host != selfcare:
        return await call_next(request)

    path = request.url.path

    # Keep the selfcare host convenient by redirecting only the bare root
    # to the configured portal landing page. All other paths stay reachable.
    if path not in {"", "/"}:
        return await call_next(request)

    redirect_target = str(routing.get("redirect", "/portal/"))
    from starlette.responses import RedirectResponse as StarletteRedirect

    return StarletteRedirect(url=redirect_target, status_code=302)


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
    needs_protection = any(
        path.startswith(protected) for protected in _CSRF_PROTECTED_PATHS
    )

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

        def _csrf_forbidden(reason: str) -> HTMLResponse:
            logger.warning(
                "CSRF validation failed for %s %s: %s",
                method,
                path,
                reason,
            )
            # Generate a request ID for tracking
            import uuid

            request_id = str(uuid.uuid4())[:8]
            try:
                from jinja2 import Environment, FileSystemLoader

                env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
                template = env.get_template("errors/csrf.html")
                content = template.render(request_id=request_id)
                return HTMLResponse(content=content, status_code=403)
            except Exception:
                # Fallback to simple HTML if template rendering fails
                return HTMLResponse(
                    content=f"""<!DOCTYPE html>
<html><head><title>Session Expired</title></head>
<body style="font-family:system-ui;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f8fafc">
<div style="text-align:center;max-width:400px;padding:20px">
<h1 style="color:#1e293b">Session Expired</h1>
<p style="color:#64748b">Your session has expired or the security token is invalid. Please refresh the page and try again.</p>
<p style="color:#94a3b8;font-size:12px">Reference: {request_id}</p>
<button onclick="location.reload()" style="margin-top:16px;padding:12px 24px;background:#2563eb;color:white;border:none;border-radius:8px;cursor:pointer;font-weight:600">Refresh Page</button>
</div></body></html>""",
                    status_code=403,
                )

        if not cookie_token:
            # No CSRF cookie - reject request
            return _csrf_forbidden(
                "CSRF token missing. Please refresh the page and try again."
            )

        # Check header first (for HTMX/fetch requests)
        header_token = request.headers.get(CSRF_HEADER_NAME)
        if header_token:
            if not secrets.compare_digest(cookie_token, header_token):
                return _csrf_forbidden(
                    "CSRF token invalid. Please refresh the page and try again."
                )
        else:
            # For form submissions, check form data
            content_type = request.headers.get("content-type", "")
            if (
                "application/x-www-form-urlencoded" in content_type
                or "multipart/form-data" in content_type
            ):
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

                        boundary_match = re.search(r"boundary=([^\s;]+)", content_type)
                        if boundary_match:
                            boundary = boundary_match.group(1).strip('"')
                            # Construct a valid MIME message for parsing
                            mime_header = f"Content-Type: multipart/form-data; boundary={boundary}\r\n\r\n"
                            mime_message = mime_header.encode("utf-8") + body

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
                                            form_token = payload.decode(
                                                "utf-8", errors="ignore"
                                            ).strip()
                                            break
                                        if isinstance(payload, str) and payload.strip():
                                            form_token = payload.strip()
                                            break
                    else:
                        form_data = parse_qs(body.decode("utf-8"))
                        form_token = form_data.get("_csrf_token", [None])[0]

                    if not form_token:
                        return _csrf_forbidden(
                            "CSRF token missing. Please refresh the page and try again."
                        )
                    if not secrets.compare_digest(cookie_token, form_token):
                        return _csrf_forbidden(
                            "CSRF token invalid. Please refresh the page and try again."
                        )
                except Exception:
                    return _csrf_forbidden(
                        "CSRF token invalid. Please refresh the page and try again."
                    )

                # Reconstruct request with body for downstream handlers
                async def receive():
                    return {"type": "http.request", "body": body}

                request = Request(scope=request.scope, receive=receive)
            else:
                # Non-form state-changing requests must use header token.
                return _csrf_forbidden(
                    "CSRF token missing. Please refresh the page and try again."
                )

    try:
        response = await call_next(request)
    except RuntimeError as exc:
        if str(exc) == "No response returned.":
            disconnected = await request.is_disconnected()
            # During client disconnects and dev auto-reload windows Starlette may not
            # produce a downstream response; treat it as a benign terminated request.
            logger.info(
                "No response returned from downstream app; request terminated (%s): %s %s",
                "client_disconnected" if disconnected else "reload_or_shutdown",
                method,
                path,
            )
            return Response(status_code=204)
        raise

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

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
