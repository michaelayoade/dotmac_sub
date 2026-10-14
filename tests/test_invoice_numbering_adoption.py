"""Boundary canaries; real allocation/rollback evidence belongs to PG CI."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from importlib import import_module
from unittest.mock import Mock
from uuid import uuid4

import pytest
from dotmac_numbering.models import NumberSeries, SeriesCounter
from sqlalchemy.orm import Session

from app.services.operator_tenant import OPERATOR_TENANT_ID

invoices = import_module("app.services.billing.invoices")
REFERENCE_DATE = date(2026, 10, 13)


def _series() -> NumberSeries:
    return NumberSeries(
        tenant_id=OPERATOR_TENANT_ID,
        series_code="invoice_number",
        prefix="INV-",
        suffix="",
        separator="",
        min_digits=6,
        include_year=0,
        year_digits=4,
        include_month=0,
        reset_policy="never",
        start_value=1,
    )


@pytest.fixture
def participant(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock]:
    db = Mock(spec=Session)
    db.scalar.side_effect = [
        _series(),
        SeriesCounter(
            tenant_id=OPERATOR_TENANT_ID,
            series_code="invoice_number",
            period_key="*",
            next_value=41,
        ),
    ]
    monkeypatch.setattr(
        invoices.settings_spec,
        "resolve_value",
        lambda _db, _domain, key: {
            "invoice_number_prefix": "INV-",
            "invoice_number_padding": 6,
            "invoice_number_start": 1,
        }[key],
    )
    allocator = Mock(return_value=Mock(formatted_number="INV-000041"))
    monkeypatch.setattr(invoices, "allocate", allocator)
    return db, allocator


def test_participant_binds_uuid_date_tenant_and_initial_key(participant) -> None:
    db, allocator = participant
    invoice_id = uuid4()
    assert (
        invoices.next_invoice_number(
            db,
            invoice_id=invoice_id,
            reference_date=REFERENCE_DATE,
            purpose=invoices.InvoiceNumberPurpose.INITIAL,
        )
        == "INV-000041"
    )
    call = allocator.call_args
    assert call.args == (db,)
    assert call.kwargs["scope"].tenant_id == OPERATOR_TENANT_ID
    assert call.kwargs["series_code"] == "invoice_number"
    assert call.kwargs["reference_date"] == REFERENCE_DATE
    assert call.kwargs["idempotency_key"] == f"billing:invoice:{invoice_id}:initial"
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
    # Both prerequisites must be locked before allocation, not lazily created.
    for call in db.scalar.call_args_list:
        assert call.args[0]._for_update_arg is not None


@pytest.mark.parametrize(
    "fault",
    ["missing_series", "incompatible_series", "missing_counter", "invalid_counter"],
)
def test_unready_configuration_refuses_before_allocator(
    participant, fault: str
) -> None:
    db, allocator = participant
    series = _series()
    counter = SeriesCounter(next_value=41)
    if fault == "incompatible_series":
        series.separator = "-"
    if fault == "invalid_counter":
        counter.next_value = 0
    db.scalar.side_effect = [
        None if fault == "missing_series" else series,
        None if fault == "missing_counter" else counter,
    ]
    with pytest.raises(invoices.InvoiceOwnerError) as refused:
        invoices.next_invoice_number(
            db,
            invoice_id=uuid4(),
            reference_date=REFERENCE_DATE,
            purpose=invoices.InvoiceNumberPurpose.INITIAL,
        )
    assert refused.value.code.endswith(
        "series_unready" if "series" in fault else "counter_unready"
    )
    allocator.assert_not_called()
    db.add.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


@pytest.mark.parametrize(
    "purpose,key",
    [
        (invoices.InvoiceNumberPurpose.INITIAL, "a" * 64),
        (invoices.InvoiceNumberPurpose.PROFORMA_CONVERSION, None),
        (invoices.InvoiceNumberPurpose.PROFORMA_CONVERSION, "raw-command-key"),
        (invoices.InvoiceNumberPurpose.PROFORMA_CONVERSION, "A" * 64),
    ],
)
def test_identity_refuses_ambiguous_conversion_key(participant, purpose, key) -> None:
    db, allocator = participant
    with pytest.raises(invoices.InvoiceOwnerError, match="identity"):
        invoices.next_invoice_number(
            db,
            invoice_id=uuid4(),
            reference_date=REFERENCE_DATE,
            purpose=purpose,
            conversion_key=key,
        )
    db.scalar.assert_not_called()
    allocator.assert_not_called()


def test_reference_date_uses_original_issued_at_else_created_at_in_utc() -> None:
    original = datetime(2020, 12, 31, 23, 30, tzinfo=timezone(-timedelta(hours=2)))
    created = datetime(2026, 10, 13, 12, tzinfo=UTC)
    assert invoices.invoice_number_reference_date(
        issued_at=original,
        created_at=created,
    ) == date(2021, 1, 1)
    assert invoices.invoice_number_reference_date(
        issued_at=None,
        created_at=created,
    ) == date(2026, 10, 13)
    assert invoices.invoice_number_reference_date(
        issued_at=None,
        created_at=created.replace(tzinfo=None),
    ) == date(2026, 10, 13)


@pytest.mark.parametrize(
    "route", ["stage_admin_draft", "stage_system_invoice", "create"]
)
@pytest.mark.parametrize("supplied_label", [None, "IMPORTED-CUSTOM-LABEL"])
def test_constructor_binds_persisted_identity_and_skips_supplied_labels(
    db_session,
    subscriber,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    supplied_label: str | None,
) -> None:
    from app.models.billing import InvoiceStatus
    from app.schemas.billing import InvoiceCreate

    participant = Mock(return_value="INV-CANARY-BOUND")
    monkeypatch.setattr(invoices, "next_invoice_number", participant)
    payload = InvoiceCreate(
        account_id=subscriber.id,
        invoice_number=supplied_label,
        status=InvoiceStatus.draft,
    )
    constructor = getattr(invoices.Invoices, route)
    if route == "stage_system_invoice":
        document = constructor(db_session, payload, reason="numbering-canary")
    elif route == "create":
        document = constructor(db_session, payload, commit=False)
    else:
        document = constructor(db_session, payload)
    assert document.id is not None and document.created_at is not None
    if supplied_label:
        participant.assert_not_called()
        assert document.invoice_number == supplied_label
    else:
        assert document.invoice_number == "INV-CANARY-BOUND"
        assert participant.call_args.kwargs == {
            "invoice_id": document.id,
            "reference_date": document.created_at.date(),
            "purpose": invoices.InvoiceNumberPurpose.INITIAL,
        }


def test_create_with_lines_allocates_from_original_issued_date(
    db_session,
    subscriber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal

    from app.models.billing import InvoiceStatus
    from app.schemas.billing import InvoiceCreate, InvoiceLineCreate

    original = datetime(2020, 12, 31, 23, 30, tzinfo=timezone(-timedelta(hours=2)))
    participant = Mock(return_value="INV-ORIGINAL-DATE")
    monkeypatch.setattr(invoices, "next_invoice_number", participant)
    document = invoices.Invoices.create_with_lines(
        db_session,
        InvoiceCreate(
            account_id=subscriber.id, status=InvoiceStatus.draft, issued_at=original
        ),
        (
            InvoiceLineCreate(
                invoice_id=uuid4(),
                description="Date canary",
                quantity=Decimal("1"),
                unit_price=Decimal("100"),
            ),
        ),
        commit=False,
    )
    assert document.issued_at is None
    assert document.total == Decimal("100.00")
    assert participant.call_args.kwargs["invoice_id"] == document.id
    assert participant.call_args.kwargs["reference_date"] == date(2021, 1, 1)


def test_unsupported_configuration_refuses_before_query_or_allocation(
    participant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, allocator = participant
    monkeypatch.setattr(invoices.settings_spec, "resolve_value", lambda *_args: None)
    with pytest.raises(invoices.InvoiceOwnerError) as refused:
        invoices.next_invoice_number(
            db,
            invoice_id=uuid4(),
            reference_date=REFERENCE_DATE,
            purpose=invoices.InvoiceNumberPurpose.INITIAL,
        )
    assert refused.value.code.endswith("configuration_unsupported")
    db.scalar.assert_not_called()
    allocator.assert_not_called()


def test_conversion_key_is_bound_separately_from_initial_identity(participant) -> None:
    db, allocator = participant
    invoice_id = uuid4()
    db.scalar.side_effect = [
        _series(),
        SeriesCounter(next_value=41),
        _series(),
        SeriesCounter(next_value=42),
    ]
    invoices.next_invoice_number(
        db,
        invoice_id=invoice_id,
        reference_date=REFERENCE_DATE,
        purpose=invoices.InvoiceNumberPurpose.INITIAL,
    )
    invoices.next_invoice_number(
        db,
        invoice_id=invoice_id,
        reference_date=REFERENCE_DATE,
        purpose=invoices.InvoiceNumberPurpose.PROFORMA_CONVERSION,
        conversion_key="a" * 64,
    )
    keys = [call.kwargs["idempotency_key"] for call in allocator.call_args_list]
    assert keys == [
        f"billing:invoice:{invoice_id}:initial",
        f"billing:invoice:{invoice_id}:proforma_conversion:{'a' * 64}",
    ]


def test_terminal_counter_is_forwarded_for_allocator_owned_receipt_replay(
    participant,
) -> None:
    db, allocator = participant
    invoice_id = uuid4()
    db.scalar.side_effect = [
        _series(),
        SeriesCounter(
            tenant_id=OPERATOR_TENANT_ID,
            series_code="invoice_number",
            period_key="*",
            next_value=invoices.MAX_VALUE,
        ),
    ]
    allocator.return_value.formatted_number = f"INV-{invoices.MAX_VALUE - 1}"
    assert (
        invoices.next_invoice_number(
            db,
            invoice_id=invoice_id,
            reference_date=REFERENCE_DATE,
            purpose=invoices.InvoiceNumberPurpose.INITIAL,
        )
        == f"INV-{invoices.MAX_VALUE - 1}"
    )
    allocator.assert_called_once()
    assert (
        allocator.call_args.kwargs["idempotency_key"]
        == f"billing:invoice:{invoice_id}:initial"
    )
    db.add.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
