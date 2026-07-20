"""Minimal branding context for auth pages rendered outside a portal.

The customer and reseller portals each have a richer context processor
(:mod:`app.web.customer.branding`, :mod:`app.web.reseller.branding`) that
exposes ``sidebar_stats`` / ``portal_name`` / ``branding_favicon_url`` to
every template. The staff and vendor auth pages (login, MFA, forgot/reset
password) are rendered from plain ``Jinja2Templates`` instances and only
receive the static ``brand`` global, so the uploaded brand logo never
reaches them. This processor exposes the same keys so the shared auth
layouts render the uploaded logo everywhere.

``get_sidebar_stats`` keeps its own 60s in-process cache, so the per-render
DB session is cheap.
"""

from __future__ import annotations

import logging

from fastapi import Request

from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


def auth_branding_context(request: Request) -> dict[str, object]:
    """Expose sidebar branding (logo, favicon, portal name) to auth templates."""
    stats: dict = {}
    portal_name = ""
    try:
        db = db_session_adapter.create_session()
        try:
            from app.services import web_admin as web_admin_service

            stats = web_admin_service.get_sidebar_stats(db)
            portal_name = str(stats.get("app_name") or "").strip()
        finally:
            db.close()
    except Exception:
        logger.debug("Failed to load branding context for auth pages")
    return {
        "sidebar_stats": stats,
        "portal_name": portal_name,
        "branding_favicon_url": str(stats.get("favicon_url") or "").strip(),
        **({"brand": stats["brand"]} if stats.get("brand") else {}),
    }
