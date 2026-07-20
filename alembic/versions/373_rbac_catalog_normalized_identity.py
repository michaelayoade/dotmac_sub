"""Enforce case-normalized RBAC catalog identities.

Revision ID: 373_rbac_catalog_normalized_identity
Revises: 372_vendor_payment_projection
Create Date: 2026-07-19

The preflight fails closed on existing case/whitespace collisions. It does not
choose a winner or rewrite authorization identities implicitly.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "373_rbac_catalog_normalized_identity"
down_revision = "372_vendor_payment_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            role_collisions bigint;
            permission_collisions bigint;
        BEGIN
            SELECT count(*) INTO role_collisions
            FROM (
                SELECT lower(btrim(name))
                FROM roles
                GROUP BY lower(btrim(name))
                HAVING count(*) > 1
            ) collisions;

            SELECT count(*) INTO permission_collisions
            FROM (
                SELECT lower(btrim(key))
                FROM permissions
                GROUP BY lower(btrim(key))
                HAVING count(*) > 1
            ) collisions;

            IF role_collisions > 0 OR permission_collisions > 0 THEN
                RAISE EXCEPTION
                    'RBAC catalog identity preflight failed: % role collision(s), % permission collision(s)',
                    role_collisions, permission_collisions;
            END IF;
        END
        $$;
        """
    )
    op.create_index(
        "uq_roles_normalized_name",
        "roles",
        [sa.text("lower(btrim(name))")],
        unique=True,
    )
    op.create_index(
        "uq_permissions_normalized_key",
        "permissions",
        [sa.text("lower(btrim(key))")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_permissions_normalized_key", table_name="permissions")
    op.drop_index("uq_roles_normalized_name", table_name="roles")
