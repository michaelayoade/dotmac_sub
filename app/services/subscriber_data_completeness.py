"""Subscriber data completeness — what a subscriber must have, who confirmed it,
and whether that is still true.

The owner of the question "is this subscriber's data good enough for X?", where
X is a declared *purpose* (filing the NCC return, satisfying KYC). One
declarative policy maps purpose → required fields, each with a human label and
the reason it is required, so a caller never re-decides what "complete" means.

**Presence is not the question.** A value can exist and never have been
checked. Of the 4,054 subscriber locations Sub files to the NCC, 3,558 were
*inferred* by matching address text against a place gazetteer and 496 were
absent — **none is a confirmed fact**. So every field carries a
:class:`Provenance`:

* ``captured`` — someone confirmed it; the ledger says who, when and how.
* ``inferred`` — we derived it and nobody has ever confirmed it. Plausible,
  unverified, possibly stale.
* ``absent`` — we do not have it.

``complete`` (no absent fields) and ``verified`` (all fields captured and
fresh) are therefore different questions, and the gap between them is the
whole backlog. Confirmations expire: a customer who moves does not tell us, so
a capture has a shelf life (see ``_DEFAULT_REVALIDATE_AFTER``).

**This module is read-only.** It derives and reports; it never writes. Capture
appends to the ledger through the capture slice, and suggestions are never
auto-applied — that distinction is the point. Reporting a subscriber we cannot
locate as though we know where they are is exactly the fabrication removed from
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
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy.orm import Session, joinedload

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import NINVerificationStatus, Subscriber
from app.models.subscriber_field_verification import SubscriberFieldVerification
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


class Provenance(StrEnum):
    """How we came to hold a field — the difference between knowing and
    assuming."""

    captured = "captured"
    inferred = "inferred"
    absent = "absent"


# How long a confirmation stands before it must be re-confirmed.
#
# This is a POLICY NUMBER, not a fact, so it is declared once here rather than
# buried in a caller. 12 months is chosen to match the annual rhythm the NCC
# returns already run on, and because a service address is stable-but-not-
# permanent: people move, and nobody tells their ISP. It is deliberately not
# tuned per field yet.
#
# It should become a setting (SettingDomain.subscriber) once someone other than
# this module needs to disagree with it — a constant that a caller cannot
# override is honest; a constant pretending to be configurable is not.
_DEFAULT_REVALIDATE_AFTER = timedelta(days=365)


@dataclass(frozen=True)
class FieldRequirement:
    """One required field, and why. ``is_present`` is the single place that
    decides presence — callers never re-implement it.

    ``revalidate_after`` is how long a capture of this field stays fresh;
    ``None`` means a confirmation never expires.
    """

    key: FieldKey
    label: str
    why: str
    is_present: Callable[[Subscriber], bool]
    revalidate_after: timedelta | None = _DEFAULT_REVALIDATE_AFTER


@dataclass(frozen=True)
class MissingField:
    key: FieldKey
    label: str
    why: str


@dataclass(frozen=True)
class FieldState:
    """What we hold for one field, and how much it is worth.

    ``provenance`` answers "do we know this or did we guess it?"; ``is_stale``
    answers "and is it still true?". A field is only trustworthy when it is
    ``captured`` and not stale.
    """

    key: FieldKey
    label: str
    provenance: Provenance
    value: str | None
    verified_at: datetime | None
    source: str | None
    is_stale: bool

    @property
    def needs_revalidation(self) -> bool:
        """True when this field should be put in front of a human: we lack it,
        we only guessed it, or the confirmation has expired."""
        return self.provenance is not Provenance.captured or self.is_stale


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
    """True when no required field is *absent*.

    **Complete is not verified.** An inferred value — a state matched out of an
    address string that nobody ever confirmed — is complete and unverified. The
    NCC return can be filed off complete data and still be reporting guesses;
    that is precisely how 3,558 unconfirmed locations came to be filed. Use
    :func:`is_verified` when the question is "do we actually know this?".
    """
    return not missing_for(subscriber, purpose)


# ── provenance + freshness ──────────────────────────────────────────────────


def _latest_captures(
    db: Session, subscriber_ids: Sequence[str]
) -> dict[tuple[str, str], SubscriberFieldVerification]:
    """Newest confirmation per (subscriber, field).

    The ledger is append-only, so a field may carry several rows: the latest is
    the current confirmation and the rest are its history. Loaded in one query
    for the whole page — the queue runs over every active subscriber, so a
    per-row lookup would be a needless N+1.
    """
    if not subscriber_ids:
        return {}
    rows = (
        db.query(SubscriberFieldVerification)
        .filter(SubscriberFieldVerification.subscriber_id.in_(subscriber_ids))
        .order_by(SubscriberFieldVerification.verified_at.asc())
        .all()
    )
    latest: dict[tuple[str, str], SubscriberFieldVerification] = {}
    for row in rows:
        # Ascending order: the last write per key wins, i.e. the newest.
        latest[(str(row.subscriber_id), str(row.field_key))] = row
    return latest


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _state_for_field(
    subscriber: Subscriber,
    requirement: FieldRequirement,
    capture: SubscriberFieldVerification | None,
    *,
    now: datetime,
) -> FieldState:
    if capture is not None:
        verified_at = _as_utc(capture.verified_at)
        window = requirement.revalidate_after
        is_stale = bool(
            window is not None
            and verified_at is not None
            and now - verified_at > window
        )
        return FieldState(
            key=requirement.key,
            label=requirement.label,
            provenance=Provenance.captured,
            value=capture.value,
            verified_at=verified_at,
            source=capture.source,
            is_stale=is_stale,
        )
    if requirement.is_present(subscriber):
        # We hold a value but nobody confirmed it — derived, not known.
        return FieldState(
            key=requirement.key,
            label=requirement.label,
            provenance=Provenance.inferred,
            value=None,
            verified_at=None,
            source=None,
            is_stale=False,
        )
    return FieldState(
        key=requirement.key,
        label=requirement.label,
        provenance=Provenance.absent,
        value=None,
        verified_at=None,
        source=None,
        is_stale=False,
    )


def state_of(
    db: Session,
    subscriber: Subscriber | None,
    purpose: Purpose,
    *,
    now: datetime | None = None,
) -> tuple[FieldState, ...]:
    """What we hold for each required field, and how we came to hold it."""
    requirements = requirements_for(purpose)
    if subscriber is None:
        return tuple(
            FieldState(
                key=r.key,
                label=r.label,
                provenance=Provenance.absent,
                value=None,
                verified_at=None,
                source=None,
                is_stale=False,
            )
            for r in requirements
        )
    moment = now or datetime.now(UTC)
    captures = _latest_captures(db, [str(subscriber.id)])
    return tuple(
        _state_for_field(
            subscriber,
            requirement,
            captures.get((str(subscriber.id), requirement.key.value)),
            now=moment,
        )
        for requirement in requirements
    )


def is_verified(
    db: Session,
    subscriber: Subscriber | None,
    purpose: Purpose,
    *,
    now: datetime | None = None,
) -> bool:
    """True only when every required field is a *confirmed, fresh* fact.

    This is the honest bar. ``is_complete`` passes on inferred data; this does
    not.
    """
    states = state_of(db, subscriber, purpose, now=now)
    return bool(states) and all(not s.needs_revalidation for s in states)


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
    """One subscriber needing capture, with what is missing and any evidence.

    ``missing`` is what we do not hold at all; ``states`` is the fuller
    picture, including fields we hold but only ever guessed. A row can have no
    missing fields and still be here — that is revalidation.
    """

    subscriber_id: str
    account_number: str | None
    display_name: str | None
    missing: tuple[MissingField, ...]
    suggestions: tuple[Suggestion, ...]
    states: tuple[FieldState, ...] = ()

    @property
    def needs_revalidation(self) -> tuple[FieldState, ...]:
        return tuple(s for s in self.states if s.needs_revalidation)


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


def _row_for(
    subscriber: Subscriber,
    purpose: Purpose,
    states: tuple[FieldState, ...],
) -> QueueRow:
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
        states=states,
    )


def _states_by_subscriber(
    db: Session,
    subscribers: Sequence[Subscriber],
    purpose: Purpose,
    *,
    now: datetime,
) -> dict[str, tuple[FieldState, ...]]:
    """Field states for a whole population in one ledger query."""
    requirements = requirements_for(purpose)
    ids = [str(s.id) for s in subscribers]
    captures = _latest_captures(db, ids)
    result: dict[str, tuple[FieldState, ...]] = {}
    for subscriber in subscribers:
        sid = str(subscriber.id)
        result[sid] = tuple(
            _state_for_field(
                subscriber,
                requirement,
                captures.get((sid, requirement.key.value)),
                now=now,
            )
            for requirement in requirements
        )
    return result


def queue(
    db: Session,
    purpose: Purpose,
    *,
    limit: int = 50,
    offset: int = 0,
    now: datetime | None = None,
) -> tuple[tuple[QueueRow, ...], int]:
    """The revalidation backlog: every in-scope subscriber we cannot vouch for.

    Not just the ones we know nothing about. A subscriber whose state we merely
    *inferred* from an address string is in this queue too — nobody ever
    confirmed it, so it is a guess we are filing to a regulator. So is one
    whose confirmation has gone stale. Absent, inferred, and stale are three
    ways of not knowing, and all three belong in front of a human.

    Returns ``(page, total)``. Ordered by account number so paging is stable
    and two operators working the queue see the same order.
    """
    moment = now or datetime.now(UTC)
    subscribers = _subscribers_in_scope(db)
    states = _states_by_subscriber(db, subscribers, purpose, now=moment)
    rows = [
        _row_for(s, purpose, states[str(s.id)])
        for s in subscribers
        if any(state.needs_revalidation for state in states[str(s.id)])
    ]
    rows.sort(key=lambda r: (r.account_number or "", r.subscriber_id))
    total = len(rows)
    start = max(offset, 0)
    end = start + max(limit, 0)
    return tuple(rows[start:end]), total


def readiness(db: Session, purpose: Purpose, *, now: datetime | None = None) -> dict:
    """Whether ``purpose`` rests on facts, for the whole in-scope population.

    The number that matters is ``verified`` — subscribers whose every required
    field was confirmed by someone and is still fresh. ``complete`` counts
    subscribers with no *absent* field, which includes every guess we ever
    made; on production today that is the difference between "88% complete"
    and "0 confirmed".
    """
    moment = now or datetime.now(UTC)
    subscribers = _subscribers_in_scope(db)
    states = _states_by_subscriber(db, subscribers, purpose, now=moment)

    by_field: dict[str, int] = {r.key.value: 0 for r in requirements_for(purpose)}
    by_provenance: dict[str, int] = {p.value: 0 for p in Provenance}
    incomplete = 0
    verified = 0
    stale_fields = 0

    for subscriber in subscribers:
        row_states = states[str(subscriber.id)]
        if any(s.provenance is Provenance.absent for s in row_states):
            incomplete += 1
        if all(not s.needs_revalidation for s in row_states):
            verified += 1
        for state in row_states:
            by_provenance[state.provenance.value] += 1
            if state.provenance is Provenance.absent:
                by_field[state.key.value] += 1
            if state.is_stale:
                stale_fields += 1

    total = len(subscribers)
    return {
        "purpose": purpose.value,
        "total_in_scope": total,
        "complete": total - incomplete,
        "incomplete": incomplete,
        "verified": verified,
        "needs_revalidation": total - verified,
        "missing_by_field": by_field,
        "fields_by_provenance": by_provenance,
        "stale_fields": stale_fields,
    }


def purposes() -> Sequence[Purpose]:
    return tuple(REQUIREMENTS)
