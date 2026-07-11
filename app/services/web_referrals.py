"""Context builders for the admin referrals web pages (Phase 3 §2.6).

Ported from the CRM's ``app/web/admin/crm_referrals.py`` onto sub identity:
referrer/referred are subscribers (not CRM people), rows link to the existing
``/admin/customers/{person|business}/{id}`` detail pages, and the program
lives in the five ``referral_*`` settings keys (``SettingDomain.subscriber``)
surfaced on the system settings page — there is no program table (§1.6).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.referral_native import (
    Referral,
    ReferralRewardStatus,
    ReferralStatus,
)
from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid
from app.services.referrals import referrals as referrals_service

# The five referral_* program keys render on the generic system settings page
# under the subscriber domain (settings_spec gives them labels).
PROGRAM_SETTINGS_URL = "/admin/system/settings?domain=subscriber"

STATUSES = [s.value for s in ReferralStatus]
REWARD_STATUSES = [s.value for s in ReferralRewardStatus]


def _subscriber_name(subscriber: Subscriber | None) -> str:
    if subscriber is None:
        return "—"
    name = (
        subscriber.display_name
        or f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
        or subscriber.email
        or "—"
    )
    return str(name).strip() or "—"


def _subscriber_link(subscriber: Subscriber | None) -> str | None:
    if subscriber is None:
        return None
    kind = "business" if subscriber.is_business else "person"
    return f"/admin/customers/{kind}/{subscriber.id}"


def _capture_meta(referral: Referral) -> dict:
    meta = referral.metadata_ if isinstance(referral.metadata_, dict) else {}
    capture = meta.get("capture")
    return capture if isinstance(capture, dict) else {}


def _reward_display(referral: Referral) -> str:
    amount = referral.reward_amount
    if amount is None:
        return "—"
    currency = (referral.reward_currency or "NGN").strip() or "NGN"
    return f"{currency} {amount:,.2f}"


def _referred_name(referral: Referral) -> str:
    if referral.referred_subscriber is not None:
        return _subscriber_name(referral.referred_subscriber)
    name = _capture_meta(referral).get("name")
    return str(name).strip() if name else "—"


def _row(referral: Referral) -> dict:
    return {
        "id": str(referral.id),
        "referrer": _subscriber_name(referral.referrer),
        "referrer_href": _subscriber_link(referral.referrer),
        "referred": _referred_name(referral),
        "referred_href": _subscriber_link(referral.referred_subscriber),
        "status": referral.status,
        "reward_status": referral.reward_status,
        "reward": _reward_display(referral),
        "source": referral.source or "—",
        "created_at": referral.created_at,
        "qualified_at": referral.qualified_at,
        # Action gates (mirror the service guards so buttons never 409 on a
        # fresh page): qualify rescues pending/expired, issue pays a
        # qualified referral, reject voids anything not yet paid out.
        "can_qualify": referral.status
        in (ReferralStatus.pending.value, ReferralStatus.expired.value),
        "can_issue": referral.status == ReferralStatus.qualified.value
        and referral.reward_status
        in (ReferralRewardStatus.pending.value, ReferralRewardStatus.approved.value),
        "can_reject": referral.status
        in (ReferralStatus.pending.value, ReferralStatus.qualified.value),
    }


def _stats(db: Session) -> dict:
    counts: dict[str, int] = {
        status: count
        for status, count in db.query(Referral.status, func.count(Referral.id))
        .filter(Referral.is_active.is_(True))
        .group_by(Referral.status)
        .all()
    }
    rewarded_total = (
        db.query(func.coalesce(func.sum(Referral.reward_amount), 0))
        .filter(Referral.is_active.is_(True))
        .filter(Referral.status == ReferralStatus.rewarded.value)
        .scalar()
    ) or Decimal("0")
    return {
        "total": sum(counts.values()),
        "pending": counts.get(ReferralStatus.pending.value, 0),
        "qualified": counts.get(ReferralStatus.qualified.value, 0),
        "rewarded": counts.get(ReferralStatus.rewarded.value, 0),
        "rewarded_total": rewarded_total,
    }


def list_data(
    db: Session,
    *,
    status: str | None = None,
    reward_status: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict:
    # Unknown filter values are ignored (filter cleared), matching the
    # service-requests queue idiom — never a 400 from a stale bookmark.
    status = status if status in STATUSES else None
    reward_status = reward_status if reward_status in REWARD_STATUSES else None

    query = (
        db.query(Referral)
        .options(
            joinedload(Referral.referrer),
            joinedload(Referral.referred_subscriber),
        )
        .filter(Referral.is_active.is_(True))
    )
    if status:
        query = query.filter(Referral.status == status)
    if reward_status:
        query = query.filter(Referral.reward_status == reward_status)

    total = query.count()
    total_pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    items = (
        query.order_by(Referral.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return {
        "referrals": [_row(r) for r in items],
        "stats": _stats(db),
        "program": referrals_service.program(db),
        "statuses": STATUSES,
        "reward_statuses": REWARD_STATUSES,
        "status_filter": status,
        "reward_status_filter": reward_status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "program_settings_url": PROGRAM_SETTINGS_URL,
    }


def detail_data(db: Session, *, referral_id: str) -> dict | None:
    try:
        rid = coerce_uuid(str(referral_id))
    except Exception:  # noqa: BLE001 - malformed id → 404, not a 500
        return None
    referral = (
        db.query(Referral)
        .options(
            joinedload(Referral.referrer),
            joinedload(Referral.referred_subscriber),
            joinedload(Referral.code),
            joinedload(Referral.lead),
        )
        .filter(Referral.id == rid)
        .first()
    )
    if referral is None:
        return None
    meta = referral.metadata_ if isinstance(referral.metadata_, dict) else {}
    return {
        "referral": referral,
        "row": _row(referral),
        "capture": _capture_meta(referral),
        "code": referral.code.code if referral.code is not None else None,
        "lead_id": str(referral.referred_lead_id)
        if referral.referred_lead_id
        else None,
        "reward_credit_id": meta.get("reward_credit_id"),
        "program": referrals_service.program(db),
        "program_settings_url": PROGRAM_SETTINGS_URL,
    }
