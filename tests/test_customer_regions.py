from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.customer_regions import (
    UNASSIGNED_REGION_FILTER,
    CustomerRegionError,
    _validate_region_input,
    customer_region_filter_clause,
    primary_geocoded_address,
    resolve_region,
)


def _region(
    *,
    name: str,
    latitude: float,
    longitude: float,
    radius_meters: float = 1_000,
    match_mode: str = "nearest",
    priority: int = 0,
    nas_device_id=None,
    pop_site_id=None,
):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        latitude=latitude,
        longitude=longitude,
        radius_meters=radius_meters,
        color="#0ea5e9",
        match_mode=match_mode,
        priority=priority,
        nas_device_id=nas_device_id,
        pop_site_id=pop_site_id,
    )


def test_region_assignment_prefers_nearest_center_by_default():
    nearer = _region(name="Gudu", latitude=9.0000, longitude=7.0000)
    farther = _region(name="Wuse", latitude=9.0050, longitude=7.0050)

    assignment = resolve_region([farther, nearer], latitude=9.0002, longitude=7.0002)

    assert assignment is not None
    assert assignment.name == "Gudu"


def test_region_assignment_uses_matching_nas_before_distance():
    nas_id = uuid4()
    nearer = _region(name="Gudu", latitude=9.0000, longitude=7.0000)
    matching = _region(
        name="NAS zone",
        latitude=9.0050,
        longitude=7.0050,
        match_mode="nas",
        nas_device_id=nas_id,
    )

    assignment = resolve_region(
        [nearer, matching],
        latitude=9.0002,
        longitude=7.0002,
        nas_device_ids=frozenset({nas_id}),
    )

    assert assignment is not None
    assert assignment.name == "NAS zone"


def test_manual_region_priority_breaks_an_overlap():
    low = _region(
        name="Low", latitude=9.0, longitude=7.0, match_mode="manual", priority=1
    )
    high = _region(
        name="High", latitude=9.0, longitude=7.0, match_mode="manual", priority=10
    )

    assignment = resolve_region([low, high], latitude=9.0, longitude=7.0)

    assert assignment is not None
    assert assignment.name == "High"


def test_primary_geocoded_address_is_deterministic():
    non_primary = SimpleNamespace(
        id=uuid4(), latitude=9.0, longitude=7.0, is_primary=False
    )
    primary = SimpleNamespace(id=uuid4(), latitude=9.1, longitude=7.1, is_primary=True)

    assert primary_geocoded_address([non_primary, primary]) is primary


@pytest.mark.parametrize(
    ("match_mode", "message"),
    [
        ("nas", "matching NAS"),
        ("pop_site", "matching POP/site"),
    ],
)
def test_infrastructure_overlap_modes_require_a_target(match_mode, message):
    with pytest.raises(CustomerRegionError, match=message):
        _validate_region_input(
            name="Zone",
            latitude=9.0,
            longitude=7.0,
            radius_meters=300,
            color="#0ea5e9",
            match_mode=match_mode,
        )


def test_unassigned_customer_filter_uses_the_canonical_assignment_relation():
    clause = customer_region_filter_clause(UNASSIGNED_REGION_FILTER)

    assert clause is not None
