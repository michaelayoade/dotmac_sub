"""Customer portal minimum-age policy and UI contract tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.db import finish_read_transaction
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber
from app.models.subscription_engine import SettingValueType
from app.services import customer_portal_profile_commands as profile_commands
from app.services import settings_spec
from app.services.owner_commands import CommandContext
from app.timezone import APP_TIMEZONE
from app.web.customer import routes as customer_routes


def _command(
    subscriber: Subscriber, *, date_of_birth: date
) -> profile_commands.UpdateCustomerProfileCommand:
    return profile_commands.UpdateCustomerProfileCommand(
        context=CommandContext(
            command_id=uuid4(),
            correlation_id=uuid4(),
            actor=str(subscriber.id),
            scope=profile_commands.PORTAL_PROFILE_WRITE_SCOPE,
            reason="customer portal minimum-age test",
        ),
        subscriber_id=subscriber.id,
        first_name=subscriber.first_name,
        last_name=subscriber.last_name,
        email=subscriber.email,
        billing_notifications=True,
        sms_updates=True,
        date_of_birth=date_of_birth.isoformat(),
    )


def test_minimum_customer_age_setting_defaults_to_thirteen() -> None:
    spec = settings_spec.get_spec(
        SettingDomain.subscriber,
        profile_commands.CUSTOMER_MINIMUM_AGE_SETTING_KEY,
    )

    assert spec is not None
    assert spec.value_type is SettingValueType.integer
    assert spec.default == 13
    assert spec.min_value == 0
    assert spec.max_value == 120
    assert spec.label == "Minimum customer age"


def test_minimum_age_policy_projects_latest_allowed_dob(
    db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings_spec, "resolve_value", lambda *_args: 13)

    policy = profile_commands.resolve_customer_minimum_age_policy(
        db_session,
        as_of_date=date(2026, 10, 4),
    )

    assert policy.minimum_age_years == 13
    assert policy.as_of_date == date(2026, 10, 4)
    assert policy.latest_allowed_date_of_birth == date(2013, 10, 4)


def test_minimum_age_policy_handles_leap_day_cutoff(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings_spec, "resolve_value", lambda *_args: 13)

    policy = profile_commands.resolve_customer_minimum_age_policy(
        db_session,
        as_of_date=date(2028, 2, 29),
    )

    assert policy.latest_allowed_date_of_birth == date(2015, 2, 28)


def test_minimum_age_policy_uses_configured_value(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings_spec, "resolve_value", lambda *_args: 18)

    policy = profile_commands.resolve_customer_minimum_age_policy(
        db_session,
        as_of_date=date(2026, 10, 4),
    )

    assert policy.minimum_age_years == 18
    assert policy.latest_allowed_date_of_birth == date(2008, 10, 4)


@pytest.mark.parametrize("invalid_value", [True, "13", -1, 121])
def test_minimum_age_policy_fails_closed_on_invalid_setting(
    db_session, monkeypatch, invalid_value: object
) -> None:
    monkeypatch.setattr(settings_spec, "resolve_value", lambda *_args: invalid_value)

    with pytest.raises(
        profile_commands.CustomerPortalProfileCommandError,
        match="minimum customer age policy is invalid",
    ):
        profile_commands.resolve_customer_minimum_age_policy(db_session)


def test_profile_owner_rejects_underage_date_of_birth(db_session, subscriber) -> None:
    today = datetime.now(APP_TIMEZONE).date()
    policy = profile_commands.resolve_customer_minimum_age_policy(
        db_session,
        as_of_date=today,
    )
    finish_read_transaction(db_session)

    with pytest.raises(
        profile_commands.CustomerPortalProfileCommandError,
        match="at least 13 years old",
    ):
        profile_commands.update_customer_profile(
            db_session,
            command=_command(
                subscriber,
                date_of_birth=policy.latest_allowed_date_of_birth + timedelta(days=1),
            ),
        )


def test_profile_owner_accepts_exact_minimum_age(
    db_session, subscriber, monkeypatch
) -> None:
    monkeypatch.setattr(
        "app.services.customer_location_requests.geocode_service_address",
        lambda *_args, **_kwargs: None,
    )
    today = datetime.now(APP_TIMEZONE).date()
    policy = profile_commands.resolve_customer_minimum_age_policy(
        db_session,
        as_of_date=today,
    )
    finish_read_transaction(db_session)

    outcome = profile_commands.update_customer_profile(
        db_session,
        command=_command(
            subscriber,
            date_of_birth=policy.latest_allowed_date_of_birth,
        ),
    )

    assert outcome.subscriber_id == subscriber.id
    assert "date_of_birth" in outcome.changed_fields


def test_profile_owner_rejects_future_date_of_birth(db_session, subscriber) -> None:
    finish_read_transaction(db_session)

    with pytest.raises(
        profile_commands.CustomerPortalProfileCommandError,
        match="cannot be in the future",
    ):
        profile_commands.update_customer_profile(
            db_session,
            command=_command(subscriber, date_of_birth=date(2999, 1, 1)),
        )


def test_profile_page_renders_owner_supplied_age_policy(
    db_session, subscriber, monkeypatch
) -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/portal/profile",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
    )
    request.state.csrf_token = "test-csrf-token"
    monkeypatch.setattr(
        customer_routes.web_customer_auth_service,
        "list_active_mfa_methods",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        customer_routes.customer_portal,
        "list_customer_sessions_for_subscriber",
        lambda *_args, **_kwargs: [],
    )
    context = customer_routes._profile_context(
        request,
        db_session,
        {"id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
    )

    rendered = customer_routes.templates.env.get_template(
        "customer/profile/index.html"
    ).render(context)

    assert f'max="{context["latest_allowed_date_of_birth"]}"' in rendered
    assert "You must be at least 13 years old." in rendered
