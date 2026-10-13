from dataclasses import FrozenInstanceError

import pytest

from app.services.customer_legacy_address_display import (
    LegacyAddressDisplayQuery,
    LegacyAddressProvenance,
    LegacyCoordinateProvenance,
    resolve_legacy_address_display,
)
from app.services.web_customer_details import _legacy_coordinate


@pytest.mark.parametrize("text", [None, "", "  ", " None "])
def test_missing_legacy_address_has_no_display(text: str | None) -> None:
    assert resolve_legacy_address_display(query=LegacyAddressDisplayQuery(text)) is None


def test_legacy_display_trims_text_preserves_zero_and_is_immutable() -> None:
    snapshot = resolve_legacy_address_display(
        query=LegacyAddressDisplayQuery(
            address_line1="  Legacy street  ",
            address_line2=" None ",
            city=" Lagos ",
            latitude=0.0,
            longitude=0.0,
        )
    )
    assert snapshot is not None
    assert snapshot.address_line1 == "Legacy street"
    assert snapshot.city == "Lagos"
    assert snapshot.address_line2 is None
    assert (snapshot.latitude, snapshot.longitude) == (0.0, 0.0)
    assert snapshot.address_provenance is LegacyAddressProvenance.SUBSCRIBER_INLINE
    assert snapshot.coordinate_provenance is LegacyCoordinateProvenance.STORED_LEGACY
    assert snapshot.id is None and snapshot.created_at is None
    with pytest.raises(FrozenInstanceError):
        snapshot.city = "Abuja"  # type: ignore[misc]


@pytest.mark.parametrize(
    "latitude,longitude",
    [
        (None, None),
        (6.5, None),
        (None, 3.3),
        (91.0, 3.3),
        (6.5, 181.0),
        (float("nan"), 3.3),
        (6.5, float("inf")),
    ],
)
def test_partial_or_invalid_coordinates_never_form_a_map(
    latitude: float | None, longitude: float | None
) -> None:
    snapshot = resolve_legacy_address_display(
        query=LegacyAddressDisplayQuery(
            "Street", latitude=latitude, longitude=longitude
        )
    )
    assert snapshot is not None
    assert snapshot.latitude is None and snapshot.longitude is None
    assert snapshot.coordinate_provenance is LegacyCoordinateProvenance.UNAVAILABLE


@pytest.mark.parametrize(
    "value",
    [True, False, "bad", "", [], {}, float("nan"), float("inf"), "91", 10**1000],
)
def test_persistence_adapter_rejects_malformed_coordinates(value: object) -> None:
    assert _legacy_coordinate(value, bound=90) is None


@pytest.mark.parametrize("value", [0, 0.0, "0", " 6.5 "])
def test_persistence_adapter_accepts_stored_numeric_coordinates(value: object) -> None:
    assert _legacy_coordinate(value, bound=90) == float(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "metadata", [None, "malformed", [], [1, 2], {"latitude": True, "longitude": 3.3}]
)
def test_presenter_rejects_unknown_metadata_shapes_without_mutation(
    metadata: object,
) -> None:
    from app.models.subscriber import Subscriber
    from app.services.web_customer_details import _build_person_fallback_address

    customer = Subscriber(address_line1="Legacy street")
    # Bypass MutableDict coercion to exercise the adapter against unknown raw
    # persistence shapes, including values normal ORM assignment would reject.
    customer.__dict__["metadata_"] = metadata
    snapshots = _build_person_fallback_address(customer=customer)
    assert len(snapshots) == 1
    assert snapshots[0].latitude is None and snapshots[0].longitude is None
    assert customer.metadata_ is metadata
