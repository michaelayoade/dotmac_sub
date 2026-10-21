"""Converge customer connection type defaults across migration paths.

Revision 671 declares these defaults when it creates the table. The fresh
metadata bootstrap can create the table earlier, so 671's create-table guard
previously left fresh installations without them. This additive repair changes
only column defaults: no rows, nullability or published revisions are changed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "684_customer_connection_type_defaults"
down_revision: str | None = "683_field_lifecycle_connection_types_merge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.alter_column(
        "customer_connection_types",
        "sort_order",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default="0",
    )
    op.alter_column(
        "customer_connection_types",
        "is_active",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.true(),
    )


def downgrade() -> None:
    """Keep defaults already required by published revision 671.

    The predecessor schema differs by installation path, and no provenance
    identifies whether these defaults were absent before this repair. Removing
    them would undo valid 671 behavior on existing installations. This repair
    therefore preserves the canonical defaults when separating the histories.
    """
