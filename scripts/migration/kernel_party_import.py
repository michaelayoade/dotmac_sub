"""Rollback-only same-Connection composition; no CLI or connection factory."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.runtime.migration import MigrationContext, RevisionStep
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.services.operator_tenant_kernel_import import (
    stage_historical_operator_tenant,
    verify_historical_operator_tenant,
)
from app.services.party_kernel_import import (
    PartyImportCohort,
    PartyImportCount,
    PartyImportRefused,
    stage_party_context,
    stage_party_roots,
    validate_party_cohort,
    verify_party_destinations,
)
from scripts.migration import kernel_lineage_entry as entry

ADOPTION_HEAD = "ska0001_party_context"
ADOPTION_VERSION_TABLE = "sub_kernel_adoption_alembic_version"


class PartyImportPhase(StrEnum):
    TENANT = "tenant"
    ROOTS = "roots"
    DDL = "ddl"
    CONTEXT = "context"


class InjectedPartyImportFailure(RuntimeError):
    """Hosted fixture fault; caller must roll back the complete root."""


@dataclass(frozen=True)
class PartyImportObserved:
    entry: entry.KernelEntryObserved
    roots: PartyImportCount
    contexts: PartyImportCount
    adoption_heads: tuple[str, ...]


def require_canonical_entry(
    connection: Connection, *, observed: entry.KernelEntryObserved
) -> None:
    """Revalidate catalog identities and all histories immediately before DDL."""
    entry._require_fixture_transaction(connection)
    canonical = tuple(
        r
        for r in entry._relations(connection, "public")
        if r.name in entry.COLLISION_TABLES
    )
    if (
        canonical != observed.canonical_relations
        or entry._relations(connection, entry.ESCROW_SCHEMA)
        != observed.predecessor_relations
        or any(
            r.oid in {p.oid for p in observed.predecessor_relations} for r in canonical
        )
        or tuple(
            connection.scalars(
                text(
                    f"SELECT version_num FROM public.{entry.KERNEL_VERSION_TABLE} ORDER BY version_num"
                )
            )
        )
        != (entry.KERNEL_HEAD,)
        or tuple(
            connection.scalars(
                text(
                    "SELECT version_num FROM public.alembic_version ORDER BY version_num"
                )
            )
        )
        != observed.sub_heads
    ):
        raise PartyImportRefused("Canonical OIDs or genuine lineage changed")


def _apply_adoption(
    connection: Connection, *, observed: entry.KernelEntryObserved
) -> tuple[str, ...]:
    require_canonical_entry(connection, observed=observed)
    if connection.scalar(
        text("SELECT to_regclass('public.sub_party_context')")
    ) or connection.scalar(
        text(f"SELECT to_regclass('public.{ADOPTION_VERSION_TABLE}')")
    ):
        raise PartyImportRefused("No replay or partial adoption repair")
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[2] / "alembic_kernel_adoption"),
    )
    config.attributes["canonical_entry"] = observed
    script = ScriptDirectory.from_config(config)
    if script.get_heads() != [ADOPTION_HEAD]:
        raise PartyImportRefused("Unreviewed adoption head")

    def upgrade(
        revision: str | tuple[str, ...], _context: MigrationContext
    ) -> list[RevisionStep]:
        return script._upgrade_revs("heads", revision)

    with EnvironmentContext(
        config, script, fn=upgrade, destination_rev="heads"
    ) as environment:
        environment.configure(
            connection=connection,
            version_table=ADOPTION_VERSION_TABLE,
            version_table_schema="public",
            transactional_ddl=True,
            transaction_per_migration=False,
        )
        environment.run_migrations()
    heads = tuple(
        str(v)
        for v in connection.scalars(
            text(f"SELECT version_num FROM public.{ADOPTION_VERSION_TABLE}")
        )
    )
    if heads != (ADOPTION_HEAD,):
        raise PartyImportRefused("Adoption history differs")
    return heads


def rehearse_party_import(
    connection: Connection,
    *,
    plan: entry.KernelEntryFixturePlan,
    cohort: PartyImportCohort,
    fail_after: PartyImportPhase | None = None,
) -> PartyImportObserved:
    """Caller ALWAYS rolls back; imports no business roles or runtime writers."""
    entry._require_fixture_transaction(connection)
    validate_party_cohort(connection, cohort=cohort)
    observed = entry.rehearse_kernel_entry(connection, plan=plan)
    # Entry holds source and destination locks through the caller's root.
    validate_party_cohort(connection, cohort=cohort, escrow=True)
    require_canonical_entry(connection, observed=observed)
    stage_historical_operator_tenant(connection, tenant_id=cohort.tenant_id)
    _fault(PartyImportPhase.TENANT, fail_after)
    verify_historical_operator_tenant(connection, tenant_id=cohort.tenant_id)
    stage_party_roots(connection, cohort=cohort)
    _fault(PartyImportPhase.ROOTS, fail_after)
    verify_party_destinations(connection, cohort=cohort, context_required=False)
    heads = _apply_adoption(connection, observed=observed)
    _fault(PartyImportPhase.DDL, fail_after)
    stage_party_context(connection, cohort=cohort)
    _fault(PartyImportPhase.CONTEXT, fail_after)
    verify_historical_operator_tenant(connection, tenant_id=cohort.tenant_id)
    roots, contexts = verify_party_destinations(connection, cohort=cohort)
    return PartyImportObserved(observed, roots, contexts, heads)


def _fault(phase: PartyImportPhase, requested: PartyImportPhase | None) -> None:
    if phase is requested:
        raise InjectedPartyImportFailure(
            f"Injected fixture failure after {phase.value}"
        )
