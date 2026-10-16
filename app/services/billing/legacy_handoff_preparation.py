"""Private flush-only composition for the future real global handoff root.

No public command, transaction root, receipt, grant restoration or activation
is installed here. The whole final owner must prohibit manual SQL savepoints.
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.services.billing.legacy_archive_fence import (
    _install_legacy_archive_fence,
    _posture,
    _quote,
    _root,
)
from app.services.billing.legacy_archive_fence_contracts import (
    ArchiveRelation,
    RelationOid,
)
from app.services.billing.legacy_handoff_preparation_contracts import (
    HandoffLockedRelationObservation,
    HandoffLockRelation,
    LockedLegacyHandoffStagedObservation,
    PrepareLockedLegacyHandoffRequest,
)
from app.services.billing.legacy_privilege_closure import (
    RelayLockInventoryQuery,
    relay_lock_inventory,
)
from app.services.billing.legacy_retained_columns_contracts import RetainedRelation
from app.services.billing.legacy_retained_columns_fence import (
    _install_legacy_retained_columns_fence,
)
from app.services.billing.opening_export import (
    billing_opening_lock_inventory,
    capture_locked_billing_opening_population,
)
from app.services.billing.opening_export_contracts import (
    BillingOpeningLockInventoryQuery,
    CaptureLockedBillingOpeningPopulationQuery,
    LockedBillingOpeningPopulation,
)
from app.services.domain_errors import DomainError


class LockedLegacyHandoffRefused(DomainError):
    """Refusal must escape and roll back the entire calling owner transaction."""


def _refuse(message: str) -> None:
    raise LockedLegacyHandoffRefused(
        code="billing_locked_handoff_preparation.refused", message=message
    )


def _inventory(db: Session) -> tuple[HandoffLockRelation, ...]:
    census = billing_opening_lock_inventory(
        db, query=BillingOpeningLockInventoryQuery()
    )
    relay = relay_lock_inventory(query=RelayLockInventoryQuery())
    return tuple(
        HandoffLockRelation(name)
        for name in sorted(
            {
                *(relation.value for relation in ArchiveRelation),
                *(relation.value for relation in RetainedRelation),
                *relay.relations,
                *census.relations,
            }
        )
    )


def _locked_relations(
    db: Session, connection: Connection, names: tuple[HandoffLockRelation, ...]
) -> tuple[HandoffLockedRelationObservation, ...]:
    observations: list[HandoffLockedRelationObservation] = []
    bind = db.get_bind()
    for name in names:
        clause = text("SELECT 1 FROM public." + _quote(name))  # noqa: S608 -- closed owner-derived inventory
        if (
            db.get_bind(clause=clause) is not bind
            or db.connection(bind_arguments={"clause": clause}) is not connection
        ):
            _refuse("Every locked relation must route to the actual owner connection.")
        rows = list(
            connection.execute(
                text("""
                SELECT c.oid, c.relkind, c.relispartition,
                       pg_catalog.to_regclass(:name)::oid = c.oid AS unshadowed,
                       NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i
                         WHERE i.inhrelid=c.oid OR i.inhparent=c.oid) AS ordinary,
                       EXISTS (SELECT 1 FROM pg_catalog.pg_locks l
                         WHERE l.pid=pg_catalog.pg_backend_pid() AND l.granted
                           AND l.locktype='relation' AND l.relation=c.oid
                           AND l.database=(SELECT oid FROM pg_catalog.pg_database
                             WHERE datname=pg_catalog.current_database())
                           AND l.mode='AccessExclusiveLock') AS locked
                  FROM pg_catalog.pg_class c
                  JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                 WHERE n.nspname='public' AND c.relname=:name
                """),
                {"name": str(name)},
            ).mappings()
        )
        if len(rows) != 1:
            _refuse("An exact public locked relation is missing.")
        row = rows[0]
        if (
            row["relkind"] != "r"
            or row["relispartition"]
            or not all(row[key] for key in ("unshadowed", "ordinary", "locked"))
        ):
            _refuse(
                "Actual ordinary public relation identity or exclusive lock refuses."
            )
        observations.append(
            HandoffLockedRelationObservation(name, RelationOid(cast(int, row["oid"])))
        )
    return tuple(observations)


def _same_census(
    before: LockedBillingOpeningPopulation, after: LockedBillingOpeningPopulation
) -> bool:
    # Only observation-envelope time/snapshot labels are excluded. All typed
    # population, migration heads, declaration/cutoff and capture semantics stay.
    normalized = replace(
        after,
        capture=replace(
            after.capture,
            captured_at=before.capture.captured_at,
            snapshot_id=before.capture.snapshot_id,
        ),
    )
    return normalized == before


def _stage_locked_legacy_handoff(
    db: Session, *, request: PrepareLockedLegacyHandoffRequest
) -> LockedLegacyHandoffStagedObservation:
    """Prelock the complete actual union, capture, stage and reobserve in one root."""
    if not isinstance(request, PrepareLockedLegacyHandoffRequest):
        _refuse("Supply the typed locked preparation request.")
    if request.source.population_version != "sub-billing-opening-census-v3":
        _refuse("Locked handoff preparation requires the complete v3 census.")
    connection = _root(db)
    root, transaction = db.get_transaction(), connection.get_transaction()
    identity = _posture(connection)
    names = _inventory(db)
    # No population capture, ACL/DDL effect or audit may precede this complete
    # sorted union statement. READ COMMITTED then observes preceding commits.
    connection.exec_driver_sql(
        "LOCK TABLE "
        + ", ".join("public." + _quote(name) for name in names)
        + " IN ACCESS EXCLUSIVE MODE"
    )
    relations = _locked_relations(db, connection, names)
    query = CaptureLockedBillingOpeningPopulationQuery(source=request.source)
    before = capture_locked_billing_opening_population(db, query=query)
    archive = _install_legacy_archive_fence(db, request=request.archive)
    retained = _install_legacy_retained_columns_fence(db, request=request.retained)
    after = capture_locked_billing_opening_population(db, query=query)
    if not _same_census(before, after):
        _refuse(
            "Locked semantic census or migration/cutoff inputs changed during staging."
        )
    # Exact replay re-observes actual fixed guards, ACLs, role/helper closure and
    # audit-bound catalog digests without another event or any repair/grant.
    archive_after = _install_legacy_archive_fence(db, request=request.archive)
    retained_after = _install_legacy_retained_columns_fence(
        db, request=request.retained
    )
    if (
        replace(archive_after, replay_observed=archive.replay_observed) != archive
        or replace(retained_after, replay_observed=retained.replay_observed) != retained
        or db.get_transaction() is not root
        or connection.get_transaction() is not transaction
        or _root(db) is not connection
        or _posture(connection) != identity
        or _inventory(db) != names
        or _locked_relations(db, connection, names) != relations
    ):
        _refuse("Actual owner root, role, relation or component observations changed.")
    return LockedLegacyHandoffStagedObservation(relations, before, archive, retained)
