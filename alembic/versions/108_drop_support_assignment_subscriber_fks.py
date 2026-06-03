"""Drop subscriber foreign keys from support assignment fields.

Revision ID: 108_drop_support_assignment_subscriber_fks
Revises: 107_add_ticket_automation_rules
Create Date: 2026-05-25 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "108_drop_support_assignment_subscriber_fks"
down_revision = "107_add_ticket_automation_rules"
branch_labels = None
depends_on = None


def _drop_fk_if_present(table_name: str, constrained_columns: list[str]) -> None:
    inspector = sa.inspect(op.get_bind())
    for foreign_key in inspector.get_foreign_keys(table_name):
        if foreign_key.get("constrained_columns") == constrained_columns:
            op.drop_constraint(
                foreign_key["name"],
                table_name,
                type_="foreignkey",
            )
            return


def upgrade() -> None:
    _drop_fk_if_present("support_tickets", ["assigned_to_person_id"])
    _drop_fk_if_present("support_tickets", ["technician_person_id"])
    _drop_fk_if_present("support_tickets", ["ticket_manager_person_id"])
    _drop_fk_if_present("support_tickets", ["site_coordinator_person_id"])
    _drop_fk_if_present("support_ticket_assignees", ["person_id"])


def downgrade() -> None:
    op.create_foreign_key(
        "fk_support_tickets_assigned_to_person_id_subscribers",
        "support_tickets",
        "subscribers",
        ["assigned_to_person_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_support_tickets_technician_person_id_subscribers",
        "support_tickets",
        "subscribers",
        ["technician_person_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_support_tickets_ticket_manager_person_id_subscribers",
        "support_tickets",
        "subscribers",
        ["ticket_manager_person_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_support_tickets_site_coordinator_person_id_subscribers",
        "support_tickets",
        "subscribers",
        ["site_coordinator_person_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_support_ticket_assignees_person_id_subscribers",
        "support_ticket_assignees",
        "subscribers",
        ["person_id"],
        ["id"],
    )
