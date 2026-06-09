"""Add foreign keys to reseller_users and merge the two open heads.

reseller_users.person_id / reseller_id were plain UUID columns with no
referential integrity, allowing orphaned/dangling links. This adds the
missing foreign keys (ON DELETE CASCADE, since the row is a pure
subscriber<->reseller association). It also linearizes the two heads that
both branched off 120 (121 support-comment identity, 122 addon ip fields).

Revision ID: 123_add_reseller_users_fks
Revises: 121_add_support_comment_author_identity, 122_add_addon_ip_fields
Create Date: 2026-06-09
"""

from __future__ import annotations

from sqlalchemy import inspect

from alembic import op

revision = "123_add_reseller_users_fks"
down_revision = (
    "121_add_support_comment_author_identity",
    "122_add_addon_ip_fields",
)
branch_labels = None
depends_on = None

_TABLE = "reseller_users"
_PERSON_FK = "reseller_users_person_id_fkey"
_RESELLER_FK = "reseller_users_reseller_id_fkey"


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite (used in tests) cannot ALTER TABLE to add constraints and does
    # not enforce them; the model metadata carries the FKs there instead.
    if bind.dialect.name == "sqlite":
        return

    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    existing_fks = {fk["name"] for fk in inspector.get_foreign_keys(_TABLE)}

    if _PERSON_FK not in existing_fks:
        # Null out orphaned references so the constraint can be created.
        op.execute(
            """
            UPDATE reseller_users
            SET person_id = NULL
            WHERE person_id IS NOT NULL
              AND person_id NOT IN (SELECT id FROM subscribers)
            """
        )
        op.create_foreign_key(
            _PERSON_FK,
            _TABLE,
            "subscribers",
            ["person_id"],
            ["id"],
            ondelete="CASCADE",
        )

    if _RESELLER_FK not in existing_fks:
        op.execute(
            """
            UPDATE reseller_users
            SET reseller_id = NULL
            WHERE reseller_id IS NOT NULL
              AND reseller_id NOT IN (SELECT id FROM resellers)
            """
        )
        op.create_foreign_key(
            _RESELLER_FK,
            _TABLE,
            "resellers",
            ["reseller_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    existing_fks = {fk["name"] for fk in inspector.get_foreign_keys(_TABLE)}
    for fk_name in (_RESELLER_FK, _PERSON_FK):
        if fk_name in existing_fks:
            try:
                op.drop_constraint(fk_name, _TABLE, type_="foreignkey")
            except Exception:  # pragma: no cover
                pass
