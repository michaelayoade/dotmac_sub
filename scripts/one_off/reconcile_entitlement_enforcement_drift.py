"""Audit and safely repair entitlement/enforcement drift.

This script is intentionally conservative:

* It reports online prepaid subscriptions whose entitlement has already ended.
* It reports active subscriptions that still carry active billing enforcement
  locks.
* With ``--execute-safe`` it resolves only stale prepaid/overdue locks where
  there is post-lock payment or service-extension evidence.

It does not bulk-suspend ambiguous accounts. Those are written to review CSVs.
"""

from __future__ import annotations

import argparse
import csv
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy import text

from app.db import SessionLocal
from app.models.catalog import Subscription
from app.models.enforcement_lock import EnforcementReason
from app.services.account_lifecycle import resolve_locks_for_trigger


SAFE_LOCK_REASONS = {"prepaid", "overdue"}


@dataclass(frozen=True)
class Paths:
    expired_online: Path
    stale_lock_safe: Path
    stale_lock_review: Path


def _radius_dsn() -> str:
    dsn = os.environ.get("RADIUS_DB_DSN")
    if dsn:
        return dsn
    host = os.environ.get("RADIUS_DB_HOST", "postgres-local")
    port = os.environ.get("RADIUS_DB_PORT", "5432")
    name = os.environ.get("RADIUS_DB_NAME", "radius")
    user = os.environ.get("RADIUS_DB_USER", "postgres")
    password = os.environ.get("RADIUS_DB_PASS", "")
    auth = f"{user}:{password}@" if password else f"{user}@"
    return f"postgresql://{auth}{host}:{port}/{name}"


def _open_radius_usernames() -> list[str]:
    with psycopg.connect(_radius_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select distinct username
                from radacct
                where acctstoptime is null
                  and username ~ '^[0-9]+$'
                """
            )
            return [str(row[0]) for row in cur.fetchall()]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _rows(result: Any) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in result]


def _load_open_users(db, usernames: list[str]) -> None:
    db.execute(text("create temp table open_radius_users(username text primary key)"))
    if usernames:
        db.execute(
            text("insert into open_radius_users(username) values (:username)"),
            [{"username": username} for username in sorted(set(usernames))],
        )


def _expired_online(db) -> list[dict[str, Any]]:
    return _rows(
        db.execute(
            text(
                """
                select
                    s.id::text as subscriber_id,
                    sub.id::text as subscription_id,
                    s.subscriber_number,
                    s.splynx_customer_id,
                    coalesce(
                        nullif(s.company_name, ''),
                        nullif(s.display_name, ''),
                        trim(coalesce(s.first_name, '') || ' ' || coalesce(s.last_name, ''))
                    ) as customer_name,
                    s.status::text as account_status,
                    sub.status::text as subscription_status,
                    sub.access_state,
                    sub.next_billing_at,
                    sub.login,
                    sub.ipv4_address
                from subscribers s
                join subscriptions sub on sub.subscriber_id = s.id
                join open_radius_users ou on ou.username = s.subscriber_number
                where sub.billing_mode = 'prepaid'
                  and s.status = 'active'
                  and sub.status = 'active'
                  and sub.next_billing_at is not null
                  and sub.next_billing_at <= now()
                order by sub.next_billing_at asc, s.subscriber_number
                """
            )
        )
    )


def _stale_lock_rows(db) -> list[dict[str, Any]]:
    return _rows(
        db.execute(
            text(
                """
                with active_lock as (
                    select
                        s.id as subscriber_id,
                        sub.id as subscription_id,
                        el.id as lock_id,
                        s.subscriber_number,
                        s.splynx_customer_id,
                        coalesce(
                            nullif(s.company_name, ''),
                            nullif(s.display_name, ''),
                            trim(coalesce(s.first_name, '') || ' ' || coalesce(s.last_name, ''))
                        ) as customer_name,
                        sub.next_billing_at,
                        sub.access_state,
                        el.reason::text as lock_reason,
                        el.source as lock_source,
                        el.created_at as lock_created_at
                    from subscribers s
                    join subscriptions sub on sub.subscriber_id = s.id
                    join enforcement_locks el
                      on el.subscription_id = sub.id
                     and el.is_active is true
                    join open_radius_users ou on ou.username = s.subscriber_number
                    where s.status = 'active'
                      and sub.status = 'active'
                ),
                evidence as (
                    select
                        al.*,
                        (
                            select max(p.paid_at)
                            from payments p
                            where p.account_id = al.subscriber_id
                              and p.is_active is true
                              and p.status = 'succeeded'
                              and p.paid_at >= al.lock_created_at
                        ) as payment_after_lock_at,
                        (
                            select max(see.created_at)
                            from service_extension_entries see
                            join service_extensions se on se.id = see.extension_id
                            where see.subscription_id = al.subscription_id
                              and se.status = 'applied'
                              and coalesce(se.applied_at, see.created_at) >= al.lock_created_at
                        ) as extension_after_lock_at,
                        (
                            select max(i.paid_at)
                            from invoice_lines il
                            join invoices i on i.id = il.invoice_id
                            where il.subscription_id = al.subscription_id
                              and il.is_active is true
                              and i.is_active is true
                              and i.status = 'paid'
                              and i.paid_at >= al.lock_created_at
                              and i.billing_period_start <= now()
                              and i.billing_period_end > now()
                        ) as covering_paid_invoice_after_lock_at
                    from active_lock al
                )
                select
                    subscriber_id::text,
                    subscription_id::text,
                    lock_id::text,
                    subscriber_number,
                    splynx_customer_id,
                    customer_name,
                    next_billing_at,
                    access_state,
                    lock_reason,
                    lock_source,
                    lock_created_at,
                    payment_after_lock_at,
                    extension_after_lock_at,
                    covering_paid_invoice_after_lock_at,
                    case
                        when lock_reason in ('prepaid', 'overdue')
                         and (
                            payment_after_lock_at is not null
                            or extension_after_lock_at is not null
                            or covering_paid_invoice_after_lock_at is not null
                         )
                        then 'safe_resolve_lock'
                        else 'review'
                    end as recommended_action
                from evidence
                order by recommended_action, next_billing_at asc, subscriber_number, lock_reason
                """
            )
        )
    )


def _resolve_safe_locks(db, rows: list[dict[str, Any]]) -> int:
    resolved = 0
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row["recommended_action"] != "safe_resolve_lock":
            continue
        grouped.setdefault(
            (str(row["subscription_id"]), str(row["lock_reason"])), []
        ).append(row)

    for (subscription_id, reason_value), reason_rows in grouped.items():
        subscription = db.get(Subscription, subscription_id)
        if subscription is None:
            continue
        has_extension = any(r["extension_after_lock_at"] for r in reason_rows)
        trigger = "admin" if has_extension else "payment"
        count, _remaining = resolve_locks_for_trigger(
            db,
            subscription,
            trigger=trigger,
            resolved_by="entitlement_enforcement_drift_reconciler",
            reason=EnforcementReason(reason_value),
            notes=(
                "Resolved stale billing enforcement lock: subscription/account are "
                "active and post-lock payment or service-extension evidence exists."
            ),
        )
        resolved += count
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-safe", action="store_true")
    parser.add_argument(
        "--out-dir",
        default="/tmp",
        help="Directory for reconciliation CSV reports.",
    )
    args = parser.parse_args()

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    paths = Paths(
        expired_online=Path(args.out_dir) / f"expired_online_{stamp}.csv",
        stale_lock_safe=Path(args.out_dir) / f"stale_lock_safe_{stamp}.csv",
        stale_lock_review=Path(args.out_dir) / f"stale_lock_review_{stamp}.csv",
    )

    open_users = _open_radius_usernames()
    db = SessionLocal()
    try:
        _load_open_users(db, open_users)
        expired_online = _expired_online(db)
        stale_locks = _stale_lock_rows(db)
        safe = [r for r in stale_locks if r["recommended_action"] == "safe_resolve_lock"]
        review = [r for r in stale_locks if r["recommended_action"] != "safe_resolve_lock"]

        _write_csv(paths.expired_online, expired_online)
        _write_csv(paths.stale_lock_safe, safe)
        _write_csv(paths.stale_lock_review, review)

        resolved = 0
        if args.execute_safe:
            resolved = _resolve_safe_locks(db, safe)
            db.commit()
        else:
            db.rollback()

        print(f"open_radius_users={len(open_users)}")
        print(f"expired_online={len(expired_online)} csv={paths.expired_online}")
        print(f"stale_lock_safe={len(safe)} csv={paths.stale_lock_safe}")
        print(f"stale_lock_review={len(review)} csv={paths.stale_lock_review}")
        print(f"resolved_locks={resolved}")
        print(f"mode={'execute_safe' if args.execute_safe else 'dry_run'}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
