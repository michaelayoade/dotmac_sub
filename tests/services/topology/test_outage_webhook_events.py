"""Outage lifecycle -> outbound webhook fan-out (create + resolve only).

Reuses the established event->webhook machinery (emit_event ->
WebhookHandler -> WebhookDelivery -> deliver_webhook). These tests cover the
outage-specific seams: deliveries are created on declare/resolve with the
right payload, nothing fires when no endpoint subscribes, and the payload
signs/verifies with the same HMAC the delivery task sends. Transport-level
retry bounds/backoff are covered by test_webhook_tasks.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from app.models.network_monitoring import NetworkDevice
from app.models.webhook import (
    WebhookDelivery,
    WebhookEndpoint,
    WebhookEventType,
    WebhookSubscription,
)
from app.services.topology.outage import (
    AUTO_DETECT_ACTOR,
    declare_outage,
    resolve_outage,
)
from app.tasks.webhooks import _compute_signature


def _node(db, name="Agg-1"):
    node = NetworkDevice(name=name, is_active=True)
    db.add(node)
    db.flush()
    return node


def _subscribe(db, url="https://crm.example/hooks/outage"):
    endpoint = WebhookEndpoint(name="CRM", url=url, secret=None, is_active=True)
    db.add(endpoint)
    db.flush()
    db.add(
        WebhookSubscription(
            endpoint_id=endpoint.id,
            event_type=WebhookEventType.network_alert,
            is_active=True,
        )
    )
    db.flush()
    return endpoint


def _deliveries(db):
    return (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.event_type == WebhookEventType.network_alert)
        .all()
    )


def test_declare_creates_delivery_with_outage_payload(db_session):
    _subscribe(db_session)
    node = _node(db_session)
    incident = declare_outage(db_session, node=node, declared_by="noc@x")

    deliveries = _deliveries(db_session)
    assert len(deliveries) == 1
    payload = deliveries[0].payload["payload"]
    assert payload["alert_type"] == "outage.created"
    assert payload["incident_id"] == str(incident.id)
    assert payload["detection_source"] == "manual"
    assert payload["scope"] == {"type": "node", "id": str(node.id), "name": node.name}
    # No PII beyond counts — detail comes from the CRM outage API.
    assert "subscribers" not in payload


def test_resolve_creates_second_delivery_only_on_transition(db_session):
    _subscribe(db_session)
    node = _node(db_session)
    incident = declare_outage(db_session, node=node)

    resolve_outage(db_session, incident.id)
    resolve_outage(db_session, incident.id)  # idempotent: no third delivery

    deliveries = _deliveries(db_session)
    assert len(deliveries) == 2
    kinds = {d.payload["payload"]["alert_type"] for d in deliveries}
    assert kinds == {"outage.created", "outage.resolved"}
    resolved = next(
        d for d in deliveries if d.payload["payload"]["alert_type"] == "outage.resolved"
    )
    assert resolved.payload["payload"]["resolved_at"] is not None


def test_auto_detected_incident_marked_in_payload(db_session):
    _subscribe(db_session)
    node = _node(db_session)
    declare_outage(db_session, node=node, declared_by=AUTO_DETECT_ACTOR)
    payload = _deliveries(db_session)[0].payload["payload"]
    assert payload["detection_source"] == "auto"


def test_no_subscription_means_no_delivery_and_no_error(db_session):
    """Webhooks disabled cleanly when nothing subscribes: declare/resolve
    still succeed and no delivery rows appear."""
    node = _node(db_session)
    incident = declare_outage(db_session, node=node)
    resolve_outage(db_session, incident.id)
    assert _deliveries(db_session) == []
    assert incident.status == "resolved"


def test_payload_signature_round_trips(db_session):
    """The serialized delivery payload verifies against the same HMAC-SHA256
    the delivery task puts in X-Webhook-Signature-256."""
    _subscribe(db_session)
    declare_outage(db_session, node=_node(db_session))
    delivery = _deliveries(db_session)[0]

    payload_json = json.dumps(delivery.payload or {})
    secret = "s3cret"  # noqa: S105 - test-only literal
    signature = _compute_signature(payload_json, secret)
    expected = hmac.new(
        secret.encode(), payload_json.encode(), hashlib.sha256
    ).hexdigest()
    assert hmac.compare_digest(signature, expected)
