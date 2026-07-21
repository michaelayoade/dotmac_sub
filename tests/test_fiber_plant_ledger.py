"""The fiber-plant ledger projects each asset type from its SOT owner .list()."""

import pytest

from app.services.web_network_fiber_plant_ledger import (
    ASSET_TYPES,
    fiber_plant_ledger_data,
)


@pytest.mark.parametrize("asset_type", [k for k, _ in ASSET_TYPES])
def test_ledger_projects_each_type_from_its_owner(db_session, asset_type):
    data = fiber_plant_ledger_data(db_session, asset_type=asset_type)
    assert data["asset_type"] == asset_type
    assert data["asset_label"]
    assert data["columns"] and isinstance(data["columns"], list)
    assert isinstance(data["rows"], list)
    assert data["row_count"] == len(data["rows"])
    assert {a["key"] for a in data["asset_types"]} == {k for k, _ in ASSET_TYPES}


def test_ledger_defaults_to_fdh_on_unknown_type(db_session):
    assert fiber_plant_ledger_data(db_session, asset_type="nope")["asset_type"] == "fdh"
