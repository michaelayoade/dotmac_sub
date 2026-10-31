"""Hosted-only: the SQL observation port against a real PostgreSQL census.

Proves, on migrated PostgreSQL, the three observations the Gap B verifier
relies on: a census re-render that is byte-identical when nothing moved and
differs when the source moves (the writer fence), the rehearsal marker, and
the restricted-role probe. Billing-side provision/seal need the Starter
billing migrations and are covered by the pure rehearsal-shaped test.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app import db as app_db
from app.models.billing import Invoice
from app.services.billing.opening_census_wire import render_opening_census_wire
from app.services.billing.opening_export import preview_billing_opening_export
from app.services.billing.opening_export_contracts import (
    PreviewBillingOpeningExportQuery,
)
from app.services.billing.opening_verifier import SqlSourceObservationPort
from tests.integration.test_billing_opening_export import (  # noqa: F401
    SeededCensus,
    census_factory,
    census_rows,
)

pytestmark = pytest.mark.integration


def _query(cutoff: datetime) -> PreviewBillingOpeningExportQuery:
    return PreviewBillingOpeningExportQuery(
        "a" * 40,
        UUID(int=123),
        cutoff,
        (("NGN", 2), ("USD", 2)),
        "sub-billing-opening-census-v3",
    )


def _baseline(cutoff: datetime) -> tuple[bytes, str, datetime]:
    with app_db.read_only_snapshot_session() as db:
        preview = preview_billing_opening_export(db, query=_query(cutoff))
    wire = render_opening_census_wire(preview).encode("utf-8")
    return wire, preview.capture.snapshot_id, preview.capture.captured_at


def test_reobservation_is_byte_identical_until_the_source_moves(
    census_factory: sessionmaker[Session],  # noqa: F811
    census_rows: SeededCensus,  # noqa: F811
) -> None:
    query = _query(census_rows.cutoff)
    wire, snapshot_id, captured_at = _baseline(census_rows.cutoff)
    port = SqlSourceObservationPort()
    assert port.census_wire(query, snapshot_id, captured_at) == wire
    with census_factory() as writer:
        invoice = writer.get(Invoice, census_rows.invoices[0])
        assert invoice is not None
        invoice.invoice_number = f"MOVED-{uuid4().hex}"
        writer.commit()
    assert port.census_wire(query, snapshot_id, captured_at) != wire


def test_marker_and_restricted_role_observations(
    census_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    port = SqlSourceObservationPort()
    assert port.marker() is None
    role = f"gapb_probe_{uuid4().hex[:12]}"
    with census_factory() as admin:
        admin.execute(
            text(
                "CREATE TABLE public.rehearsal_marker "
                "(restore_id text PRIMARY KEY, note text NOT NULL)"
            )
        )
        admin.execute(
            text("INSERT INTO public.rehearsal_marker VALUES ('hosted-test', 'n')")
        )
        try:
            admin.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        except Exception:  # noqa: BLE001 - role creation needs CREATEROLE
            admin.rollback()
            pytest.skip("test database user cannot create roles")
        admin.commit()
    try:
        assert port.marker() == "hosted-test"
        observed = port.role(role)
        assert observed is not None and observed.oid > 0
        assert not observed.privileged
        assert port.role(f"{role}_absent") is None
    finally:
        with census_factory() as admin:
            admin.execute(text("DROP TABLE IF EXISTS public.rehearsal_marker"))
            admin.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
            admin.commit()
