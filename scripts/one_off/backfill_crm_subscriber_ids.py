"""One-time backfill of subscribers.crm_subscriber_id from the CRM.

Two passes over the CRM subscriber list:

1. external_system=splynx — authoritative: the CRM record's external_id is a
   Splynx customer id, matched against subscribers.splynx_customer_id.
2. external_system=erpnext — best-effort: the CRM record's external_id is a
   customer name; matched against local display/full/company name only when
   exactly one active local subscriber matches. Ambiguous names are skipped
   and reported, never guessed.

Idempotent: subscribers that already have crm_subscriber_id are left alone,
and a CRM id is never assigned to two local subscribers (partial unique
index). Run with --dry-run first to see the match counts.
"""

from __future__ import annotations

import argparse
import logging
from uuid import UUID

from app.db import SessionLocal
from app.models.subscriber import Subscriber
from app.services.crm_client import CRMClient, get_crm_client

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
_MAX_PAGES = 500


def _iter_crm_subscribers(client: CRMClient, external_system: str):
    for page in range(1, _MAX_PAGES + 1):
        items = client.list_subscribers(
            external_system=external_system,
            page=page,
            per_page=_PAGE_SIZE,
            use_cache=False,
        )
        if not items:
            return
        yield from items
        if len(items) < _PAGE_SIZE:
            return


def _coerce_crm_uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def backfill(db, client: CRMClient, *, dry_run: bool = False) -> dict[str, int]:
    stats = {
        "splynx_linked": 0,
        "erpnext_linked": 0,
        "alias_linked": 0,
        "already_linked": 0,
        "conflicts": 0,
        "ambiguous_names": 0,
        "unmatched": 0,
    }

    taken_crm_ids: set[UUID] = {
        crm_id
        for (crm_id,) in db.query(Subscriber.crm_subscriber_id).filter(
            Subscriber.crm_subscriber_id.isnot(None)
        )
    }
    # Mirrors would-be column state so dry runs see the same collisions a live
    # run does (the CRM holds many customers twice: splynx + erpnext records).
    assigned: dict[UUID, UUID] = {}

    def _add_alias(subscriber: Subscriber, crm_id: UUID) -> bool:
        metadata = dict(subscriber.metadata_ or {})
        aliases = [str(a) for a in metadata.get("crm_alias_ids") or []]
        if str(crm_id) in aliases:
            return False
        aliases.append(str(crm_id))
        if not dry_run:
            metadata["crm_alias_ids"] = aliases
            subscriber.metadata_ = metadata
        return True

    def link(subscriber: Subscriber, crm_id: UUID, bucket: str) -> None:
        current = subscriber.crm_subscriber_id or assigned.get(subscriber.id)
        if current:
            if current == crm_id:
                stats["already_linked"] += 1
            elif _add_alias(subscriber, crm_id):
                # Same customer, second CRM record: keep as an alias so
                # tickets attached to either CRM record map locally.
                stats["alias_linked"] += 1
            else:
                stats["already_linked"] += 1
            return
        if crm_id in taken_crm_ids:
            stats["conflicts"] += 1
            logger.warning(
                "CRM id %s already linked to another subscriber; skipping %s",
                crm_id,
                subscriber.id,
            )
            return
        if not dry_run:
            subscriber.crm_subscriber_id = crm_id
        assigned[subscriber.id] = crm_id
        taken_crm_ids.add(crm_id)
        stats[bucket] += 1

    # Pass 1: splynx external ids (authoritative).
    local_by_splynx = {
        splynx_id: sub
        for sub in db.query(Subscriber).filter(
            Subscriber.splynx_customer_id.isnot(None)
        )
        for splynx_id in [sub.splynx_customer_id]
    }
    for item in _iter_crm_subscribers(client, "splynx"):
        crm_id = _coerce_crm_uuid(item.get("id"))
        external_id = str(item.get("external_id") or "").strip()
        if not crm_id or not external_id:
            continue
        try:
            splynx_id = int(external_id)
        except ValueError:
            continue
        subscriber = local_by_splynx.get(splynx_id)
        if subscriber is None:
            stats["unmatched"] += 1
            continue
        link(subscriber, crm_id, "splynx_linked")

    # Pass 2: erpnext names (unique-match only).
    by_name: dict[str, list[Subscriber]] = {}
    for sub in db.query(Subscriber).filter(Subscriber.is_active.is_(True)):
        names = {
            (sub.display_name or "").strip().lower(),
            f"{sub.first_name} {sub.last_name}".strip().lower(),
            (sub.company_name or "").strip().lower(),
        }
        for name in names:
            if name:
                by_name.setdefault(name, []).append(sub)

    for item in _iter_crm_subscribers(client, "erpnext"):
        crm_id = _coerce_crm_uuid(item.get("id"))
        name_key = str(item.get("external_id") or "").strip().lower()
        if not crm_id or not name_key:
            continue
        candidates = by_name.get(name_key, [])
        if not candidates:
            stats["unmatched"] += 1
            continue
        if len(candidates) > 1:
            stats["ambiguous_names"] += 1
            continue
        link(candidates[0], crm_id, "erpnext_linked")

    if dry_run:
        db.rollback()
    else:
        db.commit()
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only")
    args = parser.parse_args()

    client = get_crm_client()
    db = SessionLocal()
    try:
        stats = backfill(db, client, dry_run=args.dry_run)
    finally:
        db.close()
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}{stats}")


if __name__ == "__main__":
    main()
