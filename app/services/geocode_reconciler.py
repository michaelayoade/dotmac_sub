"""Reverse-geocode a captured GPS pin and reconcile it against what we were told.

The pin is the captured fact: a technician standing at the premises, or a
customer confirming their own location. Reverse-geocoding projects it into the
administrative units the NCC return needs (state, LGA). This module is the
**reconciler**, not a source of truth: it compares the projection against what
someone claimed and reports agreement, disagreement, or "cannot verify" — it
never picks a winner and never writes a field on its own.

Why the caution is not theoretical
----------------------------------
Wiring a geocoder straight through would replace "guessed from address text"
with "guessed from OpenStreetMap" — the same fabrication with better
provenance. Measured against the deployed Nominatim (five sampled Nigerian
pins):

* ``state`` — 5/5 correct. Trustworthy enough to propose, after validation.
* ``county`` (the LGA) — mostly right, sometimes absent entirely.
* ``postcode`` — **2 of 5 came back from the wrong state.** An Abuja/Garki pin
  returned ``223140`` (a Kaduna range); Victoria Island returned ``500001``
  (a Rivers range) where an independent NIPOST lookup gives ``101241``. It is
  not even self-consistent within one city: a different Abuja pin returned the
  correct ``900108``.

So a geocoded postcode is **never proposed** here (see ``_POSTCODE_NOTE``).
Postcode is a human-entered claim we validate, not a machine guess.

Nothing in this module applies a value. It returns proposals and verdicts; the
capture entrypoint writes ledger rows only for fields that reconcile cleanly,
and a human adjudicates the rest.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.subscriber_field_verification import SubscriberFieldVerification
from app.services.common import coerce_uuid
from app.services.ncc_subscriber_report import normalize_state
from app.services.subscriber_data_completeness import FieldKey

logger = logging.getLogger(__name__)


# A GPS fix carries an accuracy radius. LGA boundaries in dense Abuja and Lagos
# are small — Wuse and Garki are neighbours — so a coarse fix cannot name an
# LGA without inventing one. 150m is tighter than a typical LGA's smallest
# dimension in those cities while remaining achievable for a phone with a clear
# sky view; a cell-tower fix (hundreds of metres to kilometres) is correctly
# rejected. State is far larger than any plausible fix error, so it is not
# gated on accuracy.
#
# This should become a `SettingDomain.subscriber` setting once operations needs
# to disagree with it; a constant a caller cannot override is at least honest
# about being fixed.
_MAX_ACCURACY_M_FOR_LGA = 150.0

_POSTCODE_NOTE = (
    "geocoded postcodes are not proposed: the deployed Nominatim returned "
    "postcodes from the wrong state on 2 of 5 sampled Nigerian pins, and an "
    "independent NIPOST lookup contradicts it. Postcode is a human-entered "
    "claim, validated for format only until an authoritative NIPOST "
    "state-range dataset is sourced."
)

_NIPOST_RE = re.compile(r"^\d{6}$")

_NOMINATIM_BASE_URL_KEY = "nominatim_base_url"
_NOMINATIM_TIMEOUT_KEY = "nominatim_timeout_seconds"


class Verdict(StrEnum):
    """The outcome of comparing a claim against the geocoded pin."""

    agree = "agree"
    disagree = "disagree"
    #: We hold no evidence good enough to judge — a missing geocode, a fix too
    #: coarse, a value our reference data cannot validate, or (always) a
    #: postcode. Never a licence to pick one.
    unverifiable = "unverifiable"


@dataclass(frozen=True)
class GeocodeResult:
    """What the geocoder said. ``postcode`` is carried for the audit trail and
    deliberately never proposed — see ``_POSTCODE_NOTE``."""

    state: str | None
    lga: str | None
    postcode: str | None
    town: str | None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "lga": self.lga,
            "postcode": self.postcode,
            "town": self.town,
        }


@dataclass(frozen=True)
class FieldReconciliation:
    """One field's adjudication. ``proposed`` is only ever set when the verdict
    is ``agree``, or when there was no claim to contradict a validated
    geocode."""

    key: FieldKey
    claimed: str | None
    geocoded: str | None
    verdict: Verdict
    note: str
    proposed: str | None = None


@dataclass(frozen=True)
class Reconciliation:
    fields: tuple[FieldReconciliation, ...]
    accuracy_m: float | None

    def for_key(self, key: FieldKey) -> FieldReconciliation | None:
        return next((f for f in self.fields if f.key == key), None)

    @property
    def capturable(self) -> tuple[FieldReconciliation, ...]:
        """Fields a caller may honestly write: agreed, with a value."""
        return tuple(
            f for f in self.fields if f.verdict is Verdict.agree and f.proposed
        )

    @property
    def needs_human(self) -> tuple[FieldReconciliation, ...]:
        return tuple(f for f in self.fields if f.verdict is Verdict.disagree)

    def as_evidence(self) -> dict[str, Any]:
        return {
            f.key.value: {
                "claimed": f.claimed,
                "geocoded": f.geocoded,
                "verdict": f.verdict.value,
                "note": f.note,
            }
            for f in self.fields
        }


# ── the geocoder client ─────────────────────────────────────────────────────


def _setting(db: Session, key: str) -> Any:
    from app.services.settings_spec import resolve_value

    try:
        return resolve_value(db, SettingDomain.integration, key)
    except Exception:  # pragma: no cover - settings resolution is best-effort
        logger.debug("geocode: could not resolve %s", key, exc_info=True)
        return None


def reverse(db: Session, lat: float, lng: float) -> GeocodeResult | None:
    """Reverse-geocode a pin, or None when we cannot.

    Never raises into a capture flow: an unconfigured, unreachable, slow or
    malformed geocoder simply means we have no projection, which the reconciler
    reports as ``unverifiable``. A capture must not fail because a suggestion
    source is down.
    """
    base_url = _setting(db, _NOMINATIM_BASE_URL_KEY)
    if not base_url or not str(base_url).strip():
        logger.debug("geocode: no %s configured", _NOMINATIM_BASE_URL_KEY)
        return None

    timeout = _setting(db, _NOMINATIM_TIMEOUT_KEY) or 5
    try:
        response = httpx.get(
            f"{str(base_url).rstrip('/')}/reverse",
            params={
                "lat": lat,
                "lon": lng,
                "format": "jsonv2",
                "addressdetails": 1,
            },
            timeout=float(timeout),
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        logger.info("geocode: reverse lookup failed for (%s, %s)", lat, lng)
        return None

    address = payload.get("address") if isinstance(payload, dict) else None
    if not isinstance(address, dict):
        return None

    return GeocodeResult(
        state=_clean(address.get("state")),
        # Nominatim models the Nigerian LGA as `county`.
        lga=_clean(address.get("county")),
        postcode=_clean(address.get("postcode")),
        town=_clean(address.get("town")) or _clean(address.get("city")),
        raw=address,
    )


def _clean(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


# ── validation against our own reference data ───────────────────────────────


def _validated_lga(state: str | None, lga: str | None) -> str | None:
    """The LGA, canonicalised, only if our own reference data agrees it belongs
    to ``state``. Returns None when we cannot confirm it.

    ``ncc_location`` carries the NCC's 775-LGA table but currently lives on an
    unmerged branch. Absent it we hold no reference data, so we cannot validate
    an LGA — and an LGA we cannot validate must not be proposed. Failing closed
    here is the point: a wrong LGA is worse than a missing one.
    """
    if not state or not lga:
        return None
    try:
        from app.services import ncc_location
    except ImportError:
        logger.debug("geocode: ncc_location unavailable; cannot validate LGA")
        return None
    canonical = ncc_location.canonical_lga(state, lga)
    return canonical or None


def validate_claimed_postcode(value: str | None) -> tuple[bool, str]:
    """Validate a **human-entered** postcode. Format only.

    A Nigerian NIPOST code is six digits whose leading digits encode the state.
    Checking that range would catch a typo putting a Lagos customer in Kaduna —
    but it needs an authoritative state→prefix dataset we do not hold. Deriving
    one from the handful of codes we happen to have seen would reject valid
    codes from every state not in the sample, which is worse than not checking.
    So: format only, and range-consistency is an open item.
    """
    text = str(value or "").strip()
    if not text:
        return False, "no postcode supplied"
    if not _NIPOST_RE.match(text):
        return False, "not a 6-digit NIPOST code"
    return True, "format valid; state range unverified (no NIPOST dataset)"


# ── the reconciler ──────────────────────────────────────────────────────────


def reconcile(
    *,
    claimed_state: str | None = None,
    claimed_lga: str | None = None,
    claimed_postcode: str | None = None,
    geocoded: GeocodeResult | None,
    accuracy_m: float | None = None,
) -> Reconciliation:
    """Adjudicate a pin's projection against what someone claimed.

    Returns per-field verdicts. Nothing is applied; a caller writes only what
    ``capturable`` reports, and surfaces ``needs_human`` for adjudication.
    """
    fields: list[FieldReconciliation] = []

    # ── state ───────────────────────────────────────────────────────────────
    geo_state = normalize_state(geocoded.state) if geocoded else "Unknown"
    geo_state_value = geo_state if geo_state != "Unknown" else None
    claim_state = normalize_state(claimed_state) if claimed_state else "Unknown"
    claim_state_value = claim_state if claim_state != "Unknown" else None

    if geo_state_value is None:
        fields.append(
            FieldReconciliation(
                key=FieldKey.state,
                claimed=claim_state_value,
                geocoded=None,
                verdict=Verdict.unverifiable,
                note="no geocoded state to compare against",
            )
        )
    elif claim_state_value is None:
        fields.append(
            FieldReconciliation(
                key=FieldKey.state,
                claimed=None,
                geocoded=geo_state_value,
                verdict=Verdict.agree,
                note="no prior claim; geocoded state stands unopposed",
                proposed=geo_state_value,
            )
        )
    elif claim_state_value == geo_state_value:
        fields.append(
            FieldReconciliation(
                key=FieldKey.state,
                claimed=claim_state_value,
                geocoded=geo_state_value,
                verdict=Verdict.agree,
                note="claim matches the pin",
                proposed=geo_state_value,
            )
        )
    else:
        fields.append(
            FieldReconciliation(
                key=FieldKey.state,
                claimed=claim_state_value,
                geocoded=geo_state_value,
                verdict=Verdict.disagree,
                note="the claimed state and the pin disagree; a human decides",
            )
        )

    # ── lga ─────────────────────────────────────────────────────────────────
    fields.append(
        _reconcile_lga(
            claimed_lga=claimed_lga,
            geocoded=geocoded,
            geo_state_value=geo_state_value,
            accuracy_m=accuracy_m,
        )
    )

    # ── postcode ────────────────────────────────────────────────────────────
    # Always unverifiable. See _POSTCODE_NOTE: the geocoder's Nigerian
    # postcodes are demonstrably wrong, and we hold no dataset to check a
    # human's entry against beyond its format.
    postcode_ok, postcode_note = (
        validate_claimed_postcode(claimed_postcode)
        if claimed_postcode
        else (False, "no postcode claimed")
    )
    fields.append(
        FieldReconciliation(
            key=FieldKey.postal_code,
            claimed=str(claimed_postcode).strip() if claimed_postcode else None,
            geocoded=geocoded.postcode if geocoded else None,
            verdict=Verdict.unverifiable,
            note=f"{_POSTCODE_NOTE} ({postcode_note})"
            if postcode_ok
            else f"{_POSTCODE_NOTE} ({postcode_note})",
        )
    )

    return Reconciliation(fields=tuple(fields), accuracy_m=accuracy_m)


def _reconcile_lga(
    *,
    claimed_lga: str | None,
    geocoded: GeocodeResult | None,
    geo_state_value: str | None,
    accuracy_m: float | None,
) -> FieldReconciliation:
    claimed = str(claimed_lga).strip() if claimed_lga else None
    geo_lga_raw = geocoded.lga if geocoded else None

    if accuracy_m is not None and accuracy_m > _MAX_ACCURACY_M_FOR_LGA:
        return FieldReconciliation(
            key=FieldKey.lga,
            claimed=claimed,
            geocoded=geo_lga_raw,
            verdict=Verdict.unverifiable,
            note=(
                f"fix accuracy {accuracy_m:.0f}m exceeds "
                f"{_MAX_ACCURACY_M_FOR_LGA:.0f}m — too coarse to name an LGA"
            ),
        )

    validated = _validated_lga(geo_state_value, geo_lga_raw)
    if validated is None:
        return FieldReconciliation(
            key=FieldKey.lga,
            claimed=claimed,
            geocoded=geo_lga_raw,
            verdict=Verdict.unverifiable,
            note=(
                "no geocoded LGA"
                if not geo_lga_raw
                else "geocoded LGA is not a validated LGA of the geocoded state"
            ),
        )

    if claimed is None:
        return FieldReconciliation(
            key=FieldKey.lga,
            claimed=None,
            geocoded=geo_lga_raw,
            verdict=Verdict.agree,
            note="no prior claim; validated LGA stands unopposed",
            proposed=validated,
        )

    claimed_canonical = _validated_lga(geo_state_value, claimed)
    if claimed_canonical and claimed_canonical == validated:
        return FieldReconciliation(
            key=FieldKey.lga,
            claimed=claimed,
            geocoded=geo_lga_raw,
            verdict=Verdict.agree,
            note="claim matches the pin",
            proposed=validated,
        )

    return FieldReconciliation(
        key=FieldKey.lga,
        claimed=claimed,
        geocoded=geo_lga_raw,
        verdict=Verdict.disagree,
        note="the claimed LGA and the pin disagree; a human decides",
    )


# ── capture ─────────────────────────────────────────────────────────────────

SOURCE_FIELD_GPS = "field_gps"
SOURCE_CUSTOMER_PORTAL = "customer_portal"
SOURCE_AGENT = "agent"


@dataclass(frozen=True)
class CaptureResult:
    reconciliation: Reconciliation
    geocoded: GeocodeResult | None
    captured_keys: tuple[FieldKey, ...]


def capture_location(
    db: Session,
    subscriber_id: str,
    *,
    lat: float,
    lng: float,
    accuracy_m: float | None = None,
    source: str,
    actor_id: str | None = None,
    actor_name: str | None = None,
    claimed_state: str | None = None,
    claimed_lga: str | None = None,
    claimed_postcode: str | None = None,
) -> CaptureResult:
    """Capture a location pin, writing ledger rows only for what reconciles.

    Writes **only** to the append-only capture ledger — this service does not
    touch ``Subscriber`` columns. Projecting a captured fact onto the
    subscriber's own fields is a mutation the subscriber owner
    (``app.services.subscriber``) makes; becoming a second writer of those
    columns is exactly the parallel-authority this design exists to prevent.

    Fields that disagree, or that we cannot verify, get **no ledger row**. They
    are returned in the reconciliation for a human to adjudicate.
    """
    geocoded = reverse(db, lat, lng)
    reconciliation = reconcile(
        claimed_state=claimed_state,
        claimed_lga=claimed_lga,
        claimed_postcode=claimed_postcode,
        geocoded=geocoded,
        accuracy_m=accuracy_m,
    )

    evidence_base: dict[str, Any] = {
        "lat": lat,
        "lng": lng,
        "accuracy_m": accuracy_m,
        "geocoded": geocoded.as_evidence() if geocoded else None,
        "reconciliation": reconciliation.as_evidence(),
    }

    sub_uuid = coerce_uuid(str(subscriber_id))
    now = datetime.now(UTC)
    captured: list[FieldKey] = []
    for item in reconciliation.capturable:
        db.add(
            SubscriberFieldVerification(
                subscriber_id=sub_uuid,
                field_key=item.key.value,
                value=item.proposed,
                source=source,
                verified_at=now,
                verified_by_actor_id=actor_id,
                verified_by_actor_name=actor_name,
                evidence=evidence_base,
            )
        )
        captured.append(item.key)
    if captured:
        db.flush()

    return CaptureResult(
        reconciliation=reconciliation,
        geocoded=geocoded,
        captured_keys=tuple(captured),
    )


# ── on the `_SUGGESTERS` seam: nothing to register ──────────────────────────
#
# The brief asked for pin-backed state/LGA/postcode suggesters in the
# completeness owner's `_SUGGESTERS` registry. They are not implementable
# there, and they would be redundant if they were:
#
#   * The seam's signature is `Callable[[Subscriber], Suggestion | None]` —
#     pure, taking only a subscriber. Reverse-geocoding needs a Session (for
#     the Nominatim base-URL setting) and a network call. A suggester cannot
#     reach either, and widening the signature to hand every suggester a
#     Session and a socket would turn a pure derivation seam into an I/O one.
#   * Even given the pin, there is nothing left to suggest: `capture_location`
#     already writes the ledger rows for whatever reconciles, so a subsequent
#     "suggestion" would either restate a captured fact or re-propose the
#     value we just declined to capture.
#   * Postcode has no honest suggester at all (see `_POSTCODE_NOTE`).
#
# `_SUGGESTERS` therefore stays empty, as its own docstring predicted. The
# capture UI calls `reverse()`/`reconcile()` directly and shows the operator
# the verdicts — which is the "propose to a human, never auto-apply" contract
# the registry exists to enforce, honoured at the call site instead.
