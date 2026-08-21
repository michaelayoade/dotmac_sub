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
