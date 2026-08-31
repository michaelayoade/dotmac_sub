"""Sub's migration composition declarations.

A module lineage declares the database effects it needs
(``ModuleManifest.requires``, ``dotmac_kernel.prerequisites``). It never names a
foreign revision, because the answer differs per assembly. This file is where
Sub answers, and ``alembic/env.py`` installs it before the revision map is
built.

Sub is the case the indirection exists for. The Starter reference assembly runs
the kernel lineage, so its answers are kernel revisions. Sub does not
run the kernel lineage at all — ``app/db.py`` owns its engine and ``alembic/``
owns its schema, and the platform adoption ledger admits kernel *models* without
adopting kernel *migrations*. Sub therefore supplies the same effects from its
own lineage, in migrations written for that purpose:

- ``545_tenant_scope_catalog_prereq`` — "Supply ``tenant_scope_catalog.v1``
  from Sub's own lineage"
- ``546_module_db_roles_prereq`` — "Supply ``module_database_roles.v1``
  from Sub's own lineage"
- ``556_idempotency_ledger_prereq`` — "Supply ``idempotency_ledger.v1``
  from Sub's own lineage"
- ``557_outbox_relay_prereq`` — "Supply ``outbox_relay.v1`` from Sub's own
  lineage"

The first two predate any module composition. Migrations 556 and 557 follow the
same provider rule for the kernel-shaped storage composed modules execute
against; neither transfers a Sub caller or authority.

Prerequisite bindings answer what the database already supplies. Plane
selections answer what part of a selectable module this assembly intends to
install. Those are deliberately separate declarations: binding Sub's real
tenant catalogue does not implicitly install every module's tenant plane, and
selecting a plane never grants business write authority.

Binding is not belief. ``resolve_depends_on`` reads these declarations at
script-load time and turns them into Alembic's physical ordering edges; the
static composition gate checks that a provider revision is composed and really
declares the named effect. Separately, ``require_prerequisites`` re-proves the
effect's live catalog observables before a requiring migration emits DDL.
``alembic_version`` records current heads rather than applied history, so no
check pretends a provider ancestor must remain there as a row.
"""

from __future__ import annotations

from typing import Final

from dotmac_kernel.planes import ModulePlane, ModulePlaneSelection
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    TENANT_SCOPE_CATALOG_V1,
    PrerequisiteBinding,
)

#: ``provider_owner`` is ``"sub"`` for all four. The seam lets every assembly
#: name its truthful provider: Starter answers ``"kernel"`` while ERP, like
#: Sub, hosts equivalent effects in its own product lineage.
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
    # Migration 556 hosts both kernel-shaped at-most-once storage planes. It
    # moves no caller: Sub's idempotency_keys and task_executions remain the
    # active local owners until a separate operation-by-operation cutover.
    PrerequisiteBinding(
        prerequisite=IDEMPOTENCY_LEDGER_V1.name,
        provider_revision="556_idempotency_ledger_prereq",
        provider_owner="sub",
    ),
    # Migration 557 ports ERP's production-used module relay provider and keeps
    # every incumbent Sub outbox untouched. Dispatcher identities are created
    # only by the explicit elevated bootstrap script; the migration verifies
    # them before emitting DDL.
    PrerequisiteBinding(
        prerequisite=OUTBOX_RELAY_V1.name,
        provider_revision="557_outbox_relay_prereq",
        provider_owner="sub",
    ),
)

#: Sub composes the tenant storage of its three selectable commercial owners.
#: This is migration intent, not a runtime mount or authority switch;
#: ``alembic/env.py`` installs it before the released lineages execute.
#: Payments and Service Orders are atomic tenant-only and therefore correctly
#: have no selection entries.
ASSEMBLY_MODULE_PLANES: Final[tuple[ModulePlaneSelection, ...]] = (
    ModulePlaneSelection(module="billing", planes=(ModulePlane.TENANT,)),
    ModulePlaneSelection(module="collections", planes=(ModulePlane.TENANT,)),
    ModulePlaneSelection(module="subscriptions", planes=(ModulePlane.TENANT,)),
)

__all__ = ["ASSEMBLY_MODULE_PLANES", "ASSEMBLY_PREREQUISITE_BINDINGS"]
