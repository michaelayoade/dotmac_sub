"""Merge the referral-mirror and technical-support migration lineages.

Production had two migrations applied to its DB that were never in version
control (``186_seed_technical_support_role`` →
``187_technical_support_network_reads``), forking the Alembic history off
``185`` alongside ``main``'s lineage (… → ``186_add_referral_mirror``). This
no-op merge brings both into a single head so any environment — prod (currently
at ``187_technical_support_network_reads``) or a fresh CI DB — can
``alembic upgrade head`` deterministically.

Revision ID: 188_merge_referral_techsupport
Revises: 186_add_referral_mirror, 187_technical_support_network_reads
"""

from __future__ import annotations

revision = "188_merge_referral_techsupport"
down_revision = ("186_add_referral_mirror", "187_technical_support_network_reads")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
