"""OLT hardware auto-discovery (degraded: SNMP source retired).

This service used to read standard Entity MIB (RFC 6933) items from the OLT's
linked Zabbix host to discover shelves, line cards, card ports, power supplies,
and fan units. The Zabbix SNMP source was retired with the native monitoring
cutover, so discovery now degrades to the same "not configured" result it
already returned at runtime while Zabbix was unconfigured. Existing hardware
inventory rows are untouched.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.requests import Request

from app.models.network import OLTDevice
from app.services.network.olt_web_audit import log_olt_audit_event

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_NOT_CONFIGURED_MESSAGE = (
    "Hardware discovery is not available: the SNMP inventory source was retired."
)


def discover_olt_hardware(
    db: Session,
    olt: OLTDevice,
) -> tuple[bool, str, dict[str, object]]:
    """Discover hardware inventory for an OLT.

    Degraded: always reports the discovery source as unconfigured (matching
    the pre-cutover behaviour when Zabbix was not configured).

    Returns:
        Tuple of (success, message, stats_dict).
    """
    del db, olt
    return False, _NOT_CONFIGURED_MESSAGE, {}


def discover_olt_hardware_audited(
    db: Session,
    olt: OLTDevice,
    *,
    request: Request | None = None,
) -> tuple[bool, str, dict[str, object]]:
    ok, message, stats = discover_olt_hardware(db, olt)
    log_olt_audit_event(
        db,
        request=request,
        action="discover_hardware",
        entity_id=olt.id,
        metadata={
            "result": "success" if ok else "error",
            "message": message,
            "stats": stats,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return ok, message, stats
