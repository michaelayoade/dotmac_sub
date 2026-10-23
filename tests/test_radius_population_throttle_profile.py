"""Throttle precedence in the radreply sweep, as the single rate owner derives it.

A throttle must reach the router (SP-2): the sweep may not rebuild radreply from
the package rate while a throttle is in force. The owner recognises a collections
throttle by the credential marker or the configured throttle profile, and a FUP
throttle by its ``fup_states`` row; any other value in the credential slot is
ignored and never shapes the rate.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.models.catalog import GuaranteedSpeedType
from app.services import access_rate_profile as owner
from app.services.radius_population import _rate_limit
from tests.test_access_rate_profile import (
    _offer,
    _profile,
    _service,
    _set_collections_profile,
)


def test_collections_throttle_wins_over_the_package_rate(db_session) -> None:
    ten = _profile(db_session, "Ten", rate="10M/10M")
    slow = _profile(db_session, "Slow", rate="1M/1M", down=1000, code="coll")
    _set_collections_profile(db_session, slow)
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer, slot=slow)

    decision = owner.resolve_effective_rate(db_session, sub, credential)

    assert decision.layer is owner.RateLayer.collections_throttle
    assert decision.rate_limit == "1M/1M"


def test_no_throttle_uses_the_package_profile(db_session) -> None:
    ten = _profile(db_session, "Ten", rate="10M/10M")
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer)

    decision = owner.resolve_effective_rate(db_session, sub, credential)

    assert (decision.layer, decision.rate_limit) == (owner.RateLayer.plan, "10M/10M")


def test_a_stale_credential_profile_is_ignored(db_session) -> None:
    """Not the configured throttle and no marker: it never shapes the rate."""
    ten = _profile(db_session, "Ten", rate="10M/10M")
    stale = _profile(db_session, "Stale", rate="7M/7M")
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer, slot=stale)

    assert owner.resolve_effective_rate(db_session, sub, credential).rate_limit == (
        "10M/10M"
    )


def test_offer_derived_rate_survives_when_no_profile_at_all() -> None:
    """With no profile the offer-derived speed is used (plan-only customers)."""
    offer = SimpleNamespace(
        speed_download_mbps=200,
        speed_upload_mbps=100,
        guaranteed_speed=GuaranteedSpeedType.none,
        guaranteed_speed_limit_at=None,
    )
    # rx/tx is NAS-perspective -- upload first, download second. A 200/100 offer
    # emits 100M/200M; reversed it would sell 100 down and 200 up.
    assert _rate_limit(offer=offer, profile=None) == "100M/200M"
