"""Adopt the Paystack manifest deployed by payment control-plane cutover.

Revision ID: 414_adopt_paystack_manifest_pin
Revises: 413_audit_actor_label

PR #1567 changed the Paystack manifest while retaining connector version
1.0.0. Existing installations therefore kept the prior digest and correctly
failed closed at runtime. This migration adopts only that exact known pin.
Unknown pins remain untouched so the post-migration deployment verifier can
block an unsafe release.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "414_adopt_paystack_manifest_pin"
down_revision = "413_audit_actor_label"
branch_labels = None
depends_on = None

CONNECTOR_KEY = "paystack"
CONNECTOR_VERSION = "1.0.0"
PRE_CONTROL_PLANE_DIGEST = (
    "53791d3e2e06fe1ca128a0e3e8ced86549392af7b6131f61bd21044d71aafc6e"
)
CONTROL_PLANE_DIGEST = (
    "9f1e314860294696c825d8d49d300df903ced6c319b406f295047d25585e836c"
)
MIGRATION_ACTOR = "migration:414_adopt_paystack_manifest_pin"


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE integration_installations
            SET manifest_digest = :current_digest,
                updated_at = CURRENT_TIMESTAMP,
                updated_by = :migration_actor
            WHERE connector_key = :connector_key
              AND connector_version = :connector_version
              AND manifest_digest = :prior_digest
              AND state <> 'retired'
            """
        ),
        {
            "connector_key": CONNECTOR_KEY,
            "connector_version": CONNECTOR_VERSION,
            "prior_digest": PRE_CONTROL_PLANE_DIGEST,
            "current_digest": CONTROL_PLANE_DIGEST,
            "migration_actor": MIGRATION_ACTOR,
        },
    )


def downgrade() -> None:
    raise RuntimeError(
        "Paystack manifest-pin adoption is irreversible; restoring the prior "
        "digest would make current connector execution fail closed"
    )
