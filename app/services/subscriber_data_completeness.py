"""Subscriber data completeness — what a subscriber must have, and what is missing.

The owner of the question "is this subscriber's data good enough for X?", where
X is a declared *purpose* (filing the NCC return, satisfying KYC). One
declarative policy maps purpose → required fields, each with a human label and
the reason it is required, so a caller never re-decides what "complete" means.

**This module is read-only.** It derives and reports; it never writes. Capture
flows through the subscriber owner, and suggestions are never auto-applied —
that distinction is the point. Reporting a subscriber we cannot locate as
though we know where they are is exactly the fabrication removed from
``ncc_subscriber_report`` (unresolved state used to be filed as "Abuja"); a
suggestion that silently became a stored fact would reintroduce it one layer
up.

Completeness is *derived*, never stored: ``missing_for`` asks the same
resolver the consuming report asks, so a subscriber can never be "complete"
here and Unknown there.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session, joinedload

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import NINVerificationStatus, Subscriber
from app.services.ncc_subscriber_report import _UNKNOWN, infer_state


class Purpose(StrEnum):
    """A declared reason a subscriber's data must be complete."""

    ncc_filing = "ncc_filing"
    kyc = "kyc"


class FieldKey(StrEnum):
    """Fields the policy can require. Values are stable identifiers — a UI,
    a queue row, and a readiness breakdown all key on these."""

    state = "state"
    email = "email"
    phone = "phone"
    identity = "identity"


@dataclass(frozen=True)
class FieldRequirement:
    """One required field, and why. ``is_present`` is the single place that
    decides presence — callers never re-implement it."""

    key: FieldKey
    label: str
    why: str
    is_present: Callable[[Subscriber], bool]


@dataclass(frozen=True)
class MissingField:
    key: FieldKey
    label: str
    why: str


@dataclass(frozen=True)
class Suggestion:
    """A non-binding proposal for a missing field.

    Never applied automatically. ``source`` names the evidence so a human can
    judge it, and ``confidence`` is advisory only — a low-confidence
    suggestion and a high-confidence one are both *unconfirmed*.
    """

    key: FieldKey
    value: str
    source: str
    confidence: str
    note: str


def _has_resolvable_state(subscriber: Subscriber) -> bool:
    return infer_state(subscriber) != _UNKNOWN


def _has_text(subscriber: Subscriber, attribute: str) -> bool:
    value = getattr(subscriber, attribute, None)
    return bool(value) and str(value).strip() != ""


def _has_verified_identity(subscriber: Subscriber) -> bool:
    """A NIN verification that actually succeeded.

    Presence of a verification row is not verification: production carries 34
    rows, every one failed (the Mono lookup is not enabled for the account).
    """
    for verification in getattr(subscriber, "nin_verifications", None) or []:
        if getattr(verification, "status", None) == NINVerificationStatus.success:
            return True
    return False


# ── the policy ──────────────────────────────────────────────────────────────
# Purpose → the fields that purpose requires. The only place "complete" is
# defined; add a purpose here, not a new `if` in a caller.
#
# ``kyc`` is deliberately PRESENCE-ONLY and narrower than
# docs/designs/CUSTOMER_KYC.md, which specifies per-channel verification
# statuses rolled up into levels L0–L3 via a `subscriber_kyc` table. That table
# does not exist yet and the address/email/phone verification channels are
# unbuilt, so a verification-grade answer is not available. Identity is the one
# channel with a real status column, so it is the one checked as verified;
# email and phone are checked for presence. Widen this when the KYC rollup
# lands — do not let callers approximate it in the meantime.
REQUIREMENTS: dict[Purpose, tuple[FieldRequirement, ...]] = {
    Purpose.ncc_filing: (
        FieldRequirement(
            key=FieldKey.state,
            label="Service state",
            why=(
                "The NCC Subscriber & Capacity return reports active "
                "subscriptions by State and geopolitical zone. A subscriber "
                "whose state cannot be resolved cannot be filed honestly."
            ),
            is_present=_has_resolvable_state,
        ),
    ),
    Purpose.kyc: (
        FieldRequirement(
            key=FieldKey.email,
            label="Email address",
            why="KYC contact channel (docs/designs/CUSTOMER_KYC.md §3).",
            is_present=lambda s: _has_text(s, "email"),
        ),
        FieldRequirement(
            key=FieldKey.phone,
            label="Phone number",
            why="KYC contact channel (docs/designs/CUSTOMER_KYC.md §3).",
            is_present=lambda s: _has_text(s, "phone"),
        ),
        FieldRequirement(
            key=FieldKey.identity,
            label="Verified identity (NIN)",
            why=(
                "KYC L3 requires a successful identity verification "
                "(docs/designs/CUSTOMER_KYC.md §3)."
            ),
            is_present=_has_verified_identity,
        ),
    ),
}


def requirements_for(purpose: Purpose) -> tuple[FieldRequirement, ...]:
    return REQUIREMENTS[purpose]


def missing_for(
    subscriber: Subscriber | None, purpose: Purpose
) -> tuple[MissingField, ...]:
    """Which required fields this subscriber lacks for ``purpose``. Pure."""
    if subscriber is None:
        return tuple(
            MissingField(key=r.key, label=r.label, why=r.why)
            for r in requirements_for(purpose)
        )
    return tuple(
        MissingField(key=r.key, label=r.label, why=r.why)
        for r in requirements_for(purpose)
        if not r.is_present(subscriber)
    )


def is_complete(subscriber: Subscriber | None, purpose: Purpose) -> bool:
    return not missing_for(subscriber, purpose)


# ── suggestions ─────────────────────────────────────────────────────────────
# A suggester proposes a value for a missing field from evidence we already
# hold. It must use a signal the presence check does NOT already exhaust,
# otherwise it is dead code by construction: `missing_for` only ever reports a
# field the resolver failed on, so a suggester reusing that resolver can never
# return anything.
#
# `state` has NO suggester today, and that is a finding rather than an
# omission. `infer_state` already scans region, billing_region, city,
# billing_city, every address row's region/city/lines, all four subscriber
# address lines, and metadata location values — through the full
# `_PLACE_STATE_ALIASES` gazetteer. Every cheap text signal is spent. The
# remaining options are unsafe or unbuilt:
#
#   * Substring matching without word boundaries fabricates — "idu" (FCT)
#     matches inside "Maiduguri" (Borno). Rejected.
#   * `OntUnit.gps_latitude/gps_longitude` is the real answer: the recorded
#     position of the customer's own ONT is an operational fact we own, not a
#     guess about free text. Reverse-geocoding it needs the (already deployed)
#     Nominatim service wired in, and a subscriber→ONT resolution. That is the
#     capture slice's job; `_SUGGESTERS` is the seam it plugs into.
_Suggester = Callable[[Subscriber], Suggestion | None]

_SUGGESTERS: dict[FieldKey, _Suggester] = {}


def suggest(subscriber: Subscriber | None, key: FieldKey) -> Suggestion | None:
    """A non-binding proposal for ``key``, or None when we have no honest
    evidence. Never applied automatically."""
    if subscriber is None:
        return None
    suggester = _SUGGESTERS.get(key)
    return suggester(subscriber) if suggester else None


# ── queue + readiness ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class QueueRow:
    """One subscriber needing capture, with what is missing and any evidence."""

    subscriber_id: str
    account_number: str | None
    display_name: str | None
    missing: tuple[MissingField, ...]
    suggestions: tuple[Suggestion, ...]


def _display_name(subscriber: Subscriber) -> str | None:
    parts = [
        str(getattr(subscriber, "first_name", "") or "").strip(),
        str(getattr(subscriber, "last_name", "") or "").strip(),
    ]
    full = " ".join(p for p in parts if p).strip()
    return full or (getattr(subscriber, "company_name", None) or None)


def _subscribers_in_scope(db: Session) -> list[Subscriber]:
    """Subscribers with at least one active subscription — the same population
    the NCC return counts, so the queue and the filing cannot disagree."""
    rows = (
        db.query(Subscription)
        .options(joinedload(Subscription.subscriber))
        .filter(Subscription.status == SubscriptionStatus.active)
        .all()
    )
    seen: dict[str, Subscriber] = {}
    for subscription in rows:
        subscriber = subscription.subscriber
        if subscriber is not None:
            seen.setdefault(str(subscriber.id), subscriber)
    return list(seen.values())


def _row_for(subscriber: Subscriber, purpose: Purpose) -> QueueRow:
    missing = missing_for(subscriber, purpose)
    suggestions = tuple(
        s for s in (suggest(subscriber, m.key) for m in missing) if s is not None
    )
    return QueueRow(
        subscriber_id=str(subscriber.id),
        account_number=getattr(subscriber, "account_number", None),
        display_name=_display_name(subscriber),
        missing=missing,
        suggestions=suggestions,
    )


def queue(
    db: Session,
    purpose: Purpose,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[tuple[QueueRow, ...], int]:
    """The capture backlog: in-scope subscribers missing required fields.

    Returns ``(page, total)``. Ordered by account number so paging is stable
    and two operators working the queue see the same order.
    """
    incomplete = [
        _row_for(s, purpose)
        for s in _subscribers_in_scope(db)
        if not is_complete(s, purpose)
    ]
    incomplete.sort(key=lambda r: (r.account_number or "", r.subscriber_id))
    total = len(incomplete)
    start = max(offset, 0)
    end = start + max(limit, 0)
    return tuple(incomplete[start:end]), total


def readiness(db: Session, purpose: Purpose) -> dict:
    """Whether ``purpose`` can be satisfied for the whole in-scope population.

    ``incomplete > 0`` means the NCC return is not yet filable: the gap is a
    data-capture task, not a display problem.
    """
    subscribers = _subscribers_in_scope(db)
    by_field: dict[str, int] = {r.key.value: 0 for r in requirements_for(purpose)}
    incomplete = 0
    for subscriber in subscribers:
        missing = missing_for(subscriber, purpose)
        if missing:
            incomplete += 1
        for field in missing:
            by_field[field.key.value] += 1
    total = len(subscribers)
    return {
        "purpose": purpose.value,
        "total_in_scope": total,
        "complete": total - incomplete,
        "incomplete": incomplete,
        "missing_by_field": by_field,
    }


def purposes() -> Sequence[Purpose]:
    return tuple(REQUIREMENTS)
