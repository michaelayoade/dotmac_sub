"""Add receipt identity and link to the default payment email.

Revision ID: 391_payment_receipt_notification
Revises: 390_provisioning_lifecycle_sot

Only the exact historical seed copy is updated. Customized operator content is
left untouched, and the migration does not activate a disabled channel.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "391_payment_receipt_notification"
down_revision = "390_provisioning_lifecycle_sot"
branch_labels = None
depends_on = None

_OLD_SUBJECT = "Payment received — thank you"
_OLD_BODY = (
    "Dear {subscriber_name},\n\n"
    "We have received your payment of {amount}. Thank you!\n\n"
    "Your account balance has been updated accordingly.\n\n"
    "If you have questions about your billing, please contact support."
)
_NEW_SUBJECT = "Payment receipt {receipt_number}"
_NEW_BODY = (
    "Dear {subscriber_name},\n\n"
    "We have received your payment of {amount}. Thank you!\n\n"
    "Receipt: {receipt_number}\n"
    "View or download: {receipt_url}\n\n"
    "You can review how the payment was applied in your billing history.\n\n"
    "If you have questions about your billing, please contact support."
)


def _replace(*, old_subject: str, old_body: str, subject: str, body: str) -> None:
    op.execute(
        sa.text(
            """
            UPDATE notification_templates
               SET subject = :subject,
                   body = :body,
                   updated_at = now()
             WHERE code = 'payment_received'
               AND channel = 'email'
               AND subject = :old_subject
               AND body = :old_body
            """
        ).bindparams(
            subject=subject,
            body=body,
            old_subject=old_subject,
            old_body=old_body,
        )
    )


def upgrade() -> None:
    _replace(
        old_subject=_OLD_SUBJECT,
        old_body=_OLD_BODY,
        subject=_NEW_SUBJECT,
        body=_NEW_BODY,
    )


def downgrade() -> None:
    _replace(
        old_subject=_NEW_SUBJECT,
        old_body=_NEW_BODY,
        subject=_OLD_SUBJECT,
        body=_OLD_BODY,
    )
