"""Sub's answers to "which revision supplies that effect?".

A module lineage declares the database effects it needs
(``ModuleManifest.requires``, ``dotmac_kernel.prerequisites``). It never names a
foreign revision, because the answer differs per assembly. This file is where
Sub answers.

Sub is the case the indirection exists for. The Starter reference assembly runs
the kernel lineage, so both of its answers are kernel ``0001``. Sub does not run
the kernel lineage at all — ``app/db.py`` owns its engine and ``alembic/`` owns
its schema, and the platform adoption ledger admits kernel *models* without
adopting kernel *migrations*. Sub therefore supplies the same effects from its
own lineage, in migrations written for that purpose:

- ``545_tenant_scope_catalog_prerequisite`` — "Supply ``tenant_scope_catalog.v1``
  from Sub's own lineage"
- ``546_module_database_roles_prerequisite`` — "Supply ``module_database_roles.v1``
  from Sub's own lineage"

Both predate any module composition. They were written so that composing a
module would be a binding rather than a schema migration, which is why this file
is four lines of data and not a plan.

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
)

#: ``{effect name: the Sub revision that supplies it}``.
#:
#: Keyed by the prerequisite's declared name rather than a literal string, so a
#: kernel rename is an import error here instead of a silent unbound effect.
MIGRATION_BINDINGS: Final[dict[str, str]] = {
    TENANT_SCOPE_CATALOG_V1.name: "545_tenant_scope_catalog_prerequisite",
    MODULE_DATABASE_ROLES_V1.name: "546_module_database_roles_prerequisite",
}

__all__ = ["MIGRATION_BINDINGS"]
