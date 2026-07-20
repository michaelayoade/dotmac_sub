"""Native referral program — the CRM's ``services/crm/referrals.py``
merged onto sub identity and billing.

Closed loop: an active subscriber gets a code → a prospect capture creates a
quarantined Party and attributed Lead without an account → reviewed Subscriber
conversion attaches that exact Party → activation qualifies the referral → the
referrer earns a configurable account credit.

Deltas from the CRM source:

* Public contact values are risk guards and unverified Party contact points,
  never automatic Subscriber identity proof. New capture does not create a
  Subscriber or copy contact PII into referral metadata.
* ``qualify_for_subscriber`` re-hooks from the CRM's customer-sync path onto
  sub's subscriber-activation lifecycle event
  (``app/services/events/handlers/referral.py``). It only flushes — event
  handlers must never commit the caller's open transaction.
* ``issue_reward``'s ``selfcare.create_account_credit`` HTTP hop becomes a
  direct in-process call to the account-credit owner behind sub's
  ``POST /crm/credits`` handler (``crm_api.create_account_credit``),
  with the SAME idempotency key ``external_ref="referral:{id}"`` — a reward the
  CRM already paid pre-cutover can never be paid twice, and the
  ``with_for_update`` row lock is kept.
* The mirror's "You earned a referral reward!" push moves into ``issue_reward``
  (the mirror webhook path dies at contract, ).

The five ``referral_*`` program settings keys migrate into sub settings
(``SettingDomain.subscriber``); there is no program table.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.party import Party, PartyContactPoint, PartyContactPointType, PartyType
from app.models.referral_native import (
    Referral,
    ReferralCode,
    ReferralRewardStatus,
    ReferralStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import party as party_service
from app.services import settings_spec
from app.services.common import coerce_uuid, get_or_404, validate_enum
from app.services.customer_identity_normalization import (
    default_country_code,
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.customer_identity_resolution import resolve_customer_identity
from app.services.sales import lifecycle as lead_lifecycle

logger = logging.getLogger(__name__)

# Unambiguous alphabet (no 0/O/1/I) so codes are easy to share verbally.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8
_REFERRAL_LEAD_SOURCE = "Referrer"
_DOMAIN = SettingDomain.subscriber


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _config(db: Session) -> dict:
    amount_raw = settings_spec.resolve_value(db, _DOMAIN, "referral_reward_amount")
    try:
        amount = (
            Decimal(str(amount_raw)) if amount_raw not in (None, "") else Decimal("0")
        )
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    window_raw = settings_spec.resolve_value(
        db, _DOMAIN, "referral_qualify_window_days"
    )
    try:
        window = (
            int(cast("str | int", window_raw)) if window_raw not in (None, "") else 90
        )
    except (TypeError, ValueError):
        window = 90
    return {
        "enabled": _as_bool(
            settings_spec.resolve_value(db, _DOMAIN, "referral_program_enabled"), False
        ),
        "amount": amount,
        "currency": str(
            settings_spec.resolve_value(db, _DOMAIN, "referral_reward_currency")
            or "NGN"
        ),
        "window_days": window,
        "auto_approve": _as_bool(
            settings_spec.resolve_value(db, _DOMAIN, "referral_auto_approve_reward"),
            False,
        ),
    }


def _generate_code(db: Session) -> str:
    for _ in range(12):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
        if not db.query(ReferralCode).filter(ReferralCode.code == code).first():
            return code
    raise HTTPException(
        status_code=500, detail="Could not generate a unique referral code"
    )


def share_url(code: str) -> str:
    """The public share link for a referral code (the ``/r/{code}`` deep link).

    ``PORTAL_REFERRAL_SHARE_BASE`` already defaults to sub's own domain.
    """
    base = (os.getenv("PORTAL_REFERRAL_SHARE_BASE") or "https://app.dotmac.io").rstrip(
        "/"
    )
    return f"{base}/r/{code}"


def _subscriber_is_active(db: Session, subscriber: Subscriber) -> bool:
    """Active = derived account status active, or an active subscription.

    The subscription check covers the activation-event window:
    ``activate_subscription`` emits ``subscription.activated`` (which runs the
    qualification handler) *before* ``compute_account_status`` re-derives
    ``subscriber.status``, so the flag alone can still read ``new`` here.
    """
    if subscriber.status == SubscriberStatus.active:
        return True
    from app.models.catalog import Subscription, SubscriptionStatus

    return (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber.id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .first()
        is not None
    )


def _normalized_contact(
    db: Session, email: str | None, phone: str | None
) -> tuple[str | None, str | None]:
    country = default_country_code(db)
    return (
        normalize_email_identifier(email),
        normalize_phone_identifier(phone, default_country_code=country),
    )


def _resolved_subscribers_for_risk_guard(
    db: Session, *, email: str | None, phone: str | None
) -> list[Subscriber]:
    """Return exact account matches only for self/existing-customer rejection.

    Contact resolution here never supplies the referred Party or account link.
    Ambiguous/shared contact values resolve to no identity, by design.
    """

    matches: dict[UUID, Subscriber] = {}
    for identifier, hint in ((email, "email"), (phone, "phone")):
        if not identifier:
            continue
        resolution = resolve_customer_identity(db, identifier, channel_hint=hint)
        if resolution.matched and resolution.subscriber_id is not None:
            subscriber = db.get(Subscriber, resolution.subscriber_id)
            if subscriber is not None:
                matches[subscriber.id] = subscriber
    return list(matches.values())


def _capture_meta(referral: Referral) -> dict:
    meta = referral.metadata_ if isinstance(referral.metadata_, dict) else {}
    capture = meta.get("capture")
    return capture if isinstance(capture, dict) else {}


def _existing_referral_by_capture_contact(
    db: Session,
    *,
    referral_code_id: UUID,
    email: str | None,
    phone: str | None,
) -> Referral | None:
    """Recognize a same-code retry without treating contact as identity proof."""

    norm_email, norm_phone = _normalized_contact(db, email, phone)
    if not norm_email and not norm_phone:
        return None
    submitted: dict[str, str] = {}
    if norm_email:
        submitted[PartyContactPointType.email.value] = norm_email
    if norm_phone:
        submitted[PartyContactPointType.phone.value] = norm_phone
    candidates = (
        db.query(Referral)
        .filter(Referral.referral_code_id == referral_code_id)
        .filter(Referral.is_active.is_(True))
        .filter(
            Referral.status.in_(
                [ReferralStatus.pending.value, ReferralStatus.qualified.value]
            )
        )
        .all()
    )
    for referral in candidates:
        captured: dict[str, str] = {}
        if referral.referred_party_id is not None:
            captured = {
                point.channel_type: point.normalized_value
                for point in db.query(PartyContactPoint)
                .filter(PartyContactPoint.party_id == referral.referred_party_id)
                .filter(PartyContactPoint.is_active.is_(True))
                .filter(
                    PartyContactPoint.channel_type.in_(
                        [
                            PartyContactPointType.email.value,
                            PartyContactPointType.phone.value,
                        ]
                    )
                )
                .all()
            }
        else:
            capture = _capture_meta(referral)
            legacy_email, legacy_phone = _normalized_contact(
                db, capture.get("email"), capture.get("phone")
            )
            if legacy_email:
                captured[PartyContactPointType.email.value] = legacy_email
            if legacy_phone:
                captured[PartyContactPointType.phone.value] = legacy_phone
        if captured == submitted:
            return referral
    return None


def _create_capture_party(
    db: Session,
    *,
    name: str | None,
    email: str | None,
    phone: str | None,
) -> Party:
    """Create quarantined identity and unverified reachability observations."""

    display_name = str(name or "").strip()[:200] or "Referred prospect"
    party = party_service.create_party(
        db,
        party_type=PartyType.person,
        display_name=display_name,
        metadata={"created_by": "referrals.program"},
    )
    party_service.quarantine_party(
        db,
        party_id=party.id,
        reason="Public referral contact is unverified pending identity review",
    )
    normalized_email, normalized_phone = _normalized_contact(db, email, phone)
    for channel_type, normalized_value, display_value in (
        (PartyContactPointType.email, normalized_email, email),
        (PartyContactPointType.phone, normalized_phone, phone),
    ):
        if normalized_value is None:
            continue
        party_service.add_contact_point(
            db,
            party_id=party.id,
            channel_type=channel_type,
            normalized_value=normalized_value,
            display_value=display_value,
            is_primary=True,
            metadata={"observed_by": "referrals.program"},
        )
    return party


def _referred_display_name(referral: Referral) -> str | None:
    if referral.referred_party is not None:
        return referral.referred_party.display_name
    name = _capture_meta(referral).get("name")
    if name:
        return str(name)
    referred = referral.referred_subscriber
    if referred is not None:
        display = (
            referred.display_name
            or f"{referred.first_name} {referred.last_name}".strip()
        )
        return display or None
    return None


class Referrals:
    @staticmethod
    def program(db: Session) -> dict:
        """Public program summary (enabled + advertised reward) for portals/UI."""
        cfg = _config(db)
        return {
            "enabled": bool(cfg["enabled"]),
            "amount": cfg["amount"],
            "currency": cfg["currency"],
        }

    @staticmethod
    def ensure_code(db: Session, subscriber_id: str) -> ReferralCode:
        """Get (or mint) the active referral code for a referrer (a subscriber)."""
        sid = coerce_uuid(subscriber_id)
        subscriber = db.get(Subscriber, sid)
        if subscriber is None:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        existing = (
            db.query(ReferralCode)
            .filter(ReferralCode.subscriber_id == sid)
            .filter(ReferralCode.is_active.is_(True))
            .first()
        )
        if existing:
            return existing
        code = ReferralCode(subscriber_id=sid, code=_generate_code(db))
        db.add(code)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = (
                db.query(ReferralCode)
                .filter(ReferralCode.subscriber_id == sid)
                .filter(ReferralCode.is_active.is_(True))
                .first()
            )
            if existing:
                return existing
            raise
        db.refresh(code)
        return code

    @staticmethod
    def get_by_code(db: Session, code: str) -> ReferralCode | None:
        normalized = str(code or "").strip().upper()
        if not normalized:
            return None
        return (
            db.query(ReferralCode)
            .filter(ReferralCode.code == normalized)
            .filter(ReferralCode.is_active.is_(True))
            .first()
        )

    @staticmethod
    def capture(
        db: Session,
        *,
        code: str,
        name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        region: str | None = None,
        address: str | None = None,
        notes: str | None = None,
        source: str = "public",
    ) -> Referral:
        """Capture a quarantined Party, attributed Lead, and pending Referral.

        Contact matching is used only to reject known self/existing-customer
        captures and recognize an exact same-code retry. It never establishes
        identity, creates an account, or attaches a Subscriber.
        """
        cfg = _config(db)
        if not cfg["enabled"]:
            raise HTTPException(
                status_code=503, detail="Referral program is not enabled."
            )

        ref_code = Referrals.get_by_code(db, code)
        if ref_code is None:
            raise HTTPException(status_code=404, detail="Invalid referral code.")

        email = (email or "").strip() or None
        phone = (phone or "").strip() or None
        if not email and not phone:
            raise HTTPException(
                status_code=422,
                detail="An email or phone number is required to refer someone.",
            )

        for existing_subscriber in _resolved_subscribers_for_risk_guard(
            db, email=email, phone=phone
        ):
            if existing_subscriber.id == ref_code.subscriber_id:
                raise HTTPException(status_code=409, detail="You can't refer yourself.")
            if (
                existing_subscriber.status == SubscriberStatus.active
                and existing_subscriber.is_active
            ):
                raise HTTPException(
                    status_code=409, detail="That person is already an active customer."
                )
        existing = _existing_referral_by_capture_contact(
            db,
            referral_code_id=ref_code.id,
            email=email,
            phone=phone,
        )
        if existing is not None:
            return existing

        referred_party = _create_capture_party(db, name=name, email=email, phone=phone)
        lead = lead_lifecycle.create_party_lead(
            db,
            party_id=referred_party.id,
            title=f"Referral: {referred_party.display_name}",
            lead_source=_REFERRAL_LEAD_SOURCE,
            binding_source="referrals.program",
            binding_reason="Party created for this referral capture",
            origin_capture={
                "capture_method": "referral",
                "source_platform": "referral",
                "capture_source": source,
                "capture_reason": "Prospect submitted through a referral code",
            },
            region=region,
            address=address,
            notes=notes,
            metadata={
                "referral_code": ref_code.code,
                "referrer_subscriber_id": str(ref_code.subscriber_id),
            },
        )

        referral = Referral(
            referrer_subscriber_id=ref_code.subscriber_id,
            referral_code_id=ref_code.id,
            referred_party_id=referred_party.id,
            party_bound_at=datetime.now(UTC),
            party_binding_source="referrals.program",
            party_binding_reason="Party created for this referral capture",
            referred_lead_id=lead.id,
            status=ReferralStatus.pending.value,
            reward_currency=cfg["currency"],
            source=source,
        )
        db.add(referral)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = (
                db.query(Referral)
                .filter(Referral.referred_party_id == referred_party.id)
                .filter(Referral.is_active.is_(True))
                .first()
            )
            if existing is not None:
                return existing
            raise
        db.refresh(referral)
        logger.info(
            "referral_captured referral_id=%s referrer=%s referred=%s code=%s",
            referral.id,
            ref_code.subscriber_id,
            referred_party.id,
            ref_code.code,
        )
        return referral

    @staticmethod
    def qualify_for_subscriber(
        db: Session, subscriber: Subscriber | None
    ) -> Referral | None:
        """Qualify a pending referral when its referred prospect becomes an
        active subscriber. Idempotent and side-effect-safe to call on every
        activation event.

        Flush-only: this runs inside the subscriber-activation event handler,
        which must never commit the caller's open transaction (dispatcher
        contract) — the emitting service's commit lands the change.
        """
        if subscriber is None:
            return None
        if not _subscriber_is_active(db, subscriber):
            return None
        cfg = _config(db)
        if not cfg["enabled"]:
            return None

        # A legacy exact account link remains valid. New Party-first referrals
        # attach only through exact reviewed Party equality, never by contact.
        referral = (
            db.query(Referral)
            .filter(Referral.referred_subscriber_id == subscriber.id)
            .filter(Referral.is_active.is_(True))
            .first()
        )
        if referral is None and subscriber.party_id is not None:
            referral = (
                db.query(Referral)
                .filter(Referral.referred_party_id == subscriber.party_id)
                .filter(Referral.is_active.is_(True))
                .first()
            )
        if (
            referral is not None
            and referral.referred_party_id is not None
            and referral.status == ReferralStatus.pending.value
        ):
            try:
                referral = Referrals.attach_subscriber(
                    db,
                    referral_id=str(referral.id),
                    subscriber_id=str(subscriber.id),
                    source="subscriber_activation",
                    reason="Activated Subscriber has the referral's reviewed Party",
                )
            except HTTPException:
                return None
        if referral is None or referral.status != ReferralStatus.pending.value:
            return None

        now = datetime.now(UTC)
        created = referral.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if (
            cfg["window_days"]
            and created is not None
            and (now - created) > timedelta(days=cfg["window_days"])
        ):
            referral.status = ReferralStatus.expired.value
            db.flush()
            logger.info("referral_expired referral_id=%s", referral.id)
            return referral

        referral.status = ReferralStatus.qualified.value
        referral.qualified_at = now
        referral.reward_amount = cfg["amount"]
        referral.reward_currency = cfg["currency"]
        referral.reward_status = (
            ReferralRewardStatus.approved.value
            if cfg["auto_approve"]
            else ReferralRewardStatus.pending.value
        )
        db.flush()
        logger.info(
            "referral_qualified referral_id=%s referrer=%s amount=%s",
            referral.id,
            referral.referrer_subscriber_id,
            referral.reward_amount,
        )
        return referral

    @staticmethod
    def attach_subscriber(
        db: Session,
        *,
        referral_id: str,
        subscriber_id: str,
        source: str,
        reason: str,
    ) -> Referral:
        """Attach the exact reviewed Subscriber account to a Party referral.

        This is the conversion boundary. It is idempotent for an exact retry,
        refuses a different account or Party, and delegates the corresponding
        Lead account link to ``sales.lead_lifecycle``. It never commits.
        """

        referral = db.get(Referral, coerce_uuid(str(referral_id)))
        if referral is None:
            raise HTTPException(status_code=404, detail="Referral not found")
        if referral.referred_party_id is None:
            raise HTTPException(
                status_code=409,
                detail="Referral needs a reviewed Party binding before conversion.",
            )
        if not (
            referral.party_bound_at is not None
            and str(referral.party_binding_source or "").strip()
            and str(referral.party_binding_reason or "").strip()
        ):
            raise HTTPException(
                status_code=409,
                detail="Referral has incomplete Party binding evidence.",
            )
        normalized_source = str(source or "").strip()
        normalized_reason = str(reason or "").strip()
        if not normalized_source or not normalized_reason:
            raise HTTPException(
                status_code=422,
                detail="Subscriber attachment requires source and reason.",
            )
        subscriber = db.get(Subscriber, coerce_uuid(str(subscriber_id)))
        if subscriber is None:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        if subscriber.id == referral.referrer_subscriber_id:
            raise HTTPException(status_code=409, detail="A referrer cannot self-refer.")
        if subscriber.party_id is None:
            raise HTTPException(
                status_code=409,
                detail="Subscriber needs a reviewed Party binding before conversion.",
            )
        if subscriber.party_id != referral.referred_party_id:
            raise HTTPException(
                status_code=409,
                detail="Subscriber Party does not match the referred Party.",
            )
        referrer = db.get(Subscriber, referral.referrer_subscriber_id)
        if referrer is not None and referrer.party_id == subscriber.party_id:
            raise HTTPException(status_code=409, detail="A referrer cannot self-refer.")
        if (
            referral.referred_subscriber_id is not None
            and referral.referred_subscriber_id != subscriber.id
        ):
            raise HTTPException(
                status_code=409,
                detail="Referral is already attached to a different Subscriber.",
            )
        if referral.referred_lead_id is None:
            raise HTTPException(
                status_code=409,
                detail="Referral needs its attributed Lead before conversion.",
            )
        complete_link_evidence = bool(
            referral.subscriber_linked_at is not None
            and str(referral.subscriber_link_source or "").strip()
            and str(referral.subscriber_link_reason or "").strip()
        )
        if (
            referral.referred_subscriber_id == subscriber.id
            and not complete_link_evidence
            and any(
                value is not None
                for value in (
                    referral.subscriber_linked_at,
                    referral.subscriber_link_source,
                    referral.subscriber_link_reason,
                )
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="Referral has incomplete Subscriber-link evidence.",
            )
        try:
            lead_lifecycle.attach_lead_subscriber(
                db,
                lead_id=referral.referred_lead_id,
                subscriber_id=subscriber.id,
                source=normalized_source,
                reason=normalized_reason,
            )
        except lead_lifecycle.LeadLifecycleError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if referral.referred_subscriber_id == subscriber.id and complete_link_evidence:
            return referral
        referral.referred_subscriber_id = subscriber.id
        referral.subscriber_linked_at = datetime.now(UTC)
        referral.subscriber_link_source = normalized_source
        referral.subscriber_link_reason = normalized_reason
        db.flush()
        return referral

    @staticmethod
    def issue_reward(db: Session, referral_id: str) -> Referral:
        """Apply the referrer's reward as account credit and mark the referral
        rewarded.

        The credit goes through ``crm_api.create_account_credit`` —
        the same service behind ``POST /crm/credits`` the CRM used remotely —
        with the SAME idempotency key ``external_ref="referral:{id}"``, so a
        reward the CRM already paid pre-cutover is returned, never re-credited.
        """
        from app.services import crm_api

        # Lock the referral row so two concurrent calls can't both pass the
        # status check and double-credit (serializes the read-then-write).
        referral = (
            db.query(Referral)
            .filter(Referral.id == coerce_uuid(str(referral_id)))
            .with_for_update()
            .first()
        )
        if referral is None:
            raise HTTPException(status_code=404, detail="Referral not found")
        if referral.status not in (
            ReferralStatus.qualified.value,
            ReferralStatus.rewarded.value,
        ):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot issue a reward for a referral in status {referral.status}",
            )

        # Already credited → idempotent: normalize status, never re-credit.
        if referral.reward_status == ReferralRewardStatus.issued.value:
            referral.status = ReferralStatus.rewarded.value
            db.commit()
            db.refresh(referral)
            return referral

        amount = referral.reward_amount or Decimal("0")
        if amount <= 0:
            # Never mark a referral "rewarded" with no credit behind it.
            raise HTTPException(
                status_code=400,
                detail="Referral has no positive reward amount to issue.",
            )

        currency = (referral.reward_currency or "NGN").strip() or "NGN"
        external_ref = f"referral:{referral.id}"
        try:
            entry = crm_api.create_account_credit(
                db,
                subscriber_id=str(referral.referrer_subscriber_id),
                amount=amount,
                reason=f"Referral reward (referral {referral.id})",
                external_ref=external_ref,
                currency=currency,
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=409,
                detail="Referrer has no active subscriber account to credit.",
            ) from exc

        meta = dict(referral.metadata_ or {})
        meta["reward_credit_id"] = str(entry.id)
        meta["reward_subscriber_id"] = str(referral.referrer_subscriber_id)
        referral.metadata_ = meta

        referral.reward_status = ReferralRewardStatus.issued.value
        referral.reward_issued_at = datetime.now(UTC)
        referral.status = ReferralStatus.rewarded.value
        db.commit()
        db.refresh(referral)
        logger.info(
            "referral_reward_issued referral_id=%s referrer=%s amount=%s credit=%s",
            referral.id,
            referral.referrer_subscriber_id,
            referral.reward_amount,
            (referral.metadata_ or {}).get("reward_credit_id"),
        )
        # Best-effort nudge (moved here from the mirror's webhook path, ).
        try:
            from app.services import push as push_service

            push_service.send_push(
                db,
                str(referral.referrer_subscriber_id),
                title="You earned a referral reward!",
                body=f"{currency} {amount} has been added to your account credit.",
                data={"type": "referral_reward", "referral_id": str(referral.id)},
            )
        except Exception as exc:  # noqa: BLE001 - notification is advisory
            logger.warning(
                "referral_reward_push_failed referral_id=%s: %s", referral.id, exc
            )
        return referral

    @staticmethod
    def qualify_override(db: Session, referral_id: str) -> Referral:
        """Admin override: force-qualify a referral without
        waiting for the referred subscriber's activation event.

        Deliberately bypasses the program-enabled, subscriber-active and
        qualification-window checks of ``qualify_for_subscriber`` — the
        operator is asserting the referral is genuine (prospect signed up
        out-of-band, or the window lapsed unfairly, so ``expired`` rows may
        be rescued too). Reward fields snapshot the current program config,
        exactly like the automatic path.
        """
        referral = get_or_404(db, Referral, str(referral_id), "Referral not found")
        if referral.status not in (
            ReferralStatus.pending.value,
            ReferralStatus.expired.value,
        ):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot qualify a referral in status {referral.status}",
            )
        if referral.referred_party_id is not None:
            if referral.referred_subscriber_id is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Attach the reviewed Subscriber account before qualifying "
                        "this Party-first referral."
                    ),
                )
            referral = Referrals.attach_subscriber(
                db,
                referral_id=str(referral.id),
                subscriber_id=str(referral.referred_subscriber_id),
                source="admin_qualification_review",
                reason="Operator reviewed account conversion before override",
            )
        cfg = _config(db)
        referral.status = ReferralStatus.qualified.value
        referral.qualified_at = datetime.now(UTC)
        referral.reward_amount = cfg["amount"]
        referral.reward_currency = cfg["currency"]
        referral.reward_status = (
            ReferralRewardStatus.approved.value
            if cfg["auto_approve"]
            else ReferralRewardStatus.pending.value
        )
        db.commit()
        db.refresh(referral)
        logger.info(
            "referral_qualified_override referral_id=%s referrer=%s amount=%s",
            referral.id,
            referral.referrer_subscriber_id,
            referral.reward_amount,
        )
        return referral

    @staticmethod
    def reject(db: Session, referral_id: str, reason: str) -> Referral:
        referral = get_or_404(db, Referral, str(referral_id), "Referral not found")
        referral.status = ReferralStatus.rejected.value
        referral.reward_status = ReferralRewardStatus.void.value
        marker = f"Rejected: {reason}"
        referral.notes = f"{referral.notes}\n{marker}" if referral.notes else marker
        db.commit()
        db.refresh(referral)
        return referral

    @staticmethod
    def list(
        db: Session,
        *,
        status: str | None = None,
        reward_status: str | None = None,
        referrer_subscriber_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Referral]:
        query = db.query(Referral).filter(Referral.is_active.is_(True))
        if status:
            member = validate_enum(status, ReferralStatus, "status")
            query = query.filter(Referral.status == member.value)
        if reward_status:
            reward_member = validate_enum(
                reward_status, ReferralRewardStatus, "reward_status"
            )
            query = query.filter(Referral.reward_status == reward_member.value)
        if referrer_subscriber_id:
            query = query.filter(
                Referral.referrer_subscriber_id == coerce_uuid(referrer_subscriber_id)
            )
        return (
            query.order_by(Referral.created_at.desc()).limit(limit).offset(offset).all()
        )

    @staticmethod
    def get(db: Session, referral_id: str) -> Referral:
        return get_or_404(db, Referral, str(referral_id), "Referral not found")

    # ── native portal read/create (legacy response-shape compatible) ────────

    @staticmethod
    def read_for_subscriber(db: Session, subscriber_id: str) -> dict:
        """Native Refer & Earn payload retaining the established portal shape:
        ``{code, share_url, program{…}, totals{…}, referrals[]}``.

        ``reward_status`` surfaces the native vocabulary (``issued`` — mobile
        already tolerates it via reconcile) and ``expired`` rows appear.
        ``GET/POST /me/referrals`` and the portal page call this native owner.
        """
        sid = coerce_uuid(str(subscriber_id))
        code = Referrals.ensure_code(db, str(sid))
        cfg = _config(db)
        rows = (
            db.query(Referral)
            .filter(Referral.referrer_subscriber_id == sid)
            .filter(Referral.is_active.is_(True))
            .order_by(Referral.created_at.desc())
            .all()
        )

        counts: dict[str, int] = {
            "total": 0,
            "pending": 0,
            "qualified": 0,
            "rewarded": 0,
        }
        earned = Decimal("0")
        referrals = []
        for r in rows:
            counts["total"] += 1
            if r.status in counts:
                counts[r.status] += 1
            if r.status == ReferralStatus.rewarded.value:
                earned += r.reward_amount or Decimal("0")
            referrals.append(
                {
                    "id": str(r.id),
                    "status": r.status,
                    "referred_name": _referred_display_name(r),
                    "reward_amount": str(r.reward_amount)
                    if r.reward_amount is not None
                    else None,
                    "reward_currency": r.reward_currency or "NGN",
                    "reward_status": r.reward_status,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "qualified_at": r.qualified_at.isoformat()
                    if r.qualified_at
                    else None,
                }
            )
        return {
            "code": code.code,
            "share_url": share_url(code.code),
            "program": {
                "enabled": bool(cfg["enabled"]),
                "reward_amount": str(cfg["amount"]),
                "reward_currency": cfg["currency"],
            },
            "totals": {**counts, "total_earned": str(earned)},
            "referrals": referrals,
        }

    @staticmethod
    def refer_a_friend(
        db: Session,
        subscriber_id: str,
        *,
        name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        note: str | None = None,
    ) -> dict:
        """Native refer-a-friend, shape-compatible with the mirror's
        write-through response (``{id, status, message}``). PR8 repoints
        ``POST /me/referrals`` and the portal form here."""
        code = Referrals.ensure_code(db, subscriber_id)
        referral = Referrals.capture(
            db,
            code=code.code,
            name=name,
            email=email,
            phone=phone,
            notes=note,
            source="portal",
        )
        return {
            "id": str(referral.id),
            "status": referral.status,
            "message": "Referral submitted",
        }


referrals = Referrals()
