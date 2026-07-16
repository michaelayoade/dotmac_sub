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
from app.services import settings_spec
from app.services.access_resolution import resolve_prepaid_enforcement_currency
from app.services.prepaid_enforcement_planner import (
    PrepaidEnforcementAction,
    PrepaidEnforcementPlan,
    candidate_prepaid_account_ids,
    plan_prepaid_enforcement,
    resolve_prepaid_enforcement_policy,
)
from app.services.prepaid_funding_reconstruction import authority_cutover_batch


@dataclass(frozen=True)
class PrepaidReadinessComparison:
    candidate_account_count: int
    candidate_account_ids_hash: str
    configuration_hash: str
    funding_decisions_hash: str
    currency: str
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


def _configuration_hash(db: Session, plan: PrepaidEnforcementPlan) -> str:
    """Hash config-resolved policy, excluding mutable financial observations."""
    return _hash(
        {
            "policy": plan.policy.report_values(),
            "readiness": {
                "max_age_minutes": int(_max_snapshot_age(db).total_seconds() // 60),
                "activation_max_grace_days": _max_activation_grace_days(db),
            },
            "accounts": [
                {
                    "account_id": item.account_id,
                    "billing_mode": item.billing_mode,
                    "currency": item.currency,
                    "grace_days": item.grace_days,
                    "grace_source": item.grace_source,
                    "grace_policy_set_id": item.grace_policy_set_id,
                    "required_balance": item.required_balance,
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
            }
            for item in sorted(plan.items, key=lambda value: value.account_id)
        ]
    )


def _max_snapshot_age(db: Session) -> timedelta:
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
    captured_at = _as_utc(now or datetime.now(UTC))
    intended_activation_at = _as_utc(activation_at)
    configured_currency = resolve_prepaid_enforcement_currency(db)
    account_ids = sorted(
        (str(value) for value in candidate_prepaid_account_ids(db)), key=str
    )
    blockers: list[str] = []
    if authority_cutover_batch(db) is None:
        return PrepaidReadinessComparison(
            candidate_account_count=len(account_ids),
            candidate_account_ids_hash=_candidate_hash(account_ids),
            configuration_hash="0" * 64,
            funding_decisions_hash="0" * 64,
            currency=configured_currency,
            blockers=("prepaid_funding_authority_cutover_missing",),
        )
    if intended_activation_at < captured_at:
        blockers.append("activation_precedes_readiness_observation")
    if intended_activation_at - captured_at > _max_snapshot_age(db):
        blockers.append("readiness_observation_too_old_for_activation")

    local_plan = plan_prepaid_enforcement(
        db,
        now=captured_at,
        account_ids=account_ids,
        activation_at=intended_activation_at,
    )
    max_activation_grace_days = _max_activation_grace_days(db)
    for local in local_plan.items:
        account_id = local.account_id
        if local.currency != configured_currency:
            blockers.append(f"currency_mismatch:{account_id}")
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
        currency=configured_currency,
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
    """Persist a successful full-cohort live-owner review as cutover evidence."""
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
    batch = authority_cutover_batch(db)
    if batch is None:
        raise ValueError("prepaid funding authority cutover is missing")
    record = PrepaidEnforcementReadiness(
        intended_activation_at=_as_utc(activation_at),
        snapshot_captured_at=observed_at,
        source=(f"financial.prepaid_funding_reconstruction:{batch.manifest_sha256}"),
        evidence_ref=evidence,
        currency=comparison.currency,
        candidate_account_count=comparison.candidate_account_count,
        candidate_account_ids_hash=comparison.candidate_account_ids_hash,
        configuration_hash=comparison.configuration_hash,
        funding_decisions_hash=comparison.funding_decisions_hash,
        blocker_count=0,
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
    record = active_prepaid_enforcement_readiness(db)
    if record is None:
        return "prepaid_funding_readiness_missing"
    if record.blocker_count:
        return "prepaid_funding_readiness_has_blockers"

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
    max_snapshot_age = _max_snapshot_age(db)
    if (
        _as_utc(policy.activation_at) - _as_utc(record.snapshot_captured_at)
        > max_snapshot_age
        or effective_now - _as_utc(record.snapshot_captured_at) > max_snapshot_age
    ):
        return "prepaid_funding_readiness_expired"
    account_ids = sorted(
        (str(value) for value in candidate_prepaid_account_ids(db)), key=str
    )
    if _candidate_hash(account_ids) != record.candidate_account_ids_hash:
        return "prepaid_funding_readiness_cohort_changed"
    current_plan = plan_prepaid_enforcement(
        db,
        now=effective_now,
        account_ids=account_ids,
        activation_at=policy.activation_at,
    )
    if _configuration_hash(db, current_plan) != record.configuration_hash:
        return "prepaid_funding_readiness_configuration_changed"
    return None


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
