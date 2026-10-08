#!/usr/bin/env python
"""Explain blocking prepaid coverage quarantine work items, read-only.

For each selected account (``--account-id``, repeatable) or, by default, every
open ``prepaid-coverage:quarantine:<account_id>`` work item, list the exact
records behind ``malformed_paid_invoice_period`` and ``malformed_renewal_origin``
and the reviewed owner (if any) that may correct each one. Procedure:
``docs/runbooks/PREPAID_COVERAGE_QUARANTINE_FINANCE_REVIEW.md``.

The command cannot write: it runs in one REPEATABLE READ, READ ONLY snapshot
(PostgreSQL) and the session is rolled back. It never infers a period from
memo or description text and never applies a correction.

Exit codes: 0 no malformed evidence found; 2 at least one finding reported.

    poetry run python -m scripts.billing.diagnose_prepaid_coverage_quarantine
    poetry run python -m scripts.billing.diagnose_prepaid_coverage_quarantine \\
        --account-id <uuid> [--json]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from app.services.prepaid_coverage_quarantine_review import (
    AccountQuarantineReview,
    InvoicePeriodFinding,
    PrepaidCoverageQuarantineReview,
    PrepaidCoverageQuarantineReviewQuery,
    RenewalOriginFinding,
    ResolutionOption,
    review_prepaid_coverage_quarantine,
)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone offset")
    return parsed


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("identifier must be a UUID") from exc


def _jsonable(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        payload = {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
        if isinstance(value, InvoicePeriodFinding | RenewalOriginFinding):
            payload["sanctioned_repair_available"] = value.sanctioned_repair_available
        if isinstance(value, AccountQuarantineReview):
            payload["blocking_reasons"] = _jsonable(value.blocking_reasons)
            payload["finding_count"] = value.finding_count
        if isinstance(value, PrepaidCoverageQuarantineReview):
            payload["finding_count"] = value.finding_count
        return payload
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, UUID):
        return str(value)
    return value


def review_payload(review: PrepaidCoverageQuarantineReview) -> dict[str, object]:
    payload = _jsonable(review)
    if not isinstance(payload, dict):
        raise TypeError("review must serialize to an object")
    payload["financial_state_changed"] = False
    return payload


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "NULL"


def _options_text(options: tuple[ResolutionOption, ...]) -> list[str]:
    lines: list[str] = []
    for option in options:
        label = "SANCTIONED" if option.sanctioned else "NO SANCTIONED REPAIR"
        lines.append(f"      - [{label}] {option.route.value}")
        lines.append(f"        when: {option.when}")
        if option.owner:
            lines.append(f"        owner: {option.owner}")
        if option.runbook:
            lines.append(f"        runbook: {option.runbook}")
        if option.command:
            lines.append(f"        command: {option.command}")
        if option.missing_capability:
            lines.append(f"        needs engineering: {option.missing_capability}")
    return lines


def review_text(review: PrepaidCoverageQuarantineReview) -> str:
    out = [
        f"Prepaid coverage quarantine review as of {review.as_of.isoformat()}",
        f"Runbook: {review.runbook}",
        "Read-only: no financial state changed.",
        "",
    ]
    if not review.accounts:
        out.append("No open prepaid-coverage quarantine work items.")
    for account in review.accounts:
        out.append(f"Account {account.account_id}")
        if account.work_item is None:
            out.append("  work item: none open")
        else:
            item = account.work_item
            out.append(
                f"  work item: {item.fingerprint} status={item.status} "
                f"reasons={','.join(item.reason_codes) or '-'} "
                f"sla_due_at={item.sla_due_at or '-'}"
            )
        blocking = ",".join(reason.value for reason in account.blocking_reasons)
        out.append(f"  quarantined now: {blocking or 'none'}")
        for state in account.subscriptions:
            out.append(
                f"    subscription {state.subscription_id}: "
                f"{state.decision.value}/{state.reason.value}"
            )
        for finding in account.invoice_findings:
            out.append(
                f"  malformed_paid_invoice_period: invoice {finding.invoice_id} "
                f"number={finding.invoice_number or '-'} "
                f"splynx_id={finding.splynx_invoice_id or '-'}"
            )
            out.append(
                f"    defect={finding.defect.value} "
                f"period_start={_iso(finding.billing_period_start)} "
                f"period_end={_iso(finding.billing_period_end)}"
            )
            out.append(
                f"    {finding.currency} total={finding.total:.2f} "
                f"balance_due={finding.balance_due:.2f} "
                f"allocated_payments={finding.allocated_payments:.2f} "
                f"credit_notes={finding.applied_credit_notes:.2f} "
                f"paid_at={_iso(finding.paid_at)}"
            )
            for line in finding.lines:
                entitlements = (
                    ";".join(
                        f"{row.entitlement_id}[{row.status}] "
                        f"{row.starts_at.isoformat()}..{row.ends_at.isoformat()}"
                        for row in line.source_entitlements
                    )
                    or "none"
                )
                out.append(
                    f"    line {line.line_id} subscription={line.subscription_id} "
                    f"amount={line.amount:.2f} kind={line.line_kind or '-'} "
                    f"scoped={'yes' if line.in_quarantine_scope else 'no'} "
                    f"metadata_period={_iso(line.metadata_period_start)}.."
                    f"{_iso(line.metadata_period_end)}"
                    f"({line.metadata_period_source or '-'}) "
                    f"entitlements={entitlements}"
                )
            out.append(
                f"    structured period proof={finding.period_proof.value} "
                f"{_iso(finding.proven_period_start)}..{_iso(finding.proven_period_end)}"
            )
            out.extend(_options_text(finding.options))
        for origin in account.renewal_origin_findings:
            out.append(
                f"  malformed_renewal_origin: adjustment {origin.adjustment_id} "
                f"origin_ref={origin.origin_ref!r}"
            )
            out.append(
                f"    defects={','.join(defect.value for defect in origin.defects)} "
                f"parsed_subscription={origin.parsed_subscription_id or '-'} "
                f"parsed_period={_iso(origin.parsed_starts_at)}.."
                f"{_iso(origin.parsed_ends_at)}"
            )
            ledger = origin.ledger
            out.append(
                f"    adjustment {origin.currency} {origin.amount:.2f} "
                f"account={origin.account_id} | ledger {ledger.ledger_entry_id} "
                f"{ledger.currency} {ledger.amount:.2f} account={ledger.account_id} "
                f"source={ledger.source or '-'} invoice={ledger.invoice_id or '-'}"
            )
            for row in origin.linked_entitlements:
                out.append(
                    f"    linked entitlement {row.entitlement_id}[{row.status}] "
                    f"subscription={row.subscription_id} "
                    f"{row.starts_at.isoformat()}..{row.ends_at.isoformat()} "
                    f"{row.currency} {row.amount_funded:.2f}"
                )
            out.extend(_options_text(origin.options))
        if not account.finding_count:
            out.append(
                "  no malformed invoice-period or renewal-origin evidence; if the "
                "work item is still open it closes on the next prepaid sweep"
            )
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--account-id", action="append", type=_uuid, default=[])
    parser.add_argument("--as-of", type=_timestamp)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    from app.db import read_only_snapshot_session

    with read_only_snapshot_session() as db:
        review = review_prepaid_coverage_quarantine(
            db,
            PrepaidCoverageQuarantineReviewQuery(
                account_ids=tuple(args.account_id) or None,
                as_of=args.as_of,
            ),
        )
        rendered = (
            json.dumps(review_payload(review), indent=2, sort_keys=True)
            if args.json
            else review_text(review)
        )
    print(rendered)
    return 2 if review.finding_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
