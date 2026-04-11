"""Backwards compatibility for OLT and ONT route modules."""

from fastapi import APIRouter

from app.web.admin.network_olts import router as _olt_router  # noqa: F401
from app.web.admin.network_onts import router as _ont_router  # noqa: F401
from app.web.admin.network_onts_actions import (
    router as _ont_actions_router,  # noqa: F401
)
from app.web.admin.network_onts_inventory import (
    router as _ont_inventory_router,  # noqa: F401
)

# Legacy: expose a router for code that imports `router` from this module.
# Callers that import from this compatibility module should still see the split
# OLT and ONT route sets.
router = APIRouter()
router.include_router(_olt_router)
router.include_router(_ont_inventory_router)
router.include_router(_ont_router)
router.include_router(_ont_actions_router)
