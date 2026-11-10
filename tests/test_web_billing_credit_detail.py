"""Read-only credit drill-down preserves the incumbent owner's exact facts."""

import importlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from sqlalchemy.orm import Session

from app.models.billing import (
    CreditNote,
    CreditNoteApplication,
    CreditNoteLine,
    CreditNoteStatus,
)
from app.services import web_billing_credits


def _credit() -> CreditNote:
    created = datetime(2026, 11, 9, 10, tzinfo=UTC)
    credit = CreditNote(
        id=uuid4(),
        account_id=uuid4(),
        credit_number="CN-2026-42",
        currency="NGN",
        status=CreditNoteStatus.partially_applied,
        subtotal=Decimal("1000000.01"),
        tax_total=Decimal("75000.00"),
        total=Decimal("1075000.01"),
        applied_total=Decimal("400000.00"),
        memo="Correction <script>alert(1)</script>",
        created_at=created,
        issued_at=created,
    )
    credit.lines = [
        CreditNoteLine(
            id=uuid4(),
            description="Service correction",
            quantity=Decimal("1.000"),
            unit_price=Decimal("1000000.01"),
            amount=Decimal("1000000.01"),
        )
    ]
    credit.applications = [
        CreditNoteApplication(
            id=uuid4(),
            invoice_id=uuid4(),
            amount=Decimal("400000.00"),
            created_at=created,
            memo="Applied to the original invoice",
        )
    ]
    return credit


def test_credit_detail_delegates_to_owner_without_recalculating_money(monkeypatch):
    credit = _credit()
    reader = Mock(return_value=credit)
    monkeypatch.setattr(web_billing_credits.billing_service.credit_notes, "get", reader)
    monkeypatch.setattr(
        web_billing_credits.web_billing_customers_service,
        "account_label",
        lambda account: "Customer with a long account name",
    )
    db = cast(Session, Mock(spec=Session))

    detail = web_billing_credits.build_credit_detail(
        db=db,
        query=web_billing_credits.CreditDetailQuery(credit_note_id=credit.id),
    )

    reader.assert_called_once_with(db=db, credit_note_id=str(credit.id))
    assert detail.total == Decimal("1075000.01")
    assert detail.applied_total == Decimal("400000.00")
    assert detail.status == CreditNoteStatus.partially_applied
    assert detail.lines[0].description == "Service correction"
    assert detail.applications[0].invoice_id == credit.applications[0].invoice_id
    assert detail.memo == credit.memo
    db.commit.assert_not_called()
    db.flush.assert_not_called()


def test_missing_credit_preserves_owner_not_found(monkeypatch):
    credit_notes = importlib.import_module("app.services.billing.credit_notes")
    monkeypatch.setattr(credit_notes, "get_by_id", lambda *args, **kwargs: None)
    with pytest.raises(HTTPException) as exc:
        web_billing_credits.build_credit_detail(
            db=cast(Session, Mock(spec=Session)),
            query=web_billing_credits.CreditDetailQuery(credit_note_id=uuid4()),
        )
    assert exc.value.status_code == 404


def test_credit_detail_template_escapes_memo_and_gates_invoice_link(monkeypatch):
    credit = _credit()
    monkeypatch.setattr(
        web_billing_credits.billing_service.credit_notes,
        "get",
        Mock(return_value=credit),
    )
    detail = web_billing_credits.build_credit_detail(
        db=cast(Session, Mock(spec=Session)),
        query=web_billing_credits.CreditDetailQuery(credit_note_id=credit.id),
    )
    root = Path(__file__).resolve().parents[1]
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"layouts/admin.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(root / "templates"),
            ]
        ),
        autoescape=True,
    )
    env.filters["app_datetime"] = lambda value, *args, **kwargs: str(value or "—")
    env.globals["can"] = lambda request, permission: False
    template = env.get_template("admin/billing/credit_detail.html")
    html = template.render(credit=detail, request=None)

    assert "1,075,000.01" in html
    assert "&lt;script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert (
        f'href="/admin/billing/invoices/{credit.applications[0].invoice_id}"'
        not in html
    )
    env.globals["can"] = lambda request, permission: True
    allowed = template.render(credit=detail, request=None)
    assert (
        f'href="/admin/billing/invoices/{credit.applications[0].invoice_id}"' in allowed
    )


def test_credit_detail_route_requires_read_permission():
    from app.web.admin.billing_credits import router

    route = next(
        route
        for route in router.routes
        if route.path == "/billing/credits/{credit_note_id:uuid}"
    )
    assert route.methods == {"GET"}
    dependency = route.dependencies[0].dependency
    assert "billing:credit_note:read" in {
        cell.cell_contents
        for cell in (dependency.__closure__ or ())
        if isinstance(cell.cell_contents, str)
    }
