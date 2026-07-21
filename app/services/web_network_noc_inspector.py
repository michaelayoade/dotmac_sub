"""NOC live inspector page data — the on-demand drill-in for a NOC queue outage.

Projects the outage-console owner's per-node impact read
(topology.outage_console.outage_detail) into a compact inspector panel: the down
node, its outage class + affected/online counts, the affected customers with
their operator-facing verdict, and the predictive branches. Read-only; the
inspector is keyed by the outage's root node (the incident->node shim is simply
incident.root_node_id, resolved by the NOC queue). Node outages only — the
console read has no basestation/FDH drill-in.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.schemas.status_presentation import StatusIcon, StatusPresentation, StatusTone
from app.services.topology import outage_console

_CUSTOMER_CAP = 100

_ONLINE = StatusPresentation(
    value="online", label="Online", tone=StatusTone.positive, icon=StatusIcon.check
)
_OFFLINE = StatusPresentation(
    value="offline", label="Session down", tone=StatusTone.negative, icon=StatusIcon.x
)


def noc_inspector_data(db: Session, node_id: uuid.UUID) -> dict:
    """Return the inspector projection for one outage node, or a not-found marker."""
    detail = outage_console.outage_detail(db, node_id)
    if detail is None:
        return {"found": False, "node_id": str(node_id)}

    count = detail["count"]
    online = detail["online_count"]
    customers = [
        {
            "name": c["subscriber_name"],
            "status": _ONLINE if c["online"] else _OFFLINE,
            "medium": c.get("medium") or "—",
            "signal_dbm": c.get("signal_dbm"),
            "message": c.get("customer_message") or "",
            "action": c.get("agent_action") or "",
        }
        for c in detail["customers"]
    ]

    predictive = detail.get("predictive") or {}
    return {
        "found": True,
        "node": detail["node"],
        "cls": detail["class"],
        "count": count,
        "online_count": online,
        "offline_count": max(count - online, 0),
        "capped": detail.get("capped", False),
        "customers": customers,
        "predictive": {
            "co_failure": len(predictive.get("co_failure") or []),
            "rx_droop": len(predictive.get("rx_droop") or []),
        },
    }
