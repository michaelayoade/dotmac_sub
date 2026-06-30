from pathlib import Path

from app.web.admin import billing_consolidated


def test_consolidated_record_payment_route_is_registered():
    routes = {
        (getattr(route, "path", None), tuple(sorted(getattr(route, "methods", []))))
        for route in billing_consolidated.router.routes
    }

    assert (
        "/billing/consolidated-accounts/{billing_account_id}/record-payment",
        ("POST",),
    ) in routes


def test_consolidated_payment_template_confirms_and_shows_feedback():
    template = Path("templates/admin/billing/consolidated/detail.html").read_text()

    assert "Record and distribute this consolidated payment" in template
    assert "button[type=submit]').disabled = true" in template
    assert 'min="0.01"' in template
    assert "payment_note" in template
    assert "payment_error" in template


def test_consolidated_record_payment_redirects_with_success(db_session, monkeypatch):
    captured = {}

    def _fake_record_bulk_payment(db, **kwargs):
        captured.update(kwargs)
        return "payment-1"

    monkeypatch.setattr(
        billing_consolidated.web_consolidated_billing_service,
        "record_bulk_payment",
        _fake_record_bulk_payment,
    )

    response = billing_consolidated.consolidated_record_payment(
        request=None,
        billing_account_id="account-1",
        amount="1000.00",
        currency="NGN",
        memo="bank ref",
        collection_account_id="collection-1",
        db=db_session,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith(
        "/admin/billing/consolidated-accounts/account-1?payment_note="
    )
    assert captured == {
        "billing_account_id": "account-1",
        "amount": "1000.00",
        "currency": "NGN",
        "memo": "bank ref",
        "collection_account_id": "collection-1",
    }


def test_consolidated_record_payment_uses_default_currency_when_omitted(
    db_session, monkeypatch
):
    captured = {}

    def _fake_record_bulk_payment(db, **kwargs):
        captured.update(kwargs)
        return "payment-1"

    monkeypatch.setattr(
        billing_consolidated.web_consolidated_billing_service,
        "record_bulk_payment",
        _fake_record_bulk_payment,
    )
    monkeypatch.setattr(
        billing_consolidated.settings_spec,
        "resolve_value",
        lambda *_args, **_kwargs: "USD",
    )

    response = billing_consolidated.consolidated_record_payment(
        request=None,
        billing_account_id="account-1",
        amount="1000.00",
        currency=None,
        memo=None,
        collection_account_id=None,
        db=db_session,
    )

    assert response.status_code == 303
    assert captured["currency"] == "USD"


def test_consolidated_record_payment_redirects_with_safe_error(db_session, monkeypatch):
    def _fake_record_bulk_payment(db, **kwargs):
        raise ValueError("raw decimal stack detail")

    monkeypatch.setattr(
        billing_consolidated.web_consolidated_billing_service,
        "record_bulk_payment",
        _fake_record_bulk_payment,
    )

    response = billing_consolidated.consolidated_record_payment(
        request=None,
        billing_account_id="account-1",
        amount="not-money",
        currency="NGN",
        memo=None,
        collection_account_id=None,
        db=db_session,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith(
        "/admin/billing/consolidated-accounts/account-1?payment_error="
    )
    assert "raw" not in response.headers["location"]
    assert "stack" not in response.headers["location"]
