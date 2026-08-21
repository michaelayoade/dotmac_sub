"""PostgreSQL-only guarantees of the cohort export.

Two things cannot be proved on SQLite, and both are the kind of guarantee that
looks fine until it is needed: that the read-only snapshot seam really refuses
a write, and that the exported watermark is a real transaction snapshot rather
than a plausible string. They are proved here, against the migrated schema, or
not claimed at all.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.db import READ_ONLY_SNAPSHOT_OPTIONS
from app.migration_source.cohort import CohortEntityType, cohort_table_names
from app.migration_source.snapshot import ContractVersion
from app.services.migration_source_export import (
    CohortExportCommand,
    CrossTenantExportRefused,
    export_page,
)
from app.services.operator_tenant import operator_tenant_id

#: Sorts after anything the migration chain seeds, so a drain reaches these
#: last and a page boundary can be placed inside the group deliberately.
_CANARY_PREFIX = "fbadc0de"


def test_the_read_only_seam_refuses_a_write(engine) -> None:
    """The guarantee the whole export path rests on."""

    with engine.connect().execution_options(**READ_ONLY_SNAPSHOT_OPTIONS) as connection:
        with connection.begin():
            assert connection.execute(text("SELECT 1")).scalar() == 1
            with pytest.raises(DBAPIError):
                connection.execute(
                    text("UPDATE parties SET display_name = display_name")
                )


def test_the_read_only_seam_still_reads(engine) -> None:
    """The acceptance half: read-only must not mean unusable."""

    with engine.connect().execution_options(**READ_ONLY_SNAPSHOT_OPTIONS) as connection:
        with connection.begin():
            for table in sorted(cohort_table_names()):
                connection.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608


def test_an_export_under_the_read_only_seam_produces_a_real_watermark(
    engine,
) -> None:
    """`pg_current_snapshot()` observes visibility without assigning an XID.

    `txid_current()` would assign one, which a READ ONLY transaction refuses —
    the watermark must not be the thing that breaks the read it describes.
    """

    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session: Session = factory()
    try:
        session.connection(execution_options=dict(READ_ONLY_SNAPSHOT_OPTIONS))
        page = export_page(
            session,
            CohortExportCommand(
                contract_version=ContractVersion.V1.value,
                entity_type=CohortEntityType.PARTY,
                tenant_id=operator_tenant_id(),
                page_size=5,
            ),
        )
        assert page.source_revision.snapshot_transaction_id
        assert ":" in page.source_revision.snapshot_transaction_id
        assert page.source_revision.schema_revision != "unknown"
    finally:
        session.rollback()
        session.close()


def test_a_foreign_tenant_is_refused_on_the_migrated_schema(
    db_session: Session,
) -> None:
    with pytest.raises(CrossTenantExportRefused):
        export_page(
            db_session,
            CohortExportCommand(
                contract_version=ContractVersion.V1.value,
                entity_type=CohortEntityType.PARTY,
                tenant_id=uuid.uuid4(),
            ),
        )


@pytest.mark.parametrize(
    "entity_type", sorted(CohortEntityType, key=lambda value: value.value)
)
def test_every_declared_entity_type_reads_against_the_deployed_schema(
    db_session: Session, entity_type: CohortEntityType
) -> None:
    """A field mapping that names a dropped column fails here, not at cutover."""

    page = export_page(
        db_session,
        CohortExportCommand(
            contract_version=ContractVersion.V1.value,
            entity_type=entity_type,
            tenant_id=operator_tenant_id(),
            page_size=5,
        ),
    )
    assert page.entity_type is entity_type
    for record in page.records:
        assert record.digest()


def _canary_id(index: int) -> uuid.UUID:
    return uuid.UUID(f"{_CANARY_PREFIX}-0000-4000-8000-{index:012d}")


def _insert_party(engine, identifier: uuid.UUID, name: str) -> None:
    """Insert one committed party on its own connection."""

    with engine.connect() as connection:
        with connection.begin():
            connection.execute(
                text(
                    "INSERT INTO parties (id, party_type, display_name, status,"
                    " data_classification, created_at, updated_at) VALUES"
                    " (:id, 'person', :name, 'active', 'test', now(), now())"
                ),
                {"id": identifier, "name": name},
            )


def _delete_canaries(engine) -> None:
    with engine.connect() as connection:
        with connection.begin():
            connection.execute(
                text("DELETE FROM parties WHERE id::text LIKE :prefix"),
                {"prefix": f"{_CANARY_PREFIX}-%"},
            )


def _drain(session: Session, page_size: int, interrupt=None) -> tuple[list, list]:
    """Page an entity type to completion, returning ids and per-page watermarks."""

    identifiers: list[uuid.UUID] = []
    watermarks: list[str | None] = []
    after: uuid.UUID | None = None
    for page_number in range(200):
        page = export_page(
            session,
            CohortExportCommand(
                contract_version=ContractVersion.V1.value,
                entity_type=CohortEntityType.PARTY,
                after_source_id=after,
                page_size=page_size,
                tenant_id=operator_tenant_id(),
            ),
        )
        identifiers.extend(record.source_id for record in page.records)
        watermarks.append(page.source_revision.snapshot_transaction_id)
        if interrupt is not None and page_number == 0:
            interrupt()
        if page.next_cursor is None:
            return identifiers, watermarks
        after = page.next_cursor.after_source_id
    raise AssertionError("keyset drain did not terminate")


def test_one_snapshot_holds_across_a_complete_paginated_drain(engine) -> None:
    """A drain sees the database as it was when it began, and says so.

    This is the guarantee the whole export rests on and the one that cannot be
    proved on SQLite. A cohort assembled from twelve statements across many
    pages has to see ONE state; if it does not, two entity types can disagree
    with each other and the digest compares a database that never existed.

    The canary commits a row from a separate connection *between pages* and
    proves three things at once: the row is invisible to the running drain,
    every page reports the same PostgreSQL snapshot watermark, and — the
    acceptance half — a fresh drain does see it. Without that last assertion
    the test would also pass if the exporter simply never returned the row.
    """

    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    seeded = [_canary_id(index) for index in range(1, 6)]
    latecomer = _canary_id(9)
    try:
        for position, identifier in enumerate(seeded, start=1):
            _insert_party(engine, identifier, f"Canary {position}")

        session: Session = factory()
        try:
            session.connection(execution_options=dict(READ_ONLY_SNAPSHOT_OPTIONS))
            drained, watermarks = _drain(
                session,
                page_size=2,
                interrupt=lambda: _insert_party(engine, latecomer, "Latecomer"),
            )
        finally:
            session.rollback()
            session.close()

        assert set(seeded) <= set(drained), (
            "the drain lost rows that were committed before it began"
        )
        assert latecomer not in drained, (
            "a row committed by another connection mid-drain became visible; "
            "the export is not reading one snapshot and its per-entity counts "
            "cannot be trusted to agree with each other"
        )
        assert len(set(watermarks)) == 1, (
            f"pages reported {len(set(watermarks))} different snapshot "
            "watermarks; a consumer cannot tell which snapshot it assembled"
        )
        assert watermarks[0] is not None
        assert len(watermarks) > 1, (
            "fixture assumption: the drain must span several pages, or it "
            "proves nothing about holding a snapshot across them"
        )

        fresh: Session = factory()
        try:
            fresh.connection(execution_options=dict(READ_ONLY_SNAPSHOT_OPTIONS))
            after_drain, after_watermarks = _drain(fresh, page_size=2)
        finally:
            fresh.rollback()
            fresh.close()

        assert latecomer in after_drain, (
            "the late row is invisible to a NEW drain too, so the first "
            "drain's not seeing it proves nothing about snapshot isolation"
        )
        assert after_watermarks[0] != watermarks[0], (
            "a later transaction reported the same snapshot watermark as the "
            "earlier one, so the watermark is not identifying a snapshot"
        )
    finally:
        _delete_canaries(engine)
