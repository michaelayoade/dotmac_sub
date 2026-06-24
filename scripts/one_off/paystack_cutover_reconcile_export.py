"""Export Paystack cutover transactions missing from local billing.

This is read-only. It lists Paystack transactions for a date window and
classifies each transaction against local payment/provider-event/top-up intent
records so finance can identify customer funds that were captured at Paystack
but not posted into the local ledger during cutover.

Examples
--------
  python -m scripts.one_off.paystack_cutover_reconcile_export
  python -m scripts.one_off.paystack_cutover_reconcile_export \
      --from-date 2026-06-15 --to-date 2026-06-18 \
      --output scratchpad/paystack_cutover_reconcile.csv
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.billing import Payment, PaymentProviderEvent, TopupIntent
from app.models.subscriber import Subscriber, SubscriberContact
from app.services.paystack import PAYSTACK_API_BASE, _get_secret_key, kobo_to_naira

DEFAULT_FROM_DATE = "2026-06-15"
DEFAULT_TO_DATE = "2026-06-18"
DEFAULT_OUTPUT = "scratchpad/paystack_cutover_reconcile.csv"


@dataclass(frozen=True)
class GatewayTransaction:
    reference: str
    external_id: str
    status: str
    amount: Decimal
    currency: str
    paid_at: str
    created_at: str
    customer_email: str
    metadata: dict[str, Any]
    raw: dict[str, Any]


def _metadata_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _transaction_from_payload(payload: dict[str, Any]) -> GatewayTransaction:
    customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
    return GatewayTransaction(
        reference=str(payload.get("reference") or ""),
        external_id=str(payload.get("id") or ""),
        status=str(payload.get("status") or ""),
        amount=kobo_to_naira(int(payload.get("amount") or 0)),
        currency=str(payload.get("currency") or "NGN"),
        paid_at=str(payload.get("paid_at") or ""),
        created_at=str(payload.get("created_at") or ""),
        customer_email=str(customer.get("email") or ""),
        metadata=_metadata_dict(payload.get("metadata")),
        raw=payload,
    )


def _list_paystack_transactions(
    db: Session,
    *,
    from_date: str,
    to_date: str,
    status: str | None,
    per_page: int,
) -> list[GatewayTransaction]:
    secret_key = _get_secret_key(db)
    if not secret_key:
        raise RuntimeError("Paystack secret key is not configured")

    transactions: list[GatewayTransaction] = []
    page = 1
    with httpx.Client(
        base_url=PAYSTACK_API_BASE,
        headers={"Authorization": f"Bearer {secret_key}"},
        timeout=30.0,
    ) as client:
        while True:
            params: dict[str, str | int] = {
                "from": from_date,
                "to": to_date,
                "page": page,
                "perPage": per_page,
            }
            if status:
                params["status"] = status
            response = client.get("/transaction", params=params)
            response.raise_for_status()
            body = response.json()
            if not body.get("status"):
                raise RuntimeError(body.get("message") or "Paystack list failed")
            rows = body.get("data") or []
            transactions.extend(_transaction_from_payload(row) for row in rows)
            meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
            page_count = int(meta.get("pageCount") or page)
            if page >= page_count or not rows:
                break
            page += 1
    return transactions


def _find_payment(db: Session, tx: GatewayTransaction) -> Payment | None:
    candidates = [v for v in {tx.external_id, tx.reference} if v]
    if not candidates:
        return None
    return db.scalars(
        select(Payment)
        .where(Payment.external_id.in_(candidates))
        .order_by(Payment.is_active.desc(), Payment.created_at.desc())
        .limit(1)
    ).first()


def _find_provider_event(
    db: Session, tx: GatewayTransaction
) -> PaymentProviderEvent | None:
    predicates = []
    if tx.external_id:
        predicates.append(PaymentProviderEvent.external_id == tx.external_id)
    if tx.reference:
        predicates.append(PaymentProviderEvent.idempotency_key == f"paystack-{tx.reference}")
    if not predicates:
        return None
    return db.scalars(
        select(PaymentProviderEvent)
        .where(or_(*predicates))
        .order_by(PaymentProviderEvent.received_at.desc())
        .limit(1)
    ).first()


def _find_topup_intent(db: Session, reference: str) -> TopupIntent | None:
    if not reference:
        return None
    return db.scalars(
        select(TopupIntent).where(TopupIntent.reference == reference).limit(1)
    ).first()


def _matched_subscribers_by_email(db: Session, email: str) -> list[Subscriber]:
    normalized = email.strip().lower()
    if not normalized:
        return []

    direct = db.scalars(
        select(Subscriber).where(func.lower(Subscriber.email) == normalized)
    ).all()
    contact = db.scalars(
        select(Subscriber)
        .join(SubscriberContact, SubscriberContact.subscriber_id == Subscriber.id)
        .where(func.lower(SubscriberContact.email) == normalized)
    ).all()

    by_id: dict[str, Subscriber] = {}
    for subscriber in [*direct, *contact]:
        by_id[str(subscriber.id)] = subscriber
    return list(by_id.values())


def _subscriber_label(subscriber: Subscriber) -> str:
    name = subscriber.display_name or subscriber.company_name or (
        f"{subscriber.first_name} {subscriber.last_name}".strip()
    )
    status = subscriber.status.value if subscriber.status else ""
    number = subscriber.subscriber_number or ""
    return f"{subscriber.id}:{status}:{number}:{name}"


def _classification(
    tx: GatewayTransaction,
    payment: Payment | None,
    event: PaymentProviderEvent | None,
    intent: TopupIntent | None,
) -> str:
    if payment is not None:
        return "recorded_payment"
    if event is not None and event.payment_id is not None:
        return "provider_event_links_payment_missing_locally"
    if intent is not None and intent.completed_payment_id is not None:
        return "intent_completed_payment_missing_locally"
    if event is not None:
        return "provider_event_only_no_payment"
    if intent is not None:
        return "intent_only_no_payment"
    if tx.status == "success":
        return "missing_success_payment"
    return "not_success_no_local_payment"


def _recovery_bucket(classification: str, matched_subscriber_count: int) -> str:
    if classification != "missing_success_payment":
        return "already_recorded_or_not_success"
    if matched_subscriber_count == 1:
        return "single_email_match"
    if matched_subscriber_count == 0:
        return "no_email_match"
    return "ambiguous_email_match"


def _row(
    db: Session, tx: GatewayTransaction
) -> dict[str, str]:
    payment = _find_payment(db, tx)
    event = _find_provider_event(db, tx)
    intent = _find_topup_intent(db, tx.reference)
    matched_subscribers = _matched_subscribers_by_email(db, tx.customer_email)
    classification = _classification(tx, payment, event, intent)
    metadata = tx.metadata
    return {
        "classification": classification,
        "recovery_bucket": _recovery_bucket(
            classification,
            len(matched_subscribers),
        ),
        "reference": tx.reference,
        "paystack_id": tx.external_id,
        "status": tx.status,
        "amount": str(tx.amount),
        "currency": tx.currency,
        "paid_at": tx.paid_at,
        "created_at": tx.created_at,
        "customer_email": tx.customer_email,
        "metadata_account_id": str(metadata.get("account_id") or ""),
        "metadata_invoice_id": str(metadata.get("invoice_id") or ""),
        "metadata_topup_intent_id": str(metadata.get("topup_intent_id") or ""),
        "local_payment_id": str(payment.id) if payment else "",
        "local_payment_status": payment.status.value if payment else "",
        "local_payment_amount": str(payment.amount) if payment else "",
        "local_payment_account_id": str(payment.account_id) if payment else "",
        "local_payment_billing_account_id": (
            str(payment.billing_account_id) if payment and payment.billing_account_id else ""
        ),
        "provider_event_id": str(event.id) if event else "",
        "provider_event_status": event.status.value if event else "",
        "provider_event_payment_id": str(event.payment_id) if event and event.payment_id else "",
        "topup_intent_id": str(intent.id) if intent else "",
        "topup_intent_status": intent.status if intent else "",
        "topup_intent_completed_payment_id": (
            str(intent.completed_payment_id) if intent and intent.completed_payment_id else ""
        ),
        "matched_subscriber_count": str(len(matched_subscribers)),
        "matched_subscriber_id": (
            str(matched_subscribers[0].id) if len(matched_subscribers) == 1 else ""
        ),
        "matched_subscribers": " | ".join(
            _subscriber_label(subscriber) for subscriber in matched_subscribers
        ),
    }


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else [
        "classification",
        "recovery_bucket",
        "reference",
        "paystack_id",
        "status",
        "amount",
        "currency",
        "paid_at",
        "created_at",
        "customer_email",
        "metadata_account_id",
        "metadata_invoice_id",
        "metadata_topup_intent_id",
        "local_payment_id",
        "local_payment_status",
        "local_payment_amount",
        "local_payment_account_id",
        "local_payment_billing_account_id",
        "provider_event_id",
        "provider_event_status",
        "provider_event_payment_id",
        "topup_intent_id",
        "topup_intent_status",
        "topup_intent_completed_payment_id",
        "matched_subscriber_count",
        "matched_subscriber_id",
        "matched_subscribers",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _parse_date(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%d")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date", default=DEFAULT_FROM_DATE, type=_parse_date)
    parser.add_argument("--to-date", default=DEFAULT_TO_DATE, type=_parse_date)
    parser.add_argument(
        "--status",
        default="success",
        help="Paystack status filter. Use an empty string to include all statuses.",
    )
    parser.add_argument("--per-page", type=int, default=100)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    status = args.status.strip() or None
    db = SessionLocal()
    try:
        transactions = _list_paystack_transactions(
            db,
            from_date=args.from_date,
            to_date=args.to_date,
            status=status,
            per_page=args.per_page,
        )
        rows = [_row(db, tx) for tx in transactions]
    finally:
        db.close()

    output = Path(args.output)
    _write_csv(output, rows)

    counts: dict[str, int] = {}
    bucket_counts: dict[str, int] = {}
    bucket_totals: dict[str, Decimal] = {}
    total_missing = Decimal("0.00")
    for row in rows:
        classification = row["classification"]
        counts[classification] = counts.get(classification, 0) + 1
        bucket = row["recovery_bucket"]
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        bucket_totals[bucket] = bucket_totals.get(bucket, Decimal("0.00")) + Decimal(
            row["amount"]
        )
        if classification == "missing_success_payment":
            total_missing += Decimal(row["amount"])

    print(
        "Paystack cutover reconcile export "
        f"{args.from_date}..{args.to_date} status={status or '(all)'}"
    )
    print(f"transactions: {len(rows)}")
    for key in sorted(counts):
        print(f"  {key}: {counts[key]}")
    print("recovery buckets:")
    for key in sorted(bucket_counts):
        print(f"  {key}: {bucket_counts[key]} ({bucket_totals[key]})")
    print(f"missing_success_total: {total_missing}")
    print(f"output: {output}")


if __name__ == "__main__":
    main()
