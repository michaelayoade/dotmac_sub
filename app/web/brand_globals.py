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
import sys
from datetime import UTC, datetime

from fastapi.templating import Jinja2Templates

from app.services.branding_config import get_brand
from app.version import get_app_version

logger = logging.getLogger(__name__)

_installed = False


def _current_year() -> int:
    """Live current year for footers, so hardcoded years can't go stale."""
    return datetime.now(UTC).year


def _attach_globals(templates: Jinja2Templates) -> None:
    templates.env.globals.setdefault("brand", get_brand())
    templates.env.globals.setdefault("current_year", _current_year)
    templates.env.globals.setdefault("app_version", get_app_version)


def _backfill_loaded_template_instances() -> None:
    """Attach globals to template instances created before the init patch."""
    seen: set[int] = set()
    for module in list(sys.modules.values()):
        namespace = getattr(module, "__dict__", None)
        if not namespace:
            continue
        for value in namespace.values():
            if not isinstance(value, Jinja2Templates) or id(value) in seen:
                continue
            seen.add(id(value))
            try:
                _attach_globals(value)
            except Exception:
                logger.debug(
                    "Could not backfill Jinja globals on existing template instance",
                    exc_info=True,
                )


def install_brand_jinja_global() -> None:
    """Patch Jinja2Templates so every instance exposes the ``brand`` global."""
    global _installed
    if _installed:
        return

    _original_init = Jinja2Templates.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _original_init(self, *args, **kwargs)
        try:
            _attach_globals(self)
        except Exception:  # pragma: no cover - never break template setup
            logger.debug("Could not attach brand Jinja global", exc_info=True)

    Jinja2Templates.__init__ = _patched_init  # type: ignore[method-assign]
    _installed = True

    _backfill_loaded_template_instances()
