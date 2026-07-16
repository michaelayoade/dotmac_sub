"""Validate reviewed prepaid-funding gaps and emit owner action packets.

This command never changes a balance, source table, payment, service schedule,
or reconstruction baseline. It binds one reviewed decision to every blocker in
an exact exporter manifest and produces a sanitized action plan. A blocker is
cleared only when its owning source is corrected and the independent replay no
longer reports it.

Bank statement evidence can authorize a canonical payment action only when the
reviewer attests definitive customer attribution and supplies a non-secret
evidence reference. Amount/date coincidence is explicitly insufficient. Raw
statement rows, narrations, customer names, bank accounts, and transaction
references are rejected from the decision packet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID

BLOCKER_SCHEMA = "dotmac.prepaid_funding_blockers.v1"
DECISION_SCHEMA = "dotmac.prepaid_funding_gap_decisions.v1"
ACTION_PLAN_SCHEMA = "dotmac.prepaid_funding_gap_actions.v1"

SOURCE_EVIDENCE_REQUIRED = "source_evidence_required"
CANONICAL_PAYMENT_REQUIRED = "canonical_payment_required"
QUARANTINE = "quarantine"
DISPOSITIONS = {
    SOURCE_EVIDENCE_REQUIRED,
    CANONICAL_PAYMENT_REQUIRED,
    QUARANTINE,
}

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REASON_RE = re.compile(r"^[a-z0-9_]+$")
_COMMON_DECISION_FIELDS = {
    "account_id",
    "reason",
    "disposition",
    "evidence_ref",
}
_PAYMENT_DECISION_FIELDS = _COMMON_DECISION_FIELDS | {
    "amount",
    "currency",
    "occurred_at",
    "definitive_attribution",
    "evidence_sha256",
}
_MANIFEST_FIELDS = {
    "schema",
    "source",
    "captured_at",
    "financial_handoff_at",
    "currency",
    "candidate_accounts",
    "candidate_cohort_sha256",
    "blockers",
}
_DECISION_PACKET_FIELDS = {
    "schema",
    "blocker_manifest_sha256",
    "review_id",
    "reviewed_by",
    "reviewed_at",
    "decisions",
}


class GapAdjudicationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedBlockerManifest:
    payload: dict[str, Any]
    sha256: str
    captured_at: datetime
    financial_handoff_at: datetime
    currency: str
    blockers: frozenset[tuple[str, str]]


def _payload_sha256(payload: dict[str, Any]) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _require_exact_fields(
    payload: dict[str, Any], expected: set[str], *, label: str
) -> None:
    actual = set(payload)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("unexpected=" + ",".join(extra))
        raise GapAdjudicationError(f"{label} fields invalid: {'; '.join(details)}")


def _timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise GapAdjudicationError(f"{label} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise GapAdjudicationError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise GapAdjudicationError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _reference(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 200 or "\n" in text or "\r" in text:
        raise GapAdjudicationError(
            f"{label} must be a non-secret single-line reference up to 200 characters"
        )
    return text


def _account_id(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise GapAdjudicationError(f"invalid blocker account_id: {value!s}") from exc


def _reason(value: object) -> str:
    reason = str(value or "").strip()
    if not _REASON_RE.fullmatch(reason):
        raise GapAdjudicationError(f"invalid blocker reason: {reason!s}")
    return reason


def _currency(value: object) -> str:
    currency = str(value or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise GapAdjudicationError("currency must be a three-letter code")
    return currency


def _amount(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise GapAdjudicationError("payment amount is invalid") from exc
    if (
        not amount.is_finite()
        or amount <= 0
        or amount != amount.quantize(Decimal("0.01"))
    ):
        raise GapAdjudicationError(
            "payment amount must be positive with no more than two decimal places"
        )
    return amount


def validate_blocker_manifest(payload: dict[str, Any]) -> ValidatedBlockerManifest:
    wrapper_hash = payload.get("blocker_manifest_sha256")
    manifest = payload.get("blocker_manifest", payload)
    if not isinstance(manifest, dict):
        raise GapAdjudicationError("blocker_manifest must be an object")
    _require_exact_fields(manifest, _MANIFEST_FIELDS, label="blocker manifest")
    if manifest["schema"] != BLOCKER_SCHEMA:
        raise GapAdjudicationError("unsupported blocker manifest schema")
    source = _reference(manifest["source"], label="source")
    captured_at = _timestamp(manifest["captured_at"], label="captured_at")
    handoff_at = _timestamp(
        manifest["financial_handoff_at"], label="financial_handoff_at"
    )
    if handoff_at > captured_at:
        raise GapAdjudicationError("financial handoff is later than the snapshot")
    currency = _currency(manifest["currency"])
    candidate_count = manifest["candidate_accounts"]
    if type(candidate_count) is not int or candidate_count < 0:
        raise GapAdjudicationError("candidate_accounts must be a non-negative integer")
    cohort_hash = str(manifest["candidate_cohort_sha256"] or "").strip().lower()
    if not _HASH_RE.fullmatch(cohort_hash):
        raise GapAdjudicationError("candidate_cohort_sha256 is invalid")
    blocker_rows = manifest["blockers"]
    if not isinstance(blocker_rows, list):
        raise GapAdjudicationError("blockers must be a list")
    blockers: set[tuple[str, str]] = set()
    for row in blocker_rows:
        if not isinstance(row, dict):
            raise GapAdjudicationError("blocker rows must be objects")
        _require_exact_fields(row, {"account_id", "reason"}, label="blocker row")
        blocker = (_account_id(row["account_id"]), _reason(row["reason"]))
        if blocker in blockers:
            raise GapAdjudicationError(
                f"duplicate blocker decision key: {blocker[0]}:{blocker[1]}"
            )
        blockers.add(blocker)
    if len({account_id for account_id, _ in blockers}) > candidate_count:
        raise GapAdjudicationError("blocked accounts exceed the candidate cohort")

    normalized_manifest = {
        **manifest,
        "source": source,
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "financial_handoff_at": handoff_at.isoformat().replace("+00:00", "Z"),
        "currency": currency,
        "candidate_cohort_sha256": cohort_hash,
        "blockers": [
            {"account_id": account_id, "reason": reason}
            for account_id, reason in sorted(blockers)
        ],
    }
    digest = _payload_sha256(normalized_manifest)
    if wrapper_hash is not None and str(wrapper_hash).strip().lower() != digest:
        raise GapAdjudicationError("blocker manifest hash does not match its content")
    return ValidatedBlockerManifest(
        payload=normalized_manifest,
        sha256=digest,
        captured_at=captured_at,
        financial_handoff_at=handoff_at,
        currency=currency,
        blockers=frozenset(blockers),
    )


def _payment_action(
    decision: dict[str, Any], manifest: ValidatedBlockerManifest
) -> dict[str, Any]:
    if decision["definitive_attribution"] is not True:
        raise GapAdjudicationError(
            "canonical payment requires definitive customer attribution; "
            "amount/date coincidence is insufficient"
        )
    amount = _amount(decision["amount"])
    currency = _currency(decision["currency"])
    if currency != manifest.currency:
        raise GapAdjudicationError(
            "canonical payment currency does not match the reconstruction manifest"
        )
    occurred_at = _timestamp(decision["occurred_at"], label="payment occurred_at")
    if occurred_at < manifest.financial_handoff_at:
        raise GapAdjudicationError(
            "pre-handoff evidence belongs in the reviewed source baseline, "
            "not a new canonical payment"
        )
    if occurred_at > manifest.captured_at:
        raise GapAdjudicationError("payment occurred after the replay snapshot")
    evidence_ref = _reference(decision["evidence_ref"], label="evidence_ref")
    evidence_sha256 = str(decision["evidence_sha256"] or "").strip().lower()
    if not _HASH_RE.fullmatch(evidence_sha256):
        raise GapAdjudicationError("payment evidence_sha256 is invalid")
    return {
        "action_owner": "financial.payments",
        "next_action": "preview_and_confirm_missing_payment",
        "amount": f"{amount:.2f}",
        "currency": currency,
        "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
        "evidence_ref": evidence_ref,
        "evidence_sha256": evidence_sha256,
        "idempotency_key": "prepaid-gap-payment:" + evidence_sha256,
    }


def build_gap_action_plan(
    blocker_payload: dict[str, Any],
    decision_payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    manifest = validate_blocker_manifest(blocker_payload)
    _require_exact_fields(
        decision_payload, _DECISION_PACKET_FIELDS, label="decision packet"
    )
    if decision_payload["schema"] != DECISION_SCHEMA:
        raise GapAdjudicationError("unsupported decision packet schema")
    reviewed_hash = (
        str(decision_payload["blocker_manifest_sha256"] or "").strip().lower()
    )
    if reviewed_hash != manifest.sha256:
        raise GapAdjudicationError(
            "decision packet is not bound to this blocker manifest"
        )
    review_id = _reference(decision_payload["review_id"], label="review_id")
    reviewed_by = _reference(decision_payload["reviewed_by"], label="reviewed_by")
    reviewed_at = _timestamp(decision_payload["reviewed_at"], label="reviewed_at")
    effective_now = now or datetime.now(UTC)
    if effective_now.tzinfo is None:
        raise GapAdjudicationError("now must include a timezone")
    if reviewed_at > effective_now.astimezone(UTC):
        raise GapAdjudicationError("reviewed_at is in the future")
    if reviewed_at < manifest.captured_at:
        raise GapAdjudicationError("reviewed_at predates the blocker manifest")

    rows = decision_payload["decisions"]
    if not isinstance(rows, list):
        raise GapAdjudicationError("decisions must be a list")
    decisions: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise GapAdjudicationError("decision rows must be objects")
        disposition = str(row.get("disposition") or "").strip()
        if disposition not in DISPOSITIONS:
            raise GapAdjudicationError(f"unsupported disposition: {disposition}")
        expected_fields = (
            _PAYMENT_DECISION_FIELDS
            if disposition == CANONICAL_PAYMENT_REQUIRED
            else _COMMON_DECISION_FIELDS
        )
        _require_exact_fields(row, expected_fields, label="decision row")
        key = (_account_id(row["account_id"]), _reason(row["reason"]))
        if key in decisions:
            raise GapAdjudicationError(f"duplicate decision: {key[0]}:{key[1]}")
        decisions[key] = row

    decision_keys = set(decisions)
    missing = sorted(manifest.blockers - decision_keys)
    extra = sorted(decision_keys - manifest.blockers)
    if missing or extra:
        details = []
        if missing:
            details.append(
                "missing="
                + ",".join(f"{account}:{reason}" for account, reason in missing)
            )
        if extra:
            details.append(
                "unexpected="
                + ",".join(f"{account}:{reason}" for account, reason in extra)
            )
        raise GapAdjudicationError(
            "decisions must cover the exact blocker manifest: " + "; ".join(details)
        )

    actions = []
    disposition_counts: dict[str, int] = {}
    payment_evidence_owners: dict[str, tuple[str, str]] = {}
    for key in sorted(decisions):
        decision = decisions[key]
        disposition = str(decision["disposition"])
        evidence_ref = _reference(decision["evidence_ref"], label="evidence_ref")
        action: dict[str, Any] = {
            "account_id": key[0],
            "reason": key[1],
            "disposition": disposition,
            "evidence_ref": evidence_ref,
        }
        if disposition == CANONICAL_PAYMENT_REQUIRED:
            payment_action = _payment_action(decision, manifest)
            idempotency_key = str(payment_action["idempotency_key"])
            existing_owner = payment_evidence_owners.get(idempotency_key)
            if existing_owner is not None:
                raise GapAdjudicationError(
                    "one payment evidence hash cannot authorize multiple blocker "
                    f"actions: {existing_owner[0]}:{existing_owner[1]} and "
                    f"{key[0]}:{key[1]}"
                )
            payment_evidence_owners[idempotency_key] = key
            action.update(payment_action)
        elif disposition == SOURCE_EVIDENCE_REQUIRED:
            action.update(
                {
                    "action_owner": "financial.prepaid_funding_reconstruction",
                    "next_action": "replace_independent_source_evidence_and_rerun",
                }
            )
        else:
            action.update(
                {
                    "action_owner": "financial.prepaid_funding_reconstruction",
                    "next_action": "keep_quarantined_and_rerun_after_resolution",
                }
            )
        actions.append(action)
        disposition_counts[disposition] = disposition_counts.get(disposition, 0) + 1

    return {
        "schema": ACTION_PLAN_SCHEMA,
        "blocker_manifest_sha256": manifest.sha256,
        "candidate_cohort_sha256": manifest.payload["candidate_cohort_sha256"],
        "review_id": review_id,
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at.isoformat().replace("+00:00", "Z"),
        "status": (
            "blocked_pending_owner_actions_and_independent_replay"
            if actions
            else "independent_replay_has_no_blockers"
        ),
        "action_count": len(actions),
        "disposition_counts": dict(sorted(disposition_counts.items())),
        "actions": actions,
    }


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise GapAdjudicationError(f"JSON root must be an object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blockers", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    plan = build_gap_action_plan(
        _read_json(args.blockers),
        _read_json(args.decisions),
    )
    _write_json(args.out, plan, overwrite=args.overwrite)
    print(
        json.dumps(
            {key: value for key, value in plan.items() if key != "actions"},
            indent=2,
            sort_keys=True,
        )
    )
    print("Action plan written; blockers remain until independent replay is clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
