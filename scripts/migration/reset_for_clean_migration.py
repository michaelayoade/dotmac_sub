"""Reset DotMac Sub database for clean Splynx re-migration.

Preserves:
- roles, permissions, role_permissions (RBAC config)
- domain_settings (system configuration)
- scheduled_tasks (Celery beat config)
- document_sequences (invoice numbering)
- table_column_default_config (UI defaults)
- user_credentials + system_users for REAL admin accounts (those with credentials)
- subscriber_roles, system_user_roles for admin users

Truncates everything else (subscribers, subscriptions, invoices, payments, etc.)
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import text

from scripts.migration.db_connections import dotmac_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Tables to TRUNCATE (order matters — children before parents due to FKs)
TRUNCATE_TABLES = [
    # Splynx archives
    "splynx_archived_ticket_messages",
    "splynx_archived_tickets",
    "splynx_archived_quote_items",
    "splynx_archived_quotes",
    # Comms & notifications
    "customer_notification_events",
    "eta_updates",
    "survey_responses",
    # Webhooks
    "webhook_deliveries",
    "webhook_subscriptions",
    # Events & audit
    "event_stores",
    "audit_events",
    # Dunning & collections
    "dunning_action_logs",
    "dunning_cases",
    "payment_arrangement_installments",
    "payment_arrangements",
    # Billing — children first
    "invoice_pdf_exports",
    "credit_note_applications",
    "credit_note_lines",
    "credit_notes",
    "payment_allocations",
    "ledger_entries",
    "invoice_lines",
    "invoices",
    "payment_provider_events",
    "payments",
    "bank_reconciliation_items",
    "bank_reconciliation_runs",
    "billing_runs",
    # Usage & bandwidth
    "usage_charges",
    "usage_records",
    "quota_buckets",
    "usage_rating_runs",
    "radius_accounting_sessions",
    "bandwidth_samples",
    # Subscriptions & catalog dependencies
    "subscription_lifecycle_events",
    "subscription_change_requests",
    "subscription_add_ons",
    # Provisioning
    "provisioning_logs",
    "provisioning_runs",
    "provisioning_steps",
    "provisioning_tasks",
    "service_state_transitions",
    "install_appointments",
    "service_orders",
    # Network assignments
    "ont_assignments",
    "splitter_port_assignments",
    "ip_assignments",
    "ipv4_addresses",
    "ipv6_addresses",
    # Subscriptions
    "subscriptions",
    # Subscriber children
    "contract_signatures",
    "subscriber_custom_fields",
    "subscriber_channels",
    "addresses",
    "payment_methods",
    "bank_accounts",
    # RADIUS
    "radius_users",
    "radius_clients",
    "access_credentials",
    # Network — children
    "nas_connection_rules",
    "nas_config_backups",
    "queue_mappings",
    # CPE
    "tr069_parameters",
    "tr069_jobs",
    "tr069_sessions",
    "tr069_cpe_devices",
    "cpe_devices",
    # External references
    "external_references",
    # Subscribers (main)
    "subscribers",
    # Catalog offer dependencies
    "offer_radius_profiles",
    "offer_add_ons",
    "offer_prices",
    "offer_versions",
    "fup_rules",
    "fup_policies",
    "usage_allowances",
    "catalog_offers",
    # Add-ons
    "add_on_prices",
    "add_ons",
    # Categories
    "plan_categories",
    # Network infrastructure
    "nas_devices",
    "nas_vendors",
    "ip_blocks",
    "ip_pools",
    # Resellers & orgs
    "organizations",
    "resellers",
    # Tax
    "tax_rates",
    # Payment channels
    "payment_channel_accounts",
    "payment_channels",
    "payment_providers",
    "collection_accounts",
    # Tickets
    "ticket_sla_events",
    "ticket_comments",
    "ticket_assignees",
    "ticket_links",
    "ticket_merges",
    "tickets",
    # Admin users — only delete the bulk-imported ones
    # (handled separately below)
]

# Tables with data to KEEP
PRESERVE_TABLES = [
    "roles",
    "permissions",
    "role_permissions",
    "domain_settings",
    "scheduled_tasks",
    "document_sequences",
    "table_column_default_config",
    "table_column_config",
    "legal_documents",
    "notification_templates",
    "kpi_configs",
]


def reset_database(dry_run: bool = True) -> None:
    """Truncate subscriber/billing data, preserve system config."""
    with dotmac_session() as db:
        if dry_run:
            logger.info("=== DRY RUN — no changes will be made ===")

        # Count before
        logger.info("--- Current data counts ---")
        for table in TRUNCATE_TABLES:
            try:
                cnt = db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
                if cnt and cnt > 0:
                    logger.info("  %s: %d rows (will truncate)", table, cnt)
            except Exception:
                db.rollback()

        logger.info("--- Preserved data ---")
        for table in PRESERVE_TABLES:
            try:
                cnt = db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
                logger.info("  %s: %d rows (keeping)", table, cnt)
            except Exception:
                db.rollback()

        # Count real admin system_users (those with credentials)
        admin_count = db.execute(
            text(
                "SELECT COUNT(*) FROM system_users s "
                "JOIN user_credentials uc ON uc.system_user_id = s.id"
            )
        ).scalar()
        total_system_users = db.execute(
            text("SELECT COUNT(*) FROM system_users")
        ).scalar()
        logger.info(
            "  system_users: %d total, %d real admins (keeping admins, removing %d bulk-imported)",
            total_system_users,
            admin_count,
            total_system_users - admin_count,
        )

        if dry_run:
            logger.info("=== DRY RUN complete. Run with --execute to apply. ===")
            return

        # Execute truncation
        logger.info("--- Truncating tables ---")

        # Disable FK checks temporarily
        db.execute(text("SET session_replication_role = 'replica'"))

        for table in TRUNCATE_TABLES:
            try:
                result = db.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
                logger.info("  Truncated %s", table)
            except Exception as e:
                logger.warning("  Skip %s: %s", table, e)
                db.rollback()
                db.execute(text("SET session_replication_role = 'replica'"))

        # Clean bulk-imported system_users (keep those with credentials)
        db.execute(
            text(
                "DELETE FROM system_user_roles WHERE system_user_id NOT IN "
                "(SELECT system_user_id FROM user_credentials)"
            )
        )
        db.execute(
            text(
                "DELETE FROM system_user_permissions WHERE system_user_id NOT IN "
                "(SELECT system_user_id FROM user_credentials)"
            )
        )
        db.execute(
            text(
                "DELETE FROM sessions WHERE system_user_id IS NOT NULL "
                "AND system_user_id NOT IN "
                "(SELECT system_user_id FROM user_credentials)"
            )
        )
        db.execute(
            text(
                "DELETE FROM system_users WHERE id NOT IN "
                "(SELECT system_user_id FROM user_credentials WHERE system_user_id IS NOT NULL)"
            )
        )
        logger.info("  Cleaned bulk-imported system_users")

        # Re-enable FK checks
        db.execute(text("SET session_replication_role = 'origin'"))

        db.commit()
        logger.info("=== Database reset complete ===")

        # Verify
        for table in ["subscribers", "subscriptions", "invoices", "payments", "system_users"]:
            cnt = db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            logger.info("  POST-RESET %s: %d", table, cnt)


if __name__ == "__main__":
    if "--execute" in sys.argv:
        reset_database(dry_run=False)
    else:
        reset_database(dry_run=True)
        print("\nTo execute: poetry run python scripts/migration/reset_for_clean_migration.py --execute")
