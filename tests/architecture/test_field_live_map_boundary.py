"""Protect the typed, privacy-aware dispatch field live-map projection."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import get_type_hints

from fastapi.routing import APIRoute

from app.api.field import manager as field_manager_api
from app.schemas.field import (
    FieldLiveMapFeed,
    FieldLiveMapFeedQuery,
    FieldLiveMapSearchQuery,
    FieldLiveMapSearchResponse,
    FieldManagerTechniciansQuery,
    FieldManagerTechniciansResponse,
)
from app.services import field_maps
from app.services.auth_dependencies import permission_requirement
from app.services.field.manager import field_manager
from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def test_field_live_map_projection_has_a_complete_owner_contract() -> None:
    owner = service_relationship("ui.field_live_map_projection")

    assert owner.module == "app.services.field_maps"
    assert owner.contract is not None
    assert "operations.work_orders" in owner.depends_on
    assert "customer.accounts" in owner.depends_on


def test_field_live_map_public_reads_have_typed_outcomes() -> None:
    feed_hints = get_type_hints(field_maps.list_technician_positions)
    search_hints = get_type_hints(field_maps.search_live_map)

    assert feed_hints["query"] is FieldLiveMapFeedQuery
    assert feed_hints["return"] is FieldLiveMapFeed
    assert search_hints["search"] is FieldLiveMapSearchQuery
    assert search_hints["return"] is FieldLiveMapSearchResponse
    assert "search" in inspect.signature(field_maps.search_live_map).parameters

    roster_hints = get_type_hints(field_manager.list_technicians)
    assert roster_hints["query"] is FieldManagerTechniciansQuery
    assert roster_hints["return"] is FieldManagerTechniciansResponse
    assert {
        "last_latitude",
        "last_longitude",
        "accuracy_m",
        "last_location_at",
    }.isdisjoint(
        FieldManagerTechniciansResponse.model_json_schema()["$defs"][
            "FieldManagerTechnician"
        ]["properties"]
    )


def test_live_map_navigation_uses_the_route_permission() -> None:
    sidebar = (ROOT / "templates/components/navigation/admin_sidebar.html").read_text(
        encoding="utf-8"
    )

    permission_gate = '{% if can(request, "operations:dispatch:read") %}'
    link = 'nav_link("Field Live Map", "/admin/dispatch/live-map"'
    assert permission_gate in sidebar
    assert sidebar.index(permission_gate) < sidebar.index(link)


def test_manager_mobile_map_uses_the_typed_owner_and_dispatch_permission() -> None:
    route = next(
        route
        for route in field_manager_api.router.routes
        if isinstance(route, APIRoute)
        and route.path == "/manager/team-map"
        and "GET" in route.methods
    )
    requirements = [
        requirement
        for dependency in route.dependant.dependencies
        if (requirement := permission_requirement(dependency.call)) is not None
    ]

    assert get_type_hints(field_manager_api.field_manager_team_map)["return"] is (
        FieldLiveMapFeed
    )
    assert any(
        requirement.read_any_of == ("operations:dispatch:read",)
        for requirement in requirements
    )
