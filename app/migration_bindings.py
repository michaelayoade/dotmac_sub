"""Sub's answers to "which revision supplies that effect?".

A module lineage declares the database effects it needs
(``ModuleManifest.requires``, ``dotmac_kernel.prerequisites``). It never names a
foreign revision, because the answer differs per assembly. This file is where
Sub answers, and ``alembic/env.py`` installs it before the revision map is
built.

Sub is the case the indirection exists for. The Starter reference assembly runs
the kernel lineage, so both of its answers are kernel revisions. Sub does not
run the kernel lineage at all — ``app/db.py`` owns its engine and ``alembic/``
owns its schema, and the platform adoption ledger admits kernel *models* without
adopting kernel *migrations*. Sub therefore supplies the same effects from its
own lineage, in migrations written for that purpose:

- ``545_tenant_scope_catalog_prereq`` — "Supply ``tenant_scope_catalog.v1``
  from Sub's own lineage"
- ``546_module_db_roles_prereq`` — "Supply ``module_database_roles.v1``
  from Sub's own lineage"

Both predate any module composition. They were written so that composing a
module would be a binding rather than a schema migration, which is why this file
is two declarations and not a plan.

Binding is not belief. ``require_prerequisites`` re-proves each effect against
the live catalog before the requiring migration runs, and the order canary
requires the named revision to be present in ``alembic_version``. A wrong entry
here fails at ``alembic upgrade``, before any DDL — so the failure mode is a
refused migration, never a module running against a database that does not have
what it asked for.
"""

from __future__ import annotations

from typing import Final

from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
    PrerequisiteBinding,
)

#: ``provider_owner`` is ``"sub"`` for both, and that is the whole point of the
#: seam: every other assembly in the fleet answers ``"kernel"`` here.
ASSEMBLY_PREREQUISITE_BINDINGS: Final[tuple[PrerequisiteBinding, ...]] = (
    # Migration 545 creates the tenant scope catalog Sub's own tables use, and
    # composed modules resolve their tenant scope through. Bound to the
    # revision that makes the effect whole rather than to Sub's head: a
    # database stopped between 545 and head still satisfies this prerequisite,
    # and binding to head would refuse a migration that could safely run.
    PrerequisiteBinding(
        prerequisite=TENANT_SCOPE_CATALOG_V1.name,
        provider_revision="545_tenant_scope_catalog_prereq",
        provider_owner="sub",
    ),
    # Migration 546 creates the per-module database roles a composed module's
    # own migration grants against. It depends on 545, so the pair is ordered
    # by Sub's own lineage rather than by anything declared here.
    PrerequisiteBinding(
        prerequisite=MODULE_DATABASE_ROLES_V1.name,
        provider_revision="546_module_db_roles_prereq",
        provider_owner="sub",
    ),
)

__all__ = ["ASSEMBLY_PREREQUISITE_BINDINGS"]
