"""Merge duplicate CRM subscriber records (erpnext copies -> imported primaries).

The CRM holds ~4.5k customers twice: an imported primary record (our primary
crm_subscriber_id link) and an erpnext-sourced copy whose id we keep in
metadata crm_alias_ids. Tickets mostly hang off the erpnext copies. This
merge re-points each alias's tickets to the primary record, soft-deletes the
alias in the CRM, and retires the local alias entry (moved to
crm_merged_alias_ids for audit).

Work orders (Phase 2 flip): sub is the work-order system-of-record, so
CRM-side work-order reassignment stopped at the WO flip — the merge no longer
lists or updates CRM work orders. Instead, any local ``work_order_mirror``
rows attached to a duplicate local subscriber still linked to the alias CRM id
are re-pointed natively to the primary subscriber
(``work_order_mirror.subscriber_id``). CRM work-order history for merged
aliases stays frozen in the CRM (archive posture).

Safety: only erpnext-sourced aliases are merged; anything else is skipped
and reported. Dry-run counts every move without writing. DESTRUCTIVE on the
CRM (soft-delete) — run live only with explicit operator approval.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.work_order_mirror import WorkOrderMirror
from app.services.common import coerce_uuid
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


def _alias_local_work_order_rows(db: Session, alias_id: str) -> list[WorkOrderMirror]:
    """Local mirror rows hanging off a duplicate local subscriber row that is
    still linked to the alias CRM id (historical duplicate-link residue)."""
    try:
        alias_uuid = coerce_uuid(alias_id)
    except (TypeError, ValueError):
        return []
    if alias_uuid is None:
        return []
    alias_subscriber_ids = db.scalars(
        select(Subscriber.id).where(Subscriber.crm_subscriber_id == alias_uuid)
    ).all()
    if not alias_subscriber_ids:
        return []
    return list(
        db.scalars(
            select(WorkOrderMirror).where(
                WorkOrderMirror.subscriber_id.in_(alias_subscriber_ids)
            )
        ).all()
    )


def merge_alias(
    db: Session,
    client: CRMClient,
    subscriber_id,
    primary_id: str,
    alias_id: str,
    *,
    dry_run: bool,
    stats: dict[str, int],
) -> bool:
    """Merge one alias CRM record into the primary. True if merged (live)."""
    try:
        alias = client.get_subscriber(alias_id)
    except CRMClientError:
        stats["alias_missing"] += 1
        return False
    if str(alias.get("external_system") or "").lower() != "erpnext":
        stats["alias_not_erpnext"] += 1
        return False

    tickets = _list_alias_tickets(client, alias_id)
    work_order_rows = _alias_local_work_order_rows(db, alias_id)
    stats["tickets_moved"] += len(tickets)
    stats["work_orders_moved"] += len(work_order_rows)
    if dry_run:
        stats["merged"] += 1
        return False

    for ticket in tickets:
        client.update_ticket(str(ticket["id"]), {"subscriber_id": primary_id})
    # Native work-order reassignment (sub is the work-order SoT — no CRM
    # write): re-point the mirror rows to the primary local subscriber.
    for row in work_order_rows:
        row.subscriber_id = subscriber_id
    if work_order_rows:
        db.commit()
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

    # Materialize plain rows and end the transaction: the CRM-heavy loop can
    # run for an hour, and an idle-in-transaction connection gets killed by
    # the server (observed live). pool_pre_ping reconnects per live write.
    rows = [
        (row_id, str(crm_id), [str(a) for a in (md or {}).get("crm_alias_ids") or []])
        for row_id, crm_id, md in (
            db.query(Subscriber.id, Subscriber.crm_subscriber_id, Subscriber.metadata_)
            .filter(
                Subscriber.crm_subscriber_id.isnot(None),
                Subscriber.metadata_.isnot(None),
            )
            .order_by(Subscriber.id)
            .all()
        )
    ]
    db.commit()

    processed = 0
    for subscriber_id, primary_crm_id, aliases in rows:
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
                    db,
                    client,
                    subscriber_id,
                    primary_crm_id,
                    alias_id,
                    dry_run=dry_run,
                    stats=stats,
                )
            except CRMClientError as exc:
                stats["errors"] += 1
                logger.warning("merge failed alias=%s: %s", alias_id, exc)
                remaining.append(alias_id)
                continue
            (merged_now if merged else remaining).append(alias_id)

        if not dry_run and merged_now:
            # Fresh short transaction per write; pre_ping revives stale conns.
            subscriber = db.get(Subscriber, subscriber_id)
            if subscriber is None:
                continue
            metadata = dict(subscriber.metadata_ or {})
            metadata["crm_alias_ids"] = remaining or None
            merged_history = [
                str(a) for a in metadata.get("crm_merged_alias_ids") or []
            ]
            metadata["crm_merged_alias_ids"] = merged_history + merged_now
            subscriber.metadata_ = {k: v for k, v in metadata.items() if v is not None}
            db.commit()

    return stats
