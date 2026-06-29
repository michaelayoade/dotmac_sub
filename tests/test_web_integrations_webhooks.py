from pathlib import Path
from uuid import uuid4

from app.models.webhook import WebhookEventType, WebhookSubscription
from app.services import web_integrations
from app.services.credential_crypto import decrypt_credential, is_encrypted


def test_integrations_webhook_create_encrypts_secret_and_subscribes_events(db_session):
    endpoint = web_integrations.create_webhook_endpoint(
        db_session,
        name="Partner webhook",
        url=f"https://example.com/{uuid4()}",
        connector_config_id=None,
        secret="partner-secret",
        event_types=["subscriber.created", "invoice.paid"],
        is_active=True,
    )

    assert endpoint.secret != "partner-secret"
    assert is_encrypted(endpoint.secret)
    assert decrypt_credential(endpoint.secret) == "partner-secret"
    subscriptions = (
        db_session.query(WebhookSubscription)
        .filter(WebhookSubscription.endpoint_id == endpoint.id)
        .all()
    )
    assert {subscription.event_type for subscription in subscriptions} == {
        WebhookEventType.subscriber_created,
        WebhookEventType.invoice_paid,
    }


def test_integrations_webhook_templates_do_not_render_secret_values():
    new_template = Path("templates/admin/integrations/webhooks/new.html").read_text()
    detail_template = Path(
        "templates/admin/integrations/webhooks/detail.html"
    ).read_text()

    assert 'type="password" name="secret"' in new_template
    assert "form.secret" not in new_template
    assert "endpoint.secret[-4:]" not in detail_template
    assert "Configured" in detail_template


def test_connector_detail_exposes_check_connection_action():
    template = Path("templates/admin/integrations/connectors/detail.html").read_text()

    assert "Check Connection" in template
    assert "/embed?check=1" in template
