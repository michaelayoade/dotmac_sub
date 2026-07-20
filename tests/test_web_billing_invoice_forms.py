import uuid
from types import SimpleNamespace

from app.services import web_billing_invoice_forms as invoice_forms


def _account():
    return SimpleNamespace(id=uuid.uuid4(), account_number="ACC-001")


def _stub_common(monkeypatch, account, due_days: int) -> None:
    monkeypatch.setattr(
        invoice_forms.web_billing_invoices_service,
        "load_tax_rates",
        lambda _db: [],
    )
    monkeypatch.setattr(
        invoice_forms,
        "resolve_payment_due_days",
        lambda _db, *, subscriber=None: due_days,
    )
    monkeypatch.setattr(
        invoice_forms.settings_spec,
        "resolve_value",
        lambda *_args, **_kwargs: "NGN",
    )
    monkeypatch.setattr(
        invoice_forms.web_billing_customers_service,
        "account_label",
        lambda _account: "Test Customer",
    )
    monkeypatch.setattr(
        invoice_forms,
        "resolve_selected_account",
        lambda _db, _account_id: account,
    )


def test_new_form_state_uses_resolved_payment_due_days(monkeypatch):
    account = _account()
    _stub_common(monkeypatch, account, due_days=14)

    state = invoice_forms.new_form_state(object(), account_id=str(account.id))

    assert state["invoice_config"]["paymentTermsDays"] == 14


def test_edit_form_state_uses_resolved_payment_due_days(monkeypatch):
    account = _account()
    _stub_common(monkeypatch, account, due_days=21)
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        account_id=account.id,
        account=account,
        currency="NGN",
        invoice_number="INV-001",
        status=SimpleNamespace(value="draft"),
        issued_at=None,
        due_at=None,
        memo="",
        lines=[],
    )
    monkeypatch.setattr(
        invoice_forms.billing_service.invoices,
        "get",
        lambda db, invoice_id: invoice,
    )

    state = invoice_forms.edit_form_state(object(), invoice_id=str(invoice.id))

    assert state["invoice_config"]["paymentTermsDays"] == 21
