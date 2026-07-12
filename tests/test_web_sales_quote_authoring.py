"""Admin quote authoring — the staff-side write path.

Sub could render quotes but never author them: the whole write path was
missing, so a staff member could not create a quote at all, and there was no
form to drop an install pin from.

The pin contract lives in ``metadata_["install"]`` and is stamped by
``sales.selfserve`` for portal-originated quotes; downstream estimate/survey/
billing read it from there. A staff-authored quote must land in exactly the
same shape, so these tests assert the metadata contract, not just HTTP codes.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models.sales import Quote, QuoteStatus
from app.services import web_sales


@pytest.fixture
def feasible(monkeypatch):
    """Stub feasibility so the pin tests don't depend on coverage-area data."""
    with patch.object(
        web_sales,
        "compute_feasibility",
        return_value={"coverage": "green", "feasible": True, "distance_meters": 120},
    ) as stub:
        yield stub


def _create(db_session, subscriber, **overrides):
    fields: dict[str, str | None] = {
        "subscriber_id": str(subscriber.id),
        "lead_id": None,
        "status": "draft",
        "currency": "NGN",
        "tax_rate": None,
        "expires_at": None,
        "notes": None,
        "latitude": None,
        "longitude": None,
        "address": None,
        "region": None,
    }
    fields.update(overrides)
    return web_sales.create_quote_from_form(db_session, **fields)  # type: ignore[arg-type]


def test_create_quote_stamps_the_selfserve_install_pin_contract(
    db_session, subscriber, feasible
):
    quote_id = _create(
        db_session,
        subscriber,
        latitude="9.057000",
        longitude="7.495000",
        address="12 Aminu Kano Cres",
        region="Abuja",
    )

    quote = db_session.get(Quote, quote_id)
    install = quote.metadata_["install"]

    # Exactly the keys selfserve.py stamps -- downstream reads these by name.
    assert install == {
        "latitude": 9.057,
        "longitude": 7.495,
        "address": "12 Aminu Kano Cres",
        "region": "Abuja",
    }
    # Feasibility is computed from the pin, as it is for portal quotes.
    feasible.assert_called_once_with(db_session, 9.057, 7.495)
    assert quote.metadata_["feasibility"]["coverage"] == "green"


def test_create_quote_without_a_pin_computes_no_feasibility(
    db_session, subscriber, feasible
):
    quote_id = _create(db_session, subscriber)

    quote = db_session.get(Quote, quote_id)
    assert (quote.metadata_ or {}).get("install") is None
    feasible.assert_not_called()


def test_half_a_pin_is_rejected(db_session, subscriber, feasible):
    """A latitude with no longitude is meaningless -- fail loudly rather than
    persist a corrupt pin that downstream survey/billing would trust."""
    with pytest.raises(ValueError, match="latitude and longitude go together"):
        _create(db_session, subscriber, latitude="9.057000")

    feasible.assert_not_called()


def test_out_of_range_coordinate_is_rejected(db_session, subscriber):
    with pytest.raises(ValueError, match="Latitude must be between"):
        _create(db_session, subscriber, latitude="120.0", longitude="7.495")


def test_edit_preserves_unrelated_metadata(db_session, subscriber, feasible):
    """``metadata_`` carries the whole portal contract (source, project_type,
    deposit, pricing_mode...). An admin edit must merge into it, never clobber
    it -- otherwise editing a portal quote silently destroys its deposit terms.
    """
    quote_id = _create(db_session, subscriber, latitude="9.05", longitude="7.49")
    quote = db_session.get(Quote, quote_id)
    quote.metadata_ = {
        **quote.metadata_,
        "source": "portal_self_serve",
        "project_type": "fiber_install",
        "deposit_percent": 40,
        "pricing_mode": "provisional",
    }
    db_session.commit()

    web_sales.update_quote_from_form(
        db_session,
        quote_id=quote_id,
        subscriber_id=str(subscriber.id),
        lead_id=None,
        status="sent",
        currency="NGN",
        tax_rate="7.5",
        expires_at=None,
        notes="Revised after site walk",
        latitude="9.060000",
        longitude="7.500000",
        address="New address",
        region="Abuja",
    )

    refreshed = db_session.get(Quote, quote_id)
    meta = refreshed.metadata_

    # The pin moved...
    assert meta["install"]["latitude"] == 9.06
    assert meta["install"]["address"] == "New address"
    # ...and the rest of the portal contract survived.
    assert meta["source"] == "portal_self_serve"
    assert meta["project_type"] == "fiber_install"
    assert meta["deposit_percent"] == 40
    assert meta["pricing_mode"] == "provisional"
    assert refreshed.status == QuoteStatus.sent.value


def test_clearing_the_pin_drops_its_stale_feasibility(db_session, subscriber, feasible):
    """Feasibility is derived from the pin. If the pin goes, a stale
    feasibility verdict must not linger and get trusted downstream."""
    quote_id = _create(db_session, subscriber, latitude="9.05", longitude="7.49")
    assert db_session.get(Quote, quote_id).metadata_["feasibility"]

    web_sales.update_quote_from_form(
        db_session,
        quote_id=quote_id,
        subscriber_id=str(subscriber.id),
        lead_id=None,
        status="draft",
        currency="NGN",
        tax_rate=None,
        expires_at=None,
        notes=None,
        latitude=None,
        longitude=None,
        address=None,
        region=None,
    )

    meta = db_session.get(Quote, quote_id).metadata_ or {}
    assert "install" not in meta
    assert "feasibility" not in meta


def test_create_requires_a_subscriber(db_session):
    with pytest.raises(ValueError, match="subscriber is required"):
        web_sales.create_quote_from_form(
            db_session,
            subscriber_id=None,
            lead_id=None,
            status="draft",
            currency="NGN",
            tax_rate=None,
            expires_at=None,
            notes=None,
            latitude=None,
            longitude=None,
            address=None,
            region=None,
        )
