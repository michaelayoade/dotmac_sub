"""Parity tests for the canonical ``billing_day`` domain.

These travel with ``app/services/billing_day.py`` when it is extracted into the
shared ``dotmac-billing`` ``BillingDay`` contract — Starter ``AGENTS.md``
rule 24 requires a qualifying production implementation to be ported together
with its tests, not rebuilt beside one.

Every date here is INJECTED. The defect this module exists for was a clock:
the activation-day default wrote ``datetime.now(UTC).day``, so the browser
suite was green on the 28th and red from the 29th, on every branch, with no
code change in between. A domain rule that can only be exercised on the day
the calendar happens to supply gets its bugs found by the calendar. Note that
29 February is not hypothetical in this area either — Sub's cancellation-credit
path swallowed a ``ValueError`` on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.services.billing_day import (
    BillingDayDomain,
    BillingDayOutOfDomain,
    billing_day_domain,
    is_legacy_value,
    resolve_activation_day,
    validate_billing_day_change,
)

DOMAIN = BillingDayDomain(minimum=1, maximum=28)


# --- the domain is read from the spec, never restated ----------------------


def test_the_domain_comes_from_the_billing_setting_spec():
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    for key in ("prepaid_default_billing_day", "postpaid_default_billing_day"):
        spec = settings_spec.get_spec(SettingDomain.billing, key)
        assert spec is not None, f"{key} is no longer a registered setting"
        assert billing_day_domain(key).maximum == spec.max_value


def test_the_form_renders_the_domain_rather_than_a_literal():
    """Sensitivity proof for the test above.

    Pinning the writer to the spec is worthless if the template quietly goes
    back to a hardcoded bound: the two would drift again and nothing would
    fail. So assert the literal is gone and the bound is interpolated.
    """

    from pathlib import Path

    template = Path("templates/admin/customers/form.html").read_text(encoding="utf-8")
    assert 'max="28"' not in template, (
        "the customer form hardcodes a billing-day bound again; render "
        "billing_form.billing_day_max so writer and form share one declaration"
    )
    assert "billing_form.billing_day_max" in template


# --- activation day, on an injected clock ----------------------------------


@pytest.mark.parametrize(
    ("activation", "expected"),
    [
        (datetime(2026, 8, 1, tzinfo=UTC), 1),
        (datetime(2026, 8, 15, tzinfo=UTC), 15),
        (datetime(2026, 8, 28, tzinfo=UTC), 28),  # the boundary, must survive
        (datetime(2026, 8, 29, tzinfo=UTC), 28),  # the day the incident began
        (datetime(2026, 8, 30, tzinfo=UTC), 28),
        (datetime(2026, 8, 31, tzinfo=UTC), 28),  # month end
        (datetime(2026, 2, 28, tzinfo=UTC), 28),  # short month, last day
        (datetime(2028, 2, 29, tzinfo=UTC), 28),  # leap day
        (datetime(2026, 12, 31, tzinfo=UTC), 28),  # year end
    ],
)
def test_activation_day_resolves_inside_the_domain(activation, expected):
    assert resolve_activation_day(activation, DOMAIN) == expected


def test_an_in_domain_activation_day_is_never_altered():
    """The negative half of the control.

    An implementation that returned 28 unconditionally would satisfy every
    clamped case above and destroy the feature. Walk the whole in-domain range.
    """

    for day in range(1, 29):
        moment = datetime(2026, 1, day, tzinfo=UTC)
        assert resolve_activation_day(moment, DOMAIN) == day


def test_the_caller_owns_the_timezone_and_the_clock_shows_it():
    """The day depends on the instant the CALLER passes, not on a wall clock.

    Shortly after midnight in Lagos is still the previous day in UTC. Both
    land inside the domain here, so the assertion is about which timezone the
    caller supplied, not about the clamp — that is the property that makes the
    rule testable at any date.
    """

    lagos = timezone(timedelta(hours=1))
    local_morning = datetime(2026, 1, 16, 0, 30, tzinfo=lagos)

    assert resolve_activation_day(local_morning, DOMAIN) == 16
    assert resolve_activation_day(local_morning.astimezone(UTC), DOMAIN) == 15


def test_a_timezone_choice_at_a_month_boundary_changes_the_clamped_day():
    """The same instant yields 1 or 28 depending on the zone the caller passes.

    Just after midnight on 1 January in Lagos is still 31 December in UTC, and
    31 is out of domain — so the timezone decides between a billing day of 1
    and a clamped 28. That is the strongest form of the property: the zone is
    not cosmetic, it moves the stored value. Dotmac operates in Africa/Lagos
    (UTC+1), so this is the real boundary rather than a contrived one.

    The case above deliberately keeps both sides in domain, which proves the
    instant is respected but not that the choice matters. This one bites.
    """

    lagos = timezone(timedelta(hours=1))
    new_year_lagos = datetime(2026, 1, 1, 0, 30, tzinfo=lagos)

    assert new_year_lagos.astimezone(UTC).day == 31
    assert resolve_activation_day(new_year_lagos, DOMAIN) == 1
    assert resolve_activation_day(new_year_lagos.astimezone(UTC), DOMAIN) == 28


# --- change rules ----------------------------------------------------------


@pytest.mark.parametrize("proposed", [1, 14, 28, None])
def test_an_in_domain_change_is_accepted(proposed):
    assert (
        validate_billing_day_change(current=5, proposed=proposed, domain=DOMAIN)
        == proposed
    )


@pytest.mark.parametrize("proposed", [0, 29, 30, 31, 99, -1])
def test_creating_or_changing_to_an_out_of_domain_value_is_rejected(proposed):
    with pytest.raises(BillingDayOutOfDomain):
        validate_billing_day_change(current=None, proposed=proposed, domain=DOMAIN)
    with pytest.raises(BillingDayOutOfDomain):
        validate_billing_day_change(current=14, proposed=proposed, domain=DOMAIN)


@pytest.mark.parametrize("legacy", [29, 30, 31])
def test_resubmitting_a_legacy_value_unchanged_is_not_a_change(legacy):
    """The repair for the uneditable customer.

    An unrelated edit round-trips the billing day the record already has. If
    that counted as a change the save would fail, and the customer would still
    be uneditable — the incident would simply have moved from the browser to
    the server.
    """

    assert (
        validate_billing_day_change(current=legacy, proposed=legacy, domain=DOMAIN)
        == legacy
    )


@pytest.mark.parametrize("legacy", [29, 30, 31])
def test_a_legacy_value_may_be_moved_into_the_domain_but_not_sideways(legacy):
    assert validate_billing_day_change(current=legacy, proposed=15, domain=DOMAIN) == 15
    with pytest.raises(BillingDayOutOfDomain):
        validate_billing_day_change(
            current=legacy, proposed=30 if legacy != 30 else 31, domain=DOMAIN
        )


def test_a_legacy_value_is_never_silently_corrected():
    """Preserved, not repaired. Rewriting it changes when a customer is billed."""

    assert validate_billing_day_change(current=31, proposed=31, domain=DOMAIN) == 31


@pytest.mark.parametrize(
    ("stored", "legacy"),
    [(None, False), (1, False), (28, False), (29, True), (31, True), (0, True)],
)
def test_legacy_detection(stored, legacy):
    assert is_legacy_value(stored, DOMAIN) is legacy
