"""`migration.cohort_export` behaviour: refusal, ordering, and leaving no trace.

The fast lane. Ordering, checkpoint continuation and refusal are properties of
the service rather than of PostgreSQL, so they are proved here where they are
cheap to run and easy to read. The PostgreSQL-only guarantees — that the
read-only seam really does reject a write, and that the snapshot watermark is
real — live in `tests/integration/test_isp_cohort_export_postgres.py`, because
SQLite cannot represent either and pretending otherwise is what hides a defect.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from app.migration_source.cohort import CohortEntityType
from app.migration_source.snapshot import (
    Completeness,
    ContractVersion,
    UnsupportedContractVersionError,
)
from app.models.party import Party
from app.services.migration_source_export import (
    CohortExportCommand,
    CrossTenantExportRefused,
    export_cohort_digest,
    export_page,
)
from app.services.operator_tenant import operator_tenant_id


@pytest.fixture
def parties(db_session: Session) -> list[Party]:
    """Five parties, deliberately inserted out of primary-key order."""

    rows = [
        Party(
            id=uuid.UUID(f"{index:08x}-0000-4000-8000-000000000000"),
            party_type="person",
            display_name=f"Person {index}",
            status="active",
            data_classification="test",
        )
        for index in (3, 1, 5, 2, 4)
    ]
    db_session.add_all(rows)
    db_session.flush()
    return rows


def _command(**overrides: object) -> CohortExportCommand:
    payload: dict[str, object] = {
        "contract_version": ContractVersion.V1.value,
        "entity_type": CohortEntityType.PARTY,
        "tenant_id": operator_tenant_id(),
    }
    payload.update(overrides)
    return CohortExportCommand.model_validate(payload)


def test_a_foreign_tenant_is_refused_not_answered_empty(db_session: Session) -> None:
    """An importer cannot tell an empty page from a wrong-tenant one."""

    with pytest.raises(CrossTenantExportRefused) as caught:
        export_page(db_session, _command(tenant_id=uuid.uuid4()))
    assert "refused rather than answered with an empty page" in str(caught.value)


def test_the_operator_tenant_is_admitted(db_session: Session) -> None:
    """The acceptance half of the tenant check."""

    page = export_page(db_session, _command())
    assert page.tenant.tenant_id == operator_tenant_id()


def test_an_unsupported_contract_version_is_refused(db_session: Session) -> None:
    with pytest.raises(UnsupportedContractVersionError):
        export_page(db_session, _command(contract_version="99"))


def test_a_page_is_ordered_by_source_id_whatever_the_insert_order(
    db_session: Session, parties: list[Party]
) -> None:
    page = export_page(db_session, _command(page_size=10))
    exported = [record.source_id for record in page.records]
    assert exported == sorted(exported, key=str)
    assert {party.id for party in parties} <= set(exported)


def test_paging_reassembles_into_the_same_ordered_set(
    db_session: Session, parties: list[Party]
) -> None:
    """Keyset continuation must lose nothing and repeat nothing."""

    whole = export_page(db_session, _command(page_size=500))
    assert whole.completeness is Completeness.COMPLETE

    drained: list[uuid.UUID] = []
    after: uuid.UUID | None = None
    for _ in range(50):
        page = export_page(db_session, _command(page_size=2, after_source_id=after))
        drained.extend(record.source_id for record in page.records)
        if page.next_cursor is None:
            assert page.completeness is Completeness.COMPLETE
            break
        assert page.completeness is Completeness.PARTIAL
        after = page.next_cursor.after_source_id
    else:  # pragma: no cover - a drain this long means the checkpoint is broken
        pytest.fail("keyset drain did not terminate")

    assert drained == [record.source_id for record in whole.records]
    assert len(set(drained)) == len(drained)


def test_the_same_cursor_returns_the_same_page(
    db_session: Session, parties: list[Party]
) -> None:
    first = export_page(db_session, _command(page_size=2))
    again = export_page(db_session, _command(page_size=2))
    assert [record.digest() for record in first.records] == [
        record.digest() for record in again.records
    ]


def test_an_export_leaves_no_pending_change(
    db_session: Session, parties: list[Party]
) -> None:
    export_page(db_session, _command(page_size=2))
    assert not db_session.new
    assert not db_session.dirty
    assert not db_session.deleted


def test_a_digest_drain_covers_every_declared_entity_type(
    db_session: Session, parties: list[Party]
) -> None:
    digest = export_cohort_digest(
        db_session,
        contract_version=ContractVersion.V1.value,
        tenant_id=operator_tenant_id(),
        page_size=2,
    )
    assert {item.entity_type for item in digest.entity_types} == set(CohortEntityType)
    party_side = digest.entity_type_digest(CohortEntityType.PARTY)
    assert party_side is not None
    assert party_side.count >= len(parties)
    assert party_side.completeness is Completeness.COMPLETE


def test_a_digest_drain_reports_partial_when_its_budget_runs_out(
    db_session: Session, parties: list[Party]
) -> None:
    """A truncated drain must say so rather than look complete."""

    digest = export_cohort_digest(
        db_session,
        contract_version=ContractVersion.V1.value,
        tenant_id=operator_tenant_id(),
        page_size=1,
        page_budget=1,
    )
    party_side = digest.entity_type_digest(CohortEntityType.PARTY)
    assert party_side is not None
    assert party_side.completeness is Completeness.PARTIAL
    assert party_side.resume_from is not None
    assert digest.completeness is Completeness.PARTIAL


def test_a_digest_carries_no_field_values(
    db_session: Session, parties: list[Party]
) -> None:
    digest = export_cohort_digest(
        db_session,
        contract_version=ContractVersion.V1.value,
        tenant_id=operator_tenant_id(),
    )
    rendered = digest.model_dump_json()
    assert "Person 1" not in rendered
    assert "Person 5" not in rendered
