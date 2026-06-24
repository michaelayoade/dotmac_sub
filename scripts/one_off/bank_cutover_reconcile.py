"""Reconcile cutover bank credits against local billing records.

Read-only by default. This script is intentionally conservative: it does not
post credits. It exports local bank-transfer-like payments and payment proofs,
and, when given a bank statement CSV, classifies each credit as matched,
ambiguous, or missing from the local ledger.

Expected bank CSV columns are flexible. Common aliases:

- date: date, paid_at, transaction_date, value_date, posted_at
- amount: amount, credit, credit_amount, deposit, inflow
- reference: reference, ref, transaction_id, session_id
- narration: narration, description, details, remarks
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.billing import Payment, PaymentStatus
from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.models.subscriber import Subscriber
from app.services.common import round_money

DEFAULT_FROM_DATE = "2026-06-15"
DEFAULT_TO_DATE = "2026-06-18"
DEFAULT_OUTPUT = "scratchpad/bank_cutover_reconcile.csv"
SYSTEM_OUTPUT = "scratchpad/bank_cutover_system_records.csv"
DATE_ALIASES = ("date", "paid_at", "transaction_date", "value_date", "posted_at")
AMOUNT_ALIASES = ("amount", "credit", "credit_amount", "deposit", "inflow")
REFERENCE_ALIASES = ("reference", "ref", "transaction_id", "session_id")
NARRATION_ALIASES = ("narration", "description", "details", "remarks", "memo")
AMOUNT_TOLERANCE = Decimal("0.01")
DATE_WINDOW_DAYS = 1


@dataclass(frozen=True)
class StatementRow:
    row_number: int
    paid_date: date | None
    amount: Decimal
    reference: str
    narration: str
    raw: dict[str, str]


@dataclass(frozen=True)
class LocalRecord:
    record_type: str
    record_id: str
    account_id: str
    subscriber_number: str
    subscriber_name: str
    status: str
    paid_at: datetime | None
    amount: Decimal
    reference: str
    memo: str


def _parse_date(raw: str | None) -> date | None:
    value = (raw or "").strip()
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        return None


def _parse_amount(raw: str | None) -> Decimal:
    value = (raw or "").strip().replace(",", "")
    if value.startswith("(") and value.endswith(")"):
        value = f"-{value[1:-1]}"
    try:
        return round_money(Decimal(value))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def _first(row: dict[str, str], aliases: tuple[str, ...]) -> str:
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for alias in aliases:
        value = lowered.get(alias)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _read_statement(path: Path) -> list[StatementRow]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows: list[StatementRow] = []
        for idx, row in enumerate(reader, start=2):
            amount = _parse_amount(_first(row, AMOUNT_ALIASES))
            if amount <= 0:
                continue
            rows.append(
                StatementRow(
                    row_number=idx,
                    paid_date=_parse_date(_first(row, DATE_ALIASES)),
                    amount=amount,
                    reference=_first(row, REFERENCE_ALIASES),
                    narration=_first(row, NARRATION_ALIASES),
                    raw={str(k): str(v) for k, v in row.items()},
                )
            )
    return rows


def _date_bounds(start: str, end: str) -> tuple[datetime, datetime]:
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    return (
        datetime.combine(start_date, time.min, tzinfo=UTC),
        datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC),
    )


def _subscriber_label(subscriber: Subscriber | None) -> tuple[str, str]:
    if subscriber is None:
        return "", ""
    name = (
        subscriber.display_name
        or subscriber.company_name
        or f"{subscriber.first_name} {subscriber.last_name}".strip()
    )
    return subscriber.subscriber_number or "", name


def _load_local_records(
    db: Session, start: datetime, end: datetime
) -> list[LocalRecord]:
    records: list[LocalRecord] = []
    payments = db.scalars(
        select(Payment)
        .where(Payment.is_active.is_(True))
        .where(Payment.status == PaymentStatus.succeeded)
        .where(Payment.paid_at >= start)
        .where(Payment.paid_at < end)
        .order_by(Payment.paid_at.asc(), Payment.created_at.asc())
    ).all()
    for payment in payments:
        memo = payment.memo or ""
        external_id = payment.external_id or ""
        bank_like = (
            external_id.upper().startswith("TRF-")
            or "bank" in memo.lower()
            or "transfer" in memo.lower()
            or payment.provider_id is None
        )
        if not bank_like:
            continue
        subscriber = (
            db.get(Subscriber, payment.account_id) if payment.account_id else None
        )
        number, name = _subscriber_label(subscriber)
        records.append(
            LocalRecord(
                record_type="payment",
                record_id=str(payment.id),
                account_id=str(payment.account_id or ""),
                subscriber_number=number,
                subscriber_name=name,
                status=payment.status.value if payment.status else "",
                paid_at=payment.paid_at,
                amount=round_money(payment.amount),
                reference=external_id,
                memo=memo,
            )
        )

    # Only VERIFIED proofs are evidence of a real bank credit. Submitted proofs
    # are unconfirmed and rejected proofs were explicitly disproved; either can
    # fall back to the *claimed* amount and falsely match a genuine credit.
    proofs = db.scalars(
        select(PaymentProof)
        .where(PaymentProof.status == PaymentProofStatus.verified)
        .where(PaymentProof.created_at >= start)
        .where(PaymentProof.created_at < end)
        .order_by(PaymentProof.created_at.asc())
    ).all()
    for proof in proofs:
        subscriber = db.get(Subscriber, proof.account_id) if proof.account_id else None
        number, name = _subscriber_label(subscriber)
        records.append(
            LocalRecord(
                record_type="payment_proof",
                record_id=str(proof.id),
                account_id=str(proof.account_id or ""),
                subscriber_number=number,
                subscriber_name=name,
                status=proof.status.value if proof.status else "",
                paid_at=proof.paid_at or proof.created_at,
                amount=round_money(proof.verified_amount or proof.amount),
                reference=proof.reference or "",
                memo=proof.review_notes or "",
            )
        )
    return records


def _text_match(statement: StatementRow, record: LocalRecord) -> bool:
    haystack = f"{statement.reference} {statement.narration}".lower()
    for needle in (
        record.reference,
        record.subscriber_number,
        record.subscriber_name,
    ):
        normalized = (needle or "").strip().lower()
        if normalized and normalized in haystack:
            return True
    return False


def _matches(statement: StatementRow, records: list[LocalRecord]) -> list[LocalRecord]:
    """Return records that definitively match the statement credit.

    A record only counts as a match on a *definitive* signal: an exact
    reference match (which wins outright) or a text match (reference /
    subscriber number / name found in the statement reference or narration).
    Amount + date alone is a coincidence, not evidence, so such records are
    never counted as matches and never feed matched/ambiguous totals.
    """
    reference_matches: list[LocalRecord] = []
    text_matches: list[LocalRecord] = []
    for record in records:
        if abs(record.amount - statement.amount) > AMOUNT_TOLERANCE:
            continue
        if statement.paid_date and record.paid_at:
            delta = abs((record.paid_at.date() - statement.paid_date).days)
            if delta > DATE_WINDOW_DAYS:
                continue
        if statement.reference and statement.reference == record.reference:
            reference_matches.append(record)
            continue
        if _text_match(statement, record):
            text_matches.append(record)
            continue
        # Amount/date-only coincidence is not definitive: do NOT count it.
    # A strong reference match wins over weaker text-only matches.
    if reference_matches:
        return reference_matches
    return text_matches


def _write_system(path: Path, records: list[LocalRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(LocalRecord.__dataclass_fields__)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({field: getattr(record, field) for field in fieldnames})


def _write_reconcile(
    path: Path,
    statements: list[StatementRow],
    records: list[LocalRecord],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "classification",
        "statement_row",
        "statement_date",
        "statement_amount",
        "statement_reference",
        "statement_narration",
        "match_count",
        "matched_record_type",
        "matched_record_id",
        "matched_account_id",
        "matched_subscriber_number",
        "matched_subscriber_name",
        "matched_status",
        "matched_paid_at",
        "matched_amount",
        "matched_reference",
        "matched_memo",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for statement in statements:
            matches = _matches(statement, records)
            classification = (
                "matched"
                if len(matches) == 1
                else "missing_from_local"
                if not matches
                else "ambiguous"
            )
            rows = matches or [None]
            for match in rows:
                writer.writerow(
                    {
                        "classification": classification,
                        "statement_row": statement.row_number,
                        "statement_date": statement.paid_date or "",
                        "statement_amount": statement.amount,
                        "statement_reference": statement.reference,
                        "statement_narration": statement.narration,
                        "match_count": len(matches),
                        "matched_record_type": match.record_type if match else "",
                        "matched_record_id": match.record_id if match else "",
                        "matched_account_id": match.account_id if match else "",
                        "matched_subscriber_number": (
                            match.subscriber_number if match else ""
                        ),
                        "matched_subscriber_name": match.subscriber_name
                        if match
                        else "",
                        "matched_status": match.status if match else "",
                        "matched_paid_at": match.paid_at if match else "",
                        "matched_amount": match.amount if match else "",
                        "matched_reference": match.reference if match else "",
                        "matched_memo": match.memo if match else "",
                    }
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date", default=DEFAULT_FROM_DATE)
    parser.add_argument("--to-date", default=DEFAULT_TO_DATE)
    parser.add_argument("--statement", default=None, help="Bank statement CSV path")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--system-output", default=SYSTEM_OUTPUT)
    parser.add_argument("--export-system-only", action="store_true")
    args = parser.parse_args()

    start, end = _date_bounds(args.from_date, args.to_date)
    db = SessionLocal()
    try:
        records = _load_local_records(db, start, end)
    finally:
        db.close()

    _write_system(Path(args.system_output), records)
    if args.export_system_only:
        print(f"system_records={len(records)} output={args.system_output}")
        return

    if not args.statement:
        raise SystemExit("--statement is required unless --export-system-only is used")
    statements = _read_statement(Path(args.statement))
    _write_reconcile(Path(args.output), statements, records)
    matched = ambiguous = missing = Decimal("0")
    for statement in statements:
        count = len(_matches(statement, records))
        if count == 1:
            matched += statement.amount
        elif count == 0:
            missing += statement.amount
        else:
            ambiguous += statement.amount
    print(f"statement_rows={len(statements)} system_records={len(records)}")
    print(f"matched_total={matched}")
    print(f"ambiguous_total={ambiguous}")
    print(f"missing_total={missing}")
    print(f"output={args.output}")
    print(f"system_output={args.system_output}")


if __name__ == "__main__":
    main()
