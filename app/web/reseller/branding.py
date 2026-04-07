"""Shared branding context for reseller portal templates."""

import logging
from threading import Lock
from time import monotonic
from typing import TypedDict

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.db import SessionLocal

logger = logging.getLogger(__name__)

# Cache branding data to avoid creating a DB session on every template render
_BRANDING_CACHE_TTL = 30.0  # seconds


class _BrandingCache(TypedDict):
    ts: float
    stats: dict
    portal_name: str
    favicon: str


_branding_cache: _BrandingCache = {"ts": 0.0, "stats": {}, "portal_name": "", "favicon": ""}
_branding_cache_lock = Lock()


def _get_cached_branding() -> tuple[dict, str, str] | None:
    """Return cached branding (stats, portal_name, favicon) if valid, else None."""
    with _branding_cache_lock:
        if monotonic() - _branding_cache["ts"] < _BRANDING_CACHE_TTL:
            return (_branding_cache["stats"], _branding_cache["portal_name"], _branding_cache["favicon"])
    return None


def _update_branding_cache(stats: dict, portal_name: str, favicon: str) -> None:
    """Update the branding cache."""
    with _branding_cache_lock:
        _branding_cache["ts"] = monotonic()
        _branding_cache["stats"] = stats
        _branding_cache["portal_name"] = portal_name
        _branding_cache["favicon"] = favicon


def reseller_branding_context(_request: Request) -> dict[str, object]:
    """Build branding context (favicon, portal name) for reseller portal templates."""
    # Check cache first to avoid unnecessary session creation
    cached = _get_cached_branding()
    if cached is not None:
        stats, portal_name, favicon = cached
        return {
            "sidebar_stats": stats,
            "branding_favicon_url": favicon,
            "portal_name": portal_name,
        }

    db = SessionLocal()
    try:
        from app.services import web_admin as web_admin_service

        stats = web_admin_service.get_sidebar_stats(db)
    except Exception:
        logger.debug("Failed to load sidebar stats for reseller branding context")
        stats = {}

    portal_name = ""
    try:
        from app.services.web_system_company_info import get_company_info

        info = get_company_info(db)
        portal_name = info.get("company_name") or ""
    except Exception:
        logger.debug("Failed to load company name for reseller branding")
    finally:
        db.close()

    favicon = str(stats.get("favicon_url") or "").strip()
    _update_branding_cache(stats, portal_name, favicon)

    return {
        "sidebar_stats": stats,
        "branding_favicon_url": favicon,
        "portal_name": portal_name,
    }


def get_reseller_templates() -> Jinja2Templates:
    """Return Jinja2Templates configured with reseller branding context."""
    return Jinja2Templates(
        directory="templates",
        context_processors=[reseller_branding_context],
    )
