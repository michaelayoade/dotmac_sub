"""Allow notification templates to share code across channels.

Revision ID: b8c9d0e1f2a3
Revises: z7b8c9d0e1f2
Create Date: 2026-03-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "z7b8c9d0e1f2"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_LEGACY_CODE_RENAMES = {
    "subscription_created_sms": ("subscription_created", "sms"),
    "subscription_activated_sms": ("subscription_activated", "sms"),
    "subscription_suspended_sms": ("subscription_suspended", "sms"),
    "suspension_warning_sms": ("suspension_warning", "sms"),
    "subscription_expiring_sms": ("subscription_expiring", "sms"),
    "invoice_sent_sms": ("invoice_sent", "sms"),
    "invoice_overdue_sms": ("invoice_overdue", "sms"),
    "payment_received_sms": ("payment_received", "sms"),
    "provisioning_completed_sms": ("provisioning_completed", "sms"),
}


def _drop_single_code_unique(bind) -> None:
    inspector = inspect(bind)
    uniques = inspector.get_unique_constraints("notification_templates")
    for unique in uniques:
        columns = unique.get("column_names") or []
        name = unique.get("name")
        if name and columns == ["code"]:
            op.drop_constraint(name, "notification_templates", type_="unique")


def _create_code_channel_unique(bind) -> None:
    inspector = inspect(bind)
    uniques = inspector.get_unique_constraints("notification_templates")
    for unique in uniques:
        columns = unique.get("column_names") or []
        if columns == ["code", "channel"]:
            return
    op.create_unique_constraint(
        "uq_notification_templates_code_channel",
        "notification_templates",
        ["code", "channel"],
    )


def upgrade() -> None:
    bind = op.get_bind()

    _drop_single_code_unique(bind)

    for old_code, (new_code, channel) in _LEGACY_CODE_RENAMES.items():
        op.execute(
            sa.text(
                """
                UPDATE notification_templates
                SET code = :new_code
                WHERE code = :old_code
                  AND channel::text = :channel
                """
            ).bindparams(new_code=new_code, old_code=old_code, channel=channel)
        )

    _create_code_channel_unique(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    uniques = inspector.get_unique_constraints("notification_templates")
    for unique in uniques:
        columns = unique.get("column_names") or []
        name = unique.get("name")
        if name and columns == ["code", "channel"]:
            op.drop_constraint(name, "notification_templates", type_="unique")

    for old_code, (new_code, channel) in _LEGACY_CODE_RENAMES.items():
        op.execute(
            sa.text(
                """
                UPDATE notification_templates
                SET code = :old_code
                WHERE code = :new_code
                  AND channel::text = :channel
                """
            ).bindparams(new_code=new_code, old_code=old_code, channel=channel)
        )

    existing_uniques = inspect(bind).get_unique_constraints("notification_templates")
    if not any((uc.get("column_names") or []) == ["code"] for uc in existing_uniques):
        op.create_unique_constraint(
            "uq_notification_templates_code",
            "notification_templates",
            ["code"],
        )
