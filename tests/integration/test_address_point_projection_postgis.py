"""Address coordinates and their PostGIS projection, on real PostGIS.

The two owners of an address coordinate — `customer.accounts` for the row's
identity and text, `gis.spatial_sync` for the point and its projection — are
exercised against the deployed geometry type rather than a Python string
comparison, because both defects this canary pins were invisible to a string
comparison:

1. **Axis order.** `POINT` is written longitude-first. `Address.geom` was
   built by passing latitude and longitude positionally in the wrong order, so
   the stored point disagreed with the very columns beside it. Any assertion on
   the EWKT text would have matched whatever the code produced; only
   `ST_X`/`ST_Y` on the stored geometry can catch it.
2. **A projection with no geometry.** The full sweep set
   `GeoLocation.latitude`/`.longitude` and left `geom` NULL, while a second
   writer set all three. Spatial queries read `geom`.

Abuja coordinates are deliberate: 9.06 and 7.49 are *both* a valid latitude and
a valid longitude, so a swap stays inside every range check and fails only
semantically. A test at 51.5, -0.12 would catch the swap by accident, through
an out-of-range latitude, and would keep passing if the ranges were the only
thing holding the axes in place.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.models.gis import GeoLocation, GeoLocationType
from app.models.party import Party
from app.models.subscriber import Address, AddressType, Subscriber
from app.schemas.subscriber import AddressCreate
from app.services.gis import POINT_SRID, point_wkt
from app.services.gis_sync import project_address_point
from app.services.subscriber import _default_reseller_id, create_address

# Abuja: latitude 9.06 N, longitude 7.49 E. Distinct values, both in range for
# either axis, so an axis swap is numerically legal and semantically wrong.
ABUJA_LAT = 9.0643
ABUJA_LON = 7.4892
# A second, equally interchangeable pair, for the replay and update cases.
ABUJA_LAT_2 = 9.0721
ABUJA_LON_2 = 7.4517


def _subscriber(db) -> Subscriber:
    suffix = uuid.uuid4().hex[:8]
    party = Party(
        display_name=f"Address canary {suffix}",
        party_type="person",
        status="active",
    )
    db.add(party)
    db.flush()
    subscriber = Subscriber(
        first_name="Address",
        last_name="Canary",
        email=f"addr-{suffix}@example.com",
        reseller_id=_default_reseller_id(db),
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Address projection canary Party binding",
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def _address(db, subscriber: Subscriber) -> Address:
    return create_address(
        db,
        AddressCreate(
            subscriber_id=subscriber.id,
            address_type=AddressType.service,
            label="Primary service",
            address_line1="Plot 1234 Ahmadu Bello Way",
            city="Abuja",
            region="FCT",
            country_code="NG",
            is_primary=True,
        ),
        geocode=False,
    )


def _point(db, table, row_id) -> tuple[float, float, int]:
    """Read x, y and SRID back out of PostGIS, not out of Python."""

    return db.execute(
        select(
            func.ST_X(table.geom),
            func.ST_Y(table.geom),
            func.ST_SRID(table.geom),
        ).where(table.id == row_id)
    ).one()


def _projection(db, address: Address) -> GeoLocation:
    return db.query(GeoLocation).filter(GeoLocation.address_id == address.id).one()


# --------------------------------------------------------------------------
# axis order, on the stored geometry
# --------------------------------------------------------------------------


def test_the_stored_point_puts_longitude_on_x_and_latitude_on_y(db_session):
    subscriber = _subscriber(db_session)
    address = _address(db_session, subscriber)

    project_address_point(db_session, address, latitude=ABUJA_LAT, longitude=ABUJA_LON)
    db_session.flush()

    x, y, srid = _point(db_session, Address, address.id)
    assert x == pytest.approx(ABUJA_LON), (
        "ST_X is the longitude. Reading the latitude here means the point was "
        "built with the axes swapped, which is exactly what "
        "`_point_wkt(address.longitude, address.latitude)` did."
    )
    assert y == pytest.approx(ABUJA_LAT)
    assert srid == POINT_SRID

    # And the geometry agrees with the plain columns beside it. Their
    # disagreement was the whole defect: neither was NULL, neither was out of
    # range, and nothing compared them.
    assert address.latitude == pytest.approx(ABUJA_LAT)
    assert address.longitude == pytest.approx(ABUJA_LON)
    assert y == pytest.approx(address.latitude)
    assert x == pytest.approx(address.longitude)


def test_the_projection_carries_the_same_point_as_the_address(db_session):
    subscriber = _subscriber(db_session)
    address = _address(db_session, subscriber)

    project_address_point(db_session, address, latitude=ABUJA_LAT, longitude=ABUJA_LON)
    db_session.flush()

    projection = _projection(db_session, address)
    px, py, psrid = _point(db_session, GeoLocation, projection.id)
    ax, ay, asrid = _point(db_session, Address, address.id)

    assert (px, py, psrid) == (ax, ay, asrid)
    assert projection.latitude == pytest.approx(ABUJA_LAT)
    assert projection.longitude == pytest.approx(ABUJA_LON)
    assert projection.location_type is GeoLocationType.address
    assert projection.is_active is True


def test_the_converter_refuses_positional_arguments():
    """The keyword-only signature is the fix, not a style choice.

    Two positional floats of the same type in either order is a call the type
    checker cannot distinguish. Making it a `TypeError` is what stops the
    defect recurring.
    """

    with pytest.raises(TypeError):
        point_wkt(ABUJA_LAT, ABUJA_LON)  # type: ignore[misc]

    assert point_wkt(latitude=ABUJA_LAT, longitude=ABUJA_LON) == (
        f"SRID={POINT_SRID};POINT({ABUJA_LON} {ABUJA_LAT})"
    )


# --------------------------------------------------------------------------
# an existing projection and a newly created one must behave identically
# --------------------------------------------------------------------------


def test_an_existing_projection_is_updated_not_duplicated(db_session):
    """The pre-existing-row branch and the create branch must agree.

    They were separate implementations in separate modules, and they had
    drifted: one wrote `geom`, the other did not. This drives both branches
    through the same operation and compares the outcomes field by field.
    """

    subscriber = _subscriber(db_session)

    # A projection created by this operation.
    fresh = _address(db_session, subscriber)
    project_address_point(db_session, fresh, latitude=ABUJA_LAT, longitude=ABUJA_LON)
    db_session.flush()

    # A projection that already existed — with a stale point and, like the
    # sweep's own output, no geometry at all.
    stale = _address(db_session, subscriber)
    db_session.add(
        GeoLocation(
            name="Stale projection",
            location_type=GeoLocationType.address,
            latitude=ABUJA_LAT_2,
            longitude=ABUJA_LON_2,
            geom=None,
            address_id=stale.id,
            is_active=False,
        )
    )
    db_session.flush()

    created = project_address_point(
        db_session, stale, latitude=ABUJA_LAT, longitude=ABUJA_LON
    )
    db_session.flush()

    assert created is False, "an address that already has a projection is updated"
    assert (
        db_session.query(GeoLocation).filter(GeoLocation.address_id == stale.id).count()
        == 1
    ), "updating a projection must not leave a second one beside it"

    fresh_projection = _projection(db_session, fresh)
    stale_projection = _projection(db_session, stale)

    assert _point(db_session, GeoLocation, fresh_projection.id) == _point(
        db_session, GeoLocation, stale_projection.id
    )
    assert stale_projection.geom is not None, (
        "the pre-existing branch left `geom` NULL, which is the defect the "
        "full sweep wrote into every address it touched"
    )
    assert stale_projection.is_active is True
    assert stale_projection.location_type is fresh_projection.location_type


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


def test_replaying_the_same_point_changes_nothing(db_session):
    subscriber = _subscriber(db_session)
    address = _address(db_session, subscriber)

    assert (
        project_address_point(
            db_session, address, latitude=ABUJA_LAT, longitude=ABUJA_LON
        )
        is True
    )
    db_session.flush()
    projection_id = _projection(db_session, address).id
    before = (
        _point(db_session, Address, address.id),
        _point(db_session, GeoLocation, projection_id),
    )

    for _ in range(3):
        assert (
            project_address_point(
                db_session, address, latitude=ABUJA_LAT, longitude=ABUJA_LON
            )
            is False
        )
        db_session.flush()

    assert (
        db_session.query(GeoLocation)
        .filter(GeoLocation.address_id == address.id)
        .count()
        == 1
    )
    assert _projection(db_session, address).id == projection_id
    assert before == (
        _point(db_session, Address, address.id),
        _point(db_session, GeoLocation, projection_id),
    )


def test_replaying_a_different_point_moves_both_rows(db_session):
    """Idempotent is not inert — a new pin must actually land."""

    subscriber = _subscriber(db_session)
    address = _address(db_session, subscriber)
    project_address_point(db_session, address, latitude=ABUJA_LAT, longitude=ABUJA_LON)
    db_session.flush()
    projection_id = _projection(db_session, address).id

    project_address_point(
        db_session, address, latitude=ABUJA_LAT_2, longitude=ABUJA_LON_2
    )
    db_session.flush()

    x, y, _ = _point(db_session, Address, address.id)
    assert (x, y) == (pytest.approx(ABUJA_LON_2), pytest.approx(ABUJA_LAT_2))
    px, py, _ = _point(db_session, GeoLocation, projection_id)
    assert (px, py) == (pytest.approx(ABUJA_LON_2), pytest.approx(ABUJA_LAT_2))


# --------------------------------------------------------------------------
# neither owner operation owns the transaction
# --------------------------------------------------------------------------


def test_neither_owner_operation_commits_or_rolls_back(db_session, monkeypatch):
    """Flush-only is what makes these callable from inside a transaction.

    The committing methods (`Addresses.create`, `GeoSync.sync_addresses`) are
    exactly why three modules wrote addresses themselves instead of asking the
    owner. A regression here would not fail loudly — it would quietly commit a
    caller's partial work — so the seam is asserted rather than assumed.
    """

    subscriber = _subscriber(db_session)

    calls: list[str] = []
    monkeypatch.setattr(type(db_session), "commit", lambda self: calls.append("commit"))
    monkeypatch.setattr(
        type(db_session), "rollback", lambda self: calls.append("rollback")
    )

    address = _address(db_session, subscriber)
    project_address_point(db_session, address, latitude=ABUJA_LAT, longitude=ABUJA_LON)

    assert calls == [], (
        "a composable owner operation must leave the transaction to its "
        f"caller; it called {calls}"
    )


def test_the_owner_raises_a_domain_error_not_an_http_error(db_session):
    """A domain service raises a domain error; only the adapter maps it."""

    from app.services.subscriber import AddressOwnerError

    with pytest.raises(AddressOwnerError) as caught:
        create_address(
            db_session,
            AddressCreate(
                subscriber_id=uuid.uuid4(),
                address_line1="Nowhere",
            ),
            geocode=False,
        )
    assert caught.value.code == "customer.accounts.subscriber_not_found"
