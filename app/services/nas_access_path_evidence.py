"""Cross-domain historical evidence for manual NAS lifecycle decisions."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, NasDeviceStatus, Subscription
from app.services.common import coerce_uuid
from app.services.nas_lifecycle import (
    NasLifecycleAction,
    build_nas_lifecycle_plan,
)
from app.services.network.radius_sessions import (
    latest_accounting_observation_at,
    recent_nas_history_by_subscription,
)
from app.services.subscription_lifecycle_policy import TERMINAL_SERVICE_STATUSES


class NasEvidenceRecommendation(StrEnum):
    review_reactivate = "review_reactivate"
    review_relink = "review_relink"
    repair_radius_identity = "repair_radius_identity"
    verify_radius_identity = "verify_radius_identity"
    investigate_mixed_paths = "investigate_mixed_paths"
    insufficient_evidence = "insufficient_evidence"


_ACCOUNTING_FRESHNESS = timedelta(hours=24)


@dataclass(frozen=True)
class NasCandidateEvidence:
    nas_device_id: str
    nas_name: str
    is_active: bool
    status: str
    subscriptions_observed: int
    sessions_observed: int
    last_seen_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "nas_device_id": self.nas_device_id,
            "nas_name": self.nas_name,
            "is_active": self.is_active,
            "status": self.status,
            "subscriptions_observed": self.subscriptions_observed,
            "sessions_observed": self.sessions_observed,
            "last_seen_at": self.last_seen_at.isoformat(),
        }


@dataclass(frozen=True)
class NasAccessPathEvidence:
    nas_device_id: str
    nas_name: str
    lifecycle_reason: str
    recommendation: NasEvidenceRecommendation
    subscriptions: int
    subscriptions_with_history: int
    subscriptions_without_history: int
    current_nas_subscriptions: int
    exact_active_alternate_subscriptions: int
    ambiguous_subscriptions: int
    mixed_path_subscriptions: int
    candidates: tuple[NasCandidateEvidence, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "nas_device_id": self.nas_device_id,
            "nas_name": self.nas_name,
            "lifecycle_reason": self.lifecycle_reason,
            "recommendation": self.recommendation.value,
            "subscriptions": self.subscriptions,
            "subscriptions_with_history": self.subscriptions_with_history,
            "subscriptions_without_history": self.subscriptions_without_history,
            "current_nas_subscriptions": self.current_nas_subscriptions,
            "exact_active_alternate_subscriptions": (
                self.exact_active_alternate_subscriptions
            ),
            "ambiguous_subscriptions": self.ambiguous_subscriptions,
            "mixed_path_subscriptions": self.mixed_path_subscriptions,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class NasAccessPathEvidenceReport:
    generated_at: datetime
    window_days: int
    cutoff_at: datetime
    latest_accounting_at: datetime | None
    accounting_source_fresh: bool
    evidence: tuple[NasAccessPathEvidence, ...]

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            [item.as_dict() for item in self.evidence],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def recommendation_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.evidence:
            key = item.recommendation.value
            counts[key] = counts.get(key, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def as_dict(self, *, include_details: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": "read_only" if self.accounting_source_fresh else "source_stale",
            "generated_at": self.generated_at.isoformat(),
            "window_days": self.window_days,
            "cutoff_at": self.cutoff_at.isoformat(),
            "latest_accounting_at": (
                self.latest_accounting_at.isoformat()
                if self.latest_accounting_at is not None
                else None
            ),
            "accounting_source_fresh": self.accounting_source_fresh,
            "evidence_digest": self.digest,
            "nas_records": len(self.evidence),
            "recommendations": self.recommendation_counts,
        }
        if include_details:
            payload["details"] = [item.as_dict() for item in self.evidence]
        return payload


def _status_value(device: NasDevice) -> str:
    return str(getattr(device.status, "value", device.status) or "")


def _recommendation(
    *,
    lifecycle_reason: str,
    subscriptions: int,
    current_nas_subscriptions: int,
    exact_active_alternate_subscriptions: int,
    subscriptions_without_history: int,
) -> NasEvidenceRecommendation:
    if lifecycle_reason == "active_nas_has_dependencies_without_usable_radius_identity":
        if current_nas_subscriptions:
            return NasEvidenceRecommendation.repair_radius_identity
        return NasEvidenceRecommendation.verify_radius_identity
    if current_nas_subscriptions:
        return NasEvidenceRecommendation.review_reactivate
    if subscriptions and exact_active_alternate_subscriptions == subscriptions:
        return NasEvidenceRecommendation.review_relink
    if subscriptions and subscriptions_without_history == subscriptions:
        return NasEvidenceRecommendation.insufficient_evidence
    return NasEvidenceRecommendation.investigate_mixed_paths


def build_nas_access_path_evidence_report(
    db: Session,
    *,
    window_days: int = 90,
    now: datetime | None = None,
) -> NasAccessPathEvidenceReport:
    """Aggregate historical evidence for lifecycle items requiring manual review."""
    if window_days < 1 or window_days > 3650:
        raise ValueError("window_days must be between 1 and 3650")
    generated_at = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff_at = generated_at - timedelta(days=window_days)
    lifecycle_plan = build_nas_lifecycle_plan(db)
    latest_accounting_at = latest_accounting_observation_at(db)
    accounting_age = (
        generated_at - latest_accounting_at
        if latest_accounting_at is not None
        else None
    )
    accounting_source_fresh = bool(
        accounting_age is not None
        and timedelta(0) <= accounting_age <= _ACCOUNTING_FRESHNESS
    )
    blocked = tuple(
        item
        for item in lifecycle_plan.items
        if item.action == NasLifecycleAction.manual_review
    )
    if not blocked:
        return NasAccessPathEvidenceReport(
            generated_at=generated_at,
            window_days=window_days,
            cutoff_at=cutoff_at,
            latest_accounting_at=latest_accounting_at,
            accounting_source_fresh=accounting_source_fresh,
            evidence=(),
        )

    source_ids = {coerce_uuid(item.nas_device_id) for item in blocked}
    subscriptions = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.provisioning_nas_device_id.in_(source_ids))
            .where(Subscription.status.not_in(TERMINAL_SERVICE_STATUSES))
            .order_by(Subscription.id)
        ).all()
    )
    subscriptions_by_source: dict[object, list[Subscription]] = defaultdict(list)
    for subscription in subscriptions:
        subscriptions_by_source[subscription.provisioning_nas_device_id].append(
            subscription
        )
    history = recent_nas_history_by_subscription(
        db,
        [subscription.id for subscription in subscriptions],
        since=cutoff_at,
    )
    target_ids = {
        target.nas_device_id for record in history.values() for target in record.targets
    }
    devices: dict[object, NasDevice] = {
        device.id: device
        for device in db.scalars(
            select(NasDevice).where(NasDevice.id.in_(target_ids))
        ).all()
    }

    evidence: list[NasAccessPathEvidence] = []
    for lifecycle_item in blocked:
        source_id = coerce_uuid(lifecycle_item.nas_device_id)
        source_subscriptions = subscriptions_by_source.get(source_id, [])
        without_history = 0
        current_count = 0
        exact_alternate_count = 0
        ambiguous_count = 0
        mixed_count = 0
        candidate_subscriptions: dict[object, set[object]] = defaultdict(set)
        candidate_sessions: dict[object, int] = defaultdict(int)
        candidate_last_seen: dict[object, datetime] = {}

        for subscription in source_subscriptions:
            record = history.get(subscription.id)
            targets = record.targets if record is not None else ()
            if not targets:
                without_history += 1
                continue
            target_ids_for_subscription = {target.nas_device_id for target in targets}
            if len(target_ids_for_subscription) > 1:
                mixed_count += 1
            for target in targets:
                candidate_subscriptions[target.nas_device_id].add(subscription.id)
                candidate_sessions[target.nas_device_id] += target.session_count
                previous = candidate_last_seen.get(target.nas_device_id)
                if previous is None or target.last_seen_at > previous:
                    candidate_last_seen[target.nas_device_id] = target.last_seen_at

            if any(
                str(target_id) == lifecycle_item.nas_device_id
                for target_id in target_ids_for_subscription
            ):
                current_count += 1
                continue
            active_alternates = {
                target_id
                for target_id in target_ids_for_subscription
                if (device := devices.get(target_id)) is not None
                and device.is_active
                and device.status == NasDeviceStatus.active
            }
            if len(target_ids_for_subscription) == 1 and len(active_alternates) == 1:
                exact_alternate_count += 1
            else:
                ambiguous_count += 1

        candidates: list[NasCandidateEvidence] = []
        for target_id, subscription_ids in candidate_subscriptions.items():
            device = devices.get(target_id)
            if device is None:
                continue
            candidates.append(
                NasCandidateEvidence(
                    nas_device_id=str(device.id),
                    nas_name=device.name,
                    is_active=bool(device.is_active),
                    status=_status_value(device),
                    subscriptions_observed=len(subscription_ids),
                    sessions_observed=candidate_sessions[target_id],
                    last_seen_at=candidate_last_seen[target_id],
                )
            )
        candidates.sort(
            key=lambda candidate: (
                -candidate.subscriptions_observed,
                -candidate.last_seen_at.timestamp(),
                candidate.nas_device_id,
            )
        )
        total = len(source_subscriptions)
        evidence.append(
            NasAccessPathEvidence(
                nas_device_id=lifecycle_item.nas_device_id,
                nas_name=lifecycle_item.nas_name,
                lifecycle_reason=lifecycle_item.reason,
                recommendation=_recommendation(
                    lifecycle_reason=lifecycle_item.reason,
                    subscriptions=total,
                    current_nas_subscriptions=current_count,
                    exact_active_alternate_subscriptions=exact_alternate_count,
                    subscriptions_without_history=without_history,
                ),
                subscriptions=total,
                subscriptions_with_history=total - without_history,
                subscriptions_without_history=without_history,
                current_nas_subscriptions=current_count,
                exact_active_alternate_subscriptions=exact_alternate_count,
                ambiguous_subscriptions=ambiguous_count,
                mixed_path_subscriptions=mixed_count,
                candidates=tuple(candidates),
            )
        )

    evidence.sort(key=lambda item: item.nas_device_id)
    return NasAccessPathEvidenceReport(
        generated_at=generated_at,
        window_days=window_days,
        cutoff_at=cutoff_at,
        latest_accounting_at=latest_accounting_at,
        accounting_source_fresh=accounting_source_fresh,
        evidence=tuple(evidence),
    )
