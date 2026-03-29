"""Add self-service tables for subscription changes and payment arrangements.

Revision ID: h6i7j8k9l0m1
Revises: g5h6i7j8k9l0
Create Date: 2024-01-15 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'h6i7j8k9l0m1'
down_revision = 'g5h6i7j8k9l0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create subscription_change_requests table
    op.create_table(
        'subscription_change_requests',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('subscription_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('current_offer_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('requested_offer_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('status', sa.Enum('pending', 'approved', 'rejected', 'applied', 'canceled', name='subscriptionchangestatus'), nullable=False),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('requested_by_person_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('reviewed_by_person_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('rejection_reason', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, default=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ),
        sa.ForeignKeyConstraint(['current_offer_id'], ['catalog_offers.id'], ),
        sa.ForeignKeyConstraint(['requested_offer_id'], ['catalog_offers.id'], ),
        sa.ForeignKeyConstraint(['requested_by_person_id'], ['people.id'], ),
        sa.ForeignKeyConstraint(['reviewed_by_person_id'], ['people.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_subscription_change_requests_subscription_id', 'subscription_change_requests', ['subscription_id'])
    op.create_index('ix_subscription_change_requests_status', 'subscription_change_requests', ['status'])

    # Create payment_arrangements table
    op.create_table(
        'payment_arrangements',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('account_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('invoice_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('total_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('installment_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('frequency', sa.Enum('weekly', 'biweekly', 'monthly', name='paymentfrequency'), nullable=False),
        sa.Column('installments_total', sa.Integer(), nullable=False),
        sa.Column('installments_paid', sa.Integer(), nullable=False, default=0),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=True),
        sa.Column('next_due_date', sa.Date(), nullable=True),
        sa.Column('status', sa.Enum('pending', 'active', 'completed', 'defaulted', 'canceled', name='arrangementstatus'), nullable=False),
        sa.Column('requested_by_person_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('approved_by_person_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, default=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['subscriber_accounts.id'], ),
        sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], ),
        sa.ForeignKeyConstraint(['requested_by_person_id'], ['people.id'], ),
        sa.ForeignKeyConstraint(['approved_by_person_id'], ['people.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_payment_arrangements_account_id', 'payment_arrangements', ['account_id'])
    op.create_index('ix_payment_arrangements_status', 'payment_arrangements', ['status'])

    # Create payment_arrangement_installments table
    op.create_table(
        'payment_arrangement_installments',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('arrangement_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('installment_number', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('due_date', sa.Date(), nullable=False),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('payment_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('status', sa.Enum('pending', 'due', 'paid', 'overdue', 'waived', name='installmentstatus'), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, default=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['arrangement_id'], ['payment_arrangements.id'], ),
        sa.ForeignKeyConstraint(['payment_id'], ['payments.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_payment_arrangement_installments_arrangement_id', 'payment_arrangement_installments', ['arrangement_id'])
    op.create_index('ix_payment_arrangement_installments_due_date', 'payment_arrangement_installments', ['due_date'])
    op.create_index('ix_payment_arrangement_installments_status', 'payment_arrangement_installments', ['status'])

    # Create notification_preferences table
    # Use postgresql.ENUM with create_type=False to reference existing enum
    notification_channel_enum = postgresql.ENUM(
        'email', 'sms', 'push', 'whatsapp', 'webhook',
        name='notificationchannel',
        create_type=False
    )
    op.create_table(
        'notification_preferences',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('account_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('channel', notification_channel_enum, nullable=False),
        sa.Column('category', sa.String(80), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, default=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['subscriber_accounts.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'channel', 'category', name='uq_notification_preferences_account_channel_category')
    )
    op.create_index('ix_notification_preferences_account_id', 'notification_preferences', ['account_id'])


def downgrade() -> None:
    op.drop_index('ix_notification_preferences_account_id', table_name='notification_preferences')
    op.drop_table('notification_preferences')

    op.drop_index('ix_payment_arrangement_installments_status', table_name='payment_arrangement_installments')
    op.drop_index('ix_payment_arrangement_installments_due_date', table_name='payment_arrangement_installments')
    op.drop_index('ix_payment_arrangement_installments_arrangement_id', table_name='payment_arrangement_installments')
    op.drop_table('payment_arrangement_installments')

    op.drop_index('ix_payment_arrangements_status', table_name='payment_arrangements')
    op.drop_index('ix_payment_arrangements_account_id', table_name='payment_arrangements')
    op.drop_table('payment_arrangements')

    op.drop_index('ix_subscription_change_requests_status', table_name='subscription_change_requests')
    op.drop_index('ix_subscription_change_requests_subscription_id', table_name='subscription_change_requests')
    op.drop_table('subscription_change_requests')

    op.execute("DROP TYPE IF EXISTS subscriptionchangestatus")
    op.execute("DROP TYPE IF EXISTS paymentfrequency")
    op.execute("DROP TYPE IF EXISTS arrangementstatus")
    op.execute("DROP TYPE IF EXISTS installmentstatus")
