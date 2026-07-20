"""Carry the NCC Local Government Area on the address model.

LGA is an NCC administrative unit (774 LGAs + the FCT area councils) and the
quarterly complaints return (①) files one per row. Sub had nowhere to put it:
neither ``subscribers`` nor ``addresses`` held an LGA, so every exported row
failed the workbook's own validator with "LGA is required".

CRM filled the gap by guessing the LGA from address text and defaulting an
unmatched complaint to "Municipal Area Council, FEDERAL CAPITAL TERRITORY" —
reporting customers it could not locate as Abuja. That is not ported. The
column is the honest alternative: capture the LGA, validate it against its
state (``app/services/ncc_location``), and leave it blank when unknown.

Added to BOTH models deliberately. ``addresses`` is the richer model, but it
holds 51 rows against 15,291 subscribers in production — ``Subscriber``'s own
address fields are the de-facto address, so an LGA only on ``addresses`` would
help almost nobody.

No ``billing_lga``: the NCC return is about where the *service* is, not where
the bill goes, and a second LGA would only pose a "which one do we file?"
question that invites the guessing this replaces.

Nullable with no backfill: there is no honest source to backfill *from*. The
column starts empty and fills as capture lands; blank keeps reporting the gap
in the meantime.

Revision ID: 332_address_lga
Revises: 331_ncc_ticket_categorisation
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "332_address_lga"
down_revision = "331_ncc_ticket_categorisation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subscribers", sa.Column("lga", sa.String(length=80), nullable=True))
    op.add_column("addresses", sa.Column("lga", sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column("addresses", "lga")
    op.drop_column("subscribers", "lga")
