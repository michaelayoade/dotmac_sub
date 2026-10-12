"""Customer profile country/state/LGA catalog and persistence behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import customer_profile_location
from app.services.audit_adapter import AuditActor
from app.services.customer_portal_profile_commands import (
    CustomerPortalProfileCommandError,
    UpdateCustomerProfileCommand,
    update_customer_profile,
)
from tests.customer_profile_owner_support import (
    profile_command_context,
    profile_owner_session,
)


def test_country_catalog_contains_every_iso_alpha2_code() -> None:
    catalog = customer_profile_location.location_catalog()

    assert len(catalog.country_codes) == 249
    assert len(set(catalog.country_codes)) == 249
    assert {"NG", "GH", "GB", "US"}.issubset(catalog.country_codes)
    assert customer_profile_location.canonical_country_code(" ng ") == "NG"
    assert customer_profile_location.canonical_country_code("ZZ") == ""


def test_nigeria_catalog_contains_36_states_fct_and_dependent_lgas() -> None:
    catalog = customer_profile_location.location_catalog()
    states = {option.value: option.label for option in catalog.nigeria_states}

    assert len(states) == 37
    assert states["FEDERAL CAPITAL TERRITORY"] == (
        "Federal Capital Territory (FCT) - Abuja"
    )
    assert catalog.nigeria_lgas_by_state["FEDERAL CAPITAL TERRITORY"] == (
        "Abaji",
        "Bwari",
        "Gwagwalada",
        "Kuje",
        "Kwali",
        "Municipal Area Council",
    )


def test_profile_update_canonicalizes_fct_and_abuja_municipal_alias(
    db_session, subscriber, monkeypatch
) -> None:
    monkeypatch.setattr(
        "app.services.customer_location_requests.geocode_service_address",
        lambda *_args, **_kwargs: None,
    )

    with profile_owner_session(db_session) as owner_db:
        outcome = update_customer_profile(
            owner_db,
            command=UpdateCustomerProfileCommand(
                context=profile_command_context(actor_id=subscriber.id),
                actor=AuditActor.user(str(subscriber.id)),
                subscriber_id=subscriber.id,
                first_name=subscriber.first_name,
                last_name=subscriber.last_name,
                email=subscriber.email,
                billing_notifications=True,
                sms_updates=True,
                country_code="NG",
                region="Federal Capital Territory (FCT) - Abuja",
                lga="Abuja Municipal Area Council",
            ),
        )
    db_session.refresh(subscriber)

    assert outcome is not None
    assert subscriber.country_code == "NG"
    assert subscriber.region == "FEDERAL CAPITAL TERRITORY"
    assert subscriber.lga == "Municipal Area Council"


def test_profile_update_rejects_lga_from_another_state(db_session, subscriber) -> None:
    with pytest.raises(
        CustomerPortalProfileCommandError,
        match="is not a Local Government Area",
    ):
        with profile_owner_session(db_session) as owner_db:
            update_customer_profile(
                owner_db,
                command=UpdateCustomerProfileCommand(
                    context=profile_command_context(actor_id=subscriber.id),
                    actor=AuditActor.user(str(subscriber.id)),
                    subscriber_id=subscriber.id,
                    first_name=subscriber.first_name,
                    last_name=subscriber.last_name,
                    email=subscriber.email,
                    billing_notifications=True,
                    sms_updates=True,
                    country_code="NG",
                    region="Lagos",
                    lga="Abuja Municipal Area Council",
                ),
            )
        db_session.refresh(subscriber)


@pytest.mark.parametrize(
    ("country_code", "region", "lga", "message"),
    [
        ("ZZ", "", "", "Select a country"),
        ("GH", "Greater Accra", "Municipal Area Council", "only for Nigerian"),
        ("NG", "Atlantis", "", "valid Nigerian state"),
    ],
)
def test_profile_update_rejects_invalid_location_combinations(
    db_session,
    subscriber,
    country_code: str,
    region: str,
    lga: str,
    message: str,
) -> None:
    with pytest.raises(CustomerPortalProfileCommandError, match=message):
        with profile_owner_session(db_session) as owner_db:
            update_customer_profile(
                owner_db,
                command=UpdateCustomerProfileCommand(
                    context=profile_command_context(actor_id=subscriber.id),
                    actor=AuditActor.user(str(subscriber.id)),
                    subscriber_id=subscriber.id,
                    first_name=subscriber.first_name,
                    last_name=subscriber.last_name,
                    email=subscriber.email,
                    billing_notifications=True,
                    sms_updates=True,
                    country_code=country_code,
                    region=region,
                    lga=lga,
                ),
            )
        db_session.refresh(subscriber)


def test_profile_contact_address_uses_dependent_typeahead_controls() -> None:
    template = Path("templates/customer/profile/index.html").read_text(encoding="utf-8")
    script = Path("static/js/customer-profile-location.js").read_text(encoding="utf-8")

    assert 'id="profile-country-search"' in template
    assert 'name="country_code"' in template
    assert 'name="region"' in template
    assert 'name="lga"' in template
    assert 'list="profile-country-options"' in template
    assert 'list="profile-state-options"' in template
    assert 'list="profile-lga-options"' in template
    assert "Intl.DisplayNames" in script
    assert "nigeria_lgas_by_state" in script
    assert "lgaInput.disabled" in script
