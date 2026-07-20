"""NCC location reference data: validation and canonicalisation.

These tables validate a location that was *captured*; they never infer one.
The tests below pin that contract — an unrecognised value must come back empty
rather than defaulting to somewhere plausible, because the output of these
lookups reaches a regulatory filing.
"""

from __future__ import annotations

import pytest

from app.services import ncc_location

# ── states ──────────────────────────────────────────────────────────────────


def test_states_cover_36_states_fct_and_international():
    states = ncc_location.states()
    assert len(states) == 38
    assert "FEDERAL CAPITAL TERRITORY" in states
    assert "LAGOS" in states
    # INTERNATIONAL is an NCC bucket rather than a Nigerian state.
    assert "INTERNATIONAL" in states


@pytest.mark.parametrize(
    ("captured", "expected"),
    [
        ("LAGOS", "LAGOS"),
        ("lagos", "LAGOS"),
        ("  Lagos  ", "LAGOS"),
        ("Federal Capital Territory", "FEDERAL CAPITAL TERRITORY"),
        # Alias path, reused from ncc_subscriber_report's richer alias table.
        ("fct", "FEDERAL CAPITAL TERRITORY"),
        ("Abuja", "FEDERAL CAPITAL TERRITORY"),
        ("akwa-ibom", "AKWA IBOM"),
        ("Nassarawa", "NASARAWA"),
        ("Lagos State", "LAGOS"),
        ("INTERNATIONAL", "INTERNATIONAL"),
    ],
)
def test_canonical_state_accepts_captured_values(captured, expected):
    assert ncc_location.canonical_state(captured) == expected


@pytest.mark.parametrize(
    "captured", ["Atlantis", "", None, "n/a", "-", "unknown", "none"]
)
def test_canonical_state_rejects_rather_than_guesses(captured):
    assert ncc_location.canonical_state(captured) == ""


# ── LGAs ────────────────────────────────────────────────────────────────────


def test_lgas_for_state_returns_the_states_lgas():
    lagos = ncc_location.lgas_for_state("LAGOS")
    assert "Ikeja" in lagos
    assert "Alimosho" in lagos
    assert len(lagos) == 20  # Lagos has 20 LGAs


def test_lgas_for_state_resolves_through_aliases():
    assert ncc_location.lgas_for_state("fct") == ncc_location.lgas_for_state(
        "Federal Capital Territory"
    )


def test_lgas_for_state_is_empty_for_an_unknown_state():
    assert ncc_location.lgas_for_state("Atlantis") == ()


@pytest.mark.parametrize(
    ("state", "captured", "expected"),
    [
        ("LAGOS", "Ikeja", "Ikeja"),
        ("LAGOS", "ikeja", "Ikeja"),
        ("LAGOS", "  IKEJA ", "Ikeja"),
        ("lagos", "ikeja", "Ikeja"),
        (
            "FEDERAL CAPITAL TERRITORY",
            "municipal area council",
            "Municipal Area Council",
        ),
    ],
)
def test_canonical_lga_canonicalises_a_captured_lga(state, captured, expected):
    assert ncc_location.canonical_lga(state, captured) == expected
    assert ncc_location.is_valid_lga(state, captured) is True


@pytest.mark.parametrize(
    ("state", "captured"),
    [
        ("LAGOS", "Garki"),  # a real LGA, but of the FCT — wrong state
        ("LAGOS", "Nowhere"),
        ("Atlantis", "Ikeja"),  # unknown state
        ("LAGOS", ""),
        ("LAGOS", "n/a"),
    ],
)
def test_unknown_lga_is_rejected(state, captured):
    assert ncc_location.canonical_lga(state, captured) == ""
    assert ncc_location.is_valid_lga(state, captured) is False


# ── towns ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("captured", "expected"),
    [
        ("Maitama", "Maitama"),
        ("MAITAMA", "Maitama"),
        ("  maitama  ", "Maitama"),
        ("jikwoyi", "Jikoyi"),  # alias → accepted spelling
        ("nepa village", "NEPA Village"),
        ("gwarimpa village", "Gwarinpa Village"),
    ],
)
def test_canonical_town_canonicalises_accepted_towns(captured, expected):
    assert ncc_location.canonical_town(captured) == expected
    assert ncc_location.is_accepted_town(captured) is True


@pytest.mark.parametrize("captured", ["Narnia", "", None, "n/a", "unknown"])
def test_unaccepted_town_is_rejected_rather_than_guessed(captured):
    assert ncc_location.canonical_town(captured) == ""
    assert ncc_location.is_accepted_town(captured) is False


def test_accepted_towns_is_the_published_list():
    towns = ncc_location.accepted_towns()
    assert len(towns) == 117
    assert "Maitama" in towns


# ── FCT districts ───────────────────────────────────────────────────────────


def test_fct_location_for_town_resolves_area_council_and_district():
    assert ncc_location.fct_location_for_town("Apo") == (
        "Municipal Area Council",
        "Apo",
    )


def test_fct_location_for_town_resolves_a_district_name():
    council, district = ncc_location.fct_location_for_town("Garki")
    assert council == "Municipal Area Council"
    assert district == "Garki"


def test_fct_location_for_town_resolves_through_aliases():
    assert ncc_location.fct_location_for_town("garki 2") == (
        "Municipal Area Council",
        "Garki",
    )


@pytest.mark.parametrize("captured", ["Narnia", "", None, "n/a"])
def test_fct_location_for_unknown_town_is_none_not_a_default(captured):
    # CRM defaulted an unmatched address to Municipal Area Council / FCT,
    # inventing a location for the filing. Sub reports the gap instead.
    assert ncc_location.fct_location_for_town(captured) is None


def test_fct_district_rows_are_area_council_district_towns():
    rows = ncc_location.fct_district_rows()
    assert len(rows) == 24
    for council, district, towns in rows:
        assert isinstance(council, str) and council
        assert isinstance(district, str) and district
        assert isinstance(towns, tuple)


def test_every_fct_area_council_is_a_real_fct_lga():
    fct_lgas = set(ncc_location.lgas_for_state("FEDERAL CAPITAL TERRITORY"))
    for council, _district, _towns in ncc_location.fct_district_rows():
        assert council in fct_lgas
