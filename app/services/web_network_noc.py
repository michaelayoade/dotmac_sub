"""NOC queue page data — one triage queue projected from the monitoring owners.

There is no single "NOC queue" read; the queue is the union of the authoritative
attention surfaces, merged into a common tone-coded row:
  - open outage incidents  (owner: topology.outage)
  - device mismatch worklist groups (owner: device_operational_status)
  - open threshold alarms  (owner: monitoring via web_network_monitoring)
Tone comes from the server-owned presentations. Read-only projection; each row
links to its owning detail surface (actions stay on the owners).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.schemas.status_presentation import (
    StatusIcon,
    StatusPresentation,
    StatusTone,
)
from app.services import operational_checks as operational_checks_service
from app.services import web_network_monitoring
from app.services.device_operational_status import mismatch_worklist
from app.services.status_presentation import (
    alarm_severity_presentation,
    outage_status_presentation,
)
from app.services.topology import outage

# worst-first ordering for the queue
_TONE_RANK = {
    StatusTone.negative: 0,
    StatusTone.warning: 1,
    StatusTone.info: 2,
    StatusTone.positive: 3,
    StatusTone.neutral: 4,
}

_MISMATCH_PRESENTATION = StatusPresentation(
    value="mismatch",
    label="Needs review",
    tone=StatusTone.warning,
    icon=StatusIcon.alert,
)


def _rank(presentation: StatusPresentation) -> int:
    return _TONE_RANK.get(presentation.tone, 5)


def _incident_title(incident: object) -> str:
    if getattr(incident, "root_node_id", None):
        return "Node outage"
    if getattr(incident, "basestation_id", None):
        return "Base-station outage"
    if getattr(incident, "fdh_cabinet_id", None):
        return "FDH outage"
    return "Outage"


_POLLER_RESULT_LABELS = {
    "no_route_to_host": "No route to host",
    "timeout": "Connection timed out",
    "authentication_rejected": "Authentication rejected",
    "connection_refused": "Connection refused",
    "network_unreachable": "Network unreachable",
    "transport_error": "RouterOS polling failed",
}


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def noc_queue_data(db: Session) -> dict:
    """Merge the open outage / mismatch / alarm queues into one triage list."""
    items: list[dict] = []
    outage_count = mismatch_count = alarm_count = collector_count = 0

    # 1. Open outage incidents
    for incident in outage.list_open_incidents(db):
        presentation = outage_status_presentation(getattr(incident, "status", None))
        items.append(
            {
                "kind": "outage",
                "id": str(incident.id),
                "title": _incident_title(incident),
                "subtitle": getattr(incident, "note", None)
                or getattr(incident, "classification", None)
                or (str(getattr(incident, "detection_source", "") or "")).title(),
                "status": presentation,
                "count": getattr(incident, "affected_count", None),
                "count_label": "affected",
                "when": getattr(incident, "started_at", None),
                "url": "/admin/network/outages",
                "node_id": str(incident.root_node_id)
                if getattr(incident, "root_node_id", None)
                else None,
                "_rank": _rank(presentation),
            }
        )
        outage_count += 1

    # 2. Device mismatch worklist (grouped by reason/owner)
    for group in mismatch_worklist(db).get("groups", []):
        count = group.get("count", len(group.get("rows", [])))
        items.append(
            {
                "kind": "mismatch",
                "id": str(group.get("reason", "")),
                "title": group.get("label", "Device mismatch"),
                "subtitle": f"owner: {group.get('owner', '—')}",
                "status": _MISMATCH_PRESENTATION,
                "count": count,
                "count_label": "devices",
                "when": None,
                "url": "/admin/network/device-status-worklist",
                "_rank": _rank(_MISMATCH_PRESENTATION),
            }
        )
        mismatch_count += 1

    # 3. Open threshold alarms
    alarm_data = web_network_monitoring.alarms_page_data(db, severity=None, status=None)
    alarm_rows = alarm_data.get("alarms", [])
    alarms = alarm_rows if isinstance(alarm_rows, list) else []
    for alarm in alarms:
        presentation = alarm_severity_presentation(getattr(alarm, "severity", None))
        measured = getattr(alarm, "measured_value", None)
        items.append(
            {
                "kind": "alarm",
                "id": str(alarm.id),
                "title": str(getattr(alarm, "metric_type", None) or "Alarm"),
                "subtitle": f"measured {measured}" if measured is not None else "",
                "status": presentation,
                "count": None,
                "count_label": "",
                "when": getattr(alarm, "triggered_at", None),
                "url": "/admin/network/alarms",
                "_rank": _rank(presentation),
            }
        )
        alarm_count += 1

    # 4. Exact device-level bandwidth collector failures. These are evidence
    # gaps, not device/service "down" decisions, so rows state what failed,
    # customer-data impact, and the next retry instead of rendering a status.
    poller_snapshot = operational_checks_service.bandwidth_poller_snapshot()
    for failure in operational_checks_service.bandwidth_device_failures(
        poller_snapshot
    ):
        category = str(failure.get("error_category") or "transport_error")
        items.append(
            {
                "kind": "collector",
                "id": str(failure.get("device_id") or ""),
                "title": str(failure.get("name") or failure.get("device_id") or "NAS"),
                "subtitle": (
                    f"Live bandwidth collection cannot reach {failure.get('host') or 'the router'}. "
                    "This does not prove customer service is down."
                ),
                "status": None,
                "result": _POLLER_RESULT_LABELS.get(
                    category, "RouterOS polling failed"
                ),
                "next_attempt_at": _parse_datetime(failure.get("next_attempt_at")),
                "count": int(failure.get("services_without_live_bandwidth") or 0),
                "count_label": "services without live bandwidth",
                "when": _parse_datetime(failure.get("last_attempt_at")),
                "url": f"/admin/network/nas/{failure.get('device_id')}",
                "_rank": 0,
            }
        )
        collector_count += 1

    # worst tone first, then most recent
    items.sort(key=lambda i: (i["_rank"], -(i["when"].timestamp() if i["when"] else 0)))

    return {
        "items": items,
        "counts": {
            "total": len(items),
            "outages": outage_count,
            "mismatches": mismatch_count,
            "alarms": alarm_count,
            "collectors": collector_count,
        },
        "operational_checks": operational_checks_service.operational_checks(
            db,
            poller_snapshot=poller_snapshot,
        ),
    }
