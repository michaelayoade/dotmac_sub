from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.infrastructure import InfrastructureType
from app.schemas.infrastructure import InfrastructureOption, InfrastructureOptions
from app.services import infrastructure_ticket_selection as selection
from app.services.infrastructure_ticket_selection import (
    InfrastructureKind,
    InfrastructureTicketReference,
    InfrastructureTicketSearch,
    allowed_kinds,
    catalogue_type,
    category_policy,
    preview,
    search,
)


def test_infrastructure_categories_have_exact_asset_kinds():
    assert allowed_kinds(" BTS Outage ") == (InfrastructureKind.basestation,)
    assert allowed_kinds("Cabinet Disconnection") == (InfrastructureKind.cabinet,)
    assert allowed_kinds("Multiple Cabinet Disconnection") == (
        InfrastructureKind.basestation,
        InfrastructureKind.node,
    )
    assert allowed_kinds("Core Link Disconnection") == (InfrastructureKind.manual,)
    assert allowed_kinds("Customer Link Disconnection") == ()
    assert category_policy()["bts outage"] == ("basestation",)


def test_core_link_uses_operator_text_without_inventory_choices(db_session):
    assert (
        search(
            db_session,
            query=InfrastructureTicketSearch("Core Link Disconnection", "core"),
        )
        == ()
    )


def test_incompatible_preview_does_not_leak_an_asset(db_session):
    assert (
        preview(
            db_session,
            query=InfrastructureTicketReference(
                "BTS Outage", InfrastructureKind.cabinet, uuid4()
            ),
        )
        is None
    )


def test_category_maps_to_canonical_catalogue_kinds():
    assert (
        catalogue_type(InfrastructureKind.basestation, "BTS Outage")
        is InfrastructureType.base_station
    )
    assert (
        catalogue_type(InfrastructureKind.cabinet, "Cabinet Disconnection")
        is InfrastructureType.cabinet
    )
    assert (
        catalogue_type(InfrastructureKind.node, "Access Point Outage")
        is InfrastructureType.access_point
    )
    assert (
        catalogue_type(InfrastructureKind.node, "Multiple Cabinet Disconnection")
        is None
    )


def test_multi_cabinet_uses_catalogue_network_node_query(monkeypatch):
    asset_id = uuid4()
    seen = []

    def fake_search(_db, *, query):
        seen.append(query)
        return InfrastructureOptions(
            results=(InfrastructureOption(id=asset_id, label="Distribution node"),)
        )

    monkeypatch.setattr(
        selection.infrastructure_catalogue, "search_network_nodes", fake_search
    )
    monkeypatch.setattr(
        selection.infrastructure_catalogue,
        "search",
        lambda *_args, **_kwargs: InfrastructureOptions(results=()),
    )
    results = search(
        object(),
        query=InfrastructureTicketSearch(
            "Multiple Cabinet Disconnection", "Distribution"
        ),
    )
    assert results == (
        selection.InfrastructureChoice(
            InfrastructureKind.node, asset_id, "Distribution node"
        ),
    )
    assert len(seen) == 1


def test_search_delegates_active_inventory_lookup(monkeypatch):
    asset_id = uuid4()
    seen = []

    def fake_search(_db, *, query):
        seen.append(query)
        return InfrastructureOptions(
            results=(InfrastructureOption(id=asset_id, label="FDH 12"),)
        )

    monkeypatch.setattr(selection.infrastructure_catalogue, "search", fake_search)
    results = search(
        object(), query=InfrastructureTicketSearch("Cabinet Disconnection", "FDH")
    )
    assert results == (
        selection.InfrastructureChoice(InfrastructureKind.cabinet, asset_id, "FDH 12"),
    )
    assert len(seen) == 1 and seen[0].type is InfrastructureType.cabinet


def test_preview_shows_count_without_customer_audience(monkeypatch):
    asset_id = uuid4()
    monkeypatch.setattr(
        selection.infrastructure_catalogue,
        "resolve",
        lambda _db, *, reference: InfrastructureOption(id=asset_id, label="FDH 12"),
    )
    monkeypatch.setattr(
        selection,
        "resolve_fdh_impact",
        lambda _db, _asset: SimpleNamespace(affected_count=7),
    )
    db = SimpleNamespace(get=lambda _model, _id: SimpleNamespace(id=asset_id))
    result = preview(
        db,
        query=InfrastructureTicketReference(
            "Cabinet Disconnection", InfrastructureKind.cabinet, asset_id
        ),
    )
    assert result is not None and result.affected_count == 7
    assert not hasattr(result, "customers")


def test_stale_reference_does_not_calculate_impact(monkeypatch):
    monkeypatch.setattr(
        selection.infrastructure_catalogue, "resolve", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        selection,
        "resolve_fdh_impact",
        lambda *_args, **_kwargs: pytest.fail("impact read for stale target"),
    )
    assert (
        preview(
            object(),
            query=InfrastructureTicketReference(
                "Cabinet Disconnection", InfrastructureKind.cabinet, uuid4()
            ),
        )
        is None
    )
