"""Verify `module_database_roles.v1` from Sub's own lineage.

Revision ID: 546_module_db_roles_prereq
Revises: 545_tenant_scope_catalog_prereq
Create Date: 2026-08-20

ADR-0011. The second effect every installable module declares. A module never
creates these roles itself — the kernel's spec is explicit that creating a role
needs privileges a module migration must not assume, and that a module inventing
its own roles is a second authority over cluster access. So the product supplies
them, which here means Sub.

Three roles, and the RLS posture is the point rather than the names:

- ``app_admin`` — ``LOGIN BYPASSRLS``. Offline and migration work has to see
  every tenant's rows; one that cannot turns maintenance into silent zero-row
  success. Deliberately NOT a superuser.
- ``app_user`` — ``LOGIN``, no bypass. Online tenant request traffic.
- ``platform_api`` — ``LOGIN``, no bypass. Online platform request traffic.

The verifier checks ``rolsuper`` as well as ``rolbypassrls``, because **a
superuser bypasses RLS regardless of the flag** — so a superuser ``app_user``
would satisfy a naive check while defeating tenant isolation for every composed
module at once. The explicit elevated bootstrap creates or repairs the roles;
this migration only proves they exist before module DDL depends on them.

**Passwords are not set here**, matching the kernel. Operators set them out of
band and wire each role to its own connection string.

**Privileges this migration needs.** None beyond read access to ``pg_roles``.
Creating a role requires ``CREATEROLE`` (or superuser), so it belongs to
``scripts/bootstrap_commercial_module_prereqs.py``. A deployment where the roles
are missing fails here, loudly, before any composed module grants to a role that
does not exist.

Additive, idempotent, and forward-only. Roles are NOT dropped on downgrade —
they are cluster-wide objects another database or a later migration may be
using, and dropping one because a single lineage stepped back would reach well
outside this migration's business.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.commercial_module_prereqs import (
    MODULE_DATABASE_ROLE_CONTRACT,
    RolePosture,
    module_database_role_violations,
)

revision: str = "546_module_db_roles_prereq"
down_revision: str | None = "545_tenant_scope_catalog_prereq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DATABASE_ROLE_CONTRACT: dict[str, RolePosture] = {
    role: contract.posture for role, contract in MODULE_DATABASE_ROLE_CONTRACT.items()
}


def _observe_module_roles() -> dict[str, RolePosture]:
    rows = op.get_bind().execute(
        sa.text(
            "SELECT rolname, rolcanlogin, rolbypassrls, rolsuper "
            "FROM pg_roles WHERE rolname = ANY(:roles)"
        ),
        {"roles": list(DATABASE_ROLE_CONTRACT)},
    )
    return {str(row[0]): (bool(row[1]), bool(row[2]), bool(row[3])) for row in rows}


def _assert_module_database_roles_exist() -> None:
    violations = module_database_role_violations(_observe_module_roles())
    if violations:
        raise RuntimeError(
            "module_database_roles.v1 is not satisfied: "
            + "; ".join(violations)
            + ". Run scripts/bootstrap_commercial_module_prereqs.py with "
            "elevated BOOTSTRAP_DATABASE_URL, then rerun Alembic with the "
            "restricted migration role."
        )


def upgrade() -> None:
    _assert_module_database_roles_exist()


def downgrade() -> None:
    """Deliberately empty — see the module docstring on not dropping roles."""
