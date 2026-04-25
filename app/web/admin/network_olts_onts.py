"""Backwards compatibility for split OLT and ONT route modules."""

from fastapi import APIRouter

from app.web.admin.network_olts_inventory import router as _olt_inventory_router
from app.web.admin.network_olts_profiles import router as _olt_profiles_router
from app.web.admin.network_onts import router as _ont_router  # noqa: F401
from app.web.admin.network_onts_actions import (
    router as _ont_actions_router,  # noqa: F401
)
from app.web.admin.network_onts_inventory import (
    router as _ont_inventory_router,  # noqa: F401
)

# Compatibility: expose a router for callers that still import `router` from
# this module, but compose it from the active split routers rather than the
# parked legacy monolith.
router = APIRouter()
router.include_router(_olt_inventory_router)
router.include_router(_olt_profiles_router)
router.include_router(_ont_inventory_router)
router.include_router(_ont_router)
router.include_router(_ont_actions_router)
