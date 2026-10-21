"""Invoice numbering stays on the incumbent sequence until cutover approval."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

invoices = pytest.importorskip("app.services.billing.invoices")


def test_invoice_number_owner_uses_legacy_required_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Mock(spec=Session)
    allocator = Mock(return_value="INV-000041")
    monkeypatch.setattr(invoices.numbering, "generate_required_number", allocator)

    assert invoices.next_invoice_number(db) == "INV-000041"
    allocator.assert_called_once_with(
        db,
        invoices.SettingDomain.billing,
        "invoice_number",
        "invoice_number_prefix",
        "invoice_number_padding",
        "invoice_number_start",
    )
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


@pytest.mark.parametrize(
    "route", ["stage_admin_draft", "stage_system_invoice", "create"]
)
@pytest.mark.parametrize("supplied_label", [None, "IMPORTED-CUSTOM-LABEL"])
def test_invoice_constructors_preserve_supplied_labels_and_delegate_legacy_numbering(
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
        participant.assert_called_once_with(db_session)
        assert document.invoice_number == "INV-CANARY-BOUND"


def test_proforma_conversion_uses_the_same_legacy_number_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import invoice_draft_authoring

    db = Mock(spec=Session)
    participant = Mock(return_value="INV-000042")
    monkeypatch.setattr(invoice_draft_authoring, "next_invoice_number", participant)

    assert invoice_draft_authoring.next_invoice_number(db) == "INV-000042"
    participant.assert_called_once_with(db)
