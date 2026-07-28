"""IPAM ledger page-data projection tests."""

import pytest

from app.services import web_network_ipam_ledger as ipam_ledger


@pytest.mark.parametrize("facet", ["pools", "blocks", "ipv6_prefixes"])
def test_ipam_ledger_data_shape_per_facet(db_session, facet):
    data = ipam_ledger.ipam_ledger_data(db_session, facet)
    assert data["facet"] == facet
    assert data["facet_label"]
    assert [f["key"] for f in data["facets"]] == ["pools", "blocks", "ipv6_prefixes"]
    assert isinstance(data["columns"], list) and data["columns"]
    assert data["rows"] == []
    assert data["row_count"] == 0


def test_ipam_ledger_data_defaults_unknown_facet_to_pools(db_session):
    assert ipam_ledger.ipam_ledger_data(db_session, "bogus")["facet"] == "pools"


def test_ipam_ledger_data_default_facet_is_pools(db_session):
    assert ipam_ledger.ipam_ledger_data(db_session)["facet"] == "pools"
