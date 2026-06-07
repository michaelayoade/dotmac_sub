"""Add connector_config_id to alert_notification_policies.

Revision ID: 118_add_connector_config_id_to_alert_notification_policies
Revises: 117_add_payment_webhook_dead_letters
Create Date: 2026-06-07

AlertNotificationPolicyCreate accepts connector_config_id (so an alert policy can
route through a specific connector), but the model/table lacked the column — so
POST /api/v1/alert-notification-policies 500'd with
``TypeError: 'connector_config_id' is an invalid keyword argument``. Add the
nullable FK to connector_configs to match the schema.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "118_add_connector_config_id_to_alert_notification_policies"
down_revision = "117_add_payment_webhook_dead_letters"
branch_labels = None
depends_on = None

_TABLE = "alert_notification_policies"
_COLUMN = "connector_config_id"
_FK = "fk_alert_notification_policies_connector_config_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(_FK, _TABLE, "connector_configs", [_COLUMN], ["id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        return
    try:
        op.drop_constraint(_FK, _TABLE, type_="foreignkey")
    except Exception:  # pragma: no cover - dialects without named FK drop
        pass
    op.drop_column(_TABLE, _COLUMN)
