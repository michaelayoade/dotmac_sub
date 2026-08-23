"""Map Integrator installations to Sub-owned payment providers.

Revision ID: 550_integrator_provider_ref
Revises: 549_gateway_intent_lifecycle
Create Date: 2026-08-23

The nullable unique reference is additive.  Existing direct gateway paths keep
working with NULL; a future Integrator cutover explicitly maps each installation
before the product port can admit financial observations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "550_integrator_provider_ref"
down_revision: str | None = "549_gateway_intent_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payment_providers",
        sa.Column(
            "integrator_installation_ref",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_unique_constraint(
        "uq_payment_providers_integrator_installation_ref",
        "payment_providers",
        ["integrator_installation_ref"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_payment_providers_integrator_installation_ref",
        "payment_providers",
        type_="unique",
    )
    op.drop_column("payment_providers", "integrator_installation_ref")
