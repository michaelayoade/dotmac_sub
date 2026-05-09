from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services.network.profile_id_allocator import (
    ProfileIdAllocationError,
    ProfileIdExhausted,
    allocate_dba_profile_id,
    allocate_line_profile_id,
    allocate_profile_id,
    allocate_service_profile_id,
    allocate_tr069_profile_id,
    allocate_traffic_table_id,
    allocate_wan_profile_id,
)


@dataclass(frozen=True)
class ProfileEntry:
    profile_id: int


@dataclass(frozen=True)
class TrafficTableEntry:
    index: int


def test_allocate_profile_id_chooses_first_free_id() -> None:
    allocation = allocate_profile_id(
        profile_type="line",
        live_ids=[100, 101],
        id_range=(100, 103),
    )

    assert allocation.allocated_id == 102
    assert allocation.profile_type == "line"
    assert allocation.reserved_range == (100, 103)
    assert allocation.used_ids == (100, 101)


def test_allocate_profile_id_skips_imported_and_reserved_ids() -> None:
    allocation = allocate_profile_id(
        profile_type="service",
        live_ids=[100],
        imported_ids=[101],
        reserved_ids=[102],
        id_range=(100, 104),
    )

    assert allocation.allocated_id == 103
    assert allocation.used_ids == (100, 101, 102)


def test_allocate_profile_id_fails_when_range_is_exhausted() -> None:
    with pytest.raises(ProfileIdExhausted, match="No free DBA profile ID"):
        allocate_profile_id(
            profile_type="DBA",
            live_ids=[100, 101],
            id_range=(100, 101),
        )


def test_allocate_profile_id_rejects_invalid_range() -> None:
    with pytest.raises(ProfileIdAllocationError, match="start 200 is greater"):
        allocate_profile_id(
            profile_type="line",
            live_ids=[],
            id_range=(200, 100),
        )


@pytest.mark.parametrize(
    "bad_id",
    [True, "100", 1.5, None],
)
def test_allocate_profile_id_rejects_non_integer_ids(bad_id: object) -> None:
    with pytest.raises(ProfileIdAllocationError, match="non-integer"):
        allocate_profile_id(
            profile_type="line",
            live_ids=[bad_id],  # type: ignore[list-item]
            id_range=(100, 101),
        )


def test_allocate_profile_id_rejects_negative_ids() -> None:
    with pytest.raises(ProfileIdAllocationError, match="negative"):
        allocate_profile_id(
            profile_type="line",
            live_ids=[-1],
            id_range=(100, 101),
        )


def test_allocate_dba_profile_id_reads_profile_id() -> None:
    allocation = allocate_dba_profile_id(
        [ProfileEntry(profile_id=100)],
        id_range=(100, 101),
    )

    assert allocation.allocated_id == 101
    assert allocation.profile_type == "DBA"


def test_allocate_traffic_table_id_reads_index() -> None:
    allocation = allocate_traffic_table_id(
        [TrafficTableEntry(index=100)],
        id_range=(100, 101),
    )

    assert allocation.allocated_id == 101
    assert allocation.profile_type == "traffic-table"


def test_allocate_wrappers_cover_profile_id_based_inventory() -> None:
    live = [ProfileEntry(profile_id=100)]

    assert allocate_line_profile_id(live, id_range=(100, 101)).allocated_id == 101
    assert allocate_service_profile_id(live, id_range=(100, 101)).allocated_id == 101
    assert allocate_wan_profile_id(live, id_range=(100, 101)).allocated_id == 101
    assert allocate_tr069_profile_id(live, id_range=(100, 101)).allocated_id == 101


def test_profile_wrapper_fails_when_entry_has_no_expected_id_field() -> None:
    with pytest.raises(ProfileIdAllocationError, match="does not expose 'profile_id'"):
        allocate_line_profile_id(
            [TrafficTableEntry(index=100)],
            id_range=(100, 101),
        )


def test_traffic_wrapper_fails_when_entry_has_no_index_field() -> None:
    with pytest.raises(ProfileIdAllocationError, match="does not expose 'index'"):
        allocate_traffic_table_id(
            [ProfileEntry(profile_id=100)],
            id_range=(100, 101),
        )
