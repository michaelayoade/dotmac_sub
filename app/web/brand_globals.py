"""Attach the static white-label ``brand`` value to every Jinja2 environment.

Brand fields are deployment-static (see :mod:`app.services.branding_config`), so
they belong as a Jinja *global* available to every template rather than a
per-request context processor. The web route modules each create their own
``Jinja2Templates`` instance and are imported lazily during app startup (see the
router specs in :mod:`app.main`), so patching ``Jinja2Templates.__init__`` once
before routers are imported makes ``brand`` available to all of them.

Templates should still guard with a default (``brand.primary_color if brand is
defined and brand else "#3b82f6"``) so error pages rendered by instances created
before this installer runs keep working.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi.templating import Jinja2Templates

from app.services.branding_config import get_brand
from app.version import get_app_version

logger = logging.getLogger(__name__)

_installed = False


def _current_year() -> int:
    """Live current year for footers, so hardcoded years can't go stale."""
    return datetime.now(UTC).year


def install_brand_jinja_global() -> None:
    """Patch Jinja2Templates so every instance exposes the ``brand`` global."""
    global _installed
    if _installed:
        return

    _original_init = Jinja2Templates.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _original_init(self, *args, **kwargs)
        try:
            self.env.globals.setdefault("brand", get_brand())
            self.env.globals.setdefault("current_year", _current_year)
            self.env.globals.setdefault("app_version", get_app_version)
        except Exception:  # pragma: no cover - never break template setup
            logger.debug("Could not attach brand Jinja global", exc_info=True)

    Jinja2Templates.__init__ = _patched_init  # type: ignore[method-assign]
    _installed = True

    # Backfill the shared templates instance if it was already imported.
    try:
        from app.web.templates import templates as _shared

        _shared.env.globals.setdefault("brand", get_brand())
        _shared.env.globals.setdefault("current_year", _current_year)
        _shared.env.globals.setdefault("app_version", get_app_version)
    except Exception:  # pragma: no cover
        logger.debug("Could not backfill shared templates brand global", exc_info=True)
