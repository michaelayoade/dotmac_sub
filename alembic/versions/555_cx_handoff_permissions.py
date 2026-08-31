"""Retain the reverted customer-experience handoff revision as a tombstone.

Revision ID: 555_cx_handoff_permissions
Revises: 554_ai_intake_canary_library
Create Date: 2026-08-25

The handoff workflow and its permission seeds were reverted by PR #2726.
Later revisions already depend on this revision identifier, so the migration
must remain in the lineage even though it no longer changes the database.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "555_cx_handoff_permissions"
down_revision: str | None = "554_ai_intake_canary_library"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Keep the retired permission seed absent on new installations."""


def downgrade() -> None:
    """The retired permission seed has no schema state to reverse."""
