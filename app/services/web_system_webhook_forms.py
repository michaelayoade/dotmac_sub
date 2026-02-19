"""Service helpers for admin system webhook form/create/update pages."""

from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from app.models.webhook import WebhookEndpoint
from app.services.common import coerce_uuid


def get_webhook_new_form_context() -> dict:
    """Return default context fragment for webhook create form."""
    return {
        "endpoint": None,
        "subscribed_events": [],
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
        subscribed_events = [sub.event_type.value for sub in endpoint.subscriptions if sub.is_active]

    return {
        "endpoint": endpoint,
        "subscribed_events": subscribed_events,
    }


def create_webhook_endpoint(
    db: Session,
    *,
    name: str,
    url: str,
    secret: str | None,
    is_active: bool,
) -> WebhookEndpoint:
    """Create and persist a webhook endpoint."""
    endpoint_secret = secret or secrets.token_urlsafe(32)
    endpoint = WebhookEndpoint(
        name=name,
        url=url,
        secret=endpoint_secret,
        is_active=is_active,
    )
    db.add(endpoint)
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
) -> WebhookEndpoint | None:
    """Update an existing webhook endpoint."""
    endpoint = db.get(WebhookEndpoint, coerce_uuid(endpoint_id))
    if not endpoint:
        return None

    endpoint.name = name
    endpoint.url = url
    if secret:
        endpoint.secret = secret
    endpoint.is_active = is_active
    db.commit()
    return endpoint
