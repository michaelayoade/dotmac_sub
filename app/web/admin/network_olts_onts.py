"""Backwards compatibility -- routes split into network_olts.py and network_onts.py."""

from fastapi import APIRouter

from app.web.admin.network_olts import router as _olt_router  # noqa: F401

from app.web.admin.network_onts import router as _ont_router  # noqa: F401

# Legacy: expose a router for code that imports `router` from this module.
# Callers that import from this compatibility module should still see both
# the OLT and ONT route sets.
router = APIRouter()
router.include_router(_olt_router)
router.include_router(_ont_router)
