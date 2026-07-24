"""Cut payment gateway control over to connector capability bindings.

Revision ID: 412_payment_gateway_control_plane
Revises: 411_uisp_olt_config_pack_exemption
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "412_payment_gateway_control_plane"
down_revision = "411_uisp_olt_config_pack_exemption"
branch_labels = None
depends_on = None

_RETIRED_SETTINGS = (
    "payment_gateway_failover_enabled",
    "payment_gateway_primary_provider",
    "payment_gateway_secondary_provider",
)


def upgrade() -> None:
    op.add_column(
        "topup_intents",
        sa.Column("capability_binding_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_topup_intents_capability_binding_id",
        "topup_intents",
        "integration_capability_bindings",
        ["capability_binding_id"],
        ["id"],
    )
    op.create_index(
        "ix_topup_intents_capability_binding_id",
        "topup_intents",
        ["capability_binding_id"],
    )
    op.execute(
        sa.text(
            "DELETE FROM domain_settings "
            "WHERE domain = 'billing' AND key IN "
            "('payment_gateway_failover_enabled', "
            "'payment_gateway_primary_provider', "
            "'payment_gateway_secondary_provider')"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_topup_intents_capability_binding_id",
        table_name="topup_intents",
    )
    op.drop_constraint(
        "fk_topup_intents_capability_binding_id",
        "topup_intents",
        type_="foreignkey",
    )
    op.drop_column("topup_intents", "capability_binding_id")
    # Retired routing settings are deliberately not recreated.
