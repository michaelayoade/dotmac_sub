"""Service helpers for admin system webhook form/create/update pages."""

from __future__ import annotations

import logging
import secrets

from sqlalchemy.orm import Session

from app.models.webhook import WebhookEndpoint, WebhookEventType, WebhookSubscription
from app.services.common import coerce_uuid, validate_enum
from app.services.credential_crypto import encrypt_credential

logger = logging.getLogger(__name__)


def _event_type_options() -> list[dict[str, str]]:
    return [{"value": event.value, "label": event.value} for event in WebhookEventType]


def _normalize_events(events: list[str] | None) -> list[WebhookEventType]:
    normalized: list[WebhookEventType] = []
    seen: set[WebhookEventType] = set()
    for event in events or []:
        if not event:
            continue
        event_type = validate_enum(event, WebhookEventType, "events")
        if event_type in seen:
            continue
        seen.add(event_type)
        normalized.append(event_type)
    return normalized


def _sync_subscriptions(
    db: Session, endpoint: WebhookEndpoint, events: list[str] | None
) -> None:
    selected = set(_normalize_events(events))
    existing = {
        subscription.event_type: subscription
        for subscription in db.query(WebhookSubscription).filter(
            WebhookSubscription.endpoint_id == endpoint.id
        )
    }
    for event_type in selected:
        subscription = existing.get(event_type)
        if subscription:
            subscription.is_active = True
        else:
            db.add(
                WebhookSubscription(
                    endpoint_id=endpoint.id,
                    event_type=event_type,
                    is_active=True,
                )
            )
    for event_type, subscription in existing.items():
        if event_type not in selected:
            subscription.is_active = False


def get_webhook_new_form_context() -> dict:
    """Return default context fragment for webhook create form."""
    return {
        "endpoint": None,
        "subscribed_events": [],
        "event_types": _event_type_options(),
        "action_url": "/admin/system/webhooks",
        "error": None,
    }


def get_webhook_form_data(db: Session, endpoint_id: str | None = None):
    """Return webhook form data; None if endpoint id is invalid/not found."""
    endpoint = None
    if endpoint_id:
        endpoint = db.get(WebhookEndpoint, coerce_uuid(endpoint_id))
        if not endpoint:
            return None

    subscribed_events = []
    if endpoint:
        subscribed_events = [
            sub.event_type.value for sub in endpoint.subscriptions if sub.is_active
        ]

    return {
        "endpoint": endpoint,
        "subscribed_events": subscribed_events,
        "event_types": _event_type_options(),
    }


def create_webhook_endpoint(
    db: Session,
    *,
    name: str,
    url: str,
    secret: str | None,
    is_active: bool,
    events: list[str] | None = None,
) -> WebhookEndpoint:
    """Create and persist a webhook endpoint."""
    endpoint_secret = secret or secrets.token_urlsafe(32)
    endpoint = WebhookEndpoint(
        name=name,
        url=url,
        secret=encrypt_credential(endpoint_secret),
        is_active=is_active,
    )
    db.add(endpoint)
    db.flush()
    _sync_subscriptions(db, endpoint, events)
    db.commit()
    db.refresh(endpoint)
    return endpoint


def update_webhook_endpoint(
    db: Session,
    *,
    endpoint_id: str,
    name: str,
    url: str,
    secret: str | None,
    is_active: bool,
    events: list[str] | None = None,
) -> WebhookEndpoint | None:
    """Update an existing webhook endpoint."""
    endpoint = db.get(WebhookEndpoint, coerce_uuid(endpoint_id))
    if not endpoint:
        return None

    endpoint.name = name
    endpoint.url = url
    if secret:
        endpoint.secret = encrypt_credential(secret)
    endpoint.is_active = is_active
    _sync_subscriptions(db, endpoint, events)
    db.commit()
    return endpoint
