"""One-time batched CRM ticket import.

Keeps CRM ticket numbers unchanged and maps tickets to local subscribers via
CRM subscriber external_system=splynx / external_id.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict

from app.config import settings
from app.db import SessionLocal
from app.services.crm_client import CRMClient
from app.services.crm_ticket_pull import (
    CrmTicketPullResult,
    build_subscriber_cache_from_map,
    load_local_subscriber_map,
    sync_ticket,
)


def _add_result(target: CrmTicketPullResult, source: CrmTicketPullResult) -> None:
    target.fetched += source.fetched
    target.created += source.created
    target.updated += source.updated
    target.skipped_leads += source.skipped_leads
    target.skipped_unmapped_subscribers += source.skipped_unmapped_subscribers
    target.comments_created += source.comments_created
    target.errors.extend(source.errors)


def _process_batch(
    client: CRMClient,
    subscriber_cache,
    local_subscribers,
    *,
    limit: int,
    offset: int,
    sync_comments: bool,
) -> tuple[int, CrmTicketPullResult]:
    tickets = client.list_tickets(limit=limit, offset=offset, use_cache=False)
    result = CrmTicketPullResult(fetched=len(tickets))
    if not tickets:
        return 0, result

    with SessionLocal() as db:
        for crm_ticket in tickets:
            ticket_id = str(crm_ticket.get("id") or "")
            try:
                outcome, comments_created, _local_ticket = sync_ticket(
                    db,
                    crm_ticket,
                    client=client,
                    sync_comments=sync_comments,
                    subscriber_cache=subscriber_cache,
                    local_by_splynx=local_subscribers,
                )
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                result.errors.append({"ticket_id": ticket_id, "error": str(exc)})
                continue

            if outcome == "created":
                result.created += 1
            elif outcome == "updated":
                result.updated += 1
            elif outcome == "skipped_lead":
                result.skipped_leads += 1
            elif outcome == "skipped_unmapped_subscriber":
                result.skipped_unmapped_subscribers += 1
            result.comments_created += comments_created
        db.commit()

    return len(tickets), result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--max-batches", type=int, default=1000)
    parser.add_argument("--start-offset", type=int, default=0)
    parser.add_argument("--skip-comments", action="store_true")
    args = parser.parse_args()

    batch_size = min(max(args.batch_size, 1), 200)
    client = CRMClient(
        base_url=settings.crm_base_url,
        username=settings.crm_username,
        password=settings.crm_password,
        timeout=45.0,
    )

    with SessionLocal() as db:
        local_subscribers = load_local_subscriber_map(db)
    print(f"local_splynx_subscribers={len(local_subscribers)}", flush=True)

    subscriber_cache = build_subscriber_cache_from_map(local_subscribers, client)
    print(f"crm_subscriber_matches={len(subscriber_cache)}", flush=True)

    total = CrmTicketPullResult()
    offset = max(args.start_offset, 0)
    for batch_index in range(max(args.max_batches, 1)):
        print(
            f"batch={batch_index + 1} offset={offset} limit={batch_size}",
            flush=True,
        )
        fetched, result = _process_batch(
            client,
            subscriber_cache,
            local_subscribers,
            limit=batch_size,
            offset=offset,
            sync_comments=not args.skip_comments,
        )
        _add_result(total, result)
        print(f"batch_result={result.as_dict()}", flush=True)
        print(f"total={total.as_dict()}", flush=True)

        if fetched < batch_size:
            break
        offset += batch_size

    print(f"final={asdict(total)}", flush=True)


if __name__ == "__main__":
    main()
