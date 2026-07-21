"""Access / RADIUS / FUP ledger page-data projection tests."""

import pytest

from app.services import web_network_access_ledger as access_ledger


@pytest.mark.parametrize("facet", ["sessions", "fup"])
def test_access_ledger_data_shape_per_facet(db_session, facet):
    data = access_ledger.access_ledger_data(db_session, facet)
    assert data["facet"] == facet
    assert data["facet_label"]
    assert [f["key"] for f in data["facets"]] == ["sessions", "fup"]
    assert isinstance(data["columns"], list) and data["columns"]
    assert data["rows"] == []
    assert data["row_count"] == 0
    # live counts come from the canonical read owner
    assert data["summary"] == {"sessions": 0, "customers": 0}


def test_access_ledger_data_defaults_unknown_facet_to_sessions(db_session):
    data = access_ledger.access_ledger_data(db_session, "bogus")
    assert data["facet"] == "sessions"


def test_access_ledger_data_default_facet_is_sessions(db_session):
    assert access_ledger.access_ledger_data(db_session)["facet"] == "sessions"
