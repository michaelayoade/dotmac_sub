"""Merge decoupling and provisioning heads.

Revision ID: 035_merge_decoupling_heads
Revises: 028_preserve_profile_owner, 035_add_provisioning_architecture
Create Date: 2026-04-18

"""

revision = "035_merge_decoupling_heads"
down_revision = ("028_preserve_profile_owner", "035_add_provisioning_architecture")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
