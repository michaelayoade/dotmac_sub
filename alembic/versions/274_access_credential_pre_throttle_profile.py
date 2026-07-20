"""Remember the profile a credential had before a collections throttle.

The dunning throttle replaced ``access_credentials.radius_profile_id`` and wrote
the previous value to a LOG LINE. Nothing persisted it, so once the customer paid
there was no way to give their speed back: ``_restore_throttle`` guessed from the
offer's profile, or set NULL, silently discarding any admin credential-level
override. A customer who paid in full stayed rate-limited, and
``radius_population`` re-applied the throttle on every sweep.

The throttle is a temporary override, so the value it overrides must survive it.

No backfill. A credential currently throttled has no recoverable previous profile
— it was never stored — so it stays NULL and ``_restore_throttle`` keeps its
legacy offer-profile fallback for exactly that cohort. Inventing a value here
would be guessing at a customer's contracted speed.

Revision ID: 274_access_credential_pre_throttle_profile
Revises: 273_communication_suppressions
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "274_access_credential_pre_throttle_profile"
down_revision = "273_communication_suppressions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "access_credentials",
        sa.Column(
            "pre_throttle_radius_profile_id", sa.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_access_credentials_pre_throttle_radius_profile_id",
        "access_credentials",
        "radius_profiles",
        ["pre_throttle_radius_profile_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_access_credentials_pre_throttle_radius_profile_id",
        "access_credentials",
        type_="foreignkey",
    )
    op.drop_column("access_credentials", "pre_throttle_radius_profile_id")
