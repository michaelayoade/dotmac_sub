from pathlib import Path
from uuid import uuid4

from app.models.webhook import WebhookEventType, WebhookSubscription
from app.services import web_system_webhook_forms as forms
from app.services.credential_crypto import decrypt_credential


def _url() -> str:
    return f"https://example.com/{uuid4()}"


def test_system_webhook_create_syncs_event_subscriptions(db_session):
    endpoint = forms.create_webhook_endpoint(
        db_session,
        name="Billing events",
        url=_url(),
        secret="shared-secret",
        is_active=True,
        events=["invoice.created", "invoice.paid", "invoice.created"],
    )

    subscriptions = (
        db_session.query(WebhookSubscription)
        .filter(WebhookSubscription.endpoint_id == endpoint.id)
        .all()
    )

    assert decrypt_credential(endpoint.secret) == "shared-secret"
    assert {subscription.event_type for subscription in subscriptions} == {
        WebhookEventType.invoice_created,
        WebhookEventType.invoice_paid,
    }
    assert all(subscription.is_active for subscription in subscriptions)


def test_system_webhook_update_preserves_secret_and_resyncs_events(db_session):
    endpoint = forms.create_webhook_endpoint(
        db_session,
        name="Operations events",
        url=_url(),
        secret="original-secret",
        is_active=True,
        events=["provisioning.completed", "network.alert"],
    )
    original_secret = endpoint.secret

    updated = forms.update_webhook_endpoint(
        db_session,
        endpoint_id=str(endpoint.id),
        name="Operations events",
        url=endpoint.url,
        secret="",
        is_active=False,
        events=["network.alert", "usage.recorded"],
    )

    assert updated is not None
    assert updated.secret == original_secret
    assert updated.is_active is False
    subscriptions = (
        db_session.query(WebhookSubscription)
        .filter(WebhookSubscription.endpoint_id == endpoint.id)
        .all()
    )
    active_events = {
        subscription.event_type
        for subscription in subscriptions
        if subscription.is_active
    }
    inactive_events = {
        subscription.event_type
        for subscription in subscriptions
        if not subscription.is_active
    }
    assert active_events == {
        WebhookEventType.network_alert,
        WebhookEventType.usage_recorded,
    }
    assert WebhookEventType.provisioning_completed in inactive_events


def test_system_webhook_form_masks_stored_secret_and_renders_enum_events():
    template = Path("templates/admin/system/webhook_form.html").read_text()

    assert "endpoint.secret" not in template
    assert "Leave blank to keep current secret" in template
    assert "{% for event_type in event_types %}" in template
