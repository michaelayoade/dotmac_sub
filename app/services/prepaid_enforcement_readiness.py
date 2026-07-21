"""Cutover gate for prepaid enforcement funding provenance.

The signed, materialized reconstruction proves that Sub's canonical financial
resolver has a complete opening position. Readiness records one fresh plan from
that same live owner; it never accepts another balance input or supplies a
runtime balance itself.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.prepaid_enforcement import PrepaidEnforcementReadiness
from app.models.prepaid_funding import (
    PrepaidFundingBaseline,
    PrepaidFundingReconstructionBatch,
)
from app.services import settings_spec
from app.services.common import coerce_uuid
from app.services.prepaid_currency import resolve_prepaid_enforcement_currency
from app.services.prepaid_enforcement_planner import (
    PrepaidEnforcementAction,
    PrepaidEnforcementPlan,
    candidate_prepaid_funding_account_ids,
    plan_prepaid_enforcement,
    resolve_prepaid_enforcement_policy,
)
from app.services.prepaid_funding_reconstruction import (
    authority_cutover_batch,
    prepaid_funding_quarantined_account_ids,
)


@dataclass(frozen=True)
class PrepaidReadinessComparison:
    candidate_account_count: int
    candidate_account_ids_hash: str
    configuration_hash: str
    funding_decisions_hash: str
    reconstruction_evidence_sha256: str
    coverage_evidence_sha256: str
    coverage_blocker_count: int
    source: str
    observed_at: datetime
    currency: str
    quarantined_account_count: int
    blockers: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: (
            f"{value:.2f}" if isinstance(value, Decimal) else value.isoformat()
        ),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _candidate_hash(account_ids: list[str]) -> str:
    return _hash(sorted(account_ids))


def _coverage_evidence(
    db: Session, *, observed_at: datetime
) -> tuple[str, int, int, int]:
    from app.services.prepaid_coverage_reconciliation import (
        preview_prepaid_coverage_reconciliation,
    )

    preview = preview_prepaid_coverage_reconciliation(db, as_of=observed_at)
    evidence_hash = _hash(
        {
            "subscription_ids": [str(value) for value in preview.subscription_ids],
            "item_evidence": [item.evidence_fingerprint for item in preview.items],
        }
    )
    return (
        evidence_hash,
        preview.blocker_count,
        preview.repairable_count,
        preview.quarantined_count,
    )


def _configuration_hash(db: Session, plan: PrepaidEnforcementPlan) -> str:
    """Hash config-resolved policy, excluding mutable financial observations."""
    return _hash(
        {
            "policy": plan.policy.report_values(),
            "readiness": {
                "max_age_minutes": int(_max_readiness_age(db).total_seconds() // 60),
                "activation_max_grace_days": _max_activation_grace_days(db),
            },
            "accounts": [
                {
                    "account_id": item.account_id,
                    "billing_mode": (
                        item.billing_mode.value if item.billing_mode else None
                    ),
                    "currency": item.currency,
                    "grace_days": item.grace_days,
                    "grace_source": item.grace_source.value,
                    "grace_policy_set_id": item.grace_policy_set_id,
                    "required_balance": item.required_balance,
                    "covered_subscription_ids": [
                        str(value) for value in item.covered_subscription_ids
                    ],
                    "actionable_uncovered_subscription_ids": [
                        str(value)
                        for value in item.actionable_uncovered_subscription_ids
                    ],
                    "unresolved_projection_subscription_ids": [
                        str(value)
                        for value in item.unresolved_projection_subscription_ids
                    ],
                }
                for item in plan.items
            ],
        }
    )


def _funding_hash(plan: PrepaidEnforcementPlan) -> str:
    return _hash(
        [
            {
                "account_id": item.account_id,
                "currency": item.currency,
                "available_balance": item.available_balance,
                "required_balance": item.required_balance,
                "covered_subscription_ids": [
                    str(value) for value in item.covered_subscription_ids
                ],
                "actionable_uncovered_subscription_ids": [
                    str(value) for value in item.actionable_uncovered_subscription_ids
                ],
                "unresolved_projection_subscription_ids": [
                    str(value) for value in item.unresolved_projection_subscription_ids
                ],
            }
            for item in sorted(plan.items, key=lambda value: value.account_id)
        ]
    )


def _reconstruction_evidence(
    db: Session, *, account_ids: list[str], currency: str
) -> tuple[str, str]:
    """Bind readiness to the exact sealed authority and active baselines."""
    cutover = authority_cutover_batch(db)
    if cutover is None:
        raise ValueError("prepaid funding authority cutover is missing")
    ids = {coerce_uuid(value) for value in account_ids}
    active_baselines: list[dict[str, object]] = []
    if ids:
        rows = db.execute(
            select(
                PrepaidFundingBaseline.account_id,
                PrepaidFundingBaseline.amount,
                PrepaidFundingBaseline.position_at,
                PrepaidFundingReconstructionBatch.manifest_sha256,
                PrepaidFundingReconstructionBatch.attestation_sha256,
            )
            .join(
                PrepaidFundingReconstructionBatch,
                PrepaidFundingReconstructionBatch.id == PrepaidFundingBaseline.batch_id,
            )
            .where(
                PrepaidFundingBaseline.account_id.in_(ids),
                PrepaidFundingBaseline.currency == currency,
                PrepaidFundingBaseline.is_active.is_(True),
            )
            .order_by(PrepaidFundingBaseline.account_id)
        ).all()
        active_baselines = [
            {
                "account_id": str(row.account_id),
                "amount": row.amount,
                "position_at": row.position_at,
                "manifest_sha256": row.manifest_sha256,
                "attestation_sha256": row.attestation_sha256,
            }
            for row in rows
        ]
    quarantined_ids = sorted(
        str(value)
        for value in prepaid_funding_quarantined_account_ids(
            db, account_ids, currency=currency
        )
    )
    source = f"financial.prepaid_funding_reconstruction:{cutover.manifest_sha256}"
    return (
        _hash(
            {
                "authority_cutover": {
                    "manifest_sha256": cutover.manifest_sha256,
                    "manifest_payload_sha256": cutover.manifest_payload_sha256,
                    "attestation_sha256": cutover.attestation_sha256,
                    "attestation_key_fingerprint_sha256": (
                        cutover.attestation_key_fingerprint_sha256
                    ),
                    "blocker_manifest_sha256": cutover.blocker_manifest_sha256,
                    "candidate_cohort_sha256": cutover.candidate_cohort_sha256,
                    "position_at": cutover.position_at,
                    "currency": cutover.currency,
                },
                "candidate_account_ids": sorted(account_ids),
                "quarantined_account_ids": quarantined_ids,
                "active_baselines": active_baselines,
            }
        ),
        source,
    )


def _max_readiness_age(db: Session) -> timedelta:
    raw = settings_spec.resolve_value(
        db, SettingDomain.collections, "prepaid_readiness_max_age_minutes"
    )
    try:
        minutes = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "collections.prepaid_readiness_max_age_minutes must be an integer"
        ) from exc
    if minutes < 1:
        raise ValueError(
            "collections.prepaid_readiness_max_age_minutes must be at least 1"
        )
    return timedelta(minutes=minutes)


def _max_activation_grace_days(db: Session) -> int:
    raw = settings_spec.resolve_value(
        db, SettingDomain.collections, "prepaid_activation_max_grace_days"
    )
    try:
        days = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "collections.prepaid_activation_max_grace_days must be an integer"
        ) from exc
    if days < 0:
        raise ValueError(
            "collections.prepaid_activation_max_grace_days must not be negative"
        )
    return days


def evaluate_prepaid_enforcement_readiness(
    db: Session,
    *,
    activation_at: datetime,
    now: datetime | None = None,
) -> PrepaidReadinessComparison:
    """Evaluate the exact live cohort through the materialized funding owner."""
    observed_at = _as_utc(now or datetime.now(UTC))
    intended_activation_at = _as_utc(activation_at)
    configured_currency = resolve_prepaid_enforcement_currency(db)
    account_ids = sorted(
        (str(value) for value in candidate_prepaid_funding_account_ids(db)), key=str
    )
    quarantined_ids = {
        str(value)
        for value in prepaid_funding_quarantined_account_ids(
            db, account_ids, currency=configured_currency
        )
    }
    enforceable_ids = [
        account_id for account_id in account_ids if account_id not in quarantined_ids
    ]
    blockers: list[str] = []
    from app.services import control_registry

    if not control_registry.is_enabled(db, "billing.prepaid_service_renewals"):
        blockers.append("canonical_prepaid_renewals_disabled")
    if authority_cutover_batch(db) is None:
        return PrepaidReadinessComparison(
            candidate_account_count=len(account_ids),
            candidate_account_ids_hash=_candidate_hash(account_ids),
            configuration_hash="0" * 64,
            funding_decisions_hash="0" * 64,
            reconstruction_evidence_sha256="0" * 64,
            coverage_evidence_sha256="0" * 64,
            coverage_blocker_count=len(account_ids),
            source="",
            observed_at=observed_at,
            currency=configured_currency,
            quarantined_account_count=len(account_ids),
            blockers=("prepaid_funding_authority_cutover_missing",),
        )
    if not enforceable_ids:
        blockers.append("prepaid_funding_enforceable_cohort_empty")
    if intended_activation_at < observed_at:
        blockers.append("activation_precedes_readiness_observation")
    if intended_activation_at - observed_at > _max_readiness_age(db):
        blockers.append("readiness_observation_too_old_for_activation")

    local_plan = plan_prepaid_enforcement(
        db,
        now=observed_at,
        account_ids=enforceable_ids,
        activation_at=intended_activation_at,
    )
    reconstruction_hash, source = _reconstruction_evidence(
        db,
        account_ids=account_ids,
        currency=configured_currency,
    )
    (
        coverage_hash,
        coverage_blocker_count,
        coverage_repairable_count,
        coverage_quarantined_count,
    ) = _coverage_evidence(db, observed_at=observed_at)
    if coverage_repairable_count:
        blockers.append(f"prepaid_coverage_repair_required:{coverage_repairable_count}")
    if coverage_quarantined_count:
        blockers.append(f"prepaid_coverage_quarantined:{coverage_quarantined_count}")
    max_activation_grace_days = _max_activation_grace_days(db)
    for local in local_plan.items:
        account_id = local.account_id
        if local.currency != configured_currency:
            blockers.append(f"currency_mismatch:{account_id}")
        if local.action == PrepaidEnforcementAction.coverage_unresolved:
            blockers.append(f"prepaid_coverage_unresolved:{account_id}")
        if (
            local.available_balance < local.required_balance
            and local.action
            in {PrepaidEnforcementAction.warn, PrepaidEnforcementAction.waiting}
            and local.grace_days > max_activation_grace_days
        ):
            blockers.append(f"activation_grace_exceeds_configured_max:{account_id}")

    return PrepaidReadinessComparison(
        candidate_account_count=len(account_ids),
        candidate_account_ids_hash=_candidate_hash(account_ids),
        configuration_hash=_configuration_hash(db, local_plan),
        funding_decisions_hash=_funding_hash(local_plan),
        reconstruction_evidence_sha256=reconstruction_hash,
        coverage_evidence_sha256=coverage_hash,
        coverage_blocker_count=coverage_blocker_count,
        source=source,
        observed_at=observed_at,
        currency=configured_currency,
        quarantined_account_count=len(quarantined_ids),
        blockers=tuple(blockers),
    )


def record_prepaid_enforcement_readiness(
    db: Session,
    *,
    activation_at: datetime,
    evidence_ref: str,
    verified_by: str,
    now: datetime | None = None,
) -> PrepaidEnforcementReadiness:
    """Persist a successful live-owner review of the enforceable cohort."""
    evidence = evidence_ref.strip()
    actor = verified_by.strip()
    if not evidence:
        raise ValueError("evidence_ref is required")
    if not actor:
        raise ValueError("verified_by is required")
    observed_at = _as_utc(now or datetime.now(UTC))
    comparison = evaluate_prepaid_enforcement_readiness(
        db,
        activation_at=activation_at,
        now=observed_at,
    )
    if comparison.blockers:
        raise ValueError(
            "prepaid funding readiness blocked: " + ", ".join(comparison.blockers)
        )
    db.execute(
        update(PrepaidEnforcementReadiness)
        .where(PrepaidEnforcementReadiness.is_active.is_(True))
        .values(is_active=False)
    )
    record = PrepaidEnforcementReadiness(
        intended_activation_at=_as_utc(activation_at),
        funding_observed_at=comparison.observed_at,
        source=comparison.source,
        evidence_ref=evidence,
        currency=comparison.currency,
        candidate_account_count=comparison.candidate_account_count,
        candidate_account_ids_hash=comparison.candidate_account_ids_hash,
        configuration_hash=comparison.configuration_hash,
        funding_decisions_hash=comparison.funding_decisions_hash,
        reconstruction_evidence_sha256=(comparison.reconstruction_evidence_sha256),
        coverage_evidence_sha256=comparison.coverage_evidence_sha256,
        coverage_blocker_count=comparison.coverage_blocker_count,
        blocker_count=comparison.quarantined_account_count,
        verified_by=actor,
        is_active=True,
    )
    db.add(record)
    db.flush()
    return record


def active_prepaid_enforcement_readiness(
    db: Session,
) -> PrepaidEnforcementReadiness | None:
    return db.scalar(
        select(PrepaidEnforcementReadiness)
        .where(PrepaidEnforcementReadiness.is_active.is_(True))
        .order_by(PrepaidEnforcementReadiness.created_at.desc())
        .limit(1)
    )


def prepaid_enforcement_readiness_block_reason(
    db: Session, *, now: datetime | None = None
) -> str | None:
    """Return why the configured feature must remain fail-closed, if any."""
    from app.services import control_registry

    # This is a continuous architecture gate, not part of the one-time funding
    # attestation. Without the canonical renewal writer, access enforcement can
    # only drift farther from paid service coverage.
    if not control_registry.is_enabled(db, "billing.prepaid_service_renewals"):
        return "canonical_prepaid_renewals_disabled"
    (
        coverage_hash,
        coverage_blocker_count,
        _coverage_repairable_count,
        _coverage_quarantined_count,
    ) = _coverage_evidence(db, observed_at=_as_utc(now or datetime.now(UTC)))
    if coverage_blocker_count:
        return "prepaid_coverage_reconciliation_required"
    record = active_prepaid_enforcement_readiness(db)
    if record is None:
        return "prepaid_funding_readiness_missing"
    policy = resolve_prepaid_enforcement_policy(db)
    if policy.activation_error:
        return policy.activation_error
    assert policy.activation_at is not None
    if _as_utc(record.intended_activation_at) != _as_utc(policy.activation_at):
        return "prepaid_funding_readiness_activation_mismatch"
    if record.currency != resolve_prepaid_enforcement_currency(db):
        return "prepaid_funding_readiness_currency_mismatch"
    if record.activated_at is not None:
        return None

    effective_now = _as_utc(now or datetime.now(UTC))
    max_readiness_age = _max_readiness_age(db)
    if (
        _as_utc(policy.activation_at) - _as_utc(record.funding_observed_at)
        > max_readiness_age
        or effective_now - _as_utc(record.funding_observed_at) > max_readiness_age
    ):
        return "prepaid_funding_readiness_expired"
    account_ids = sorted(
        (str(value) for value in candidate_prepaid_funding_account_ids(db)), key=str
    )
    if _candidate_hash(account_ids) != record.candidate_account_ids_hash:
        return "prepaid_funding_readiness_cohort_changed"
    quarantined_ids = {
        str(value)
        for value in prepaid_funding_quarantined_account_ids(
            db, account_ids, currency=record.currency
        )
    }
    if len(quarantined_ids) != record.blocker_count:
        return "prepaid_funding_readiness_quarantine_changed"
    enforceable_ids = [
        account_id for account_id in account_ids if account_id not in quarantined_ids
    ]
    try:
        reconstruction_hash, source = _reconstruction_evidence(
            db,
            account_ids=account_ids,
            currency=record.currency,
        )
    except ValueError:
        return "prepaid_funding_readiness_reconstruction_missing"
    if (
        reconstruction_hash != record.reconstruction_evidence_sha256
        or source != record.source
    ):
        return "prepaid_funding_readiness_reconstruction_changed"
    if (
        coverage_hash != record.coverage_evidence_sha256
        or record.coverage_blocker_count != 0
    ):
        return "prepaid_coverage_readiness_changed"
    current_plan = plan_prepaid_enforcement(
        db,
        now=effective_now,
        account_ids=enforceable_ids,
        activation_at=policy.activation_at,
    )
    if _configuration_hash(db, current_plan) != record.configuration_hash:
        return "prepaid_funding_readiness_configuration_changed"
    if _funding_hash(current_plan) != record.funding_decisions_hash:
        return "prepaid_funding_readiness_funding_changed"
    return None


def prepaid_enforcement_enablement_block_reason(
    db: Session, *, now: datetime | None = None
) -> str | None:
    """Require a newly recorded readiness generation for every OFF→ON cutover."""
    record = active_prepaid_enforcement_readiness(db)
    if record is not None and record.activated_at is not None:
        return "prepaid_funding_readiness_rearm_required"
    return prepaid_enforcement_readiness_block_reason(db, now=now)


def mark_prepaid_enforcement_activated(
    db: Session, *, activated_at: datetime
) -> PrepaidEnforcementReadiness:
    """Seal the verified cutover once the first eligible sweep starts."""
    reason = prepaid_enforcement_readiness_block_reason(db, now=activated_at)
    if reason:
        raise ValueError(reason)
    record = active_prepaid_enforcement_readiness(db)
    assert record is not None
    if record.activated_at is None:
        record.activated_at = _as_utc(activated_at)
        db.flush()
    return record
