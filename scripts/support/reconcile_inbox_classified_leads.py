#!/usr/bin/env python
"""Report or repair classified Inbox sales candidates missing a Lead link.

The default mode is read-only and emits only PII-free identifiers and
classification values. ``--apply`` re-enters the canonical Sales owner for
each finding and deliberately suppresses all invitation delivery. Running
``--apply`` against staging or production requires explicit operator approval.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.sales import lead_intake


def _finding_payload(
    finding: lead_intake.ClassifiedLeadCandidateDrift,
) -> dict[str, object]:
    return {
        "conversation_id": str(finding.conversation_id),
        "message_id": str(finding.message_id),
        "classified_at": finding.classified_at.isoformat(),
        "intent": finding.classification.intent.value,
        "party_type": finding.classification.party_type.value,
        "review_reason": finding.review_reason.value if finding.review_reason else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create/link Leads through sales.lead_intake; never sends forms.",
    )
    parser.add_argument("--actor", help="Required audit actor label with --apply.")
    parser.add_argument("--reason", help="Required audit reason with --apply.")
    args = parser.parse_args()
    if not 1 <= args.days <= 365:
        parser.error("--days must be between 1 and 365")
    if not 1 <= args.limit <= 2_000:
        parser.error("--limit must be between 1 and 2000")
    if args.apply and (not args.actor or not args.reason):
        parser.error("--apply requires --actor and --reason")

    since = datetime.now(UTC) - timedelta(days=args.days)
    with db_session_adapter.owner_command_session() as db:
        findings = lead_intake.classified_candidate_drift(
            db,
            query=lead_intake.ClassifiedCandidateDriftQuery(
                since=since, limit=args.limit
            ),
        )
        payload: dict[str, object] = {
            "mode": "apply" if args.apply else "preview",
            "since": since.isoformat(),
            "count": len(findings),
            "findings": [_finding_payload(item) for item in findings],
        }
        if args.apply:
            db_session_adapter.release_read_transaction(db)
            repaired: list[dict[str, object]] = []
            failed: list[dict[str, object]] = []
            for finding in findings:
                if finding.review_reason is not None:
                    failed.append(
                        {
                            **_finding_payload(finding),
                            "error": "staff_review_required",
                            "message": "Identify the customer type before materializing this Lead.",
                        }
                    )
                    continue
                try:
                    outcome = lead_intake.assess_inbound(
                        db,
                        lead_intake.AssessInboundCommand(
                            context=CommandContext.system(
                                actor=args.actor,
                                scope="sales.lead_intake:historical-repair",
                                reason=args.reason,
                                idempotency_key=(
                                    f"classified-lead-repair:{finding.message_id}"
                                ),
                            ),
                            conversation_id=finding.conversation_id,
                            message_id=finding.message_id,
                            classification=finding.classification,
                            provider_label=finding.provider_label,
                            model_label=finding.model_label,
                            attribution=finding.attribution,
                            allow_invitation=False,
                        ),
                    )
                    repaired.append(
                        {
                            **_finding_payload(finding),
                            "action": outcome.action,
                            "lead_id": str(outcome.lead_id),
                        }
                    )
                except DomainError as exc:
                    failed.append(
                        {
                            **_finding_payload(finding),
                            "error": exc.code,
                            "message": exc.message,
                        }
                    )
            payload["repaired"] = repaired
            payload["failed"] = failed

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if args.apply and payload.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
