"""Customer-safe connection status (Phase 3, selfcare).

Maps the internal topology path to a coarse, customer-safe view: the
basestation name + a {healthy, degraded, outage, unknown} status. Deliberately
exposes NO internal IPs, device names, node ids, or gap internals — the
customer sees "you're connected via <BTS>, status: healthy", nothing more.
Reads the warmed live_status cache; never live-polls Zabbix.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.services.topology.customer_path import resolve_customer_path

# internal live_status -> customer-safe status
_SAFE = {"up": "healthy", "problem": "degraded", "down": "outage"}


def customer_connection_status(session: Session, subscription: Subscription) -> dict:
    """Return {basestation, status} for the customer's connection.

    ``status`` is healthy/degraded/outage/unknown; ``basestation`` is the site
    name or None. Nothing internal (IPs, device names, gap reasons) leaks.
    """
    from app.services.topology.outage import open_incident_for_path

    path = resolve_customer_path(session, subscription)
    live = path.node.live_status if path.node is not None else None
    known_outage = open_incident_for_path(session, path) is not None
    # A declared outage overrides the cached dot for the customer-facing view.
    status = "outage" if known_outage else _SAFE.get(live or "", "unknown")
    return {
        "basestation": path.basestation.name if path.basestation is not None else None,
        "status": status,
        "known_outage": known_outage,
    }
