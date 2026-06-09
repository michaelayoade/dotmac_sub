"""Reclassify standalone public-IP-block *subscriptions* into add-ons.

Historically each extra public IP block a customer bought was imported from
Splynx as its own ``CatalogOffer`` and a standalone ``Subscription`` (≈223 of
them). The right model is an add-on on the customer's real plan, billed on the
same invoice. This module plans and applies that move:

    IP-block Subscription  →  SubscriptionAddOn on the subscriber's main plan
                              (+ the standalone IP subscription is archived)

``build_reclassification_plan`` is READ-ONLY and returns exactly what would
happen, per subscription, including the rows it cannot safely move (no main
plan, no matching add-on, …) and which carry a real allocated IP that needs
provisioning continuity. ``apply_reclassification`` performs it (gated on
``commit``). Idempotent: an already-archived IP subscription is skipped.

Depends on the IP add-ons existing first (run import_addons_from_splynx).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import (
    AddOn,
    CatalogOffer,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber
from app.services.migrations.sync_addons_from_splynx import ip_prefix_length

logger = logging.getLogger(__name__)

_RECLASS_REASON = "reclassified_to_addon"


def _ip_offer_prefixes(db: Session) -> dict[str, int]:
    """Map each IP-block offer id -> its prefix length (e.g. '/29 IP' -> 29)."""
    offers = db.scalars(select(CatalogOffer)).all()
    out: dict[str, int] = {}
    for offer in offers:
        prefix = ip_prefix_length(offer.name or "")
        if prefix is not None:
            out[str(offer.id)] = prefix
    return out


def _addon_by_prefix(db: Session) -> dict[int, AddOn]:
    addons = db.scalars(
        select(AddOn).where(AddOn.ip_is_public.is_(True), AddOn.is_active.is_(True))
    ).all()
    out: dict[int, AddOn] = {}
    for add_on in addons:
        if add_on.ip_prefix_length is not None:
            out.setdefault(int(add_on.ip_prefix_length), add_on)
    return out


def _pick_main_subscription(
    db: Session, subscriber_id, ip_offer_ids: set[str]
) -> tuple[Subscription | None, int]:
    """The subscriber's main plan to attach the IP add-on to: an active
    subscription that isn't itself an IP-block. Returns (chosen, candidate_count);
    chosen is the most recently started candidate."""
    subs = db.scalars(
        select(Subscription).where(
            Subscription.subscriber_id == subscriber_id,
            Subscription.status == SubscriptionStatus.active,
        )
    ).all()
    candidates = [s for s in subs if str(s.offer_id) not in ip_offer_ids]
    if not candidates:
        return None, 0
    candidates.sort(
        key=lambda s: s.start_at or datetime.min.replace(tzinfo=UTC), reverse=True
    )
    return candidates[0], len(candidates)


def build_reclassification_plan(db: Session) -> dict:
    """Read-only: what reclassifying the IP-block subscriptions would do."""
    ip_prefixes = _ip_offer_prefixes(db)
    ip_offer_ids = set(ip_prefixes)
    addon_by_prefix = _addon_by_prefix(db)

    items: list[dict] = []
    if ip_offer_ids:
        ip_subs = db.scalars(
            select(Subscription).where(Subscription.offer_id.in_(ip_offer_ids))
        ).all()
    else:
        ip_subs = []

    for sub in ip_subs:
        prefix = ip_prefixes[str(sub.offer_id)]
        offer = db.get(CatalogOffer, sub.offer_id)
        item: dict = {
            "ip_subscription_id": str(sub.id),
            "subscriber_id": str(sub.subscriber_id),
            "subscriber_name": _subscriber_label(db, sub.subscriber_id),
            "ip_offer_name": offer.name if offer else None,
            "prefix": prefix,
            "ipv4_address": getattr(sub, "ipv4_address", None),
            "decision": "skip",
            "reason": None,
            "target_main_subscription_id": None,
            "target_addon_id": None,
        }

        # Already reclassified (idempotent) or otherwise not active → leave it.
        if sub.status != SubscriptionStatus.active:
            item["reason"] = "not_active"
            items.append(item)
            continue

        add_on = addon_by_prefix.get(prefix)
        if add_on is None:
            item["reason"] = "no_matching_addon"
            items.append(item)
            continue

        main, candidate_count = _pick_main_subscription(
            db, sub.subscriber_id, ip_offer_ids
        )
        if main is None:
            item["reason"] = "no_main_subscription"
            items.append(item)
            continue

        item["decision"] = "reclassify"
        item["reason"] = None
        item["target_main_subscription_id"] = str(main.id)
        item["target_addon_id"] = str(add_on.id)
        item["main_candidates"] = candidate_count
        items.append(item)

    reclass = [i for i in items if i["decision"] == "reclassify"]
    skipped = [i for i in items if i["decision"] == "skip"]
    summary = {
        "ip_subscriptions": len(items),
        "would_reclassify": len(reclass),
        "skipped": len(skipped),
        "skip_reasons": _count_by(skipped, "reason"),
        "by_prefix": _count_by(reclass, "prefix"),
        # rows carrying a real allocated IP need provisioning continuity before
        # the standalone subscription is archived (RADIUS/CPE must keep routing).
        "with_allocated_ip": sum(1 for i in reclass if i.get("ipv4_address")),
        "ambiguous_main_plan": sum(
            1 for i in reclass if i.get("main_candidates", 1) > 1
        ),
    }
    return {"summary": summary, "items": items}


def _count_by(rows: list[dict], key: str) -> dict:
    out: dict = {}
    for r in rows:
        out[r.get(key)] = out.get(r.get(key), 0) + 1
    return out


def apply_reclassification(db: Session, *, commit: bool = True) -> dict:
    """Execute the plan: attach an add-on to each main plan and archive the
    standalone IP subscription. ``commit=False`` only flushes (dry-run)."""
    plan = build_reclassification_plan(db)
    applied = 0
    now = datetime.now(UTC)
    for item in plan["items"]:
        if item["decision"] != "reclassify":
            continue
        main_id = item["target_main_subscription_id"]
        addon_id = item["target_addon_id"]
        # Don't duplicate an existing add-on link.
        exists = db.scalar(
            select(SubscriptionAddOn).where(
                SubscriptionAddOn.subscription_id == main_id,
                SubscriptionAddOn.add_on_id == addon_id,
                SubscriptionAddOn.end_at.is_(None),
            )
        )
        if exists is None:
            db.add(
                SubscriptionAddOn(
                    subscription_id=main_id,
                    add_on_id=addon_id,
                    quantity=1,
                    start_at=now,
                )
            )
        ip_sub = db.get(Subscription, item["ip_subscription_id"])
        if ip_sub is None:
            continue
        ip_sub.status = SubscriptionStatus.archived
        ip_sub.end_at = now
        ip_sub.cancel_reason = f"{_RECLASS_REASON}:{addon_id}"
        applied += 1

    if commit:
        db.commit()
    else:
        db.flush()
    result = {"applied": applied, **plan["summary"]}
    logger.info("ip_reclassification_complete", extra={"summary": result})
    return result


def _subscriber_label(db: Session, subscriber_id) -> str:
    sub = db.get(Subscriber, subscriber_id)
    if sub is None:
        return str(subscriber_id)
    name = f"{sub.first_name or ''} {sub.last_name or ''}".strip()
    return name or sub.email or str(subscriber_id)
