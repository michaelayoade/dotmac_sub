"""The canonical ``billing_day`` domain.

A subscriber's ``billing_day`` is a day-of-month. Its valid domain is declared
ONCE, by the billing setting spec (``*_default_billing_day``, ``max_value=28``),
and every writer -- server form handlers, JSON APIs, importers, activation
defaults -- resolves it through this module rather than restating the bound.

Why this exists as a contract rather than a check at one call site: the writer
and the validator disagreeing about this field's domain is precisely what
produced the 2026-08-29 incident. ``_apply_billing_defaults`` wrote
``datetime.now(UTC).day`` -- up to 31 -- while the admin form rendered
``max="28"``. The stored value was legal to write and impossible to edit: the
control lives in a collapsed tab, so Chromium failed validation on an element
it could not focus and silently refused to submit the WHOLE form. A customer
activated on the 29th could not have their phone number changed, and nothing
reached the server to say so.

Three rules follow, and they are deliberately not the same rule:

* **New values must be in domain.** Creating or changing a billing day requires
  a valid choice.
* **An existing out-of-domain value is PRESERVED, not corrected.** Rewriting it
  would change when a real customer is billed. Legacy rows stay legacy.
* **A legacy value must never block an unrelated edit.** Resubmitting the value
  a record already has is not a change, and is allowed.

This module is the product-first source for the shared ``dotmac-billing``
``BillingDay`` contract. Starter ``AGENTS.md`` rule 24 (product-first
extraction; ADR-0006 amendment 2026-08-08, enforced by
``tests/architecture/test_product_first_extraction.py``) makes a qualifying
production implementation the mandatory reference and initial code source for
shared behaviour: port it and its parity tests, generalise only at typed
seams, never fork it and never stand up a second writer beside it.

Built to be ported, therefore: it takes an INJECTED CLOCK and imports none of
Sub's models, session or web layer, so the extraction is a move rather than a
rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.services.domain_errors import DomainError

__all__ = [
    "BillingDayDomain",
    "BillingDayOutOfDomain",
    "billing_day_domain",
    "is_legacy_value",
    "resolve_activation_day",
    "validate_billing_day_change",
]

#: Used only when the setting spec declares no upper bound. 28 is the last
#: day-of-month EVERY month has, which is what makes it the safe bound: a
#: billing day of 29-31 simply does not occur in February.
_FALLBACK_MAXIMUM = 28
_FALLBACK_MINIMUM = 1


class BillingDayOutOfDomain(DomainError, ValueError):
    """A caller tried to set a billing day outside the declared domain.

    Both bases are load-bearing, because the two surfaces that raise this read
    it differently. ``DomainError`` gives the JSON API a 422 through the shared
    handler in ``app/errors.py`` instead of a 500 from an unhandled flush
    error. ``ValueError`` is what ``web_customer_actions._safe_form_error``
    checks before showing a message verbatim, so the admin sees the actual
    range rule rather than "Something went wrong".
    """

    def __init__(self, message: str) -> None:
        super().__init__(code="billing_day_invalid", message=message)


@dataclass(frozen=True)
class BillingDayDomain:
    """The inclusive range a billing day may take."""

    minimum: int
    maximum: int

    def contains(self, day: int | None) -> bool:
        if day is None:
            return True  # null means "inherit"; absence is always in domain.
        return self.minimum <= day <= self.maximum

    def describe(self) -> str:
        return f"{self.minimum}-{self.maximum}"


def billing_day_domain(
    spec_key: str = "prepaid_default_billing_day",
) -> BillingDayDomain:
    """Read the domain from the billing setting spec.

    Derived rather than restated, so a writer cannot drift away from the bound
    the spec declares and the form renders. The prepaid spec's ``min_value`` is
    0 because 0 encodes "day of activation" -- a policy sentinel, not a
    storable day -- so the stored minimum is 1 regardless.
    """

    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    spec = settings_spec.get_spec(SettingDomain.billing, spec_key)
    maximum = getattr(spec, "max_value", None) if spec is not None else None
    return BillingDayDomain(
        minimum=_FALLBACK_MINIMUM,
        maximum=int(maximum) if maximum else _FALLBACK_MAXIMUM,
    )


def resolve_activation_day(now: datetime, domain: BillingDayDomain) -> int:
    """The billing day for a subscriber whose setting says "day of activation".

    ``now`` is injected, never read from the wall clock here. The incident this
    module exists for was a clock: the suite was green on the 28th and red from
    the 29th, on every branch, with no code change in between. A domain rule
    that cannot be tested at an arbitrary date gets its bugs found by the
    calendar instead of by CI.
    """

    return min(max(now.day, domain.minimum), domain.maximum)


def is_legacy_value(stored: int | None, domain: BillingDayDomain) -> bool:
    """True for a persisted value that predates the domain being enforced."""

    return stored is not None and not domain.contains(stored)


def validate_billing_day_change(
    *,
    current: int | None,
    proposed: int | None,
    domain: BillingDayDomain,
) -> int | None:
    """Authorize a write, returning the value to persist.

    Enforcement is on CHANGE, not on every write, and the distinction is the
    whole point. An unrelated edit -- a phone number, say -- resubmits the
    record's existing billing day untouched, and a legacy 31 must survive that
    round trip. Only a genuine change is required to choose a valid day.

    Raises ``BillingDayOutOfDomain`` when a caller creates or changes a value
    to something outside the domain.
    """

    if proposed == current:
        return current  # Not a change. Legacy values survive unrelated edits.
    if domain.contains(proposed):
        return proposed
    raise BillingDayOutOfDomain(
        f"Billing day must be between {domain.describe()}; got {proposed}. "
        "An existing value outside that range is kept as a legacy value, but a "
        "new value must be in range."
    )
