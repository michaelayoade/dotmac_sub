"""Allocate OLT-local profile IDs without mutating the OLT.

The allocator is intentionally strict: bad inventory data should stop profile
creation before a command is generated, not be hidden by selecting a nearby ID.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

DEFAULT_PROFILE_ID_RANGE = (100, 999)

DBA_PROFILE_ID_RANGE = DEFAULT_PROFILE_ID_RANGE
TRAFFIC_TABLE_ID_RANGE = DEFAULT_PROFILE_ID_RANGE
LINE_PROFILE_ID_RANGE = DEFAULT_PROFILE_ID_RANGE
SERVICE_PROFILE_ID_RANGE = DEFAULT_PROFILE_ID_RANGE
WAN_PROFILE_ID_RANGE = DEFAULT_PROFILE_ID_RANGE
TR069_PROFILE_ID_RANGE = DEFAULT_PROFILE_ID_RANGE


class ProfileIdAllocationError(ValueError):
    """Base error for profile ID allocation failures."""


class ProfileIdExhausted(ProfileIdAllocationError):
    """Raised when no unused ID exists inside the reserved range."""


@dataclass(frozen=True)
class ProfileIdAllocation:
    """Selected ID plus enough context for a dry-run/admin preview."""

    profile_type: str
    allocated_id: int
    reserved_range: tuple[int, int]
    used_ids: tuple[int, ...]


def _validate_id_range(id_range: tuple[int, int]) -> tuple[int, int]:
    start, end = id_range
    if not isinstance(start, int) or not isinstance(end, int):
        raise ProfileIdAllocationError("Profile ID range bounds must be integers")
    if start < 0 or end < 0:
        raise ProfileIdAllocationError("Profile ID range cannot contain negative IDs")
    if start > end:
        raise ProfileIdAllocationError(
            f"Profile ID range start {start} is greater than end {end}"
        )
    return start, end


def _coerce_profile_id(value: Any, *, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileIdAllocationError(
            f"{source} contains non-integer profile ID {value!r}"
        )
    if value < 0:
        raise ProfileIdAllocationError(f"{source} contains negative profile ID {value}")
    return value


def _collect_ids(values: Iterable[Any], *, source: str) -> set[int]:
    return {_coerce_profile_id(value, source=source) for value in values}


def _ids_from_entries(entries: Iterable[Any], *, attr: str, source: str) -> set[int]:
    ids: set[int] = set()
    for entry in entries:
        if not hasattr(entry, attr):
            raise ProfileIdAllocationError(
                f"{source} entry {entry!r} does not expose {attr!r}"
            )
        ids.add(_coerce_profile_id(getattr(entry, attr), source=source))
    return ids


def allocate_profile_id(
    *,
    profile_type: str,
    live_ids: Iterable[int],
    imported_ids: Iterable[int] = (),
    reserved_ids: Iterable[int] = (),
    id_range: tuple[int, int] = DEFAULT_PROFILE_ID_RANGE,
) -> ProfileIdAllocation:
    """Return the first unused ID inside ``id_range``.

    ``live_ids`` should come from current OLT inventory. ``imported_ids`` and
    ``reserved_ids`` let admin workflows include database state or IDs already
    planned in the same dry-run batch.
    """
    start, end = _validate_id_range(id_range)
    used_ids = (
        _collect_ids(live_ids, source=f"{profile_type} live inventory")
        | _collect_ids(imported_ids, source=f"{profile_type} imported inventory")
        | _collect_ids(reserved_ids, source=f"{profile_type} reserved IDs")
    )

    for candidate in range(start, end + 1):
        if candidate not in used_ids:
            return ProfileIdAllocation(
                profile_type=profile_type,
                allocated_id=candidate,
                reserved_range=(start, end),
                used_ids=tuple(sorted(used_ids)),
            )

    raise ProfileIdExhausted(
        f"No free {profile_type} profile ID in reserved range {start}-{end}"
    )


def allocate_dba_profile_id(
    live_profiles: Iterable[Any],
    *,
    imported_ids: Iterable[int] = (),
    reserved_ids: Iterable[int] = (),
    id_range: tuple[int, int] = DBA_PROFILE_ID_RANGE,
) -> ProfileIdAllocation:
    return allocate_profile_id(
        profile_type="DBA",
        live_ids=_ids_from_entries(
            live_profiles, attr="profile_id", source="DBA live inventory"
        ),
        imported_ids=imported_ids,
        reserved_ids=reserved_ids,
        id_range=id_range,
    )


def allocate_traffic_table_id(
    live_tables: Iterable[Any],
    *,
    imported_ids: Iterable[int] = (),
    reserved_ids: Iterable[int] = (),
    id_range: tuple[int, int] = TRAFFIC_TABLE_ID_RANGE,
) -> ProfileIdAllocation:
    return allocate_profile_id(
        profile_type="traffic-table",
        live_ids=_ids_from_entries(
            live_tables, attr="index", source="traffic table live inventory"
        ),
        imported_ids=imported_ids,
        reserved_ids=reserved_ids,
        id_range=id_range,
    )


def allocate_line_profile_id(
    live_profiles: Iterable[Any],
    *,
    imported_ids: Iterable[int] = (),
    reserved_ids: Iterable[int] = (),
    id_range: tuple[int, int] = LINE_PROFILE_ID_RANGE,
) -> ProfileIdAllocation:
    return allocate_profile_id(
        profile_type="line",
        live_ids=_ids_from_entries(
            live_profiles, attr="profile_id", source="line profile live inventory"
        ),
        imported_ids=imported_ids,
        reserved_ids=reserved_ids,
        id_range=id_range,
    )


def allocate_service_profile_id(
    live_profiles: Iterable[Any],
    *,
    imported_ids: Iterable[int] = (),
    reserved_ids: Iterable[int] = (),
    id_range: tuple[int, int] = SERVICE_PROFILE_ID_RANGE,
) -> ProfileIdAllocation:
    return allocate_profile_id(
        profile_type="service",
        live_ids=_ids_from_entries(
            live_profiles, attr="profile_id", source="service profile live inventory"
        ),
        imported_ids=imported_ids,
        reserved_ids=reserved_ids,
        id_range=id_range,
    )


def allocate_wan_profile_id(
    live_profiles: Iterable[Any],
    *,
    imported_ids: Iterable[int] = (),
    reserved_ids: Iterable[int] = (),
    id_range: tuple[int, int] = WAN_PROFILE_ID_RANGE,
) -> ProfileIdAllocation:
    return allocate_profile_id(
        profile_type="WAN",
        live_ids=_ids_from_entries(
            live_profiles, attr="profile_id", source="WAN profile live inventory"
        ),
        imported_ids=imported_ids,
        reserved_ids=reserved_ids,
        id_range=id_range,
    )


def allocate_tr069_profile_id(
    live_profiles: Iterable[Any],
    *,
    imported_ids: Iterable[int] = (),
    reserved_ids: Iterable[int] = (),
    id_range: tuple[int, int] = TR069_PROFILE_ID_RANGE,
) -> ProfileIdAllocation:
    return allocate_profile_id(
        profile_type="TR-069",
        live_ids=_ids_from_entries(
            live_profiles, attr="profile_id", source="TR-069 profile live inventory"
        ),
        imported_ids=imported_ids,
        reserved_ids=reserved_ids,
        id_range=id_range,
    )
