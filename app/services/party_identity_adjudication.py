"""Pure, read-only adjudication and Party backfill plan owner.

The identity audit produces evidence, not permission to write. This module
validates explicit human decisions against a current audit digest and produces
a PII-free dry-run plan. It has no database session and cannot create Parties,
bind accounts, merge identities, quarantine rows, or assign roles.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.models.party import (
    PartyDataClassification,
    PartyIdentityStatus,
    PartyType,
)
from app.services.party_identity_audit import (
    DuplicateConfidence,
    SubscriberAuditRow,
    SubscriberIdentityAudit,
    subscriber_audit_row_fingerprint,
    subscriber_identity_audit_digest,
)


class PartyAdjudicationAction(StrEnum):
    create_active_party = "create_active_party"
    create_quarantined_party = "create_quarantined_party"
    defer = "defer"


class PartyDisplayNameSource(StrEnum):
    subscriber_full_name = "subscriber_full_name"
    subscriber_display_name = "subscriber_display_name"
    company_name = "company_name"
    legal_name = "legal_name"


_PERSON_DISPLAY_SOURCES = frozenset(
    {
        PartyDisplayNameSource.subscriber_full_name,
        PartyDisplayNameSource.subscriber_display_name,
    }
)
_ORGANIZATION_DISPLAY_SOURCES = frozenset(
    {
        PartyDisplayNameSource.subscriber_display_name,
        PartyDisplayNameSource.company_name,
        PartyDisplayNameSource.legal_name,
    }
)
_MAX_FUTURE_REVIEW_SKEW = timedelta(minutes=5)


class PartyAdjudicationError(ValueError):
    """Raised when reviewed decisions cannot produce a safe dry-run plan."""

    def __init__(self, errors: list[str] | tuple[str, ...]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class PartyIdentityDecision:
    decision_id: UUID
    subscriber_id: UUID
    audit_digest: str
    row_fingerprint: str
    action: str
    reviewer: str
    reviewed_at: datetime
    reason: str
    planned_party_id: UUID | None = None
    identity_source_subscriber_id: UUID | None = None
    party_type: str | None = None
    data_classification: str | None = None
    display_name_source: str | None = None


@dataclass(frozen=True)
class _ValidatedDecision:
    decision: PartyIdentityDecision
    row: SubscriberAuditRow
    action: PartyAdjudicationAction
    planned_party_id: UUID | None = None
    identity_source_subscriber_id: UUID | None = None
    party_type: PartyType | None = None
    data_classification: PartyDataClassification | None = None
    display_name_source: PartyDisplayNameSource | None = None


@dataclass(frozen=True)
class PartyBackfillBinding:
    decision_id: UUID
    subscriber_id: UUID
    planned_party_id: UUID
    action: PartyAdjudicationAction
    row_fingerprint: str
    reviewed_at: datetime
    reviewer_sha256: str
    reason_sha256: str

    def digest_value(self) -> dict[str, str]:
        return {
            "decision_id": str(self.decision_id),
            "subscriber_id": str(self.subscriber_id),
            "planned_party_id": str(self.planned_party_id),
            "action": self.action.value,
            "row_fingerprint": self.row_fingerprint,
            "reviewed_at": self.reviewed_at.isoformat(),
            "reviewer_sha256": self.reviewer_sha256,
            "reason_sha256": self.reason_sha256,
        }


@dataclass(frozen=True)
class DeferredPartyDecision:
    decision_id: UUID
    subscriber_id: UUID
    row_fingerprint: str
    reviewed_at: datetime
    reviewer_sha256: str
    reason_sha256: str

    def digest_value(self) -> dict[str, str]:
        return {
            "decision_id": str(self.decision_id),
            "subscriber_id": str(self.subscriber_id),
            "row_fingerprint": self.row_fingerprint,
            "reviewed_at": self.reviewed_at.isoformat(),
            "reviewer_sha256": self.reviewer_sha256,
            "reason_sha256": self.reason_sha256,
        }


@dataclass(frozen=True)
class PlannedPartyGroup:
    planned_party_id: UUID
    party_type: PartyType
    data_classification: PartyDataClassification
    target_status: PartyIdentityStatus
    identity_source_subscriber_id: UUID
    display_name_source: PartyDisplayNameSource
    subscriber_ids: tuple[UUID, ...]
    decision_ids: tuple[UUID, ...]

    def digest_value(self) -> dict[str, Any]:
        return {
            "planned_party_id": str(self.planned_party_id),
            "party_type": self.party_type.value,
            "data_classification": self.data_classification.value,
            "target_status": self.target_status.value,
            "identity_source_subscriber_id": str(self.identity_source_subscriber_id),
            "display_name_source": self.display_name_source.value,
            "subscriber_ids": [str(value) for value in self.subscriber_ids],
            "decision_ids": [str(value) for value in self.decision_ids],
        }


@dataclass(frozen=True)
class PartyBackfillPlan:
    audit_digest: str
    audit_generated_at: datetime | None
    planned_at: datetime
    total_audit_rows: int
    reviewed_decisions: int
    unreviewed_rows: int
    groups: tuple[PlannedPartyGroup, ...]
    bindings: tuple[PartyBackfillBinding, ...]
    deferred: tuple[DeferredPartyDecision, ...]

    def digest_payload(self) -> dict[str, Any]:
        """Return the canonical PII-free manifest protected by plan_digest."""

        return {
            "contract_version": 1,
            "audit_digest": self.audit_digest,
            "groups": [group.digest_value() for group in self.groups],
            "bindings": [binding.digest_value() for binding in self.bindings],
            "deferred": [item.digest_value() for item in self.deferred],
        }

    @property
    def plan_digest(self) -> str:
        return party_backfill_plan_digest(self.digest_payload())

    def summary(self) -> dict[str, Any]:
        actions = Counter(binding.action.value for binding in self.bindings)
        classifications = Counter(
            group.data_classification.value for group in self.groups
        )
        party_types = Counter(group.party_type.value for group in self.groups)
        return {
            "plan_digest": self.plan_digest,
            "audit_digest": self.audit_digest,
            "audit_generated_at": (
                self.audit_generated_at.isoformat() if self.audit_generated_at else None
            ),
            "planned_at": self.planned_at.isoformat(),
            "total_audit_rows": self.total_audit_rows,
            "reviewed_decisions": self.reviewed_decisions,
            "unreviewed_rows": self.unreviewed_rows,
            "planned_parties": len(self.groups),
            "planned_bindings": len(self.bindings),
            "deferred": len(self.deferred),
            "actions": dict(sorted(actions.items())),
            "party_types": dict(sorted(party_types.items())),
            "data_classifications": dict(sorted(classifications.items())),
            "artifact_contract": {
                "read_only": True,
                "execution_supported": False,
                "execution_requires_separate_approval": True,
                "contains_raw_contact_values": False,
                "contains_display_names": False,
                "contains_reason_text": False,
                "automatic_merge_allowed": False,
            },
        }


def party_backfill_plan_digest(manifest: dict[str, Any]) -> str:
    """Hash one canonical PII-free plan or durable execution manifest."""

    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()


def _nonblank(value: str | None, field_name: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required")
    return cleaned


def _enum_value(value: str | None, enum_cls, field_name: str):
    raw = (value or "").strip().lower()
    try:
        return enum_cls(raw)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_cls)
        raise ValueError(
            f"invalid {field_name} '{raw}'; expected one of: {allowed}"
        ) from exc


def _has_action_fields(decision: PartyIdentityDecision) -> bool:
    return any(
        (
            decision.planned_party_id,
            decision.identity_source_subscriber_id,
            (decision.party_type or "").strip(),
            (decision.data_classification or "").strip(),
            (decision.display_name_source or "").strip(),
        )
    )


def _validate_decision(
    decision: PartyIdentityDecision,
    *,
    row: SubscriberAuditRow,
    current_audit_digest: str,
    planned_at: datetime,
    all_subscriber_ids: set[UUID],
    existing_party_ids: set[UUID],
) -> _ValidatedDecision:
    if decision.audit_digest != current_audit_digest:
        raise ValueError("audit_digest does not match the current audit facts")
    current_row_fingerprint = subscriber_audit_row_fingerprint(row)
    if decision.row_fingerprint != current_row_fingerprint:
        raise ValueError("row_fingerprint does not match the current subscriber facts")
    reviewer = _nonblank(decision.reviewer, "reviewer")
    reason = _nonblank(decision.reason, "reason")
    if decision.reviewed_at.tzinfo is None:
        raise ValueError("reviewed_at must be timezone-aware")
    if decision.reviewed_at > planned_at + _MAX_FUTURE_REVIEW_SKEW:
        raise ValueError("reviewed_at is in the future")
    action = _enum_value(decision.action, PartyAdjudicationAction, "action")

    # Normalize now so hashing in the output cannot depend on surrounding
    # whitespace, while preserving the original protected decision packet.
    normalized = PartyIdentityDecision(
        **{
            **decision.__dict__,
            "reviewer": reviewer,
            "reason": reason,
        }
    )
    if action is PartyAdjudicationAction.defer:
        if _has_action_fields(decision):
            raise ValueError("defer decisions must not carry Party creation fields")
        return _ValidatedDecision(decision=normalized, row=row, action=action)

    if row.existing_party_id is not None:
        raise ValueError(
            f"subscriber is already bound to Party '{row.existing_party_id}'"
        )
    if decision.planned_party_id is None:
        raise ValueError("planned_party_id is required")
    if decision.planned_party_id in all_subscriber_ids:
        raise ValueError("planned_party_id must not reuse a Subscriber UUID")
    if decision.planned_party_id in existing_party_ids:
        raise ValueError("planned_party_id already exists in the current audit")
    if decision.identity_source_subscriber_id is None:
        raise ValueError("identity_source_subscriber_id is required")
    party_type = _enum_value(decision.party_type, PartyType, "party_type")
    data_classification = _enum_value(
        decision.data_classification,
        PartyDataClassification,
        "data_classification",
    )
    display_name_source = _enum_value(
        decision.display_name_source,
        PartyDisplayNameSource,
        "display_name_source",
    )
    if (
        action is PartyAdjudicationAction.create_active_party
        and data_classification is not PartyDataClassification.production
    ):
        raise ValueError(
            "create_active_party requires data_classification='production'"
        )
    return _ValidatedDecision(
        decision=normalized,
        row=row,
        action=action,
        planned_party_id=decision.planned_party_id,
        identity_source_subscriber_id=decision.identity_source_subscriber_id,
        party_type=party_type,
        data_classification=data_classification,
        display_name_source=display_name_source,
    )


def build_party_backfill_plan(
    audit: SubscriberIdentityAudit,
    decisions: tuple[PartyIdentityDecision, ...],
    *,
    planned_at: datetime | None = None,
) -> PartyBackfillPlan:
    """Validate decisions and produce a deterministic, non-executable plan.

    Medium/high duplicate evidence is closed as a group: if any member would
    create or bind a planned Party, every member of that evidence group needs
    an actionable reviewed decision. They may resolve to the same Party UUID or
    to distinct Party UUIDs, but never by omission or automatic merge.
    """

    now = planned_at or datetime.now(UTC)
    if now.tzinfo is None:
        raise PartyAdjudicationError(("planned_at must be timezone-aware",))
    current_audit_digest = subscriber_identity_audit_digest(audit)
    rows_by_subscriber = {row.subscriber_id: row for row in audit.rows}
    all_subscriber_ids = set(rows_by_subscriber)
    existing_party_ids = {
        row.existing_party_id for row in audit.rows if row.existing_party_id is not None
    }
    errors: list[str] = []
    seen_decision_ids: set[UUID] = set()
    seen_subscriber_ids: set[UUID] = set()
    validated: list[_ValidatedDecision] = []
    for decision in decisions:
        prefix = f"decision {decision.decision_id} subscriber {decision.subscriber_id}:"
        if decision.decision_id in seen_decision_ids:
            errors.append(f"{prefix} duplicate decision_id")
            continue
        seen_decision_ids.add(decision.decision_id)
        if decision.subscriber_id in seen_subscriber_ids:
            errors.append(f"{prefix} duplicate subscriber decision")
            continue
        seen_subscriber_ids.add(decision.subscriber_id)
        row = rows_by_subscriber.get(decision.subscriber_id)
        if row is None:
            errors.append(f"{prefix} subscriber is absent from the current audit")
            continue
        try:
            validated.append(
                _validate_decision(
                    decision,
                    row=row,
                    current_audit_digest=current_audit_digest,
                    planned_at=now,
                    all_subscriber_ids=all_subscriber_ids,
                    existing_party_ids=existing_party_ids,
                )
            )
        except ValueError as exc:
            errors.append(f"{prefix} {exc}")

    validated_by_subscriber = {item.decision.subscriber_id: item for item in validated}
    for group in audit.duplicate_groups:
        if group.automatic_merge_allowed:
            errors.append(
                f"duplicate group {group.group_id}: audit contract unexpectedly "
                "permits automatic merge"
            )
        if group.confidence not in {
            DuplicateConfidence.medium,
            DuplicateConfidence.high,
        }:
            continue
        actionable_members = {
            subscriber_id
            for subscriber_id in group.subscriber_ids
            if (
                subscriber_id in validated_by_subscriber
                and validated_by_subscriber[subscriber_id].action
                is not PartyAdjudicationAction.defer
            )
        }
        if not actionable_members:
            continue
        unresolved_members = {
            subscriber_id
            for subscriber_id in group.subscriber_ids
            if (
                subscriber_id not in validated_by_subscriber
                or validated_by_subscriber[subscriber_id].action
                is PartyAdjudicationAction.defer
            )
        }
        if unresolved_members:
            errors.append(
                f"duplicate group {group.group_id}: actionable planning requires "
                "actionable decisions for every medium/high-confidence member; "
                f"unresolved={','.join(sorted(map(str, unresolved_members)))}"
            )

    planned_groups: dict[UUID, list[_ValidatedDecision]] = defaultdict(list)
    deferred_items: list[DeferredPartyDecision] = []
    for item in validated:
        decision = item.decision
        if item.action is PartyAdjudicationAction.defer:
            deferred_items.append(
                DeferredPartyDecision(
                    decision_id=decision.decision_id,
                    subscriber_id=decision.subscriber_id,
                    row_fingerprint=decision.row_fingerprint,
                    reviewed_at=decision.reviewed_at,
                    reviewer_sha256=_text_digest(decision.reviewer),
                    reason_sha256=_text_digest(decision.reason),
                )
            )
        elif item.planned_party_id is not None:
            planned_groups[item.planned_party_id].append(item)

    groups: list[PlannedPartyGroup] = []
    bindings: list[PartyBackfillBinding] = []
    for planned_party_id, members in sorted(
        planned_groups.items(), key=lambda item: str(item[0])
    ):
        contracts = {
            (
                member.action,
                member.party_type,
                member.data_classification,
                member.identity_source_subscriber_id,
                member.display_name_source,
            )
            for member in members
        }
        if len(contracts) != 1:
            errors.append(
                f"planned Party {planned_party_id}: all account decisions must "
                "agree on action, type, classification, identity source, and "
                "display-name source"
            )
            continue
        (
            action,
            party_type,
            data_classification,
            identity_source_subscriber_id,
            display_name_source,
        ) = contracts.pop()
        assert party_type is not None
        assert data_classification is not None
        assert identity_source_subscriber_id is not None
        assert display_name_source is not None
        member_ids = {member.decision.subscriber_id for member in members}
        if identity_source_subscriber_id not in member_ids:
            errors.append(
                f"planned Party {planned_party_id}: identity source Subscriber "
                "must be one of the explicitly grouped accounts"
            )
            continue
        source_row = rows_by_subscriber[identity_source_subscriber_id]
        if display_name_source.value not in source_row.available_display_name_sources:
            errors.append(
                f"planned Party {planned_party_id}: display-name source "
                f"'{display_name_source.value}' is unavailable on the selected "
                "identity source Subscriber"
            )
            continue
        allowed_sources = (
            _PERSON_DISPLAY_SOURCES
            if party_type is PartyType.person
            else _ORGANIZATION_DISPLAY_SOURCES
        )
        if display_name_source not in allowed_sources:
            errors.append(
                f"planned Party {planned_party_id}: display-name source "
                f"'{display_name_source.value}' is invalid for "
                f"party_type='{party_type.value}'"
            )
            continue
        target_status = (
            PartyIdentityStatus.active
            if action is PartyAdjudicationAction.create_active_party
            else PartyIdentityStatus.quarantined
        )
        sorted_members = sorted(
            members, key=lambda item: str(item.decision.subscriber_id)
        )
        groups.append(
            PlannedPartyGroup(
                planned_party_id=planned_party_id,
                party_type=party_type,
                data_classification=data_classification,
                target_status=target_status,
                identity_source_subscriber_id=identity_source_subscriber_id,
                display_name_source=display_name_source,
                subscriber_ids=tuple(
                    member.decision.subscriber_id for member in sorted_members
                ),
                decision_ids=tuple(
                    member.decision.decision_id for member in sorted_members
                ),
            )
        )
        bindings.extend(
            PartyBackfillBinding(
                decision_id=member.decision.decision_id,
                subscriber_id=member.decision.subscriber_id,
                planned_party_id=planned_party_id,
                action=action,
                row_fingerprint=member.decision.row_fingerprint,
                reviewed_at=member.decision.reviewed_at,
                reviewer_sha256=_text_digest(member.decision.reviewer),
                reason_sha256=_text_digest(member.decision.reason),
            )
            for member in sorted_members
        )

    if errors:
        raise PartyAdjudicationError(errors)
    return PartyBackfillPlan(
        audit_digest=current_audit_digest,
        audit_generated_at=audit.generated_at,
        planned_at=now,
        total_audit_rows=len(audit.rows),
        reviewed_decisions=len(validated),
        unreviewed_rows=len(audit.rows) - len(validated),
        groups=tuple(groups),
        bindings=tuple(bindings),
        deferred=tuple(
            sorted(deferred_items, key=lambda item: str(item.subscriber_id))
        ),
    )
