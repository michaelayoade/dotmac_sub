"""The fiber-plant ledger reads bounded pages from the selected SOT owner."""

import pytest

from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestOperation,
)
from app.models.network import FdhCabinet
from app.services.fiber_note_presentation import present_fiber_notes
from app.services.network.fiber_services import fiber_splice_closures, fiber_strands
from app.services.network.splitters import splitters
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
    assert data["row_count"] >= len(data["rows"])
    assert len(data["rows"]) <= data["pagination"]["per_page"]
    assert {a["key"] for a in data["asset_types"]} == {k for k, _ in ASSET_TYPES}


def test_ledger_defaults_to_fdh_on_unknown_type(db_session):
    assert fiber_plant_ledger_data(db_session, asset_type="nope")["asset_type"] == "fdh"


def test_selected_tab_does_not_read_other_inventories(db_session, monkeypatch):
    def unexpected_read(*args, **kwargs):
        raise AssertionError("unselected inventory was loaded")

    for owner in (splitters, fiber_strands, fiber_splice_closures):
        monkeypatch.setattr(owner, "inventory_page", unexpected_read)
    assert fiber_plant_ledger_data(db_session, asset_type="fdh")["asset_type"] == "fdh"


def test_partial_tab_read_skips_full_page_activation_summary(db_session, monkeypatch):
    from app.services.network import as_built_plant_projection

    def unexpected_count(*args, **kwargs):
        raise AssertionError("full page summary was loaded for a tab fragment")

    monkeypatch.setattr(
        as_built_plant_projection, "awaiting_activation_count", unexpected_count
    )
    data = fiber_plant_ledger_data(
        db_session, asset_type="fdh", include_activation_count=False
    )
    assert data["awaiting_activation_count"] == 0


def test_fdh_ledger_pages_and_searches_database_scope(db_session):
    db_session.add_all(
        [
            FdhCabinet(
                name=f"Cabinet {index:03d}", code=f"CAB-{index:03d}", is_active=True
            )
            for index in range(55)
        ]
        + [FdhCabinet(name="Retired cabinet", code="CAB-OLD", is_active=False)]
    )
    db_session.flush()

    first = fiber_plant_ledger_data(db_session, page=1, per_page=10)
    last = fiber_plant_ledger_data(db_session, page=6, per_page=10)
    matched = fiber_plant_ledger_data(db_session, search="CAB-042", per_page=10)

    assert first["row_count"] == 56
    assert len(first["rows"]) == 10
    assert first["pagination"]["total_pages"] == 6
    assert len(last["rows"]) == 6
    assert matched["row_count"] == 1
    assert matched["rows"][0]["code"] == "CAB-042"
    retired = fiber_plant_ledger_data(db_session, search="CAB-OLD", per_page=10)
    assert retired["rows"][0]["inventory"] == "Inactive"


def test_change_request_tab_has_its_own_bounded_search_page(db_session):
    requests = [
        FiberChangeRequest(
            asset_type="splitter",
            operation=FiberChangeRequestOperation.update,
            payload={},
        )
        for _ in range(12)
    ]
    db_session.add_all(requests)
    db_session.flush()

    first = fiber_plant_ledger_data(
        db_session, asset_type="change_requests", per_page=10, search="splitter"
    )
    second = fiber_plant_ledger_data(
        db_session, asset_type="change_requests", page=2, per_page=10, search="splitter"
    )

    assert first["row_count"] == 12
    assert len(first["rows"]) == 10
    assert first["pagination"]["total_pages"] == 2
    assert len(second["rows"]) == 2
    by_id = fiber_plant_ledger_data(
        db_session, asset_type="change_requests", search=str(requests[0].id)
    )
    assert by_id["row_count"] == 1


def test_import_evidence_is_structured_without_losing_human_notes():
    evidence = present_fiber_notes(
        '{"source_system":"fiber-map","source_profile":"fdh","source_external_id":"abc"}'
    )
    assert evidence.text is None
    assert ("Source system", "fiber-map") in evidence.evidence
    assert ("Source external ID", "abc") in evidence.evidence
    assert present_fiber_notes("Needs field check").text == "Needs field check"
