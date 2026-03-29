"""Backwards compatibility -- routes split into network_olts.py and network_onts.py."""

from app.web.admin.network_olts import router as _olt_router  # noqa: F401

# Re-export for any code that imports specific functions
from app.web.admin.network_onts import ont_provision_status  # noqa: F401
from app.web.admin.network_onts import router as _ont_router  # noqa: F401

# Legacy: expose a router for code that imports `router` from this module.
# Both new modules register routes on the same prefix so either router works,
# but callers that do ``from …network_olts_onts import router`` will get
# the OLT router (the ONT router is included separately in __init__).
router = _olt_router
