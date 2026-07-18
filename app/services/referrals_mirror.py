"""Read-only compatibility view of historical CRM referral mirror rows.

Native Sub referral services own all customer reads, writes, qualification, and
rewards. This module performs no CRM request, webhook mutation, refresh enqueue,
or lifecycle decision. It remains only for controlled migration comparison and
can be deleted after historical parity/retention decisions are complete.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.referral import ReferralMirror, ReferralProgramCache
from app.services.common import coerce_uuid


def read_for_subscriber(
    db: Session,
    subscriber_id: str,
    *,
    refresh_ttl_seconds: int | None = None,
) -> dict:
    """Read already-stored historical mirror rows without external activity."""

    del refresh_ttl_seconds  # retired compatibility argument; never triggers I/O
    sub_uuid = coerce_uuid(str(subscriber_id))
    cache = db.get(ReferralProgramCache, sub_uuid)
    rows = db.scalars(
        select(ReferralMirror)
        .where(ReferralMirror.subscriber_id == sub_uuid)
        .order_by(ReferralMirror.created_at.desc())
    ).all()

    counts: dict[str, int] = {
        "total": 0,
        "pending": 0,
        "qualified": 0,
        "rewarded": 0,
    }
    earned = Decimal("0")
    referrals = []
    for referral in rows:
        counts["total"] += 1
        if referral.status in counts:
            counts[referral.status] += 1
        if referral.status == "rewarded":
            earned += referral.reward_amount or Decimal("0")
        referrals.append(
            {
                "id": referral.crm_referral_id,
                "status": referral.status,
                "referred_name": referral.referred_name,
                "reward_amount": str(referral.reward_amount)
                if referral.reward_amount is not None
                else None,
                "reward_currency": referral.reward_currency,
                "reward_status": referral.reward_status,
                "created_at": referral.referral_created_at.isoformat()
                if referral.referral_created_at
                else None,
                "qualified_at": referral.qualified_at.isoformat()
                if referral.qualified_at
                else None,
            }
        )
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
        "totals": {**counts, "total_earned": str(earned)},
        "referrals": referrals,
    }
