"""Detected-outage incident lifecycle columns (§7.6).

Evolves ``outage_incidents`` from operator-only (open/resolved) into the spine
of the classifier-driven, debounced lifecycle. Adds a ``detection_source``
discriminator ('operator' vs 'classifier'), the lifecycle timestamps
(``suspected_at`` / ``confirmed_at`` / ``cleared_at``), the classifier verdict
snapshot (``classification`` / ``confidence``), and a ``crm_ticket_id``
placeholder for the future CRM ticket link (nothing fires on it yet).

``status`` is deliberately left a String (validated in code) — the enum route
caused a prod migration collision in #876. Existing operator rows keep
``detection_source = 'operator'`` via the server default; classifier incidents
set 'classifier' at insert time.

Revision ID: 217_outage_incident_lifecycle
Revises: 216_add_outage_notification_dispatches
Create Date: 2026-07-06
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "217_outage_incident_lifecycle"
down_revision = "216_add_outage_notification_dispatches"
branch_labels = None
depends_on = None

_TABLE = "outage_incidents"

# (name, column factory) — additive, all idempotent-guarded.
_COLUMNS = (
    (
        "detection_source",
        lambda: sa.Column(
            "detection_source",
            sa.String(length=20),
            nullable=False,
            server_default="operator",
        ),
    ),
    ("classification", lambda: sa.Column("classification", sa.String(length=40))),
    ("confidence", lambda: sa.Column("confidence", sa.Float())),
    ("crm_ticket_id", lambda: sa.Column("crm_ticket_id", sa.String(length=120))),
    ("suspected_at", lambda: sa.Column("suspected_at", sa.DateTime(timezone=True))),
    ("confirmed_at", lambda: sa.Column("confirmed_at", sa.DateTime(timezone=True))),
    ("cleared_at", lambda: sa.Column("cleared_at", sa.DateTime(timezone=True))),
)


def _has_column(inspector, table: str, column: str) -> bool:
    if table not in inspector.get_table_names():
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    for name, factory in _COLUMNS:
        if not _has_column(inspector, _TABLE, name):
            op.add_column(_TABLE, factory())
    # Existing operator/auto-detect rows are already 'operator' via the server
    # default; keep it explicit so a re-run over a partially-migrated table
    # never leaves a NULL discriminator.
    op.execute(
        sa.text(
            "UPDATE outage_incidents "
            "SET detection_source = 'operator' WHERE detection_source IS NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    for name, _ in _COLUMNS:
        if _has_column(inspector, _TABLE, name):
            op.drop_column(_TABLE, name)
