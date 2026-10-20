import hmac
import ipaddress
import logging
import time

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.metrics import TR069_WEBHOOK_AUTH
from app.models.domain_settings import SettingDomain
from app.services import secrets as secrets_service
from app.services import settings_spec
from app.services.auth_dependencies import (
    require_audit_auth,
    require_permission,
    require_role,
    require_user_auth,
)


def get_current_user(auth=Depends(require_user_auth)):
    """Get current authenticated user info.

    Returns a dict with subscriber_id, session_id, roles, and scopes.
    """
    return auth


logger = logging.getLogger(__name__)

#: Shared-token guard for the routes GenieACS calls (tr069 inform/device-config
#: and the reconcile bootstrap webhook). Modes: ``log_only`` (default) logs and
#: counts failures but allows; ``enforce`` refuses with 403.
GENIEACS_TOKEN_HEADER = "X-Dotmac-Genieacs-Token"
_GENIEACS_TOKEN_TTL_SECONDS = 60.0
_token_cache: tuple[float, str] | None = None


def _load_genieacs_token() -> str:
    """Resolve the shared secret: env value / secret ref, then OpenBao path.

    Never raises and never logs the value. Errors resolve to "".
    """
    try:
        value = secrets_service.resolve_secret(settings.tr069_webhook_token)
    except Exception:
        logger.warning("GenieACS webhook token reference could not be resolved")
        value = None
    if value:
        return value
    try:
        return secrets_service.get_secret("tr069", "webhook_token")
    except Exception:
        logger.warning("GenieACS webhook token lookup failed")
        return ""


def _expected_genieacs_token() -> str:
    """Memoised lookup (about 60 s), caching failures too, so an OpenBao outage
    cannot add its timeout to every GenieACS call."""
    global _token_cache
    now = time.monotonic()
    cached = _token_cache
    if cached is not None and now - cached[0] < _GENIEACS_TOKEN_TTL_SECONDS:
        return cached[1]
    value = _load_genieacs_token()
    _token_cache = (now, value)
    return value


def _reset_genieacs_token_cache() -> None:
    global _token_cache
    _token_cache = None


def _source_allowed(allowlist: str | None, remote: str | None) -> bool:
    entries = [e.strip() for e in (allowlist or "").split(",") if e.strip()]
    if not entries:
        return True
    try:
        addr = ipaddress.ip_address(remote or "")
    except ValueError:
        return False
    for entry in entries:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            logger.warning("Ignoring invalid GenieACS webhook allowlist entry")
    return False


def require_genieacs_token(request: Request, db: Session = Depends(get_db)) -> None:
    """Guard for GenieACS-facing routes (shared token + optional source list).

    The secret and the presented token are never logged. In ``log_only`` mode a
    request with no header is counted and logged without any secret lookup.
    """
    mode = str(
        settings_spec.resolve_value(db, SettingDomain.tr069, "tr069_webhook_auth_mode")
        or "log_only"
    ).lower()
    if mode not in {"log_only", "enforce"}:
        mode = "enforce"  # unknown value fails closed

    presented = request.headers.get(GENIEACS_TOKEN_HEADER)
    remote = request.client.host if request.client else None
    reason: str | None = None
    if not presented:
        reason = "missing_token"
    else:
        expected = _expected_genieacs_token()
        if not expected:
            reason = "secret_not_configured"
        elif not hmac.compare_digest(presented.encode(), expected.encode()):
            reason = "bad_token"
        elif not _source_allowed(
            settings_spec.resolve_value(
                db, SettingDomain.tr069, "tr069_webhook_allowed_sources"
            ),
            remote,
        ):
            reason = "source_not_allowed"
    if reason is None:
        TR069_WEBHOOK_AUTH.labels(mode=mode, outcome="ok").inc()
        return
    outcome = "refused" if mode == "enforce" else "allowed_log_only"
    TR069_WEBHOOK_AUTH.labels(mode=mode, outcome=outcome).inc()
    logger.warning(
        "GenieACS webhook auth failure reason=%s mode=%s path=%s remote=%s",
        reason,
        mode,
        request.url.path,
        remote,
    )
    if mode == "enforce":
        raise HTTPException(status_code=403, detail="Forbidden")


__all__ = [
    "require_genieacs_token",
    "get_db",
    "get_current_user",
    "require_audit_auth",
    "require_permission",
    "require_role",
    "require_user_auth",
]
