#!/usr/bin/env python3
"""Plan or apply the guarded 2026-07-20 CRM placeholder-name repair.

Dry-run is the default. Apply mode requires a named target, attributable actor
and reason, plus the exact SHA-256 digest printed by a fresh dry-run. Output is
PII-free: it contains UUIDs, audit IDs, field names, classifications, and
counts, but never customer names.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.audit import AuditEvent
from app.models.subscriber import Subscriber
from app.services.customer_identity_normalization import is_placeholder_customer_name
from app.services.customer_name_repairs import (
    CustomerNameRepairItem,
    CustomerNameState,
    RepairCustomerNamesCommand,
    repair_customer_names,
)
from app.services.owner_commands import CommandContext

ACTION = "crm_customer_identity_update"
REMEDIATION_ACTION = "crm_placeholder_name_remediated"
BATCH_ACTION = "crm_placeholder_name_remediation_applied"
DEFAULT_START = datetime(2026, 7, 20, tzinfo=UTC)
DEFAULT_END = datetime(2026, 7, 21, tzinfo=UTC)
IDENTITY_FIELDS = ("first_name", "last_name", "display_name")


@dataclass
class RecoveryCandidate:
    subscriber_id: UUID
    source_audit_ids: list[str]
    expected_current: dict[str, str | None] = field(default_factory=dict)
    replacement: dict[str, str | None] = field(default_factory=dict)
    restorations: dict[str, str | None] = field(default_factory=dict)
    already_restored_fields: list[str] = field(default_factory=list)
    conflict_fields: list[str] = field(default_factory=list)
    party_bound: bool = False

    @property
    def classification(self) -> str:
        if self.party_bound:
            return "skip_party_bound"
        if self.conflict_fields:
            return "skip_drift"
        if self.restorations:
            return "eligible"
        return "already_restored"

    def public_dict(self) -> dict[str, Any]:
        return {
            "subscriber_id": str(self.subscriber_id),
            "classification": self.classification,
            "restore_fields": sorted(self.restorations),
            "already_restored_fields": sorted(self.already_restored_fields),
            "conflict_fields": sorted(self.conflict_fields),
            "source_audit_ids": self.source_audit_ids,
        }


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _audit_changes(event: AuditEvent) -> dict[str, dict[str, Any]]:
    metadata = event.metadata_ if isinstance(event.metadata_, dict) else {}
    changes = metadata.get("changes")
    return changes if isinstance(changes, dict) else {}


def _is_placeholder_regression(event: AuditEvent) -> bool:
    changes = _audit_changes(event)
    return any(
        isinstance(change, dict)
        and str(change.get("old") or "").strip() != str(change.get("new") or "").strip()
        and is_placeholder_customer_name(
            None if change.get("new") is None else str(change.get("new"))
        )
        for field_name in IDENTITY_FIELDS
        if (change := changes.get(field_name)) is not None
    )


def _field_text(subscriber: Subscriber, name: str) -> str | None:
    value = getattr(subscriber, name)
    return None if value is None else str(value)


def plan_recovery(
    db: Session,
    *,
    start_at: datetime = DEFAULT_START,
    end_at: datetime = DEFAULT_END,
    account_numbers: set[str] | None = None,
    limit: int | None = None,
) -> list[RecoveryCandidate]:
    events = (
        db.query(AuditEvent)
        .filter(AuditEvent.action == ACTION)
        .filter(AuditEvent.entity_type == "subscriber")
        .filter(AuditEvent.occurred_at >= start_at)
        .filter(AuditEvent.occurred_at < end_at)
        .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
        .all()
    )
    incident_events: dict[UUID, list[AuditEvent]] = defaultdict(list)
    for event in events:
        if not event.entity_id or not _is_placeholder_regression(event):
            continue
        try:
            subscriber_id = UUID(event.entity_id)
        except ValueError:
            continue
        incident_events[subscriber_id].append(event)
    if not incident_events:
        return []

    subscribers = {
        row.id: row
        for row in db.query(Subscriber)
        .filter(Subscriber.id.in_(tuple(incident_events)))
        .all()
    }
    candidates: list[RecoveryCandidate] = []
    for subscriber_id, source_events in incident_events.items():
        subscriber = subscribers.get(subscriber_id)
        if subscriber is None:
            continue
        number = subscriber.subscriber_number or str(subscriber.id)
        if account_numbers and number not in account_numbers:
            continue

        first_old: dict[str, str | None] = {}
        latest_new: dict[str, str | None] = {}
        for event in source_events:
            changes = _audit_changes(event)
            for field_name in IDENTITY_FIELDS:
                change = changes.get(field_name)
                if not isinstance(change, dict):
                    continue
                old = change.get("old")
                new = change.get("new")
                first_old.setdefault(field_name, None if old is None else str(old))
                latest_new[field_name] = None if new is None else str(new)

        candidate = RecoveryCandidate(
            subscriber_id=subscriber.id,
            source_audit_ids=[str(event.id) for event in source_events],
            party_bound=subscriber.party_id is not None,
        )
        missing_evidence = set(IDENTITY_FIELDS) - set(first_old)
        if missing_evidence:
            candidate.conflict_fields.extend(
                f"{field_name}:missing_evidence"
                for field_name in sorted(missing_evidence)
            )
            candidates.append(candidate)
            continue

        candidate.replacement = dict(first_old)
        candidate.expected_current = {
            field_name: _field_text(subscriber, field_name)
            for field_name in IDENTITY_FIELDS
        }
        replacement_display = (
            first_old.get("display_name")
            or " ".join(
                str(first_old.get(field_name) or "").strip()
                for field_name in ("first_name", "last_name")
            ).strip()
        )
        if (
            not str(first_old.get("first_name") or "").strip()
            or not str(first_old.get("last_name") or "").strip()
            or is_placeholder_customer_name(replacement_display)
        ):
            candidate.conflict_fields.append("replacement:not_authoritative")
            candidates.append(candidate)
            continue

        for field_name in IDENTITY_FIELDS:
            current = candidate.expected_current[field_name]
            original = first_old[field_name]
            incident = latest_new[field_name]
            if current == original:
                candidate.already_restored_fields.append(field_name)
            elif current == incident:
                candidate.restorations[field_name] = original
            else:
                candidate.conflict_fields.append(field_name)
        candidates.append(candidate)

    candidates.sort(key=lambda item: str(item.subscriber_id))
    return candidates[:limit] if limit is not None else candidates


def recovery_manifest_digest(candidates: list[RecoveryCandidate]) -> str:
    payload = {
        "version": 1,
        "action": REMEDIATION_ACTION,
        "repairs": [
            {
                "subscriber_id": str(candidate.subscriber_id),
                "source_audit_ids": sorted(candidate.source_audit_ids),
                "expected_current": candidate.expected_current,
                "replacement": candidate.replacement,
            }
            for candidate in candidates
            if candidate.classification == "eligible"
        ],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def apply_recovery(
    db: Session,
    candidates: list[RecoveryCandidate],
    *,
    manifest_digest: str,
    actor_id: str,
    reason: str,
    target: str,
) -> tuple[int, bool]:
    eligible = [
        candidate for candidate in candidates if candidate.classification == "eligible"
    ]
    if not eligible:
        raise ValueError("repair manifest has no eligible subscribers")
    repairs = tuple(
        CustomerNameRepairItem(
            subscriber_id=candidate.subscriber_id,
            expected_current=CustomerNameState(**candidate.expected_current),
            replacement=CustomerNameState(**candidate.replacement),
            source_audit_ids=tuple(UUID(value) for value in candidate.source_audit_ids),
        )
        for candidate in eligible
    )
    outcome = repair_customer_names(
        db,
        RepairCustomerNamesCommand(
            context=CommandContext.system(
                actor=actor_id,
                scope=target,
                reason=reason,
                idempotency_key=manifest_digest,
            ),
            manifest_digest=manifest_digest,
            target=target,
            repairs=repairs,
        ),
    )
    return outcome.applied_count, outcome.already_applied


def _summary(
    candidates: list[RecoveryCandidate],
    *,
    mode: str,
    manifest_digest: str,
) -> dict[str, Any]:
    counts: dict[str, int] = defaultdict(int)
    for candidate in candidates:
        counts[candidate.classification] += 1
    return {
        "mode": mode,
        "manifest_digest": manifest_digest,
        "candidates": len(candidates),
        "eligible": counts["eligible"],
        "already_restored": counts["already_restored"],
        "skipped_drift": counts["skip_drift"],
        "skipped_party_bound": counts["skip_party_bound"],
        "accounts": [candidate.public_dict() for candidate in candidates],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_parse_timestamp, default=DEFAULT_START)
    parser.add_argument("--end", type=_parse_timestamp, default=DEFAULT_END)
    parser.add_argument("--account-number", action="append", default=[])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-digest")
    parser.add_argument("--target")
    parser.add_argument("--actor-id")
    parser.add_argument("--reason")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print aggregate counts without per-account UUIDs.",
    )
    args = parser.parse_args()
    if args.start >= args.end:
        parser.error("--start must be earlier than --end")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.apply and args.limit is not None:
        parser.error("--limit is not allowed with --apply")
    if args.apply and not all(
        (args.confirm_digest, args.target, args.actor_id, args.reason)
    ):
        parser.error(
            "--apply requires --confirm-digest, --target, --actor-id, and --reason"
        )

    account_numbers = {value.strip() for value in args.account_number if value.strip()}
    with SessionLocal() as planner_db:
        candidates = plan_recovery(
            planner_db,
            start_at=args.start,
            end_at=args.end,
            account_numbers=account_numbers or None,
            limit=args.limit,
        )
    digest = recovery_manifest_digest(candidates)
    report = _summary(
        candidates,
        mode="apply" if args.apply else "read_only",
        manifest_digest=digest,
    )
    if args.apply:
        if args.confirm_digest != digest:
            parser.error("--confirm-digest does not match the current repair manifest")
        with SessionLocal() as command_db:
            applied, already_applied = apply_recovery(
                command_db,
                candidates,
                manifest_digest=digest,
                actor_id=args.actor_id,
                reason=args.reason,
                target=args.target,
            )
        report["status"] = "already_applied" if already_applied else "applied"
        report["applied"] = applied
    if args.summary_only:
        report.pop("accounts", None)
    print(json.dumps(report, indent=2, sort_keys=True))
    if not args.apply:
        print("READ ONLY — pass the exact digest and named target to --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
