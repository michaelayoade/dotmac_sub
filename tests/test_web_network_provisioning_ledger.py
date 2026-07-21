"""Provisioning ledger page-data projection tests."""

import pytest

from app.services import web_network_provisioning_ledger as prov_ledger

_FACETS = ["orders", "runs", "tasks", "appointments"]


@pytest.mark.parametrize("facet", _FACETS)
def test_provisioning_ledger_data_shape_per_facet(db_session, facet):
    data = prov_ledger.provisioning_ledger_data(db_session, facet)
    assert data["facet"] == facet
    assert data["facet_label"]
    assert [f["key"] for f in data["facets"]] == _FACETS
    assert isinstance(data["columns"], list) and data["columns"]
    assert data["rows"] == []
    assert data["row_count"] == 0


def test_provisioning_ledger_data_defaults_unknown_facet_to_orders(db_session):
    assert prov_ledger.provisioning_ledger_data(db_session, "bogus")["facet"] == "orders"


def test_provisioning_ledger_data_default_facet_is_orders(db_session):
    assert prov_ledger.provisioning_ledger_data(db_session)["facet"] == "orders"
