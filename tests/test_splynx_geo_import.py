"""Tests for the Splynx -> Sub geospatial backfill.

Covers the pure transforms (GPS parsing with axis-swap repair, site-name
normalisation) and the owner-side idempotent coordinate writes on
``gis.spatial_sync``.
"""

from __future__ import annotations

import pytest

from app.models.gis import GeoLocation, GeoLocationType
from app.models.network_monitoring import PopSite
from app.models.subscriber import Address, AddressType
from app.services.gis_sync import GeoSync
from app.services.splynx_geo_import import (
    clean_pop_name,
    detect_region,
    keys_match,
    normalize_site_name,
    parse_gps,
)


class TestParseGps:
    def test_clean_abuja_pair(self):
        point = parse_gps("9.081511583651492,7.471630153732377")
        assert point is not None
        assert round(point.latitude, 4) == 9.0815
        assert round(point.longitude, 4) == 7.4716
        assert point.swapped is False
        assert point.needs_review is False

    def test_clean_lagos_pair(self):
        point = parse_gps("6.601336624274478,3.351218364191995")
        assert point is not None
        assert round(point.latitude, 3) == 6.601
        assert round(point.longitude, 3) == 3.351
        assert point.swapped is False

    def test_axis_swapped_pair_is_repaired(self):
        # Stored as "lng,lat,alt"; Abuja truth is lat 9.05 / lng 7.48.
        point = parse_gps("7.487186221222131,9.050928562254908,0")
        assert point is not None
        assert round(point.latitude, 3) == 9.051
        assert round(point.longitude, 3) == 7.487
        assert point.swapped is True

    def test_trailing_altitude_is_dropped(self):
        point = parse_gps("9.05,7.48,0")
        assert point is not None
        assert (round(point.latitude, 2), round(point.longitude, 2)) == (9.05, 7.48)

    @pytest.mark.parametrize("raw", ["", None, "   ", "abc", "9.05", "9.05,abc"])
    def test_empty_or_garbage_returns_none(self, raw):
        assert parse_gps(raw) is None

    @pytest.mark.parametrize("raw", ["0,0", "51.5,0.12", "1.0,1.0", "20,20"])
    def test_out_of_nigeria_envelope_returns_none(self, raw):
        assert parse_gps(raw) is None

    def test_outside_tight_box_flags_review(self):
        point = parse_gps("10.5,7.0")
        assert point is not None
        assert point.swapped is False
        assert point.needs_review is True


class TestNormalizeSiteName:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Garki-Abj-Bts", "garki"),
            ("Gwarimpa-Abj- Bts", "gwarimpa"),
            ("SPDC", "spdc"),
            ("(BOI) Asokoro-Abj-Bts", "boi asokoro"),
            ("Ilupeju-Lag Bts", "ilupeju"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_normalisation(self, raw, expected):
        assert normalize_site_name(raw) == expected


class TestPopMatchingHelpers:
    @pytest.mark.parametrize(
        "site,pop,expected",
        [
            ("garki", "garki", True),
            ("boi asokoro", "asokoro", True),  # subset -> match
            ("asokoro", "boi asokoro", True),  # superset -> match
            ("garki", "gudu", False),
            ("", "garki", False),
            ("garki", "", False),
            ("lekki", "ikotun", False),
        ],
    )
    def test_keys_match(self, site, pop, expected):
        assert keys_match(site, pop) is expected

    @pytest.mark.parametrize(
        "title,region",
        [
            ("Airport-Abj-Bts", "Abuja"),
            ("Lekki-Lag Bts", "Lagos"),
            ("Festac--Lag-Bts", "Lagos"),
            ("SomewhereElse", None),
        ],
    )
    def test_detect_region(self, title, region):
        assert detect_region(title) == region

    @pytest.mark.parametrize(
        "title,name",
        [
            ("Airport-Abj-Bts", "Airport"),
            ("Mpape-abj Bts", "Mpape"),
            ("Festac--Lag-Bts", "Festac"),
        ],
    )
    def test_clean_pop_name(self, title, name):
        assert clean_pop_name(title) == name


class TestApplyPopCoordinates:
    def test_writes_projects_and_is_idempotent(self, db_session):
        pop = PopSite(name="Garki")
        db_session.add(pop)
        db_session.commit()

        first = GeoSync.apply_pop_coordinates(db_session, {pop.id: (9.04, 7.49)})
        assert (first.matched, first.written, first.unchanged) == (1, 1, 0)
        db_session.refresh(pop)
        assert (pop.latitude, pop.longitude) == (9.04, 7.49)
        assert pop.geom is not None

        # Projection into the map layer.
        geo = (
            db_session.query(GeoLocation)
            .filter(GeoLocation.pop_site_id == pop.id)
            .one()
        )
        assert geo.location_type == GeoLocationType.pop
        assert (geo.latitude, geo.longitude) == (9.04, 7.49)

        # Re-running with the same coordinates writes nothing.
        second = GeoSync.apply_pop_coordinates(db_session, {pop.id: (9.04, 7.49)})
        assert (second.matched, second.written, second.unchanged) == (1, 0, 1)

    def test_missing_id_counted(self, db_session):
        import uuid

        result = GeoSync.apply_pop_coordinates(db_session, {uuid.uuid4(): (9.0, 7.4)})
        assert (result.matched, result.written, result.missing) == (0, 0, 1)

    def test_empty_input_is_noop(self, db_session):
        result = GeoSync.apply_pop_coordinates(db_session, {})
        assert (result.matched, result.written, result.missing) == (0, 0, 0)


class TestApplyAddressCoordinates:
    def test_writes_projects_and_is_idempotent(self, db_session, subscriber):
        address = Address(
            subscriber_id=subscriber.id,
            address_line1="1 Test Close",
            address_type=AddressType.service,
            is_primary=True,
        )
        db_session.add(address)
        db_session.commit()

        first = GeoSync.apply_address_coordinates(
            db_session, {address.id: (6.51, 3.35)}
        )
        assert (first.matched, first.written) == (1, 1)
        db_session.refresh(address)
        assert (address.latitude, address.longitude) == (6.51, 3.35)
        assert address.geom is not None

        geo = (
            db_session.query(GeoLocation)
            .filter(GeoLocation.address_id == address.id)
            .one()
        )
        assert geo.location_type == GeoLocationType.address

        second = GeoSync.apply_address_coordinates(
            db_session, {address.id: (6.51, 3.35)}
        )
        assert (second.matched, second.written, second.unchanged) == (1, 0, 1)
