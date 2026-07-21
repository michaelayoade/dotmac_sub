"""Billing ledger page-data projection tests."""

import pytest

from app.services import web_billing_ledger as billing_ledger

_FACETS = ["invoices", "payments", "credit_notes"]


@pytest.mark.parametrize("facet", _FACETS)
def test_billing_ledger_data_shape_per_facet(db_session, facet):
    data = billing_ledger.billing_ledger_data(db_session, facet)
    assert data["facet"] == facet
    assert data["facet_label"]
    assert [f["key"] for f in data["facets"]] == _FACETS
    assert isinstance(data["columns"], list) and data["columns"]
    assert data["rows"] == []
    assert data["row_count"] == 0


def test_billing_ledger_data_defaults_unknown_facet_to_invoices(db_session):
    assert billing_ledger.billing_ledger_data(db_session, "bogus")["facet"] == "invoices"


def test_billing_ledger_data_default_facet_is_invoices(db_session):
    assert billing_ledger.billing_ledger_data(db_session)["facet"] == "invoices"


def test_billing_ledger_invoices_facet_links_to_detail(db_session):
    assert (
        billing_ledger.billing_ledger_data(db_session, "invoices")["detail_base"]
        == "/admin/billing/invoices/"
    )
