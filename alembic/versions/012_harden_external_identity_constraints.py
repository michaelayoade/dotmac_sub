"""harden external identity constraints

Revision ID: 012_harden_external_identity_constraints
Revises: 011_add_olt_pon_repair_operation_type
Create Date: 2026-04-05
"""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "012_harden_external_identity_constraints"
down_revision = "011_add_olt_pon_repair_operation_type"
branch_labels = None
depends_on = None


def _index_exists(index_name: str) -> bool:
    conn = op.get_bind()
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND indexname = :index_name
            """
        ),
        {"index_name": index_name},
    ).fetchone()
    return row is not None


def _assert_no_duplicates(label: str, sql: str) -> None:
    conn = op.get_bind()
    rows = conn.execute(text(sql)).fetchall()
    if not rows:
        return
    sample = "; ".join(" | ".join(str(value) for value in row) for row in rows[:5])
    raise RuntimeError(f"Cannot enforce {label}; duplicate rows exist: {sample}")


def _create_index(name: str, ddl: str) -> None:
    if _index_exists(name):
        return
    op.execute(text(ddl))


def upgrade() -> None:
    _assert_no_duplicates(
        "uq_tr069_cpe_devices_active_genieacs_device_id",
        """
        SELECT genieacs_device_id, COUNT(*)
        FROM tr069_cpe_devices
        WHERE is_active = TRUE
          AND genieacs_device_id IS NOT NULL
        GROUP BY genieacs_device_id
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, genieacs_device_id
        """,
    )
    _create_index(
        "uq_tr069_cpe_devices_active_genieacs_device_id",
        """
        CREATE UNIQUE INDEX uq_tr069_cpe_devices_active_genieacs_device_id
        ON tr069_cpe_devices (genieacs_device_id)
        WHERE is_active AND genieacs_device_id IS NOT NULL
        """,
    )

    _assert_no_duplicates(
        "uq_subscribers_splynx_customer_id",
        """
        SELECT splynx_customer_id, COUNT(*)
        FROM subscribers
        WHERE splynx_customer_id IS NOT NULL
        GROUP BY splynx_customer_id
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, splynx_customer_id
        """,
    )
    _create_index(
        "uq_subscribers_splynx_customer_id",
        """
        CREATE UNIQUE INDEX uq_subscribers_splynx_customer_id
        ON subscribers (splynx_customer_id)
        WHERE splynx_customer_id IS NOT NULL
        """,
    )

    _assert_no_duplicates(
        "uq_invoices_active_splynx_invoice_id",
        """
        SELECT splynx_invoice_id, COUNT(*)
        FROM invoices
        WHERE is_active = TRUE
          AND splynx_invoice_id IS NOT NULL
        GROUP BY splynx_invoice_id
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, splynx_invoice_id
        """,
    )
    _create_index(
        "uq_invoices_active_splynx_invoice_id",
        """
        CREATE UNIQUE INDEX uq_invoices_active_splynx_invoice_id
        ON invoices (splynx_invoice_id)
        WHERE is_active AND splynx_invoice_id IS NOT NULL
        """,
    )

    _assert_no_duplicates(
        "uq_payments_active_external_id",
        """
        SELECT provider_id, external_id, COUNT(*)
        FROM payments
        WHERE is_active = TRUE
          AND provider_id IS NOT NULL
          AND external_id IS NOT NULL
        GROUP BY provider_id, external_id
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, provider_id, external_id
        """,
    )
    _create_index(
        "uq_payments_active_external_id",
        """
        CREATE UNIQUE INDEX uq_payments_active_external_id
        ON payments (provider_id, external_id)
        WHERE is_active AND provider_id IS NOT NULL AND external_id IS NOT NULL
        """,
    )

    _assert_no_duplicates(
        "uq_payments_active_splynx_payment_id",
        """
        SELECT splynx_payment_id, COUNT(*)
        FROM payments
        WHERE is_active = TRUE
          AND splynx_payment_id IS NOT NULL
        GROUP BY splynx_payment_id
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, splynx_payment_id
        """,
    )
    _create_index(
        "uq_payments_active_splynx_payment_id",
        """
        CREATE UNIQUE INDEX uq_payments_active_splynx_payment_id
        ON payments (splynx_payment_id)
        WHERE is_active AND splynx_payment_id IS NOT NULL
        """,
    )

    _assert_no_duplicates(
        "uq_payment_provider_events_external_id",
        """
        SELECT provider_id, external_id, COUNT(*)
        FROM payment_provider_events
        WHERE external_id IS NOT NULL
        GROUP BY provider_id, external_id
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, provider_id, external_id
        """,
    )
    _create_index(
        "uq_payment_provider_events_external_id",
        """
        CREATE UNIQUE INDEX uq_payment_provider_events_external_id
        ON payment_provider_events (provider_id, external_id)
        WHERE external_id IS NOT NULL
        """,
    )

    _assert_no_duplicates(
        "uq_network_devices_active_splynx_monitoring_id",
        """
        SELECT splynx_monitoring_id, COUNT(*)
        FROM network_devices
        WHERE is_active = TRUE
          AND splynx_monitoring_id IS NOT NULL
        GROUP BY splynx_monitoring_id
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, splynx_monitoring_id
        """,
    )
    _create_index(
        "uq_network_devices_active_splynx_monitoring_id",
        """
        CREATE UNIQUE INDEX uq_network_devices_active_splynx_monitoring_id
        ON network_devices (splynx_monitoring_id)
        WHERE is_active AND splynx_monitoring_id IS NOT NULL
        """,
    )

    _assert_no_duplicates(
        "uq_notification_deliveries_provider_message",
        """
        SELECT provider, provider_message_id, COUNT(*)
        FROM notification_deliveries
        WHERE is_active = TRUE
          AND provider IS NOT NULL
          AND provider_message_id IS NOT NULL
        GROUP BY provider, provider_message_id
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, provider, provider_message_id
        """,
    )
    _create_index(
        "uq_notification_deliveries_provider_message",
        """
        CREATE UNIQUE INDEX uq_notification_deliveries_provider_message
        ON notification_deliveries (provider, provider_message_id)
        WHERE is_active AND provider IS NOT NULL AND provider_message_id IS NOT NULL
        """,
    )

    _assert_no_duplicates(
        "uq_communication_logs_channel_external_id",
        """
        SELECT channel, external_id, COUNT(*)
        FROM communication_logs
        WHERE external_id IS NOT NULL
        GROUP BY channel, external_id
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, channel, external_id
        """,
    )
    _create_index(
        "uq_communication_logs_channel_external_id",
        """
        CREATE UNIQUE INDEX uq_communication_logs_channel_external_id
        ON communication_logs (channel, external_id)
        WHERE external_id IS NOT NULL
        """,
    )

    _assert_no_duplicates(
        "uq_communication_logs_channel_splynx_message_id",
        """
        SELECT channel, splynx_message_id, COUNT(*)
        FROM communication_logs
        WHERE splynx_message_id IS NOT NULL
        GROUP BY channel, splynx_message_id
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, channel, splynx_message_id
        """,
    )
    _create_index(
        "uq_communication_logs_channel_splynx_message_id",
        """
        CREATE UNIQUE INDEX uq_communication_logs_channel_splynx_message_id
        ON communication_logs (channel, splynx_message_id)
        WHERE splynx_message_id IS NOT NULL
        """,
    )


def downgrade() -> None:
    for index_name in (
        "uq_communication_logs_channel_splynx_message_id",
        "uq_communication_logs_channel_external_id",
        "uq_notification_deliveries_provider_message",
        "uq_network_devices_active_splynx_monitoring_id",
        "uq_payment_provider_events_external_id",
        "uq_payments_active_splynx_payment_id",
        "uq_payments_active_external_id",
        "uq_invoices_active_splynx_invoice_id",
        "uq_subscribers_splynx_customer_id",
        "uq_tr069_cpe_devices_active_genieacs_device_id",
    ):
        op.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
