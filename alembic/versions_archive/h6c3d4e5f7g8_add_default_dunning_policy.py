"""Add default dunning policy with 30/37 day steps.

Seeds a default suspension policy with:
- Day 30: Notify (suspension warning)
- Day 37: Suspend (account suspension)
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "h6c3d4e5f7g8"
down_revision = "g5b2c3d4e6f7"
branch_labels = None
depends_on = None

# Fixed UUIDs for seed data
DEFAULT_POLICY_SET_ID = "00000000-0000-0000-0000-000000000001"
DUNNING_STEP_30_ID = "00000000-0000-0000-0000-000000000002"
DUNNING_STEP_37_ID = "00000000-0000-0000-0000-000000000003"


def upgrade() -> None:
    # Insert default policy set
    op.execute("""
        INSERT INTO policy_sets (
            id, name, proration_policy, downgrade_policy, trial_card_required,
            grace_days, suspension_action, refund_policy, is_active, created_at, updated_at
        )
        VALUES (
            '00000000-0000-0000-0000-000000000001',
            'Standard Suspension Policy',
            'immediate',
            'next_cycle',
            false,
            0,
            'suspend',
            'none',
            true,
            NOW(),
            NOW()
        )
        ON CONFLICT DO NOTHING
    """)

    # Insert dunning step: Day 30 - Notify (suspension warning)
    op.execute("""
        INSERT INTO policy_dunning_steps (
            id, policy_set_id, day_offset, action, note
        )
        VALUES (
            '00000000-0000-0000-0000-000000000002',
            '00000000-0000-0000-0000-000000000001',
            30,
            'notify',
            'Suspension warning - payment overdue 30 days'
        )
        ON CONFLICT DO NOTHING
    """)

    # Insert dunning step: Day 37 - Suspend
    op.execute("""
        INSERT INTO policy_dunning_steps (
            id, policy_set_id, day_offset, action, note
        )
        VALUES (
            '00000000-0000-0000-0000-000000000003',
            '00000000-0000-0000-0000-000000000001',
            37,
            'suspend',
            'Account suspended - payment overdue 37 days'
        )
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    # Remove dunning steps
    op.execute("""
        DELETE FROM policy_dunning_steps
        WHERE id IN (
            '00000000-0000-0000-0000-000000000002',
            '00000000-0000-0000-0000-000000000003'
        )
    """)

    # Remove policy set
    op.execute("""
        DELETE FROM policy_sets
        WHERE id = '00000000-0000-0000-0000-000000000001'
    """)
