"""Typed clock-range evidence shared by time-grant owners, never grant orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionEntry,
    ServiceExtensionStatus,
)
from app.models.service_period_purchase import (
    CompensatedServiceTime,
    OutageCompensationDecision,
    OutageCompensationDecisionStatus,
)
from app.models.subscription_pause import SubscriptionPauseEpisode
from app.services.domain_errors import DomainError
from app.services.outage_interval_algebra import TimeInterval, merge_intervals


class TimeCreditSource(StrEnum):
    pause = "pause"
    extension = "extension"
    outage = "outage"


@dataclass(frozen=True, slots=True)
class TimeCreditQuery:
    subscription_id: UUID


@dataclass(frozen=True, slots=True)
class UnresolvedTimeCredit:
    source_id: UUID
    interval: TimeInterval


@dataclass(frozen=True, slots=True)
class TimeCreditHistory:
    credited: tuple[TimeInterval, ...]
    unresolved: tuple[UnresolvedTimeCredit, ...]


@dataclass(frozen=True, slots=True)
class StageTimeCreditCommand:
    subscription_id: UUID
    source: TimeCreditSource
    source_id: UUID
    ranges: tuple[TimeInterval, ...]
    evidence_ref: str


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def resolve_compensated_service_time(
    db: Session, query: TimeCreditQuery
) -> TimeCreditHistory:
    rows = tuple(
        db.scalars(
            select(CompensatedServiceTime).where(
                CompensatedServiceTime.subscription_id == query.subscription_id
            )
        ).all()
    )
    known = {(row.source_kind, row.source_id) for row in rows}
    credited = [TimeInterval(row.starts_at, row.ends_at) for row in rows]
    # Exact historical pause grants carry their authoritative original clock.
    pauses = db.execute(
        select(SubscriptionPauseEpisode, ServiceEntitlement)
        .join(
            ServiceEntitlement,
            ServiceEntitlement.source_pause_episode_id == SubscriptionPauseEpisode.id,
        )
        .where(SubscriptionPauseEpisode.subscription_id == query.subscription_id)
    ).all()
    unresolved: list[UnresolvedTimeCredit] = []
    for episode, grant in pauses:
        if (
            TimeCreditSource.pause.value,
            episode.id,
        ) in known or episode.resumed_at is None:
            continue
        interval = TimeInterval(_utc(episode.effective_at), _utc(episode.resumed_at))
        seconds = int((interval.ends_at - interval.starts_at).total_seconds())
        if grant.status == ServiceEntitlementStatus.active and seconds == int(
            (_utc(grant.ends_at) - _utc(grant.starts_at)).total_seconds()
        ):
            credited.append(interval)
        else:
            unresolved.append(UnresolvedTimeCredit(episode.id, interval))
    # Existing exact outage snapshots are evidence; missing ranges require review.
    decisions = db.scalars(
        select(OutageCompensationDecision).where(
            OutageCompensationDecision.subscription_id == query.subscription_id,
            OutageCompensationDecision.status
            == OutageCompensationDecisionStatus.compensated,
        )
    ).all()
    for decision in decisions:
        if (TimeCreditSource.outage.value, decision.id) in known:
            continue
        for start, end in (decision.policy_snapshot or {}).get(
            "compensated_ranges", []
        ):
            credited.append(
                TimeInterval(datetime.fromisoformat(start), datetime.fromisoformat(end))
            )
    # A rounded legacy days grant does not prove an exact per-service clock mapping.
    extensions = db.execute(
        select(ServiceExtension, ServiceExtensionEntry)
        .join(
            ServiceExtensionEntry,
            ServiceExtensionEntry.extension_id == ServiceExtension.id,
        )
        .where(
            ServiceExtensionEntry.subscription_id == query.subscription_id,
            ServiceExtension.status == ServiceExtensionStatus.applied,
        )
    ).all()
    for extension, entry in extensions:
        if (TimeCreditSource.extension.value, entry.id) not in known:
            unresolved.append(
                UnresolvedTimeCredit(
                    entry.id,
                    TimeInterval(
                        _utc(extension.window_start), _utc(extension.window_end)
                    ),
                )
            )
    return TimeCreditHistory(merge_intervals(credited), tuple(unresolved))


def stage_compensated_service_time(
    db: Session, command: StageTimeCreditCommand
) -> None:
    """Flush-only claim participant; the grant writer holds the account lock."""
    ranges = merge_intervals(list(command.ranges))
    if not command.evidence_ref.strip() or ranges != command.ranges:
        raise DomainError(
            code="financial.compensated_service_time.evidence_invalid",
            message="Ordered exact time-credit evidence is required.",
        )
    previous = tuple(
        db.scalars(
            select(CompensatedServiceTime)
            .where(
                CompensatedServiceTime.source_kind == command.source.value,
                CompensatedServiceTime.source_id == command.source_id,
            )
            .order_by(CompensatedServiceTime.ordinal)
        ).all()
    )
    if previous:
        if (
            any(row.subscription_id != command.subscription_id for row in previous)
            or tuple(
                TimeInterval(_utc(row.starts_at), _utc(row.ends_at)) for row in previous
            )
            != ranges
        ):
            raise DomainError(
                code="financial.compensated_service_time.idempotency_conflict",
                message="This grant already names different compensated clock ranges.",
            )
        return
    for ordinal, interval in enumerate(ranges):
        db.add(
            CompensatedServiceTime(
                subscription_id=command.subscription_id,
                source_kind=command.source.value,
                source_id=command.source_id,
                ordinal=ordinal,
                starts_at=interval.starts_at,
                ends_at=interval.ends_at,
                evidence_ref=command.evidence_ref,
            )
        )
    db.flush()
