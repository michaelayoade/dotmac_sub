"""Read-only cutover balance invariant audit."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.models.billing import (
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.services.common import coerce_uuid, round_money

OPENING_MEMO = "Prepaid opening balance @ cutover"
RECONSTRUCTED_TRUE_UP_MEMO_PREFIX = "Correction: cutover reconstructed balance true-up"
PARTIAL_CONSTRUCTION_MEMO_PREFIX = (
    "Partial cutover opening balance construction adjustment"
)
CUTOVER_ACTIVITY_AT = datetime(2026, 6, 16, 9, 8, tzinfo=UTC)
PAYMENT_ACTIVITY_AT = datetime(2026, 6, 16, tzinfo=UTC)
TOLERANCE = Decimal("0.01")

RECONSTRUCTED_BALANCE_FIELDS = [
    "account_id",
    "subscriber_name",
    "subscriber_status",
    "cutover_opening_balance",
    "post_cutover_payments",
    "post_cutover_service_charges",
    "ordinary_adjustments",
    "reconstructed_balance",
    "current_local_available",
    "difference_current_minus_reconstructed",
    "direction",
    "active_seed_net",
    "inactive_seed_net",
    "inactive_opening_debits",
    "post_adjustment_entry_count",
]
RECONSTRUCTED_TRANSACTION_FIELDS = [
    "account_id",
    "activity_at",
    "row_type",
    "source_id",
    "description",
    "amount",
    "signed_amount",
    "reconstructed_running_balance",
    "status",
    "external_ref",
]
CUTOVER_CORRECTION_FIELDS = [
    "account_id",
    "subscriber_name",
    "subscriber_status",
    "current_available",
    "reconstructed_balance",
    "drift",
    "direction",
    "action",
    "entry_type",
    "amount",
    "decision",
    "reason",
    "memo",
]


def _money(value: object) -> Decimal:
    return round_money(Decimal(str(value or 0)))


def _direction(drift: Decimal) -> str:
    if abs(drift) <= TOLERANCE:
        return "balanced"
    return "overcredited" if drift > 0 else "understated"


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def _display_row(row: dict[str, Any]) -> dict[str, str]:
    current = _money(row.get("current_available"))
    target = _money(row.get("target_available"))
    drift = _money(current - target)
    return {
        "account_id": str(row.get("account_id") or ""),
        "subscriber_name": str(row.get("subscriber_name") or ""),
        "subscriber_status": str(row.get("subscriber_status") or ""),
        "cutover_opening_balance": str(_money(row.get("deposit"))),
        "post_cutover_payments": str(_money(row.get("post_cutover_payments"))),
        "post_cutover_service_charges": str(_money(row.get("post_cutover_invoices"))),
        "ordinary_adjustments": str(_money(row.get("target_adjustment_net"))),
        "reconstructed_balance": str(target),
        "current_local_available": str(current),
        "difference_current_minus_reconstructed": str(drift),
        "direction": _direction(drift),
        "active_seed_net": str(_money(row.get("active_seed_net"))),
        "inactive_seed_net": str(_money(row.get("inactive_seed_net"))),
        "inactive_opening_debits": str(_money(row.get("inactive_opening_debits"))),
        "post_adjustment_entry_count": str(
            int(row.get("post_adjustment_entry_count") or 0)
        ),
    }


def iter_reconstructed_balance_rows(db: Session) -> list[dict[str, str]]:
    """Return the finance reconstruction row for every cutover-seeded account."""
    return [_display_row(dict(row)) for row in _rows(db)]


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _load_registered_variances_from_file(path: Path) -> dict[str, Decimal]:
    registry_path = path
    if not registry_path.exists():
        return {}
    data = json.loads(registry_path.read_text())
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("cutover variance registry entries must be a list")

    variances: dict[str, Decimal] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("cutover variance registry entries must be objects")
        if entry.get("status") != "accepted":
            continue
        account_id = str(entry.get("account_id") or "")
        expected_drift = entry.get("expected_drift")
        reason = str(entry.get("reason") or "")
        recorded_at = str(entry.get("recorded_at") or "")
        if not account_id or expected_drift is None or not reason or not recorded_at:
            raise ValueError(
                "accepted cutover variance requires account_id, expected_drift, "
                "reason, and recorded_at"
            )
        if account_id in variances:
            raise ValueError(f"duplicate accepted cutover variance for {account_id}")
        variances[account_id] = _money(expected_drift)
    return variances


def _load_registered_variances_from_db(db: Session) -> dict[str, Decimal]:
    try:
        rows = db.execute(
            text(
                """
                SELECT account_id::text, expected_drift
                FROM cutover_balance_variances
                WHERE is_active IS TRUE
                  AND status = 'accepted'
                """
            )
        ).mappings()
    except ProgrammingError as exc:
        if "cutover_balance_variances" not in str(exc):
            raise
        db.rollback()
        return {}
    variances: dict[str, Decimal] = {}
    for row in rows:
        account_id = str(row["account_id"])
        if account_id in variances:
            raise ValueError(f"duplicate accepted cutover variance for {account_id}")
        variances[account_id] = _money(row["expected_drift"])
    return variances


def _load_registered_variances(
    db: Session, variance_registry_path: Path | None = None
) -> dict[str, Decimal]:
    if variance_registry_path is not None:
        return _load_registered_variances_from_file(variance_registry_path)
    return _load_registered_variances_from_db(db)


def _unregistered_drift(raw_drift: Decimal, registered_drift: Decimal) -> Decimal:
    return _money(raw_drift - registered_drift)


def _rows(db: Session):
    return db.execute(
        text(
            """
            WITH seeded AS (
                SELECT DISTINCT account_id
                FROM ledger_entries
                WHERE memo = :opening_memo
            ),
            ledger_net AS (
                SELECT le.account_id,
                       COALESCE(SUM(CASE WHEN le.entry_type = 'credit'
                                         THEN le.amount ELSE -le.amount END), 0) AS net
                FROM ledger_entries le
                JOIN seeded seeded ON seeded.account_id = le.account_id
                WHERE le.is_active
                  AND le.invoice_id IS NULL
                  AND le.currency = 'NGN'
                GROUP BY le.account_id
            ),
            open_ar AS (
                SELECT i.account_id, COALESCE(SUM(i.balance_due), 0) AS due
                FROM invoices i
                JOIN seeded seeded ON seeded.account_id = i.account_id
                WHERE i.is_active
                  AND i.balance_due > 0
                  AND i.status IN ('issued', 'partially_paid', 'overdue')
                  AND i.currency = 'NGN'
                GROUP BY i.account_id
            ),
            post_payments AS (
                SELECT p.account_id, COALESCE(SUM(p.amount), 0) AS amount
                FROM payments p
                JOIN seeded seeded ON seeded.account_id = p.account_id
                WHERE p.is_active
                  AND p.status = 'succeeded'
                  AND p.created_at >= :payment_at
                GROUP BY p.account_id
            ),
            post_invoices AS (
                SELECT i.account_id, COALESCE(SUM(i.total), 0) AS amount
                FROM invoices i
                JOIN seeded seeded ON seeded.account_id = i.account_id
                WHERE i.is_active
                  AND i.status IN ('issued', 'partially_paid', 'overdue', 'paid')
                  AND COALESCE(i.is_proforma, false) IS false
                  AND i.created_at >= :activity_at
                GROUP BY i.account_id
            ),
            external_post_invoice_allocations AS (
                SELECT i.account_id, COALESCE(SUM(pa.amount), 0) AS amount
                FROM invoices i
                JOIN seeded seeded ON seeded.account_id = i.account_id
                JOIN payment_allocations pa
                  ON pa.invoice_id = i.id
                 AND pa.is_active IS TRUE
                JOIN payments p ON p.id = pa.payment_id
                WHERE i.is_active
                  AND i.status IN ('issued', 'partially_paid', 'overdue', 'paid')
                  AND COALESCE(i.is_proforma, false) IS false
                  AND i.created_at >= :activity_at
                  AND p.is_active IS TRUE
                  AND p.status = 'succeeded'
                  AND p.account_id IS DISTINCT FROM i.account_id
                GROUP BY i.account_id
            ),
            ledger_charges AS (
                SELECT le.account_id, COALESCE(SUM(le.amount), 0) AS amount
                FROM ledger_entries le
                JOIN seeded seeded ON seeded.account_id = le.account_id
                WHERE le.is_active
                  AND le.invoice_id IS NULL
                  AND le.entry_type = 'debit'
                  AND le.source = 'invoice'
                  AND le.currency = 'NGN'
                  AND le.created_at >= :activity_at
                GROUP BY le.account_id
            ),
            seed_sums AS (
                SELECT le.account_id,
                       COALESCE(SUM(CASE
                         WHEN le.is_active AND le.entry_type = 'credit' THEN le.amount
                         WHEN le.is_active AND le.entry_type = 'debit' THEN -le.amount
                         ELSE 0 END), 0) AS active_seed_net,
                       COALESCE(SUM(CASE
                         WHEN NOT le.is_active AND le.entry_type = 'credit' THEN le.amount
                         WHEN NOT le.is_active AND le.entry_type = 'debit' THEN -le.amount
                         ELSE 0 END), 0) AS inactive_seed_net,
                       COALESCE(SUM(CASE
                         WHEN NOT le.is_active AND le.entry_type = 'debit' THEN le.amount
                         ELSE 0 END), 0) AS inactive_opening_debits
                FROM ledger_entries le
                JOIN seeded seeded ON seeded.account_id = le.account_id
                WHERE le.memo = :opening_memo
                GROUP BY le.account_id
            ),
            all_post_adjustments AS (
                SELECT le.account_id, COUNT(le.id) AS entry_count,
                       COALESCE(SUM(CASE WHEN le.entry_type = 'credit'
                                         THEN le.amount ELSE -le.amount END), 0) AS net
                FROM ledger_entries le
                JOIN seeded seeded ON seeded.account_id = le.account_id
                WHERE le.is_active
                  AND le.invoice_id IS NULL
                  AND le.source = 'adjustment'
                  AND le.memo <> :opening_memo
                  AND le.created_at >= :activity_at
                GROUP BY le.account_id
            ),
            target_adjustments AS (
                SELECT le.account_id, COUNT(le.id) AS entry_count,
                       COALESCE(SUM(CASE WHEN le.entry_type = 'credit'
                                         THEN le.amount ELSE -le.amount END), 0) AS net
                FROM ledger_entries le
                JOIN seeded seeded ON seeded.account_id = le.account_id
                WHERE le.is_active
                  AND le.invoice_id IS NULL
                  AND le.source = 'adjustment'
                  AND le.memo <> :opening_memo
                  AND le.memo NOT LIKE 'Reversal of phantom%'
                  AND le.memo NOT LIKE 'Reversal of prepaid opening%'
                  AND le.memo NOT LIKE 'Correction:%'
                  AND le.memo NOT LIKE :partial_construction_memo_prefix
                  AND le.memo NOT LIKE 'Data repair 2026-06-29:%'
                  AND le.memo NOT LIKE 'Validated account credit consumed%'
                  AND le.created_at >= :activity_at
                GROUP BY le.account_id
            )
            SELECT s.id AS account_id,
                   COALESCE(NULLIF(s.display_name, ''), NULLIF(s.company_name, ''),
                            concat_ws(' ', s.first_name, s.last_name)) AS subscriber_name,
                   s.status AS subscriber_status,
                   COALESCE(s.deposit, 0) AS deposit,
                   COALESCE(ln.net, 0) - COALESCE(oa.due, 0) AS current_available,
                   COALESCE(s.deposit, 0) + COALESCE(pp.amount, 0)
                     + COALESCE(epia.amount, 0) + COALESCE(ta.net, 0)
                     - COALESCE(pi.amount, 0)
                     - COALESCE(lc.amount, 0) AS target_available,
                   COALESCE(pp.amount, 0) + COALESCE(epia.amount, 0)
                     AS post_cutover_payments,
                   COALESCE(pi.amount, 0) + COALESCE(lc.amount, 0)
                     AS post_cutover_invoices,
                   COALESCE(ss.active_seed_net, 0) AS active_seed_net,
                   COALESCE(ss.inactive_seed_net, 0) AS inactive_seed_net,
                   COALESCE(ss.inactive_opening_debits, 0) AS inactive_opening_debits,
                   COALESCE(ta.entry_count, 0) AS target_adjustment_entry_count,
                   COALESCE(ta.net, 0) AS target_adjustment_net,
                   COALESCE(apa.entry_count, 0) AS post_adjustment_entry_count,
                   COALESCE(apa.net, 0) AS post_adjustment_net,
                   COALESCE(apa.entry_count, 0) - COALESCE(ta.entry_count, 0)
                     AS excluded_adjustment_entry_count,
                   COALESCE(apa.net, 0) - COALESCE(ta.net, 0)
                     AS excluded_adjustment_net
            FROM seeded seeded
            JOIN subscribers s ON s.id = seeded.account_id
            LEFT JOIN ledger_net ln ON ln.account_id = s.id
            LEFT JOIN open_ar oa ON oa.account_id = s.id
            LEFT JOIN post_payments pp ON pp.account_id = s.id
            LEFT JOIN post_invoices pi ON pi.account_id = s.id
            LEFT JOIN external_post_invoice_allocations epia
              ON epia.account_id = s.id
            LEFT JOIN ledger_charges lc ON lc.account_id = s.id
            LEFT JOIN seed_sums ss ON ss.account_id = s.id
            LEFT JOIN all_post_adjustments apa ON apa.account_id = s.id
            LEFT JOIN target_adjustments ta ON ta.account_id = s.id
            """
        ),
        {
            "opening_memo": OPENING_MEMO,
            "partial_construction_memo_prefix": (
                f"{PARTIAL_CONSTRUCTION_MEMO_PREFIX}%"
            ),
            "activity_at": CUTOVER_ACTIVITY_AT,
            "payment_at": PAYMENT_ACTIVITY_AT,
        },
    ).mappings()


def audit_cutover_balance_invariant(
    db: Session, *, sample_limit: int = 25, variance_registry_path: Path | None = None
) -> dict[str, Any]:
    registered_variances = _load_registered_variances(db, variance_registry_path)
    seen_registered_accounts: set[str] = set()
    changed_registered_accounts: set[str] = set()
    population = 0
    drift_rows: list[dict[str, Any]] = []
    raw_drift_count = 0
    registered_variance_count = 0
    registered_variance_total = Decimal("0")
    overcredited_total = Decimal("0")
    understated_total = Decimal("0")
    post_adjustment_entry_count = 0
    post_adjustment_net = Decimal("0")
    target_adjustment_entry_count = 0
    target_adjustment_net = Decimal("0")
    excluded_adjustment_entry_count = 0
    excluded_adjustment_net = Decimal("0")
    inactive_seed_drift_count = 0
    post_adjustment_drift_count = 0

    for row in _rows(db):
        population += 1
        post_adjustment_entry_count += int(row["post_adjustment_entry_count"] or 0)
        post_adjustment_net += _money(row["post_adjustment_net"])
        target_adjustment_entry_count += int(row["target_adjustment_entry_count"] or 0)
        target_adjustment_net += _money(row["target_adjustment_net"])
        excluded_adjustment_entry_count += int(
            row["excluded_adjustment_entry_count"] or 0
        )
        excluded_adjustment_net += _money(row["excluded_adjustment_net"])

        current = _money(row["current_available"])
        target = _money(row["target_available"])
        raw_drift = _money(current - target)
        account_id = str(row["account_id"])
        registered_drift = registered_variances.get(account_id, Decimal("0"))
        if account_id in registered_variances:
            seen_registered_accounts.add(account_id)
        drift = _unregistered_drift(raw_drift, registered_drift)
        if abs(registered_drift) > TOLERANCE and abs(drift) > TOLERANCE:
            changed_registered_accounts.add(account_id)
        if abs(raw_drift) > TOLERANCE:
            raw_drift_count += 1
        if abs(registered_drift) > TOLERANCE:
            registered_variance_count += 1
            registered_variance_total += abs(registered_drift)
        if abs(drift) <= TOLERANCE:
            continue
        if drift > 0:
            overcredited_total += drift
        else:
            understated_total += abs(drift)
        if _money(row["inactive_opening_debits"]) != Decimal("0.00"):
            inactive_seed_drift_count += 1
        if _money(row["target_adjustment_net"]) != Decimal("0.00"):
            post_adjustment_drift_count += 1
        drift_rows.append(
            {
                "account_id": account_id,
                "subscriber_name": str(row["subscriber_name"] or ""),
                "subscriber_status": str(row["subscriber_status"] or ""),
                "current_available": str(current),
                "target_available": str(target),
                "raw_drift": str(raw_drift),
                "registered_variance": str(registered_drift),
                "drift": str(drift),
                "direction": _direction(drift),
            }
        )

    drift_rows.sort(key=lambda item: abs(Decimal(item["drift"])), reverse=True)
    overcredited = [row for row in drift_rows if Decimal(row["drift"]) > 0]
    understated = [row for row in drift_rows if Decimal(row["drift"]) < 0]
    stale_registered_variances = sorted(
        (set(registered_variances) - seen_registered_accounts)
        | changed_registered_accounts
    )
    return {
        "ok": not drift_rows and not stale_registered_variances,
        "population": population,
        "raw_drift_count": raw_drift_count,
        "drift_count": len(drift_rows),
        "overcredited_count": len(overcredited),
        "overcredited_total": str(round_money(overcredited_total)),
        "understated_count": len(understated),
        "understated_total": str(round_money(understated_total)),
        "registered_variance_count": registered_variance_count,
        "registered_variance_total": str(round_money(registered_variance_total)),
        "stale_registered_variance_count": len(stale_registered_variances),
        "stale_registered_variance_accounts": stale_registered_variances[:sample_limit],
        "inactive_seed_drift_count": inactive_seed_drift_count,
        "post_adjustment_drift_count": post_adjustment_drift_count,
        "post_adjustment_entry_count": post_adjustment_entry_count,
        "post_adjustment_net": str(round_money(post_adjustment_net)),
        "target_adjustment_entry_count": target_adjustment_entry_count,
        "target_adjustment_net": str(round_money(target_adjustment_net)),
        "excluded_adjustment_entry_count": excluded_adjustment_entry_count,
        "excluded_adjustment_net": str(round_money(excluded_adjustment_net)),
        "sample_limit": sample_limit,
        "samples": drift_rows[:sample_limit],
    }


def _is_drift_row(row: dict[str, str]) -> bool:
    return abs(Decimal(row["difference_current_minus_reconstructed"])) > TOLERANCE


def _event_status(raw: Any) -> str:
    status = getattr(raw, "status", "")
    return str(getattr(status, "value", status) or "")


def _event_external_ref(raw: Any) -> str:
    for attr in (
        "invoice_number",
        "receipt_number",
        "credit_number",
        "reference",
        "external_ref",
    ):
        value = getattr(raw, attr, None)
        if value:
            return str(value)
    return ""


def _event_description(event: Any) -> str:
    return str(
        getattr(event, "memo", None)
        or getattr(getattr(event, "source", None), "value", "")
        or "Financial event"
    )


def _transaction_rows_for_drift_cases(
    db: Session, drift_rows: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Build a finance-review timeline for accounts with reconstruction drift."""
    if not drift_rows:
        return []

    from app.services.customer_financial_ledger import list_customer_financial_events

    output: list[dict[str, str]] = []
    for row in drift_rows:
        account_id = row["account_id"]
        running = _money(row["cutover_opening_balance"])
        output.append(
            {
                "account_id": account_id,
                "activity_at": PAYMENT_ACTIVITY_AT.isoformat(),
                "row_type": "cutover_opening_balance",
                "source_id": account_id,
                "description": "Splynx cutover opening balance",
                "amount": str(abs(running)),
                "signed_amount": str(running),
                "reconstructed_running_balance": str(running),
                "status": "",
                "external_ref": "",
            }
        )
        for event in list_customer_financial_events(
            db, account_id, start=PAYMENT_ACTIVITY_AT, currency="NGN"
        ):
            signed = _money(event.signed_amount)
            running = _money(running + signed)
            output.append(
                {
                    "account_id": account_id,
                    "activity_at": _iso(event.occurred_at),
                    "row_type": getattr(
                        event.entry_type, "value", str(event.entry_type)
                    ),
                    "source_id": str(event.id),
                    "description": _event_description(event),
                    "amount": str(_money(event.amount)),
                    "signed_amount": str(signed),
                    "reconstructed_running_balance": str(running),
                    "status": _event_status(event.raw),
                    "external_ref": _event_external_ref(event.raw),
                }
            )
    return output


def export_reconstructed_balance_packet(
    db: Session,
    output_dir: Path,
    *,
    generated_at: datetime | None = None,
    include_transaction_rows: bool = True,
) -> dict[str, Any]:
    """Export the cutover reconstruction packet used for finance cleanup."""
    generated = generated_at or datetime.now(UTC)
    all_rows = iter_reconstructed_balance_rows(db)
    drift_rows = [row for row in all_rows if _is_drift_row(row)]
    transaction_rows = (
        _transaction_rows_for_drift_cases(db, drift_rows)
        if include_transaction_rows
        else []
    )
    overcredited = [
        row
        for row in drift_rows
        if Decimal(row["difference_current_minus_reconstructed"]) > 0
    ]
    understated = [
        row
        for row in drift_rows
        if Decimal(row["difference_current_minus_reconstructed"]) < 0
    ]
    manifest = {
        "cutover_opening_source": "subscribers.deposit / Splynx mirror net at cutover",
        "drift_count": len(drift_rows),
        "generated_at": generated.isoformat(),
        "outputs": [
            "all_reconstructed_balances.csv",
            "drift_cases.csv",
            "drift_case_statement_transactions.csv",
        ],
        "overcredited_count": len(overcredited),
        "overcredited_total": str(
            round_money(
                sum(
                    (
                        Decimal(row["difference_current_minus_reconstructed"])
                        for row in overcredited
                    ),
                    Decimal("0.00"),
                )
            )
        ),
        "payment_activity_from": PAYMENT_ACTIVITY_AT.isoformat(),
        "population": len(all_rows),
        "service_activity_from": CUTOVER_ACTIVITY_AT.isoformat(),
        "transaction_rows_for_drift_cases": len(transaction_rows),
        "understated_count": len(understated),
        "understated_total": str(
            round_money(
                sum(
                    (
                        abs(Decimal(row["difference_current_minus_reconstructed"]))
                        for row in understated
                    ),
                    Decimal("0.00"),
                )
            )
        ),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(
        output_dir / "all_reconstructed_balances.csv",
        all_rows,
        RECONSTRUCTED_BALANCE_FIELDS,
    )
    _write_csv(output_dir / "drift_cases.csv", drift_rows, RECONSTRUCTED_BALANCE_FIELDS)
    _write_csv(
        output_dir / "drift_case_statement_transactions.csv",
        transaction_rows,
        RECONSTRUCTED_TRANSACTION_FIELDS,
    )
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    return manifest


def build_reconstructed_balance_correction_items(
    db: Session,
    *,
    apply_overcredited: bool = False,
    snapshot_date: str | None = None,
) -> list[dict[str, str]]:
    """Build correction rows from the current reconstructed-balance drift."""
    snapshot = snapshot_date or datetime.now(UTC).date().isoformat()
    items: list[dict[str, str]] = []
    for row in iter_reconstructed_balance_rows(db):
        drift = Decimal(row["difference_current_minus_reconstructed"])
        if abs(drift) <= TOLERANCE:
            continue
        direction = _direction(drift)
        amount = abs(drift)
        entry_type = (
            LedgerEntryType.debit
            if direction == "overcredited"
            else LedgerEntryType.credit
        )
        decision = (
            "apply" if direction == "understated" or apply_overcredited else "hold"
        )
        action = (
            "debit_customer"
            if direction == "overcredited" and decision == "apply"
            else "debit_or_variance_review"
            if direction == "overcredited"
            else "credit_customer"
        )
        reason = (
            "finance_approved_overcredit_reconstructed_balance_true_down"
            if direction == "overcredited" and decision == "apply"
            else "customer_debit_requires_finance_row_approval"
            if direction == "overcredited"
            else "customer_favorable_reconstructed_balance_true_up"
        )
        memo = (
            f"{RECONSTRUCTED_TRUE_UP_MEMO_PREFIX} "
            f"[account_id={row['account_id']}] "
            f"[direction={direction}] "
            f"[amount={amount}] "
            f"[snapshot={snapshot}]"
        )
        items.append(
            {
                "account_id": row["account_id"],
                "subscriber_name": row["subscriber_name"],
                "subscriber_status": row["subscriber_status"],
                "current_available": row["current_local_available"],
                "reconstructed_balance": row["reconstructed_balance"],
                "drift": str(drift),
                "direction": direction,
                "action": action,
                "entry_type": entry_type.value,
                "amount": str(amount),
                "decision": decision,
                "reason": reason,
                "memo": memo,
            }
        )
    return items


def apply_reconstructed_balance_corrections(
    db: Session,
    items: list[dict[str, str]],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Apply approved correction items as internal adjustment ledger entries."""
    apply_count = 0
    hold_count = 0
    skip_count = 0
    credit_customer_amount = Decimal("0.00")
    debit_customer_amount = Decimal("0.00")
    held_overcredited_amount = Decimal("0.00")
    output_items: list[dict[str, str]] = []
    for item in items:
        amount = _money(item["amount"])
        item = dict(item)
        if item["decision"] != "apply":
            hold_count += 1
            held_overcredited_amount += amount
            output_items.append(item)
            continue
        existing = (
            db.query(LedgerEntry.id)
            .filter(LedgerEntry.account_id == coerce_uuid(item["account_id"]))
            .filter(LedgerEntry.memo == item["memo"])
            .filter(LedgerEntry.is_active.is_(True))
            .first()
        )
        if existing is not None:
            skip_count += 1
            item["decision"] = "skip"
            item["reason"] = "already_applied"
            output_items.append(item)
            continue
        apply_count += 1
        if item["entry_type"] == LedgerEntryType.credit.value:
            credit_customer_amount += amount
        else:
            debit_customer_amount += amount
        if apply:
            db.add(
                LedgerEntry(
                    account_id=coerce_uuid(item["account_id"]),
                    invoice_id=None,
                    payment_id=None,
                    entry_type=LedgerEntryType(item["entry_type"]),
                    source=LedgerSource.adjustment,
                    category=LedgerCategory.other,
                    amount=amount,
                    currency="NGN",
                    memo=item["memo"],
                )
            )
        output_items.append(item)

    return {
        "apply": apply,
        "generated_at": datetime.now(UTC).isoformat(),
        "counts": {
            "total": len(items),
            "apply": apply_count,
            "hold": hold_count,
            "skip": skip_count,
            "credit_customer_amount": str(round_money(credit_customer_amount)),
            "debit_customer_amount": str(round_money(debit_customer_amount)),
            "held_overcredited_amount": str(round_money(held_overcredited_amount)),
        },
        "items": output_items,
    }


def write_reconstructed_balance_correction_report(
    payload: dict[str, Any],
    *,
    json_path: Path,
    csv_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    _write_csv(csv_path, list(payload.get("items") or []), CUTOVER_CORRECTION_FIELDS)
