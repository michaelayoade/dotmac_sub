from decimal import Decimal
from unittest.mock import patch

from starlette.requests import Request

from app.models.payment_proof import WithholdingTaxRecord, WithholdingTaxStatus
from app.models.subscriber import Reseller
from app.services import billing as billing_service


def _request(path: str) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
        }
    )
    request.state.actor_id = "finance-admin"
    return request


def _billing_overview_response(db_session, permission_keys: frozenset[str]):
    from app.web.admin.billing_invoices import billing_overview

    request = _request("/admin/billing")
    request.state.auth = {"permission_keys": permission_keys}
    state = {
        "stats": {},
        "invoices": [],
        "default_currency": "NGN",
        "selected_partner_id": None,
        "partner_options": [],
        "invoice_status_presentations": {},
    }

    with (
        patch(
            "app.web.admin.billing_invoices.web_billing_overview_service.build_overview_data",
            return_value=state,
        ),
        patch(
            "app.web.admin.billing_invoices.get_current_user",
            return_value={"id": "admin"},
        ),
        patch("app.web.admin.billing_invoices.get_sidebar_stats", return_value={}),
    ):
        return billing_overview(
            request=request,
            partner_id=None,
            location=None,
            period="this_month",
            db=db_session,
        )


def test_tax_accounting_operator_console_renders_owned_state(db_session) -> None:
    reseller = Reseller(
        name="Tax Console Reseller",
        contact_email="tax-console@example.com",
    )
    db_session.add(reseller)
    db_session.commit()
    billing_account = billing_service.billing_accounts.get_for_reseller(
        db_session,
        str(reseller.id),
    )
    db_session.add(
        WithholdingTaxRecord(
            billing_account_id=billing_account.id,
            reseller_id=reseller.id,
            gross_amount=Decimal("100000.00"),
            net_amount=Decimal("95000.00"),
            wht_amount=Decimal("5000.00"),
            wht_rate=Decimal("5.00"),
            currency="NGN",
            status=WithholdingTaxStatus.pending,
        )
    )
    db_session.commit()

    from app.web.admin.billing_reporting import billing_tax_accounting

    with (
        patch("app.web.admin.get_current_user", return_value={"id": "admin"}),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
    ):
        response = billing_tax_accounting(
            request=_request("/admin/billing/tax-accounting"),
            date_from=None,
            date_to=None,
            wht_status=None,
            wht_search=None,
            wht_page=1,
            error=None,
            message=None,
            db=db_session,
        )

    text = response.body.decode()
    assert response.status_code == 200
    assert "Tax Source Register" in text
    assert "Dotmac ERP is authoritative" in text
    assert "TaxCode account mappings" in text
    assert "ENABLE TAX SHADOW" not in text
    assert "Certificate reference" in text
    assert "Reconciliation and parity" not in text
    assert "Pending certificate" in text
    assert "Tax Console Reseller" in text
    assert "Page 1 of 1 \u00b7 1 records" in text


def test_billing_workbench_shows_tax_wht_link_for_authorized_finance_user(
    db_session,
) -> None:
    response = _billing_overview_response(
        db_session,
        frozenset({"billing:invoice:read", "billing:tax:read"}),
    )

    text = response.body.decode()

    assert response.status_code == 200
    assert "Tax &amp; WHT" in text
    assert "Source register and WHT evidence" in text
    assert 'href="/admin/billing/tax-accounting"' in text


def test_billing_workbench_hides_tax_wht_link_without_tax_read_permission(
    db_session,
) -> None:
    response = _billing_overview_response(
        db_session,
        frozenset({"billing:invoice:read"}),
    )

    text = response.body.decode()

    assert response.status_code == 200
    assert "Tax &amp; WHT" not in text
    assert "Source register and WHT evidence" not in text
    assert 'href="/admin/billing/tax-accounting"' not in text
