"""merge ip_assignments_153 and topology_outage_157 heads

Revision ID: bba1908a7cf5
Revises: 153_ip_assignments_subscription_owner, 157_outage_incidents
Create Date: 2026-06-17 20:40:29.831790

"""

revision = "bba1908a7cf5"
down_revision = ("153_ip_assignments_subscription_owner", "157_outage_incidents")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
