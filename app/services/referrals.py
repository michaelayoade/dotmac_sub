"""Native referral program (Phase 3 §2.1) — the CRM's ``services/crm/referrals.py``
merged onto sub identity and billing.

Closed loop: an active subscriber gets a code → a prospect captures via that code
(creating an attributed lead) → the referral qualifies when the prospect becomes
an active subscriber → the referrer earns a configurable account credit.

Deltas from the CRM source (Phase 3 §1.6/§2.1):

* ``person_identity.resolve_person`` → sub's ``customer_identity_resolution``
  cascade (doc 02 §3.2). A captured prospect that matches no subscriber gets a
  prospect subscriber row (``status='new'``, ``party_status='lead'``) — the
  party model the Phase 3 backfill also produces.
* ``qualify_for_subscriber`` re-hooks from the CRM's customer-sync path onto
  sub's subscriber-activation lifecycle event
  (``app/services/events/handlers/referral.py``). It only flushes — event
  handlers must never commit the caller's open transaction.
* ``issue_reward``'s ``selfcare.create_account_credit`` HTTP hop becomes a
  direct in-process call to the wallet credit service behind sub's
  ``POST /crm/credits`` handler (``crm_api.credit_referral_reward_to_wallet``),
  with the SAME idempotency key ``external_ref="referral:{id}"`` — a reward the
  CRM already paid pre-cutover can never be paid twice, and the
  ``with_for_update`` row lock is kept.
* The mirror's "You earned a referral reward!" push moves into ``issue_reward``
  (the mirror webhook path dies at contract, §3.3).

The five ``referral_*`` program settings keys migrate into sub settings
(``SettingDomain.subscriber``); there is no program table (§1.6).
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.referral_native import (
    Referral,
    ReferralCode,
    ReferralRewardStatus,
    ReferralStatus,
)
from app.models.sales import Lead, LeadStatus
from app.models.subscriber import PartyStatus, Subscriber, SubscriberStatus
from app.services import settings_spec
from app.services.common import coerce_uuid, get_or_404, validate_enum
from app.services.customer_identity_normalization import (
    default_country_code,
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.customer_identity_resolution import resolve_customer_identity

logger = logging.getLogger(__name__)

# Unambiguous alphabet (no 0/O/1/I) so codes are easy to share verbally.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8
_REFERRAL_LEAD_SOURCE = "Referral"
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

    ``PORTAL_REFERRAL_SHARE_BASE`` already defaults to sub's own domain (§2.4).
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


def _resolve_prospect_subscriber(
    db: Session, *, email: str | None, phone: str | None
) -> Subscriber | None:
    """Resolve a captured prospect to an existing subscriber via the identity
    cascade (doc 02 §3.2) — email first, then phone. Ambiguous matches resolve
    to None (the capture metadata dedup guard below keeps repeats idempotent)."""
    for identifier, hint in ((email, "email"), (phone, "phone")):
        if not identifier:
            continue
        resolution = resolve_customer_identity(db, identifier, channel_hint=hint)
        if resolution.matched and resolution.subscriber_id is not None:
            subscriber = db.get(Subscriber, resolution.subscriber_id)
            if subscriber is not None:
                return subscriber
    return None


def _capture_meta(referral: Referral) -> dict:
    meta = referral.metadata_ if isinstance(referral.metadata_, dict) else {}
    capture = meta.get("capture")
    return capture if isinstance(capture, dict) else {}


def _existing_referral_by_capture_contact(
    db: Session, *, email: str | None, phone: str | None
) -> Referral | None:
    """Fallback idempotent-capture guard when identity resolution can't pin a
    subscriber (unmatched/ambiguous): match the normalized capture email/phone
    stored in referral metadata against open (pending/qualified) referrals."""
    norm_email, norm_phone = _normalized_contact(db, email, phone)
    if not norm_email and not norm_phone:
        return None
    candidates = (
        db.query(Referral)
        .filter(Referral.is_active.is_(True))
        .filter(
            Referral.status.in_(
                [ReferralStatus.pending.value, ReferralStatus.qualified.value]
            )
        )
        .all()
    )
    for referral in candidates:
        capture = _capture_meta(referral)
        cap_email, cap_phone = _normalized_contact(
            db, capture.get("email"), capture.get("phone")
        )
        if norm_email and cap_email and norm_email == cap_email:
            return referral
        if norm_phone and cap_phone and norm_phone == cap_phone:
            return referral
    return None


def _split_display_name(name: str | None) -> tuple[str, str]:
    parts = [p for p in str(name or "").strip().split() if p]
    if not parts:
        return "Referred", "Prospect"
    if len(parts) == 1:
        return parts[0][:80], "Prospect"
    return parts[0][:80], " ".join(parts[1:])[:80]


def _create_prospect_subscriber(
    db: Session,
    *,
    name: str | None,
    email: str | None,
    phone: str | None,
) -> Subscriber:
    """Materialize a referred prospect as a party row (§1.9): ``status='new'``
    keeps it out of billing/RADIUS sweeps, ``party_status='lead'`` marks it a
    prospect. Email stays non-unique by design (shared/family emails)."""
    first, last = _split_display_name(name)
    prospect = Subscriber(
        first_name=first,
        last_name=last,
        display_name=(str(name).strip()[:120] or None) if name else None,
        email=(email or "").strip(),
        phone=(phone or "").strip() or None,
        status=SubscriberStatus.new,
        party_status=PartyStatus.lead.value,
    )
    db.add(prospect)
    db.flush()
    return prospect


def _referred_display_name(referral: Referral) -> str | None:
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
        """Record a referred prospect: resolve (or create) their subscriber
        party row, an attributed lead, and a pending Referral. Idempotent per
        referred prospect (unique guard + capture-contact fallback)."""
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

        referred = _resolve_prospect_subscriber(db, email=email, phone=phone)
        if referred is not None:
            if referred.id == ref_code.subscriber_id:
                raise HTTPException(status_code=409, detail="You can't refer yourself.")
            if referred.status == SubscriberStatus.active and referred.is_active:
                raise HTTPException(
                    status_code=409, detail="That person is already an active customer."
                )
            existing = (
                db.query(Referral)
                .filter(Referral.referred_subscriber_id == referred.id)
                .filter(Referral.is_active.is_(True))
                .first()
            )
            if existing is not None:
                return existing
        else:
            existing = _existing_referral_by_capture_contact(
                db, email=email, phone=phone
            )
            if existing is not None:
                return existing
            referred = _create_prospect_subscriber(
                db, name=name, email=email, phone=phone
            )

        lead = Lead(
            subscriber_id=referred.id,
            title=f"Referral: {referred.display_name or email or phone}",
            status=LeadStatus.new.value,
            lead_source=_REFERRAL_LEAD_SOURCE,
            region=region,
            address=address,
            notes=notes,
            metadata_={
                "referral_code": ref_code.code,
                "referrer_subscriber_id": str(ref_code.subscriber_id),
            },
        )
        db.add(lead)
        db.flush()

        referral = Referral(
            referrer_subscriber_id=ref_code.subscriber_id,
            referral_code_id=ref_code.id,
            referred_subscriber_id=referred.id,
            referred_lead_id=lead.id,
            status=ReferralStatus.pending.value,
            reward_currency=cfg["currency"],
            source=source,
            metadata_={"capture": {"name": name, "email": email, "phone": phone}},
        )
        db.add(referral)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = (
                db.query(Referral)
                .filter(Referral.referred_subscriber_id == referred.id)
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
            referred.id,
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

        # Any active referral already pinned to this subscriber wins (the
        # partial unique guard allows at most one).
        referral = (
            db.query(Referral)
            .filter(Referral.referred_subscriber_id == subscriber.id)
            .filter(Referral.is_active.is_(True))
            .first()
        )
        if referral is None:
            # The signup flow may have created a fresh subscriber row instead
            # of reusing the capture-time prospect row — bridge by identity
            # (the CRM got this for free from person_identity.resolve_person).
            referral = _existing_referral_by_capture_contact(
                db, email=subscriber.email, phone=subscriber.phone
            )
            if referral is not None and referral.status == ReferralStatus.pending.value:
                if referral.referrer_subscriber_id == subscriber.id:
                    return None  # self-referral via shared contact — never qualify
                referral.referred_subscriber_id = subscriber.id
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
    def issue_reward(db: Session, referral_id: str) -> Referral:
        """Apply the referrer's reward as a wallet credit and mark the referral
        rewarded.

        The credit goes through ``crm_api.credit_referral_reward_to_wallet`` —
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
            entry = crm_api.credit_referral_reward_to_wallet(
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
        # Best-effort nudge (moved here from the mirror's webhook path, §2.1).
        try:
            from app.services import push as push_service

            push_service.send_push(
                db,
                str(referral.referrer_subscriber_id),
                title="You earned a referral reward!",
                body=f"{currency} {amount} has been added to your wallet.",
                data={"type": "referral_reward", "referral_id": str(referral.id)},
            )
        except Exception as exc:  # noqa: BLE001 - notification is advisory
            logger.warning(
                "referral_reward_push_failed referral_id=%s: %s", referral.id, exc
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
        referrer_subscriber_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Referral]:
        query = db.query(Referral).filter(Referral.is_active.is_(True))
        if status:
            member = validate_enum(status, ReferralStatus, "status")
            query = query.filter(Referral.status == member.value)
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

    # ── portal read/create (mirror-shape compatible, §2.5) ─────────────────

    @staticmethod
    def read_for_subscriber(db: Session, subscriber_id: str) -> dict:
        """Native Refer & Earn payload, shape-compatible with
        ``referrals_mirror.read_for_subscriber`` (the §2.5 contract):
        ``{code, share_url, program{…}, totals{…}, referrals[]}``.

        ``reward_status`` surfaces the native vocabulary (``issued`` — mobile
        already tolerates it via reconcile) and ``expired`` rows appear (§1.7).
        PR8 repoints ``GET /me/referrals`` and the portal page here.
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
