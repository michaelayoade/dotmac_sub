"""Local mirror of CRM referral data (RFC #73).

All DB + CRM access for the Refer & Earn feature lives here so the API/web
wrappers stay thin (see tests/architecture/test_thin_wrappers). The CRM is the
source of truth; this keeps a read-optimised local copy hydrated by:

  * CRM webhooks (referral.captured / qualified / rewarded) — near-real-time, and
  * a periodic reconcile pull + lazy on-view refresh — the backstop.

Reads are served from the mirror, so the app/web render instantly and survive a
CRM outage. Writes (refer-a-friend, reward payout) go to the CRM/billing source
of truth and are reflected back into the mirror.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.referral import ReferralMirror, ReferralProgramCache
from app.models.subscriber import Subscriber
from app.services import crm_api
from app.services.common import coerce_uuid
from app.services.crm_client import CRMClientError, get_crm_client
from app.services.crm_portal import resolve_crm_subscriber_id

logger = logging.getLogger(__name__)

_DEFAULT_REFRESH_TTL_SECONDS = 600  # lazy on-view reconcile cadence


# ── parsing helpers ────────────────────────────────────────────────────────


def _to_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _to_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# ── mirror upserts ─────────────────────────────────────────────────────────


def _upsert_row(
    db: Session,
    *,
    subscriber_id,
    crm_referral_id: str,
    referred_name: str | None,
    status: str | None,
    reward_amount: Decimal | None,
    reward_currency: str | None,
    reward_status: str | None,
    referral_created_at: datetime | None,
    qualified_at: datetime | None,
    rewarded_at: datetime | None,
) -> ReferralMirror:
    row = db.scalar(
        select(ReferralMirror).where(ReferralMirror.crm_referral_id == crm_referral_id)
    )
    if row is None:
        row = ReferralMirror(
            crm_referral_id=crm_referral_id, subscriber_id=subscriber_id
        )
        db.add(row)
    row.subscriber_id = subscriber_id
    if referred_name is not None:
        row.referred_name = referred_name
    if status:
        row.status = status
    if reward_amount is not None:
        row.reward_amount = reward_amount
    if reward_currency:
        row.reward_currency = reward_currency
    if reward_status:
        row.reward_status = reward_status
    if referral_created_at is not None:
        row.referral_created_at = referral_created_at
    if qualified_at is not None:
        row.qualified_at = qualified_at
    if rewarded_at is not None:
        row.rewarded_at = rewarded_at
    return row


def _local_subscriber_for_crm(db: Session, crm_subscriber_id: str) -> Subscriber | None:
    try:
        crm_uuid = coerce_uuid(crm_subscriber_id)
    except (ValueError, TypeError):
        return None
    return db.scalar(select(Subscriber).where(Subscriber.crm_subscriber_id == crm_uuid))


# ── reconcile (pull) ───────────────────────────────────────────────────────


def reconcile_subscriber(db: Session, subscriber_id: str) -> bool:
    """Pull the subscriber's referrals from the CRM into the mirror.

    Returns True on a successful sync, False if the account isn't CRM-linked.
    Raises CRMClientError if the CRM is unreachable (callers decide whether to
    serve stale data).
    """
    crm_subscriber_id = resolve_crm_subscriber_id(db, str(subscriber_id))
    if not crm_subscriber_id:
        return False

    data = get_crm_client().get_portal_referrals(crm_subscriber_id)
    sub_uuid = coerce_uuid(str(subscriber_id))

    program = data.get("program") or {}
    cache = db.get(ReferralProgramCache, sub_uuid)
    if cache is None:
        cache = ReferralProgramCache(subscriber_id=sub_uuid, code="", share_url="")
        db.add(cache)
    cache.code = str(data.get("code") or cache.code or "")
    cache.share_url = str(data.get("share_url") or cache.share_url or "")
    cache.program_enabled = bool(program.get("enabled", True))
    cache.reward_amount = _to_decimal(program.get("reward_amount"))
    cache.reward_currency = str(program.get("reward_currency") or "NGN")
    cache.synced_at = datetime.now(UTC)

    for item in data.get("referrals") or []:
        crm_referral_id = str(item.get("id") or "").strip()
        if not crm_referral_id:
            continue
        _upsert_row(
            db,
            subscriber_id=sub_uuid,
            crm_referral_id=crm_referral_id,
            referred_name=item.get("referred_name"),
            status=item.get("status"),
            reward_amount=_to_decimal(item.get("reward_amount")),
            reward_currency=item.get("reward_currency"),
            reward_status=item.get("reward_status"),
            referral_created_at=_to_dt(item.get("created_at")),
            qualified_at=_to_dt(item.get("qualified_at")),
            rewarded_at=None,
        )
    db.commit()
    return True


def reconcile_all(db: Session, *, stale_after_seconds: int = 3600) -> int:
    """Reconcile subscribers whose mirror is stale (periodic task). Returns the
    count reconciled. Per-subscriber failures are logged and skipped."""
    cutoff = datetime.now(UTC) - timedelta(seconds=max(60, stale_after_seconds))
    stale = db.scalars(
        select(ReferralProgramCache.subscriber_id).where(
            ReferralProgramCache.synced_at < cutoff
        )
    ).all()
    done = 0
    for subscriber_id in stale:
        try:
            if reconcile_subscriber(db, str(subscriber_id)):
                done += 1
        except CRMClientError as exc:
            db.rollback()
            logger.warning(
                "referral_reconcile_failed subscriber=%s: %s", subscriber_id, exc
            )
    return done


# ── reads ──────────────────────────────────────────────────────────────────


def read_for_subscriber(
    db: Session,
    subscriber_id: str,
    *,
    refresh_ttl_seconds: int = _DEFAULT_REFRESH_TTL_SECONDS,
) -> dict:
    """Build the Refer & Earn payload from the local mirror, lazily refreshing
    from the CRM when the cache is missing or stale (best-effort)."""
    sub_uuid = coerce_uuid(str(subscriber_id))
    cache = db.get(ReferralProgramCache, sub_uuid)
    cutoff = datetime.now(UTC) - timedelta(seconds=max(0, refresh_ttl_seconds))
    is_stale = cache is None or (cache.synced_at and cache.synced_at < cutoff)
    if is_stale:
        try:
            reconcile_subscriber(db, str(subscriber_id))
            cache = db.get(ReferralProgramCache, sub_uuid)
        except CRMClientError as exc:
            # Serve whatever we have; the mirror is the resilience layer.
            db.rollback()
            logger.warning(
                "referral_lazy_refresh_failed subscriber=%s: %s", sub_uuid, exc
            )

    rows = db.scalars(
        select(ReferralMirror)
        .where(ReferralMirror.subscriber_id == sub_uuid)
        .order_by(ReferralMirror.created_at.desc())
    ).all()

    totals = {"total": 0, "pending": 0, "qualified": 0, "rewarded": 0}
    earned = Decimal("0")
    referrals = []
    for r in rows:
        totals["total"] += 1
        if r.status in totals:
            totals[r.status] += 1
        if r.status == "rewarded":
            earned += r.reward_amount or Decimal("0")
        referrals.append(
            {
                "id": r.crm_referral_id,
                "status": r.status,
                "referred_name": r.referred_name,
                "reward_amount": str(r.reward_amount)
                if r.reward_amount is not None
                else None,
                "reward_currency": r.reward_currency,
                "reward_status": r.reward_status,
                "created_at": r.referral_created_at.isoformat()
                if r.referral_created_at
                else None,
                "qualified_at": r.qualified_at.isoformat() if r.qualified_at else None,
            }
        )
    totals["total_earned"] = str(earned)

    return {
        "code": cache.code if cache else "",
        "share_url": cache.share_url if cache else "",
        "program": {
            "enabled": bool(cache.program_enabled) if cache else False,
            "reward_amount": str(cache.reward_amount)
            if cache and cache.reward_amount is not None
            else "0",
            "reward_currency": cache.reward_currency if cache else "NGN",
        },
        "totals": totals,
        "referrals": referrals,
    }


# ── writes ─────────────────────────────────────────────────────────────────


class ReferralError(Exception):
    """Domain error for refer-a-friend (maps to a 4xx at the edge)."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def refer_a_friend(
    db: Session,
    subscriber_id: str,
    *,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    note: str | None = None,
) -> dict:
    """Write-through refer-a-friend: capture in the CRM, then mirror locally."""
    email = (email or "").strip() or None
    phone = (phone or "").strip() or None
    if not email and not phone:
        raise ReferralError("An email or phone number is required.", status_code=422)

    crm_subscriber_id = resolve_crm_subscriber_id(db, str(subscriber_id))
    if not crm_subscriber_id:
        raise ReferralError("Account is not yet linked to the CRM.", status_code=409)

    try:
        result = get_crm_client().create_portal_referral(
            crm_subscriber_id, name=name, email=email, phone=phone, note=note
        )
    except CRMClientError as exc:
        raise ReferralError(
            "Couldn't submit the referral right now.", status_code=502
        ) from exc

    crm_referral_id = str(result.get("id") or "").strip()
    if crm_referral_id:
        _upsert_row(
            db,
            subscriber_id=coerce_uuid(str(subscriber_id)),
            crm_referral_id=crm_referral_id,
            referred_name=name,
            status=str(result.get("status") or "pending"),
            reward_amount=None,
            reward_currency=None,
            reward_status=None,
            referral_created_at=_to_dt(result.get("created_at")) or datetime.now(UTC),
            qualified_at=None,
            rewarded_at=None,
        )
        db.commit()
    return {
        "id": crm_referral_id,
        "status": str(result.get("status") or "pending"),
        "message": str(result.get("message") or "Referral submitted"),
    }


# ── webhook application ─────────────────────────────────────────────────────


def apply_webhook(db: Session, event_type: str, body: dict) -> dict:
    """Apply a CRM referral lifecycle event to the mirror (and pay rewards).

    Returns a small status dict for the webhook response. Acks (ignores)
    unmapped/incomplete events so the CRM doesn't retry forever.
    """
    crm_subscriber_id = str(body.get("crm_subscriber_id") or "").strip()
    crm_referral_id = str(body.get("referral_id") or body.get("id") or "").strip()
    if not crm_subscriber_id or not crm_referral_id:
        return {"status": "ignored", "reason": "incomplete_payload"}

    subscriber = _local_subscriber_for_crm(db, crm_subscriber_id)
    if subscriber is None:
        logger.warning(
            "crm_referral_event_unmapped event=%s crm_subscriber_id=%s referral_id=%s",
            event_type,
            crm_subscriber_id,
            crm_referral_id,
        )
        return {"status": "ignored", "reason": "unmapped_subscriber"}

    status_map = {
        "referral.captured": "pending",
        "referral.qualified": "qualified",
        "referral.rewarded": "rewarded",
    }
    new_status = status_map.get(event_type)
    amount = _to_decimal(body.get("amount") or body.get("reward_amount"))
    currency = str(body.get("currency") or body.get("reward_currency") or "NGN")
    now = datetime.now(UTC)

    credit_id: str | None = None
    if event_type == "referral.rewarded":
        if amount is None or amount <= 0:
            return {"status": "ignored", "reason": "non_positive_amount"}
        try:
            credit = crm_api.create_account_credit(
                db,
                subscriber_id=str(subscriber.id),
                amount=amount,
                reason=str(body.get("reason") or "Referral reward"),
                external_ref=f"referral:{crm_referral_id}",
                currency=currency,
            )
            credit_id = str(credit.id)
        except LookupError:
            return {"status": "ignored", "reason": "subscriber_not_creditable"}

    _upsert_row(
        db,
        subscriber_id=subscriber.id,
        crm_referral_id=crm_referral_id,
        referred_name=body.get("referred_name"),
        status=new_status,
        reward_amount=amount if event_type == "referral.rewarded" else None,
        reward_currency=currency if amount is not None else None,
        reward_status="paid" if event_type == "referral.rewarded" else None,
        referral_created_at=_to_dt(body.get("created_at")),
        qualified_at=now if event_type == "referral.qualified" else None,
        rewarded_at=now if event_type == "referral.rewarded" else None,
    )
    db.commit()

    if event_type == "referral.rewarded":
        # Best-effort nudge; never let a push failure undo the committed credit.
        try:
            from app.services import push as push_service

            push_service.send_push(
                db,
                str(subscriber.id),
                title="You earned a referral reward!",
                body=f"{currency} {amount} has been credited to your account.",
                data={"type": "referral_reward", "referral_id": crm_referral_id},
            )
        except Exception as exc:  # noqa: BLE001 - notification is advisory
            logger.warning(
                "referral_reward_push_failed referral_id=%s: %s", crm_referral_id, exc
            )

    result = {"status": "ok", "event": event_type}
    if credit_id:
        result["credit_id"] = credit_id
    return result
