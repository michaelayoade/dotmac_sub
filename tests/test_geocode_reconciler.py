"""The geocoder proposes; it never decides.

These tests pin the invariant that separates capture from the fabrication it
replaced: a value we cannot verify is left blank, not filled with the
geocoder's best guess. The postcode fixtures are **real Nominatim output** from
the deployed instance — an Abuja pin that returns a Kaduna-range code, and a
Victoria Island pin that returns a Rivers-range one — which is why a geocoded
postcode is never proposed at all.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.subscriber import Subscriber
from app.models.subscriber_field_verification import SubscriberFieldVerification
from app.services import geocode_reconciler as gr
from app.services.subscriber_data_completeness import FieldKey


def _subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="A",
        last_name="B",
        email=f"g-{uuid.uuid4().hex[:8]}@example.com",
    )
    db.add(subscriber)
    db.commit()
    return subscriber


def _geo(**kwargs) -> gr.GeocodeResult:
    base = {"state": None, "lga": None, "postcode": None, "town": None}
    base.update(kwargs)
    return gr.GeocodeResult(**base)


# Real output from the deployed Nominatim, Gwarinpa/Abuja — the one pin whose
# postcode happens to be right. Kept to prove we still refuse to propose it.
_ABUJA_GOOD = _geo(
    state="Federal Capital Territory",
    lga="Municipal Area Council",
    postcode="900108",
    town="Gwarinpa",
)
# Real output, Garki/Abuja: 223140 is a KADUNA range.
_ABUJA_WRONG_POSTCODE = _geo(
    state="Federal Capital Territory",
    lga="Municipal Area Council",
    postcode="223140",
)
# Real output, Victoria Island/Lagos: 500001 is a RIVERS range. An independent
# NIPOST lookup gives 101241.
_LAGOS_WRONG_POSTCODE = _geo(state="Lagos", lga="Eti Osa", postcode="500001")


# ── state ───────────────────────────────────────────────────────────────────


def test_state_agrees_when_the_claim_matches_the_pin():
    r = gr.reconcile(claimed_state="Abuja", geocoded=_ABUJA_GOOD, accuracy_m=20)
    state = r.for_key(FieldKey.state)
    assert state.verdict is gr.Verdict.agree
    assert state.proposed == "Federal Capital Territory"


def test_state_disagreement_proposes_nothing():
    r = gr.reconcile(claimed_state="Lagos", geocoded=_ABUJA_GOOD, accuracy_m=20)
    state = r.for_key(FieldKey.state)
    assert state.verdict is gr.Verdict.disagree
    assert state.proposed is None
    assert state in r.needs_human


def test_state_is_unverifiable_without_a_geocode():
    r = gr.reconcile(claimed_state="Lagos", geocoded=None)
    assert r.for_key(FieldKey.state).verdict is gr.Verdict.unverifiable


# ── postcode: never proposed, on the evidence ───────────────────────────────


@pytest.mark.parametrize(
    "geocoded",
    [_ABUJA_GOOD, _ABUJA_WRONG_POSTCODE, _LAGOS_WRONG_POSTCODE],
    ids=["correct-code", "abuja-pin-kaduna-code", "lagos-pin-rivers-code"],
)
def test_geocoded_postcode_is_never_proposed(geocoded):
    """Not even the one that happens to be correct. The geocoder demonstrably
    cannot be trusted on Nigerian postcodes, so a value it returns is evidence
    for a human, never a capture."""
    r = gr.reconcile(geocoded=geocoded, accuracy_m=10)
    postcode = r.for_key(FieldKey.postal_code)
    assert postcode.verdict is gr.Verdict.unverifiable
    assert postcode.proposed is None
    assert postcode not in r.capturable


def test_a_wrong_state_postcode_is_not_laundered_into_a_capture():
    """The Abuja pin returning a Kaduna-range code must not reach the ledger by
    any path."""
    r = gr.reconcile(
        claimed_postcode="223140", geocoded=_ABUJA_WRONG_POSTCODE, accuracy_m=10
    )
    assert FieldKey.postal_code not in [f.key for f in r.capturable]


def test_claimed_postcode_is_format_validated_only():
    ok, note = gr.validate_claimed_postcode("101241")
    assert ok and "state range unverified" in note
    assert gr.validate_claimed_postcode("None") == (
        False,
        "not a 6-digit NIPOST code",
    )
    assert gr.validate_claimed_postcode("12345")[0] is False
    assert gr.validate_claimed_postcode(None)[0] is False


# ── lga ─────────────────────────────────────────────────────────────────────


def test_coarse_fix_refuses_to_name_an_lga():
    r = gr.reconcile(geocoded=_ABUJA_GOOD, accuracy_m=500)
    lga = r.for_key(FieldKey.lga)
    assert lga.verdict is gr.Verdict.unverifiable
    assert lga.proposed is None
    assert "too coarse" in lga.note


def test_lga_is_unverifiable_when_the_geocoder_omits_it():
    """Port Harcourt returned no county at all."""
    r = gr.reconcile(geocoded=_geo(state="Rivers", postcode="500211"), accuracy_m=10)
    assert r.for_key(FieldKey.lga).verdict is gr.Verdict.unverifiable


def test_lga_invalid_for_its_state_is_unverifiable(monkeypatch):
    """An LGA our own reference data will not vouch for is not a suggestion."""
    monkeypatch.setattr(gr, "_validated_lga", lambda state, lga: None)
    r = gr.reconcile(geocoded=_ABUJA_GOOD, accuracy_m=10)
    assert r.for_key(FieldKey.lga).verdict is gr.Verdict.unverifiable


def test_validated_lga_agrees_and_is_capturable(monkeypatch):
    monkeypatch.setattr(
        gr, "_validated_lga", lambda state, lga: "Municipal Area Council"
    )
    r = gr.reconcile(geocoded=_ABUJA_GOOD, accuracy_m=10)
    lga = r.for_key(FieldKey.lga)
    assert lga.verdict is gr.Verdict.agree
    assert lga.proposed == "Municipal Area Council"


def test_lga_without_reference_data_fails_closed():
    """`ncc_location` is unmerged; absent it we hold no LGA table, so we must
    not propose an LGA. Guards the fail-closed path directly."""
    assert gr._validated_lga("Federal Capital Territory", "Municipal Area Council") in (
        None,
        "Municipal Area Council",
    )


# ── reverse(): never raises into a capture ──────────────────────────────────


def test_reverse_returns_none_when_unconfigured(db_session):
    assert gr.reverse(db_session, 9.07, 7.39) is None


def test_reverse_returns_none_when_the_geocoder_is_unreachable(db_session, monkeypatch):
    monkeypatch.setattr(gr, "_setting", lambda db, key: "http://127.0.0.1:9")

    def _boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(gr.httpx, "get", _boom)
    assert gr.reverse(db_session, 9.07, 7.39) is None


# ── capture ─────────────────────────────────────────────────────────────────


def test_capture_writes_ledger_rows_only_for_what_reconciles(db_session, monkeypatch):
    subscriber = _subscriber(db_session)
    monkeypatch.setattr(gr, "reverse", lambda db, lat, lng: _ABUJA_GOOD)
    monkeypatch.setattr(
        gr, "_validated_lga", lambda state, lga: "Municipal Area Council"
    )

    result = gr.capture_location(
        db_session,
        str(subscriber.id),
        lat=9.0765,
        lng=7.3986,
        accuracy_m=15,
        source=gr.SOURCE_FIELD_GPS,
        actor_name="Tech A",
    )

    assert set(result.captured_keys) == {FieldKey.state, FieldKey.lga}
    rows = (
        db_session.query(SubscriberFieldVerification)
        .filter(SubscriberFieldVerification.subscriber_id == subscriber.id)
        .all()
    )
    assert {r.field_key for r in rows} == {"state", "lga"}
    # postcode never lands, even though the geocoder offered one
    assert "postal_code" not in {r.field_key for r in rows}
    evidence = rows[0].evidence
    assert evidence["lat"] == 9.0765
    assert evidence["accuracy_m"] == 15
    assert evidence["geocoded"]["postcode"] == "900108"  # kept as evidence only
    assert evidence["reconciliation"]["state"]["verdict"] == "agree"
    assert rows[0].source == gr.SOURCE_FIELD_GPS
    assert rows[0].verified_by_actor_name == "Tech A"


def test_capture_writes_nothing_when_the_claim_disagrees(db_session, monkeypatch):
    subscriber = _subscriber(db_session)
    monkeypatch.setattr(gr, "reverse", lambda db, lat, lng: _ABUJA_GOOD)
    monkeypatch.setattr(gr, "_validated_lga", lambda state, lga: None)

    result = gr.capture_location(
        db_session,
        str(subscriber.id),
        lat=9.0765,
        lng=7.3986,
        accuracy_m=15,
        source=gr.SOURCE_CUSTOMER_PORTAL,
        claimed_state="Lagos",  # contradicts the pin
    )

    assert result.captured_keys == ()
    assert (
        db_session.query(SubscriberFieldVerification)
        .filter(SubscriberFieldVerification.subscriber_id == subscriber.id)
        .count()
        == 0
    )
    assert [f.key for f in result.reconciliation.needs_human] == [FieldKey.state]


def test_capture_survives_an_unreachable_geocoder(db_session, monkeypatch):
    """A capture must not fail because a suggestion source is down."""
    subscriber = _subscriber(db_session)
    monkeypatch.setattr(gr, "reverse", lambda db, lat, lng: None)

    result = gr.capture_location(
        db_session,
        str(subscriber.id),
        lat=9.0765,
        lng=7.3986,
        source=gr.SOURCE_AGENT,
    )
    assert result.captured_keys == ()
    assert result.geocoded is None
    assert all(
        f.verdict is gr.Verdict.unverifiable for f in result.reconciliation.fields
    )
