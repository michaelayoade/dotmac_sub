"""Update the stock invoice email to a neutral PDF review message.

Revision ID: 553_invoice_sent_review_email
Revises: 552_cancel_merged_ticket_sources
Create Date: 2026-08-25

Only the exact historical seed copy is updated. Operator-customized content is
left untouched, and the migration does not activate a disabled channel.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "553_invoice_sent_review_email"
down_revision: str | None = "552_cancel_merged_ticket_sources"
branch_labels = None
depends_on = None

_OLD_SUBJECT = "Invoice #{invoice_number} \u2014 payment due {due_date}"
_OLD_BODY = (
    "Dear {subscriber_name},\n\n"
    "Invoice #{invoice_number} for {amount} is due on {due_date}.\n\n"
    "Please make your payment before the due date to avoid "
    "service interruption.\n\n"
    "Pay online: {portal_url}/billing\n\n"
    "Thank you."
)
_NEW_SUBJECT = "Invoice #{invoice_number} for your review"
_NEW_BODY = (
    "Hello {subscriber_name},\n\n"
    "Please find attached invoice #{invoice_number}, created for your "
    "review regarding your service.\n\n"
    "Amount: {amount}\n"
    "Due date: {due_date}\n\n"
    "Thank you."
)


def _replace(*, old_subject: str, old_body: str, subject: str, body: str) -> None:
    op.execute(
        sa.text(
            """
            UPDATE notification_templates
               SET subject = :subject,
                   body = :body,
                   updated_at = now()
             WHERE code = 'invoice_sent'
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
