"""Single owner of a service's effective rate profile (``access.rate_profile``).

Before this module four resolvers answered "what speed does this service get"
with four different precedences, reading ``access_credentials.radius_profile_id``
as plan rate, manual override and throttle all at once. This owner decides it
once, from recorded state, and every reader asks it:

1. an active throttle -- FUP (``fup_states``) or collections (a recognised
   credential marker). When both apply the MORE RESTRICTIVE one wins
   (decision D2; a tie goes to collections);
2. a staged remote plan change (``subscription_change_requests`` while its
   reprovision is in flight);
3. the plan -- the package's linked RADIUS profile, or the rate derived from
   the offer when the package has no link.

The credential slot is NOT plan or override truth: it is read only to recognise
a collections throttle (``pre_throttle_radius_profile_id`` is set, or the slot
equals the configured collections throttle profile). Anything else in the slot
is ignored, which is how an imported stale profile stops shaping the rate.

The input/output types are shaped to the seam the Starter packages expose later
(service-catalog ``DOWNLOAD_MBPS``/``UPLOAD_MBPS`` plus a committed rate;
access-policy ``ALLOW``/``RESTRICT``/``DENY``) so this module becomes the
composition code that feeds them. Nothing here imports Starter.

The owner is the only resolver. ``radius.rate_owner_enabled`` is no longer a
reader switch: it records that the governed switch ran. This release has no
legacy precedence to fall back to, so a production process refuses to start
while the flag is off (``assert_switched_on``): deploying it before the switch
would serve rates nobody verified. Rolling back means redeploying the previous
release, not flipping the flag.

Credential writers: only the collections throttle path (the throttle marker and
slot) and :func:`clear_legacy_fup_slot` here write ``radius_profile_id`` /
``pre_throttle_radius_profile_id``. Nothing writes a plan rate into a credential
(``tests/architecture/test_rate_owner_boundary.py`` holds that line).

Read-only preflight (lists every divergence between the derived rate and what
is projected today)::

    python -m app.services.access_rate_profile preflight

Nothing in this module writes.
"""

from __future__ import annotations

import enum
import logging
import re
import sys
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from app.models.catalog import (
    AccessCredential,
    CatalogOffer,
    GuaranteedSpeedType,
    OfferRadiusProfile,
    RadiusProfile,
    Subscription,
)
from app.models.domain_settings import SettingDomain
from app.models.fup_state import FupActionStatus, FupState
from app.models.subscription_change import (
    SubscriptionChangeExecutionState,
    SubscriptionChangeRequest,
)

logger = logging.getLogger(__name__)

SETTING_KEY = "rate_owner_enabled"
SWITCH_OWNER = "access.rate_profile_switch"
_FUP_PROFILE_CODE_PREFIX = "fup-throttle-"


def rate_owner_enabled(db: Session) -> bool:
    """Whether readers derive the rate through the owner (default: off)."""
    from app.services import settings_spec

    return settings_spec.resolve_value(db, SettingDomain.radius, SETTING_KEY) is True


# --------------------------------------------------------------------------
# Typed seam (Starter-shaped)
# --------------------------------------------------------------------------


class AccessState(enum.Enum):
    """Mirror of the access-policy decision states."""

    ALLOW = "ALLOW"
    RESTRICT = "RESTRICT"
    DENY = "DENY"


class RateLayer(enum.Enum):
    """Which layer of the owner decided the rate."""

    fup_throttle = "fup_throttle"
    collections_throttle = "collections_throttle"
    plan_change_staged = "plan_change_staged"
    plan = "plan"


_RESTRICTING_LAYERS = frozenset(
    {RateLayer.fup_throttle, RateLayer.collections_throttle}
)


@dataclass(frozen=True)
class PlanRate:
    """The plan's speed: catalog-spec shaped (DOWNLOAD/UPLOAD_MBPS + committed)."""

    down_mbps: int | None
    up_mbps: int | None
    guarantee_kind: str | None
    guarantee_value: int | None
    profile_literal: str | None = None


@dataclass(frozen=True)
class AccessSignals:
    """Recorded restriction signals, access-policy shaped."""

    fup_restrict: bool = False
    collections_restrict: bool = False
    deny: bool = False


@dataclass(frozen=True)
class EffectiveRate:
    """The owner's answer. ``AccessDecision`` is the access-policy-shaped alias."""

    profile: RadiusProfile | None
    rate_limit: str | None
    layer: RateLayer
    source_ref: str
    state: AccessState = AccessState.ALLOW
    signals: AccessSignals = field(default_factory=AccessSignals)
    plan: PlanRate | None = None
    plan_ambiguous: bool = False

    @property
    def profile_id(self) -> uuid.UUID | None:
        return self.profile.id if self.profile is not None else None


AccessDecision = EffectiveRate


# --------------------------------------------------------------------------
# Rate grammar: ONE function serves radreply and CoA
# --------------------------------------------------------------------------


def committed_mbps(offer: CatalogOffer | None) -> tuple[int, int] | None:
    """The rate floor this offer promises, as (upload, download) Mbps.

    ``None`` means best effort -- the tier speed is a ceiling only, which is
    what unlimited and home_flex sell. Only ``guaranteed_speed`` decides this;
    plan family never does, so a family rename cannot silently grant or revoke
    a guarantee.
    """
    if offer is None or not offer.speed_download_mbps or not offer.speed_upload_mbps:
        return None

    guaranteed = offer.guaranteed_speed
    if guaranteed is GuaranteedSpeedType.fixed:
        # An absolute floor in Mbps, capped at the line rate: a committed rate
        # above the ceiling is unsatisfiable and RouterOS would reject the
        # whole attribute, dropping the subscriber's rate limit entirely.
        floor = offer.guaranteed_speed_limit_at
        if not floor:
            return None
        return (
            min(int(floor), offer.speed_upload_mbps),
            min(int(floor), offer.speed_download_mbps),
        )
    if guaranteed is GuaranteedSpeedType.relative:
        percent = offer.guaranteed_speed_limit_at
        if not percent:
            return None
        percent = max(0, min(int(percent), 100))
        upload = offer.speed_upload_mbps * percent // 100
        download = offer.speed_download_mbps * percent // 100
        if not upload or not download:
            return None
        return upload, download
    return None


def legacy_rate_limit(
    offer: CatalogOffer | None, profile: RadiusProfile | None
) -> str | None:
    """Pick MikroTik rate-limit string: profile literal > offer-derived > None.

    The offer-derived form is rx/tx -- NAS-perspective, so the subscriber's
    UPLOAD first and DOWNLOAD second, per
    ``app.services.bandwidth.to_subscriber_directions``.

    An offer that promises a rate floor emits the full positional grammar so
    the trailing committed rate can be reached:

        rx/tx  burst-rx/tx  threshold-rx/tx  burst-time-rx/tx  priority  min-rx/tx

    ``rx-rate-min``/``tx-rate-min`` is the CIR -- the rate HTB reserves rather
    than merely allows. Without it every tier is best effort no matter what the
    catalogue charges for.

    Priority stays at RouterOS's default 8: with a committed rate set, HTB
    honours the floor regardless of priority, which only orders the surplus.
    """
    if profile and profile.mikrotik_rate_limit:
        return profile.mikrotik_rate_limit
    return offer_rate_limit(offer)


def offer_rate_limit(offer: CatalogOffer | None) -> str | None:
    """The rate string derived from the offer alone (ceiling + committed rate)."""
    if not offer or not offer.speed_download_mbps or not offer.speed_upload_mbps:
        return None

    ceiling = f"{offer.speed_upload_mbps}M/{offer.speed_download_mbps}M"
    committed = committed_mbps(offer)
    if committed is None:
        return ceiling

    upload_min, download_min = committed
    # Burst fields are zeroed rather than omitted: the grammar is positional,
    # so they must be present to reach the committed rate at the end.
    return f"{ceiling} 0/0 0/0 0/0 8 {upload_min}M/{download_min}M"


def profile_rate_limit(profile: RadiusProfile) -> str | None:
    """Build the MikroTik-Rate-Limit string from a profile's own speeds.

    rx/tx are NAS-perspective: rx is NAS ingress -- the subscriber's UPLOAD --
    and tx is NAS egress, the subscriber's DOWNLOAD. Every pair is therefore
    upload-first; emitting download-first silently swaps a subscriber's rates.
    """
    if profile.mikrotik_rate_limit:
        return profile.mikrotik_rate_limit
    if not profile.download_speed and not profile.upload_speed:
        return None
    dl = f"{profile.download_speed}k" if profile.download_speed else "0"
    ul = f"{profile.upload_speed}k" if profile.upload_speed else "0"
    rate = f"{ul}/{dl}"
    if profile.burst_download or profile.burst_upload:
        bdl = f"{profile.burst_download}k" if profile.burst_download else dl
        bul = f"{profile.burst_upload}k" if profile.burst_upload else ul
        threshold = f"{profile.burst_threshold}k" if profile.burst_threshold else dl
        threshold_ul = f"{profile.burst_threshold}k" if profile.burst_threshold else ul
        btime = str(profile.burst_time or 10)
        rate = f"{ul}/{dl} {bul}/{bdl} {threshold_ul}/{threshold} {btime}/{btime}"
    return rate


def rate_limit_for(
    profile: RadiusProfile | None,
    offer: CatalogOffer | None,
    layer: RateLayer = RateLayer.plan,
) -> str | None:
    """The one rate string for radreply and CoA.

    A profile literal always wins. Without one, the plan derives the rate from
    the offer (committed rate included) and falls back to the profile's own
    speeds; a restricting or staged layer is the profile's decision, so its own
    speeds come before the offer's.
    """
    if profile is not None and profile.mikrotik_rate_limit:
        return profile.mikrotik_rate_limit
    if layer is RateLayer.plan:
        derived = offer_rate_limit(offer)
        if derived:
            return derived
        return profile_rate_limit(profile) if profile is not None else None
    own = profile_rate_limit(profile) if profile is not None else None
    return own or offer_rate_limit(offer)


def plan_rate_from_offer(
    offer: CatalogOffer | None, profile: RadiusProfile | None = None
) -> PlanRate:
    """Shape the offer (and its linked profile) as a catalog-spec plan rate."""
    guarantee = getattr(getattr(offer, "guaranteed_speed", None), "value", None)
    return PlanRate(
        down_mbps=getattr(offer, "speed_download_mbps", None),
        up_mbps=getattr(offer, "speed_upload_mbps", None),
        guarantee_kind=guarantee,
        guarantee_value=getattr(offer, "guaranteed_speed_limit_at", None),
        profile_literal=profile.mikrotik_rate_limit if profile is not None else None,
    )


# --------------------------------------------------------------------------
# Context (bulk-loaded, so a full sweep is not N+1) and the pure derivation
# --------------------------------------------------------------------------


def _chunks(values: Sequence[Any], size: int = 5000) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


@dataclass
class RateContext:
    """Recorded state the derivation reads. Loading flushes nothing."""

    profiles: dict[uuid.UUID, RadiusProfile] = field(default_factory=dict)
    links_by_offer: dict[uuid.UUID, list[uuid.UUID]] = field(default_factory=dict)
    fup_by_subscription: dict[uuid.UUID, FupState] = field(default_factory=dict)
    staged_by_subscription: dict[uuid.UUID, SubscriptionChangeRequest] = field(
        default_factory=dict
    )
    collections_profile_id: uuid.UUID | None = None

    @classmethod
    def load(
        cls,
        db: Session,
        subscription_ids: Iterable[uuid.UUID] | None = None,
    ) -> RateContext:
        """Read the recorded state without autoflushing the caller's changes."""
        with db.no_autoflush:
            return cls._load(db, subscription_ids)

    @classmethod
    def _load(
        cls,
        db: Session,
        subscription_ids: Iterable[uuid.UUID] | None,
    ) -> RateContext:
        from app.services import settings_spec

        ids = None if subscription_ids is None else list(subscription_ids)
        ctx = cls()
        ctx.profiles = {p.id: p for p in db.scalars(select(RadiusProfile)).all()}
        links: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for link in db.scalars(select(OfferRadiusProfile)).all():
            links[link.offer_id].append(link.profile_id)
        ctx.links_by_offer = {
            offer_id: sorted(profile_ids, key=str)
            for offer_id, profile_ids in links.items()
        }

        fup_statement = select(FupState).where(
            FupState.action_status == FupActionStatus.throttled
        )
        staged_statement = (
            select(SubscriptionChangeRequest)
            .where(
                SubscriptionChangeRequest.execution_state
                == SubscriptionChangeExecutionState.provisioning,
                SubscriptionChangeRequest.remote_reprovision_requested_at.isnot(None),
                SubscriptionChangeRequest.remote_radius_profile_id.isnot(None),
            )
            .order_by(
                SubscriptionChangeRequest.remote_reprovision_requested_at.desc(),
                SubscriptionChangeRequest.id.desc(),
            )
        )
        if ids is None:
            fup_rows = list(db.scalars(fup_statement).all())
            staged_rows = list(db.scalars(staged_statement).all())
        else:
            fup_rows, staged_rows = [], []
            for chunk in _chunks(ids):
                fup_rows += db.scalars(
                    fup_statement.where(FupState.subscription_id.in_(chunk))
                ).all()
                staged_rows += db.scalars(
                    staged_statement.where(
                        SubscriptionChangeRequest.subscription_id.in_(chunk)
                    )
                ).all()
            staged_rows.sort(
                key=lambda r: (r.remote_reprovision_requested_at, str(r.id)),
                reverse=True,
            )
        ctx.fup_by_subscription = {row.subscription_id: row for row in fup_rows}
        for request in staged_rows:  # newest first: first one wins
            ctx.staged_by_subscription.setdefault(request.subscription_id, request)

        configured = settings_spec.resolve_value(
            db, SettingDomain.collections, "throttle_radius_profile_id"
        )
        ctx.collections_profile_id = _as_uuid(configured)
        return ctx


def _as_uuid(value: object) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


_RATE_PART = re.compile(r"^(\d+(?:\.\d+)?)([kKmMgG]?)$")
_UNIT_KBPS = {"": 1 / 1000, "k": 1.0, "m": 1000.0, "g": 1_000_000.0}


def _download_kbps(profile: RadiusProfile) -> float | None:
    """A profile's download speed in kbps, or None when it cannot be known.

    Taken from ``download_speed``; failing that from the literal rate string,
    whose second half (tx) is the subscriber's download.
    """
    if profile.download_speed:
        return float(profile.download_speed)
    literal = (profile.mikrotik_rate_limit or "").split()
    if not literal or "/" not in literal[0]:
        return None
    match = _RATE_PART.match(literal[0].split("/", 1)[1].strip())
    if match is None:
        return None
    value = float(match.group(1)) * _UNIT_KBPS[match.group(2).lower()]
    return value or None


def collections_throttle_profile(
    ctx: RateContext,
    credential: AccessCredential | None,
) -> RadiusProfile | None:
    """The collections throttle profile carried by ``credential``, else None.

    The slot is recognised as a collections throttle ONLY when
    ``pre_throttle_radius_profile_id`` is set or the slot equals the configured
    collections throttle profile. Every other slot value is ignored.

    A recognised throttle is always the CONFIGURED collections profile: the
    slot is only a marker, because a live writer can overwrite it with the plan
    profile and leave the pre-throttle marker stale. The slot is used as the
    profile only when no throttle profile is configured.
    """
    if credential is None:
        return None
    slot = credential.radius_profile_id
    configured = ctx.collections_profile_id
    marked = credential.pre_throttle_radius_profile_id is not None
    is_configured = slot is not None and slot == configured
    if not marked and not is_configured:
        return None
    configured_profile = ctx.profiles.get(configured) if configured else None
    if configured_profile is not None:
        return configured_profile
    return ctx.profiles.get(slot) if slot is not None else None


def package_profile_id(
    db: Session, offer_id: object, *, preferred: object = None
) -> uuid.UUID | None:
    """The package's linked profile id (deterministic when there are several).

    One link: that link. Several: ``preferred`` when it is one of them, else the
    first by id (the ambiguity is logged and listed by the preflight). None when
    the package has no link.
    """
    package = _as_uuid(offer_id)
    if package is None:
        return None
    with db.no_autoflush:
        links = sorted(
            db.scalars(
                select(OfferRadiusProfile.profile_id).where(
                    OfferRadiusProfile.offer_id == package
                )
            ).all(),
            key=str,
        )
    if not links:
        return None
    wanted = _as_uuid(preferred)
    return wanted if wanted in links else links[0]


def collections_throttled_credentials(
    db: Session,
) -> list[tuple[AccessCredential, RadiusProfile | None]]:
    """Active credentials carrying a collections throttle, with their profile.

    Recognition is the owner's own (marker set, or slot equals the configured
    throttle profile); the profile is the one the owner would apply, or None
    when none can be found. Read-only; used by the drift report.
    """
    ctx = RateContext.load(db)
    candidates = db.scalars(
        select(AccessCredential).where(AccessCredential.is_active.is_(True))
    ).all()
    found: list[tuple[AccessCredential, RadiusProfile | None]] = []
    for credential in candidates:
        slot = credential.radius_profile_id
        marked = credential.pre_throttle_radius_profile_id is not None
        if not marked and not (slot is not None and slot == ctx.collections_profile_id):
            continue
        found.append((credential, collections_throttle_profile(ctx, credential)))
    return found


def plan_profile_for(
    ctx: RateContext, subscription: Subscription
) -> tuple[RadiusProfile | None, bool]:
    """The package's linked profile and whether the link is ambiguous."""
    links = ctx.links_by_offer.get(subscription.offer_id, [])
    if not links:
        return None, False
    if len(links) == 1:
        return ctx.profiles.get(links[0]), False
    chosen = (
        subscription.radius_profile_id
        if subscription.radius_profile_id in links
        else links[0]
    )
    logger.warning(
        "access.rate_profile.plan_ambiguous offer=%s links=%d",
        subscription.offer_id,
        len(links),
    )
    return ctx.profiles.get(chosen), True


def derive(
    ctx: RateContext,
    subscription: Subscription,
    credential: AccessCredential | None,
    *,
    include_enforcement: bool = True,
) -> EffectiveRate:
    """Pure derivation of the effective rate from recorded state."""
    offer = subscription.offer
    plan_profile, ambiguous = plan_profile_for(ctx, subscription)
    plan = plan_rate_from_offer(offer, plan_profile)

    if include_enforcement:
        fup_state = ctx.fup_by_subscription.get(subscription.id)
        fup_profile = (
            ctx.profiles.get(fup_state.throttle_profile_id)
            if fup_state is not None and fup_state.throttle_profile_id
            else None
        )
        collections_profile = collections_throttle_profile(ctx, credential)
        signals = AccessSignals(
            fup_restrict=fup_profile is not None,
            collections_restrict=collections_profile is not None,
        )
        if fup_profile is not None or collections_profile is not None:
            # D2: the MORE RESTRICTIVE throttle applies; a tie goes to collections.
            # A speed that cannot be determined fails closed: collections wins.
            use_fup = False
            if fup_profile is not None:
                if collections_profile is None:
                    use_fup = True
                else:
                    fup_kbps = _download_kbps(fup_profile)
                    coll_kbps = _download_kbps(collections_profile)
                    use_fup = (
                        fup_kbps is not None
                        and coll_kbps is not None
                        and fup_kbps < coll_kbps
                    )
            if use_fup:
                assert fup_profile is not None and fup_state is not None
                layer, profile = RateLayer.fup_throttle, fup_profile
                ref = f"fup_states:{fup_state.id}"
            else:
                assert collections_profile is not None and credential is not None
                layer, profile = RateLayer.collections_throttle, collections_profile
                ref = f"access_credentials:{credential.id}"
            return EffectiveRate(
                profile=profile,
                rate_limit=rate_limit_for(profile, offer, layer),
                layer=layer,
                source_ref=ref,
                state=AccessState.RESTRICT,
                signals=signals,
                plan=plan,
                plan_ambiguous=ambiguous,
            )

        staged = ctx.staged_by_subscription.get(subscription.id)
        staged_profile = (
            ctx.profiles.get(staged.remote_radius_profile_id)
            if staged is not None and staged.remote_radius_profile_id is not None
            else None
        )
        if staged is not None and staged_profile is not None:
            return EffectiveRate(
                profile=staged_profile,
                rate_limit=rate_limit_for(staged_profile, offer, RateLayer.plan),
                layer=RateLayer.plan_change_staged,
                source_ref=f"subscription_change_requests:{staged.id}",
                plan=plan,
                plan_ambiguous=ambiguous,
            )

    links = ctx.links_by_offer.get(subscription.offer_id, [])
    return EffectiveRate(
        profile=plan_profile,
        rate_limit=rate_limit_for(plan_profile, offer, RateLayer.plan),
        layer=RateLayer.plan,
        source_ref=(
            f"offer_radius_profiles:{subscription.offer_id}"
            if links
            else f"offers:{subscription.offer_id}"
        ),
        plan=plan,
        plan_ambiguous=ambiguous,
    )


# --------------------------------------------------------------------------
# Single-subscription entry points (readers outside the bulk sweep)
# --------------------------------------------------------------------------


def clear_legacy_fup_slot(
    db: Session,
    credentials: Iterable[AccessCredential],
    throttle_profile_id: object,
) -> int:
    """Clear a FUP throttle profile an older version left in a credential slot.

    Only a slot that equals the FUP state's throttle profile and carries no
    collections marker is cleared (it is the FUP's own old write). A slot that is
    the configured collections throttle, or any marked slot, is left alone so an
    active collections throttle survives the FUP lift. This is the one place
    outside the collections throttle path that writes the slot.
    """
    target = _as_uuid(throttle_profile_id)
    if target is None:
        return 0
    with db.no_autoflush:
        from app.services import settings_spec

        collections = _as_uuid(
            settings_spec.resolve_value(
                db, SettingDomain.collections, "throttle_radius_profile_id"
            )
        )
    if target == collections:
        return 0
    cleared = 0
    for credential in credentials:
        if (
            credential.radius_profile_id == target
            and credential.pre_throttle_radius_profile_id is None
        ):
            credential.radius_profile_id = None
            cleared += 1
    return cleared


def active_credential_for(
    db: Session, subscription: Subscription
) -> AccessCredential | None:
    """The subscription's active credential, deterministically chosen.

    The credential whose username is the login first (as the sweep matches),
    then one linked by ``subscription_id``, lowest id first -- never an
    arbitrary ``.first()``.
    """
    with db.no_autoflush:
        if subscription.login:
            by_login = db.scalars(
                select(AccessCredential)
                .where(
                    AccessCredential.username == subscription.login,
                    AccessCredential.is_active.is_(True),
                )
                .order_by(AccessCredential.id.asc())
            ).first()
            if by_login is not None:
                return by_login
        return db.scalars(
            select(AccessCredential)
            .where(
                AccessCredential.subscription_id == subscription.id,
                AccessCredential.is_active.is_(True),
            )
            .order_by(AccessCredential.id.asc())
        ).first()


def resolve_effective_rate(
    db: Session,
    subscription: Subscription,
    credential: AccessCredential | None = None,
    *,
    include_enforcement: bool = True,
) -> EffectiveRate:
    """Derive one subscription's effective rate (read-only, flush-free)."""
    if credential is None and include_enforcement:
        credential = active_credential_for(db, subscription)
    ctx = RateContext.load(db, [subscription.id])
    return derive(
        ctx, subscription, credential, include_enforcement=include_enforcement
    )


def resolve_plan_profile(
    db: Session, subscription: Subscription
) -> RadiusProfile | None:
    """The package's profile (no throttle, no staged change)."""
    return resolve_effective_rate(db, subscription, include_enforcement=False).profile


# --------------------------------------------------------------------------
# Input guards (active only with the owner enabled)
# --------------------------------------------------------------------------

OVERRIDE_NOT_SUPPORTED = "access.rate_profile.override_not_supported"


class ThrottleProfileChangeRefused(ValueError):
    """Changing the collections throttle profile would lift live throttles."""


def refuse_unrecorded_override(
    db: Session,
    *,
    offer_id: object,
    profile_id: object,
) -> None:
    """Refuse a RADIUS profile that is not the package's own (decision D1).

    A speed variant is a different offer; a per-service profile override has no
    recorded reason, actor or expiry and is not supported. NULL and the
    package's linked profile are accepted (and, for a credential, never stored).
    """
    from app.services.domain_errors import DomainError

    if profile_id in (None, ""):
        return
    wanted = _as_uuid(profile_id)
    package = _as_uuid(offer_id)
    linked: set[uuid.UUID] = set()
    if package is not None:
        linked = set(
            db.scalars(
                select(OfferRadiusProfile.profile_id).where(
                    OfferRadiusProfile.offer_id == package
                )
            ).all()
        )
    if wanted is not None and wanted in linked:
        return
    raise DomainError(
        code=OVERRIDE_NOT_SUPPORTED,
        message=(
            "A RADIUS profile other than the package's own cannot be set on a "
            "service. Choose a package with the required speed instead."
        ),
        details={"offer_id": _s(package), "profile_id": _s(wanted)},
        retryable=False,
    )


class RateOwnerNotSwitchedOn(RuntimeError):
    """The process must not serve before the governed switch has run."""


def assert_switched_on(db: Session) -> None:
    """Refuse to serve (production) while the governed switch has not run.

    This release resolves every rate through the owner and writes no plan rate
    into credentials, so it is only safe once the switch preflight, projection
    and read-back have passed. Other environments (development, tests, fresh
    databases) are not blocked; the flag is only evidence of the switch.
    """
    if rate_owner_enabled(db) is True:
        return
    raise RateOwnerNotSwitchedOn(
        "radius.rate_owner_enabled is off: run the governed rate switch "
        "(python -m scripts.network.access_rate_switch on) before deploying "
        "this release, or redeploy the previous release."
    )


def refuse_ungoverned_switch_on(
    db: Session, domain: object, key: str, pending_value: object
) -> None:
    """Refuse turning the owner on except through its governed switch command.

    The switch is flipped only by ``access.rate_profile`` after it has re-run
    the preflight in the same transaction. Turning it off is always allowed.
    """
    from app.services import settings_spec
    from app.services.owner_commands import owner_command_active

    if domain != SettingDomain.radius or key != SETTING_KEY:
        return
    spec = settings_spec.get_spec(SettingDomain.radius, SETTING_KEY)
    value, error = (
        settings_spec.coerce_value(spec, pending_value)
        if spec is not None
        else (None, "unknown")
    )
    if error is None and value is False:
        return
    if error is None and owner_command_active(db, owner=SWITCH_OWNER):
        return
    raise ThrottleProfileChangeRefused(
        "The single rate owner can only be switched on by its governed switch "
        "command, which re-checks every service first."
    )


def refuse_throttle_profile_change(
    db: Session, domain: object, key: str, pending_value: object
) -> None:
    """Refuse changing ``collections.throttle_radius_profile_id`` while in use (D6).

    A credential is throttled on the old value when it carries a collections
    marker or its slot equals the old configured profile. Changing the setting
    underneath it would lift those throttles silently.
    """
    from sqlalchemy import func, or_

    from app.services import settings_spec

    if domain != SettingDomain.collections or key != "throttle_radius_profile_id":
        return
    old = _as_uuid(settings_spec.resolve_value(db, SettingDomain.collections, key))
    new = _as_uuid(pending_value)
    if old == new:
        return
    on_old: ColumnElement[bool] = AccessCredential.pre_throttle_radius_profile_id.isnot(
        None
    )
    if old is not None:
        on_old = or_(on_old, AccessCredential.radius_profile_id == old)
    throttled = db.scalar(
        select(func.count(AccessCredential.id)).where(
            AccessCredential.is_active.is_(True), on_old
        )
    )
    if throttled:
        raise ThrottleProfileChangeRefused(
            f"{throttled} customer(s) are throttled on the current collections "
            "throttle profile. Restore them before changing the profile."
        )


# --------------------------------------------------------------------------
# Preflight: every divergence between the derived rate and today's projection
# --------------------------------------------------------------------------

DIVERGENCE_CLASSES = (
    "ambiguous_link",
    "no_package_link",
    "unrecognised_throttle",
    "credential_wins",
    "subscription_differs",
    "throttle_not_projected",
    "throttle_rate_changes",
)


@dataclass(frozen=True)
class Divergence:
    subscription_id: str
    login: str
    offer_id: str | None
    klass: str
    derived_layer: str
    derived_profile_id: str | None
    projected_profile_id: str | None
    credential_profile_id: str | None
    subscription_profile_id: str | None
    derived_rate: str | None
    projected_rate: str | None

    def as_row(self) -> dict[str, str | None]:
        return {
            "subscription_id": self.subscription_id,
            "login": self.login,
            "offer_id": self.offer_id,
            "class": self.klass,
            "derived_layer": self.derived_layer,
            "derived_profile_id": self.derived_profile_id,
            "projected_profile_id": self.projected_profile_id,
            "credential_profile_id": self.credential_profile_id,
            "subscription_profile_id": self.subscription_profile_id,
            "derived_rate": self.derived_rate,
            "projected_rate": self.projected_rate,
        }


def _s(value: object) -> str | None:
    return None if value is None else str(value)


def _provisioned_state(
    db: Session,
) -> tuple[list[Subscription], dict[str, AccessCredential], RateContext]:
    """Every provisioned login's subscription and credential, plus the context."""
    from app.services.radius_access_state import ACTIVE_STATUSES, BLOCKED_STATUSES

    subscriptions = list(
        db.scalars(
            select(Subscription).where(
                Subscription.login.isnot(None),
                Subscription.status.in_(ACTIVE_STATUSES | BLOCKED_STATUSES),
            )
        )
        .unique()
        .all()
    )
    logins = {s.login for s in subscriptions if s.login}
    credentials_by_login: dict[str, AccessCredential] = {}
    for chunk in _chunks(sorted(logins)):
        for found in db.scalars(
            select(AccessCredential)
            .where(
                AccessCredential.is_active.is_(True),
                AccessCredential.username.in_(chunk),
            )
            .order_by(AccessCredential.id.desc())
        ).all():
            credentials_by_login[found.username] = found
    ctx = RateContext.load(db, [s.id for s in subscriptions])
    return subscriptions, credentials_by_login, ctx


def derived_rates(db: Session) -> dict[str, str | None]:
    """The derived rate string for every provisioned login (read-only)."""
    subscriptions, credentials_by_login, ctx = _provisioned_state(db)
    return {
        s.login: derive(ctx, s, credentials_by_login.get(s.login or "")).rate_limit
        for s in subscriptions
        if s.login
    }


def preflight(db: Session) -> list[Divergence]:
    """List every provisioned login whose derived rate differs from the projection.

    "Projection" is the previous precedence the radius sweep applies today:
    the credential slot, else the subscription profile, rate per the legacy
    grammar. Recognised throttles are compared in the reverse direction: the
    derived throttle profile must be the profile actually projected.
    Read-only.
    """
    subscriptions, credentials_by_login, ctx = _provisioned_state(db)
    rows: list[Divergence] = []
    for subscription in sorted(subscriptions, key=lambda s: str(s.id)):
        credential = credentials_by_login.get(subscription.login or "")
        derived = derive(ctx, subscription, credential)
        slot = credential.radius_profile_id if credential is not None else None
        projected_id = slot or subscription.radius_profile_id
        projected_profile = ctx.profiles.get(projected_id) if projected_id else None
        projected_rate = legacy_rate_limit(subscription.offer, projected_profile)
        links = ctx.links_by_offer.get(subscription.offer_id, [])

        klass: str | None = None
        if derived.layer in _RESTRICTING_LAYERS:
            if projected_id != derived.profile_id:
                klass = "throttle_not_projected"
            elif projected_rate != derived.rate_limit:
                # Same profile, different rate string: e.g. a throttle profile
                # with no literal went out at full offer speed so far.
                klass = "throttle_rate_changes"
        elif derived.plan_ambiguous:
            klass = "ambiguous_link"
        elif not links and projected_id is not None:
            klass = "no_package_link"
        else:
            slot_profile = ctx.profiles.get(slot) if slot else None
            if (
                slot_profile is not None
                and (slot_profile.code or "").startswith(_FUP_PROFILE_CODE_PREFIX)
                and slot != derived.profile_id
            ):
                klass = "unrecognised_throttle"
            elif slot is not None and slot != derived.profile_id:
                klass = "credential_wins"
            elif (
                subscription.radius_profile_id != derived.profile_id
                or projected_rate != derived.rate_limit
            ):
                klass = "subscription_differs"
        if klass is None:
            continue
        rows.append(
            Divergence(
                subscription_id=str(subscription.id),
                login=subscription.login or "",
                offer_id=_s(subscription.offer_id),
                klass=klass,
                derived_layer=derived.layer.value,
                derived_profile_id=_s(derived.profile_id),
                projected_profile_id=_s(projected_id),
                credential_profile_id=_s(slot),
                subscription_profile_id=_s(subscription.radius_profile_id),
                derived_rate=derived.rate_limit,
                projected_rate=projected_rate,
            )
        )
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: ``preflight`` -- read-only, exits non-zero on any divergence."""
    import json

    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["preflight"]:
        print(
            "usage: python -m app.services.access_rate_profile preflight",
            file=sys.stderr,
        )
        return 2

    from app.db import SessionLocal, begin_read_only_snapshot

    db = SessionLocal()
    try:
        begin_read_only_snapshot(db)
        rows = preflight(db)
    finally:
        db.rollback()
        db.close()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.klass] = counts.get(row.klass, 0) + 1
        print(json.dumps(row.as_row(), sort_keys=True))
    print(json.dumps({"divergences": len(rows), "by_class": counts}, sort_keys=True))
    return 1 if rows else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
