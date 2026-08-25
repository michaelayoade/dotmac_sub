"""PostgreSQL proof for the report-only Collections shadow adapter."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, InternalError
from sqlalchemy.orm import Session, sessionmaker

from app import db as app_db
from app.models.billing import Invoice, InvoiceDueDateBasis, InvoiceStatus
from app.models.subscriber import Subscriber
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.subscriber import _default_reseller_id
from scripts.migration import collections_module_shadow_parity

pytestmark = pytest.mark.integration


@pytest.fixture()
def operator_session_factory(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> sessionmaker[Session]:
    if engine.dialect.name != "postgresql":
        pytest.fail("the shadow contract requires migrated PostgreSQL")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(app_db, "SessionLocal", factory)
    return factory


@pytest.fixture()
def snapshot(
    operator_session_factory: sessionmaker[Session],
) -> Iterator[Session]:
    del operator_session_factory
    with app_db.read_only_snapshot_session() as session:
        yield session


def test_report_only_entry_point_runs_on_the_migrated_catalog(
    operator_session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = operator_session_factory()
    marker = uuid4().hex
    try:
        subscriber = Subscriber(
            first_name="Collections",
            last_name="Shadow",
            email=f"collections-shadow-{marker}@example.invalid",
            reseller_id=_default_reseller_id(writer),
        )
        writer.add(subscriber)
        writer.flush()
        now = datetime.now(UTC)
        held = Invoice(
            account_id=subscriber.id,
            invoice_number=f"CS-HOLD-{marker}",
            status=InvoiceStatus.overdue,
            currency="NGN",
            subtotal=Decimal("100.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
            issued_at=now - timedelta(days=10),
            due_at=now - timedelta(days=1),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref="pytest:collections-shadow",
            due_date_policy_version="pytest-v1",
            metadata_={"reconciliation_hold": True},
            is_active=True,
        )
        unknown = Invoice(
            account_id=subscriber.id,
            invoice_number=f"CS-NULL-{marker}",
            status=InvoiceStatus.overdue,
            currency="NGN",
            subtotal=Decimal("50.00"),
            total=Decimal("50.00"),
            balance_due=Decimal("50.00"),
            issued_at=now - timedelta(days=10),
            due_at=None,
            due_date_basis=None,
            metadata_={},
            is_active=True,
        )
        raw_string_hold = Invoice(
            account_id=subscriber.id,
            invoice_number=f"CS-RAW-HOLD-{marker}",
            status=InvoiceStatus.overdue,
            currency="NGN",
            subtotal=Decimal("25.00"),
            total=Decimal("25.00"),
            balance_due=Decimal("25.00"),
            issued_at=now - timedelta(days=10),
            due_at=now - timedelta(days=1),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref="pytest:collections-shadow-raw-hold",
            due_date_policy_version="pytest-v1",
            metadata_={"reconciliation_hold": "false"},
            is_active=True,
        )
        future_due_without_provenance = Invoice(
            account_id=subscriber.id,
            invoice_number=f"CS-FUTURE-NULL-{marker}",
            status=InvoiceStatus.issued,
            currency="NGN",
            subtotal=Decimal("75.00"),
            total=Decimal("75.00"),
            balance_due=Decimal("75.00"),
            issued_at=now - timedelta(days=1),
            due_at=now + timedelta(days=1),
            due_date_basis=None,
            metadata_={},
            is_active=True,
        )
        writer.add_all([held, unknown, raw_string_hold, future_due_without_provenance])
        writer.commit()

        result = collections_module_shadow_parity.main(
            [
                "--report-only",
                "--as-of",
                now.isoformat(),
                "--observe-at",
                (now + timedelta(days=1)).isoformat(),
            ]
        )
    finally:
        writer.close()

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["classified"] == payload["invoices"]
    assert payload["classified"] >= 4
    assert payload["matched_blocked"] >= 1
    assert payload["module_blocked_legacy_actionable"] >= 1
    assert payload["module_actionable_legacy_blocked"] >= 1
    assert payload["null_due_date_basis"] >= 1
    assert payload["module_blockers"]["no_live_exposure"] >= 1
    assert payload["module_blockers"]["due_date_unverified"] >= 1
    assert payload["observation_horizon_seconds"] == 86400
    assert payload["latent_temporal_mismatches"] >= 1
    assert {
        (item["legacy_blocker"], item["module_blocker"])
        for item in payload["blocker_pairs"]
    } >= {("receivable_not_due", "due_date_unverified")}
    assert {
        (item["evaluation_parity"], item["observation_parity"])
        for item in payload["temporal_transitions"]
    } >= {("matched_blocked", "module_blocked_legacy_actionable")}
    assert isinstance(payload["blocking_reasons"], list)
    serialized = json.dumps(payload, sort_keys=True)
    assert not any("_id" in key or "amount" in key for key in payload)
    assert marker not in serialized
    assert now.isoformat() not in serialized


def test_snapshot_is_repeatable_read_and_tenant_scoped(snapshot: Session) -> None:
    assert snapshot.execute(text("SHOW transaction_isolation")).scalar_one() == (
        "repeatable read"
    )
    assert snapshot.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
    assert snapshot.execute(
        text("SELECT current_setting('app.current_tenant', true)")
    ).scalar_one() == str(OPERATOR_TENANT_ID)


def test_snapshot_rejects_writes(snapshot: Session) -> None:
    with pytest.raises((DBAPIError, InternalError)) as excinfo:
        snapshot.execute(
            text(
                "INSERT INTO roles (id, name, is_active) "
                "VALUES (gen_random_uuid(), 'collections-shadow-canary', true)"
            )
        )
    assert "read-only" in str(excinfo.value).lower()
