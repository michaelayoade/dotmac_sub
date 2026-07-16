"""Remove legacy feature switches that have no behavior consumer.

Revision ID: 283_canonical_feature_controls
Revises: 282_control_plane_cleanup
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "308_canonical_feature_controls"
down_revision = "307_control_plane_cleanup"
branch_labels = None
depends_on = None

_INERT_LEGACY_FEATURE_KEYS = (
    "module_billing_invoices_enabled",
    "module_billing_payments_enabled",
    "module_billing_credit_notes_enabled",
    "module_billing_payment_statements_enabled",
    "module_billing_proforma_enabled",
    "module_billing_vouchers_enabled",
    "module_catalog_internet_plans_enabled",
    "module_catalog_fup_enabled",
    "module_catalog_bundles_enabled",
    "module_catalog_onetime_enabled",
    "module_catalog_recurring_enabled",
    "module_customer_discounts_enabled",
    "module_customer_vouchers_enabled",
    "module_network_sites_enabled",
    "module_network_cpe_enabled",
    "module_network_tr069_enabled",
    "module_network_router_enabled",
    "module_network_ip_pools_enabled",
    "module_network_hardware_enabled",
)

_DELETE_SETTING = sa.text(
    "DELETE FROM domain_settings WHERE domain = CAST(:domain AS settingdomain) "
    "AND key = :key"
)


def upgrade() -> None:
    for key in _INERT_LEGACY_FEATURE_KEYS:
        op.execute(_DELETE_SETTING.bindparams(domain="modules", key=key))


def downgrade() -> None:
    # Intentional no-op. These rows had no canonical behavior consumer.
    pass
