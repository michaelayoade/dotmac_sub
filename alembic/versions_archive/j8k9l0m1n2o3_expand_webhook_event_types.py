"""Expand WebhookEventType enum to ~40 event types.

Revision ID: j8k9l0m1n2o3
Revises: i7j8k9l0m1n2
Create Date: 2025-01-20

Adds new event types to support comprehensive webhook event system:
- Subscriber: updated, suspended, reactivated
- Subscription: activated, suspended, resumed, canceled, upgraded, downgraded, expiring
- Invoice: sent, overdue
- Payment: failed, refunded
- Usage: warning, exhausted, topped_up
- Provisioning: started, failed
- Service Order: created, assigned, completed
- Appointment: scheduled, missed
- Network: device.offline, device.online, session.started, session.ended
- Ticket: created, escalated, resolved
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = 'j8k9l0m1n2o3'
down_revision = 'i7j8k9l0m1n2'
branch_labels = None
depends_on = None

# New enum values to add
NEW_VALUES = [
    # Subscriber events
    'subscriber.updated',
    'subscriber.suspended',
    'subscriber.reactivated',
    # Subscription events
    'subscription.activated',
    'subscription.suspended',
    'subscription.resumed',
    'subscription.canceled',
    'subscription.upgraded',
    'subscription.downgraded',
    'subscription.expiring',
    # Invoice events
    'invoice.sent',
    'invoice.overdue',
    # Payment events
    'payment.failed',
    'payment.refunded',
    # Usage events
    'usage.warning',
    'usage.exhausted',
    'usage.topped_up',
    # Provisioning events
    'provisioning.started',
    'provisioning.failed',
    # Service order events
    'service_order.created',
    'service_order.assigned',
    'service_order.completed',
    # Appointment events
    'appointment.scheduled',
    'appointment.missed',
    # Network events
    'device.offline',
    'device.online',
    'session.started',
    'session.ended',
    # Ticket events
    'ticket.created',
    'ticket.escalated',
    'ticket.resolved',
]


def upgrade() -> None:
    # Add new values to the webhookeventtype enum
    # Using raw SQL since SQLAlchemy doesn't support altering enums directly
    for value in NEW_VALUES:
        op.execute(
            f"ALTER TYPE webhookeventtype ADD VALUE IF NOT EXISTS '{value}'"
        )


def downgrade() -> None:
    # Note: PostgreSQL does not support removing enum values directly.
    # To fully downgrade, you would need to:
    # 1. Create a new enum type with only the original values
    # 2. Alter all columns using the old type to use the new type
    # 3. Drop the old type and rename the new one
    #
    # For simplicity, we leave the enum values in place during downgrade.
    # The extra values won't cause issues if not used.
    pass
