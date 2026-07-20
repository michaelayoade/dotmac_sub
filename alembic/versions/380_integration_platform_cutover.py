"""Retire superseded integration storage after the typed-platform cutover.

Revision ID: 380_integration_platform_cutover
Revises: 379_integration_inbox

This is intentionally an irreversible authority cutover. Installations,
capability bindings, the integration inbox, and integration deliveries are the
only supported runtime records after this revision.
"""

import sqlalchemy as sa

from alembic import op

revision = "380_integration_platform_cutover"
down_revision = "379_integration_inbox"
branch_labels = None
depends_on = None


_RETIRED_SETTING_KEYS = (
    "paystack_secret_key",
    "paystack_public_key",
    "flutterwave_secret_key",
    "flutterwave_public_key",
    "flutterwave_secret_hash",
    "payment_gateway_timeout_seconds",
    "crm_phase3_native_sync_enabled",
    "whatsapp_provider",
    "whatsapp_api_key",
    "whatsapp_api_secret",
    "whatsapp_phone_number",
    "whatsapp_waba_id",
    "whatsapp_webhook_url",
    "whatsapp_message_templates",
    "whatsapp_api_timeout_seconds",
    "meta_webhook_verify_token",
)


def upgrade() -> None:
    # Outbound events are now capability subscriptions + integration_deliveries.
    op.drop_table("webhook_deliveries")
    op.drop_table("webhook_subscriptions")
    op.drop_table("webhook_endpoints")

    # Arbitrary CLI/internal hooks are not an approved connector runtime.
    op.drop_table("integration_hook_executions")
    op.drop_table("integration_hooks")

    # Inbound deduplication and failures share integration_inbox.
    op.drop_table("crm_webhook_deliveries")
    op.drop_table("payment_webhook_dead_letters")

    # Provider rows retain business routing/provenance only. Connection and
    # secret ownership moved to version-pinned integration installations.
    op.execute(
        "ALTER TABLE payment_providers DROP CONSTRAINT IF EXISTS "
        "payment_providers_connector_config_id_fkey"
    )
    op.drop_column("payment_providers", "connector_config_id")
    op.drop_column("payment_providers", "webhook_secret_ref")

    domain_settings = sa.table(
        "domain_settings",
        sa.column("key", sa.String()),
    )
    op.execute(
        domain_settings.delete().where(domain_settings.c.key.in_(_RETIRED_SETTING_KEYS))
    )

    for enum_name in (
        "webhookdeliverystatus",
        "webhookeventtype",
        "integrationhooktype",
        "integrationhookauthtype",
        "integrationhookexecutionstatus",
        "paymentwebhookdeadletterstatus",
    ):
        op.execute(sa.text(f'DROP TYPE IF EXISTS "{enum_name}"'))


def downgrade() -> None:
    raise RuntimeError("integration platform authority cutover is irreversible")
