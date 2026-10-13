"""Pure, transitional display of stored inline customer address facts."""

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

CONCERN = "legacy inline customer address display"


class LegacyAddressProvenance(StrEnum):
    SUBSCRIBER_INLINE = "subscriber_inline"


class LegacyCoordinateProvenance(StrEnum):
    STORED_LEGACY = "stored_legacy"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class LegacyAddressDisplayQuery:
    address_line1: str | None
    address_line2: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True, slots=True)
class LegacyAddressDisplaySnapshot:
    address_line1: str
    address_line2: str | None
    city: str | None
    region: str | None
    postal_code: str | None
    country_code: str | None
    latitude: float | None
    longitude: float | None
    coordinate_provenance: LegacyCoordinateProvenance
    address_provenance: LegacyAddressProvenance = (
        LegacyAddressProvenance.SUBSCRIBER_INLINE
    )
    id: None = None
    is_primary: bool = True
    created_at: None = None


def _text(value: str | None) -> str | None:
    trimmed = value.strip() if value is not None else ""
    return trimmed if trimmed and trimmed.lower() != "none" else None


def resolve_legacy_address_display(
    *, query: LegacyAddressDisplayQuery
) -> LegacyAddressDisplaySnapshot | None:
    """Read stored facts without promoting them to a canonical service location."""
    line1 = _text(query.address_line1)
    if line1 is None:
        return None
    latitude, longitude = query.latitude, query.longitude
    complete = (
        latitude is not None
        and longitude is not None
        and not isinstance(latitude, bool)
        and not isinstance(longitude, bool)
        and isfinite(latitude)
        and isfinite(longitude)
        and abs(latitude) <= 90
        and abs(longitude) <= 180
    )
    return LegacyAddressDisplaySnapshot(
        address_line1=line1,
        address_line2=_text(query.address_line2),
        city=_text(query.city),
        region=_text(query.region),
        postal_code=_text(query.postal_code),
        country_code=_text(query.country_code),
        latitude=latitude if complete else None,
        longitude=longitude if complete else None,
        coordinate_provenance=(
            LegacyCoordinateProvenance.STORED_LEGACY
            if complete
            else LegacyCoordinateProvenance.UNAVAILABLE
        ),
    )
