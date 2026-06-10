"""Merge duplicate CRM subscriber records (erpnext copies → splynx primaries).

The CRM holds ~4.5k customers twice: a splynx-sourced record (our primary
crm_subscriber_id link) and an erpnext-sourced copy whose id we keep in
metadata crm_alias_ids. Tickets mostly hang off the erpnext copies. This
merge re-points each alias's tickets and work orders to the primary record,
soft-deletes the alias in the CRM, and retires the local alias entry
(moved to crm_merged_alias_ids for audit).

Safety: only erpnext-sourced aliases are merged; anything else is skipped
and reported. Dry-run counts every move without writing. DESTRUCTIVE on the
CRM (soft-delete) — run live only with explicit operator approval.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.services.crm_client import CRMClient, CRMClientError, get_crm_client

logger = logging.getLogger(__name__)

_PAGE = 100


def _paged(fetch, **kwargs) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = fetch(limit=_PAGE, offset=offset, **kwargs)
        if not batch:
            break
        items.extend(batch)
        if len(batch) < _PAGE:
            break
        offset += len(batch)
    return items


def _list_alias_tickets(client: CRMClient, alias_id: str) -> list[dict[str, Any]]:
    return _paged(
        lambda limit, offset: client.list_tickets(
            subscriber_id=alias_id, limit=limit, offset=offset, use_cache=False
        )
    )


def _list_alias_work_orders(client: CRMClient, alias_id: str) -> list[dict[str, Any]]:
    # list_work_orders has no offset param in the client; single page of 100
    # is comfortably above observed per-subscriber volumes.
    return client.list_work_orders(subscriber_id=alias_id) or []


def merge_alias(
    db: Session,
    client: CRMClient,
    subscriber: Subscriber,
    alias_id: str,
    *,
    dry_run: bool,
    stats: dict[str, int],
) -> bool:
    """Merge one alias CRM record into the subscriber's primary. True if merged."""
    primary_id = str(subscriber.crm_subscriber_id)
    try:
        alias = client.get_subscriber(alias_id)
    except CRMClientError:
        stats["alias_missing"] += 1
        return False
    if str(alias.get("external_system") or "").lower() != "erpnext":
        stats["alias_not_erpnext"] += 1
        return False

    tickets = _list_alias_tickets(client, alias_id)
    work_orders = _list_alias_work_orders(client, alias_id)
    stats["tickets_moved"] += len(tickets)
    stats["work_orders_moved"] += len(work_orders)
    if dry_run:
        stats["merged"] += 1
        return False

    for ticket in tickets:
        client.update_ticket(str(ticket["id"]), {"subscriber_id": primary_id})
    for work_order in work_orders:
        client.update_work_order(str(work_order["id"]), {"subscriber_id": primary_id})
    client.delete_subscriber(alias_id)
    stats["merged"] += 1
    return True


def merge_duplicates(
    db: Session,
    *,
    client: CRMClient | None = None,
    dry_run: bool = True,
    limit: int | None = None,
) -> dict[str, int]:
    """Merge erpnext-duplicate CRM records for all alias-carrying subscribers."""
    client = client or get_crm_client()
    stats = {
        "subscribers": 0,
        "merged": 0,
        "tickets_moved": 0,
        "work_orders_moved": 0,
        "alias_missing": 0,
        "alias_not_erpnext": 0,
        "errors": 0,
    }

    subscribers = (
        db.query(Subscriber)
        .filter(
            Subscriber.crm_subscriber_id.isnot(None),
            Subscriber.metadata_.isnot(None),
        )
        .order_by(Subscriber.id)
        .all()
    )
    processed = 0
    for subscriber in subscribers:
        metadata = dict(subscriber.metadata_ or {})
        aliases = [str(a) for a in metadata.get("crm_alias_ids") or []]
        if not aliases:
            continue
        if limit is not None and processed >= limit:
            break
        processed += 1
        stats["subscribers"] += 1

        remaining: list[str] = []
        merged_now: list[str] = []
        for alias_id in aliases:
            try:
                merged = merge_alias(
                    db, client, subscriber, alias_id, dry_run=dry_run, stats=stats
                )
            except CRMClientError as exc:
                stats["errors"] += 1
                logger.warning("merge failed alias=%s: %s", alias_id, exc)
                remaining.append(alias_id)
                continue
            (merged_now if merged else remaining).append(alias_id)
            if not merged and dry_run:
                # dry-run keeps everything in place
                pass

        if not dry_run and merged_now:
            metadata["crm_alias_ids"] = remaining or None
            merged_history = [
                str(a) for a in metadata.get("crm_merged_alias_ids") or []
            ]
            metadata["crm_merged_alias_ids"] = merged_history + merged_now
            subscriber.metadata_ = {k: v for k, v in metadata.items() if v is not None}
            db.commit()

    return stats
