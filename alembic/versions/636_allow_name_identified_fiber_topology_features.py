"""Allow mixed map staging rows to use a display name without a source ID.

Revision ID: 636_allow_name_identified_fiber_topology_features
Revises: 635_subscription_pause_lifecycle
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "636_allow_name_identified_fiber_topology_features"
down_revision: str | None = "635_subscription_pause_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "fiber_topology_staged_features"
_CONSTRAINT = "ck_fiber_topology_staged_feature_identity"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        "external_id IS NOT NULL OR display_name IS NOT NULL "
        "OR match_status = 'blocked'",
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        "external_id IS NOT NULL OR match_status = 'blocked'",
    )
