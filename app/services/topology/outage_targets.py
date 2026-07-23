"""Resolve which subscriptions an outage incident affects.

Extracted from ``app/web/admin/network_monitoring.py`` so the admin preview and
the automated dispatcher (ADR 0004) answer "who is affected?" identically. An
adapter must not be the only place this is computed — a scheduler cannot import
a route helper, and two copies of this would drift into notifying different
people from the same incident.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.network import FdhCabinet
from app.models.network_monitoring import NetworkDevice, OutageIncident, PopSite
from app.services.topology.affected import affected_customers
from app.services.topology.outage import (
    CLASSIFIER_CUSTOMER_VISIBLE_STATUSES,
    CLASSIFIER_SOURCE,
)


def notifiable_incident(
    session: Session, incident_id: str | uuid.UUID | None
) -> OutageIncident | None:
    """The incident, if it is a classifier incident customers may hear about."""
    if incident_id is None:
        return None
    try:
        resolved = session.get(OutageIncident, uuid.UUID(str(incident_id)))
    except (ValueError, TypeError):
        return None
    if resolved is None:
        return None
    if resolved.detection_source != CLASSIFIER_SOURCE:
        return None
    if resolved.status not in CLASSIFIER_CUSTOMER_VISIBLE_STATUSES:
        return None
    return resolved


def incident_boundary(session: Session, incident: OutageIncident):
    """The node / base station / cabinet the incident is rooted at."""
    node = (
        session.get(NetworkDevice, incident.root_node_id)
        if incident.root_node_id is not None
        else None
    )
    basestation = (
        session.get(PopSite, incident.basestation_id)
        if incident.basestation_id is not None
        else None
    )
    fdh = (
        session.get(FdhCabinet, incident.fdh_cabinet_id)
        if incident.fdh_cabinet_id is not None
        else None
    )
    return node, basestation, fdh


def incident_subscription_ids(
    session: Session, incident: OutageIncident
) -> list[uuid.UUID]:
    """Subscription ids downstream of the incident's boundary."""
    node, basestation, fdh = incident_boundary(session, incident)
    if node is None and basestation is None and fdh is None:
        return []
    impact = affected_customers(session, node=node, basestation=basestation, fdh=fdh)
    return [subscription.id for subscription in impact["subscriptions"]]


def incident_boundary_and_subscription_ids(
    session: Session, incident_id: str | uuid.UUID | None
):
    """``(incident, boundary, subscription_ids)`` for the admin preview path."""
    incident = notifiable_incident(session, incident_id)
    if incident is None:
        return None, None, []
    node, basestation, fdh = incident_boundary(session, incident)
    if node is None and basestation is None and fdh is None:
        return incident, None, []
    impact = affected_customers(session, node=node, basestation=basestation, fdh=fdh)
    return (
        incident,
        node or basestation or fdh,
        [subscription.id for subscription in impact["subscriptions"]],
    )
