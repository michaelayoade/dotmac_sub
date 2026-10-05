"""Typed SLA-breach automation consequence for one linked customer service."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.event_store import EventStore
from app.models.subscription_pause import (
    SubscriptionPauseBillingPolicy,
    SubscriptionPauseCause,
    SubscriptionPauseCauseStatus,
    SubscriptionPauseEpisode,
    SubscriptionPauseEpisodeStatus,
    SubscriptionPauseReason,
    SubscriptionPauseResumePolicy,
    SubscriptionPauseSource,
)
from app.models.support import Ticket, TicketStatus
from app.models.ticket_workflow import SlaBreach, SlaClock, SlaClockStatus
from app.services import account_lifecycle
from app.services.domain_errors import DomainError
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.service_entitlements import (
    PreviewPauseCompensationEntitlementQuery,
    preview_pause_compensation_entitlement,
)

OWNER = "support.ticket_sla_service_consequence"
PAUSE_CONCERN = "ticket resolution SLA-breach service pause consequence"

_PAUSE = OwnerCommandDefinition(
    owner=OWNER,
    concern=PAUSE_CONCERN,
    name="pause_unique_active_service_for_ticket_sla_breach",
)
_RESUME = OwnerCommandDefinition(
    owner=OWNER,
    concern=PAUSE_CONCERN,
    name="resume_ticket_paused_service",
)


class TicketSlaServiceAutomationError(DomainError):
    """Fail-closed rejection from the SLA-breach service consequence owner."""


class TicketSlaServiceSelectionPolicy(StrEnum):
    unique_active_subscription = "unique_active_subscription"


@dataclass(frozen=True, slots=True)
class PauseTicketServiceForSlaBreachCommand:
    ticket_id: UUID
    event_id: UUID
    rule_id: UUID
    rule_version_id: UUID
    step_index: int
    selection_policy: TicketSlaServiceSelectionPolicy
    resume_policy: SubscriptionPauseResumePolicy
    billing_policy: SubscriptionPauseBillingPolicy
    context: CommandContext


@dataclass(frozen=True, slots=True)
class PauseTicketServiceForSlaBreachOutcome:
    ticket_id: UUID
    subscription_id: UUID
    pause_episode_id: UUID
    pause_cause_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class TicketServicePauseResumePreview:
    cause_id: UUID
    episode_id: UUID
    ticket_id: UUID
    subscription_id: UUID
    ticket_status: TicketStatus
    effective_at: datetime
    proposed_resumed_at: datetime
    paused_seconds: int
    previous_next_billing_at: datetime | None
    projected_next_billing_at: datetime | None
    access_will_be_active: bool
    eligible: bool
    blocking_reasons: tuple[str, ...]
    fingerprint: str

    @property
    def duration_label(self) -> str:
        days, remainder = divmod(self.paused_seconds, 86_400)
        hours, remainder = divmod(remainder, 3_600)
        minutes, seconds = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m {seconds}s"


@dataclass(frozen=True, slots=True)
class TicketServicePauseResumePreviewQuery:
    subscription_id: UUID
    proposed_resumed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TicketPausedPrepaidReconciliationPreview:
    resume_preview: TicketServicePauseResumePreview
    renewal_starts_at: datetime
    renewal_ends_at: datetime
    renewal_amount: Decimal
    renewal_currency: str
    renewal_preview_fingerprint: str
    eligible: bool
    blocking_reasons: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ReconcileTicketPausedPrepaidServiceCommand:
    subscription_id: UUID
    cause_id: UUID
    preview_fingerprint: str
    resumed_at: datetime
    actor: str
    reason: str
    evidence_ref: str
    context: CommandContext


@dataclass(frozen=True, slots=True)
class ReconcileTicketPausedPrepaidServiceOutcome:
    resume: ResumeTicketPausedServiceOutcome
    renewal_entitlement_id: UUID
    renewal_invoice_id: UUID | None
    renewal_ledger_entry_id: UUID | None
    renewal_amount: Decimal
    renewal_currency: str


@dataclass(frozen=True, slots=True)
class ResumeTicketPausedServiceCommand:
    subscription_id: UUID
    cause_id: UUID
    preview_fingerprint: str
    resumed_at: datetime
    actor: str
    reason: str
    context: CommandContext


@dataclass(frozen=True, slots=True)
class ResumeTicketPausedServiceOutcome:
    ticket_id: UUID
    subscription_id: UUID
    pause_episode_id: UUID
    pause_cause_id: UUID
    resulting_status: SubscriptionStatus
    paused_seconds: int
    previous_next_billing_at: datetime | None
    resulting_next_billing_at: datetime | None
    access_restored: bool
    replayed: bool


def _error(
    suffix: str, message: str, **details: object
) -> TicketSlaServiceAutomationError:
    return TicketSlaServiceAutomationError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
        retryable=False,
    )


def _pause_source(command: PauseTicketServiceForSlaBreachCommand) -> str:
    return (
        f"automation:ticket-sla-pause:{command.event_id}:"
        f"{command.rule_version_id}:{command.step_index}"
    )[:255]


def pause_unique_active_service_for_ticket_sla_breach(
    db: Session,
    command: PauseTicketServiceForSlaBreachCommand,
) -> PauseTicketServiceForSlaBreachOutcome:
    """Pause the linked customer's unique active service after a real SLA breach."""

    def operation() -> PauseTicketServiceForSlaBreachOutcome:
        event = db.scalar(
            select(EventStore)
            .where(EventStore.event_id == command.event_id)
            .with_for_update()
        )
        if (
            event is None
            or event.event_type != EventType.support_ticket_sla_breached.value
        ):
            raise _error(
                "sla_breach_not_authoritative",
                "The automation event is not authoritative SLA-breach evidence.",
                event_id=str(command.event_id),
            )
        payload_ticket_id = str(event.payload.get("ticket_id") or "")
        payload_clock_id = str(event.payload.get("sla_clock_id") or "")
        if payload_ticket_id != str(command.ticket_id):
            raise _error(
                "sla_breach_not_authoritative",
                "The SLA-breach event does not belong to the requested ticket.",
                event_id=str(command.event_id),
            )
        try:
            sla_clock_id = UUID(payload_clock_id)
        except ValueError as exc:
            raise _error(
                "sla_breach_not_authoritative",
                "The SLA-breach event has no valid SLA clock identity.",
                event_id=str(command.event_id),
            ) from exc
        sla_clock = db.scalar(
            select(SlaClock).where(SlaClock.id == sla_clock_id).with_for_update()
        )
        sla_breach = db.scalar(
            select(SlaBreach)
            .where(SlaBreach.clock_id == sla_clock_id)
            .order_by(SlaBreach.breached_at.desc(), SlaBreach.id.desc())
            .with_for_update()
        )
        if (
            sla_clock is None
            or sla_clock.entity_id != command.ticket_id
            or sla_clock.status != SlaClockStatus.breached.value
            or sla_clock.breached_at is None
            or sla_breach is None
        ):
            raise _error(
                "sla_breach_not_authoritative",
                "The SLA clock does not confirm a current breach for this ticket.",
                event_id=str(command.event_id),
                sla_clock_id=str(sla_clock_id),
            )
        ticket = db.scalar(
            select(Ticket).where(Ticket.id == command.ticket_id).with_for_update()
        )
        if ticket is None:
            raise _error(
                "ticket_not_found",
                "The SLA-breached ticket no longer exists.",
                ticket_id=str(command.ticket_id),
            )
        if ticket.status in {
            TicketStatus.pending_confirmation.value,
            TicketStatus.closed.value,
            TicketStatus.canceled.value,
        }:
            raise _error(
                "ticket_already_resolved",
                "The ticket was resolved before the service pause executed.",
                ticket_id=str(ticket.id),
                ticket_status=ticket.status,
            )
        account_id = ticket.customer_account_id or ticket.subscriber_id
        if account_id is None:
            raise _error(
                "customer_account_missing",
                "The SLA-breached ticket is not linked to a customer account.",
                ticket_id=str(ticket.id),
            )

        source = _pause_source(command)
        prior = db.execute(
            select(SubscriptionPauseCause, SubscriptionPauseEpisode)
            .join(
                SubscriptionPauseEpisode,
                SubscriptionPauseEpisode.id == SubscriptionPauseCause.pause_episode_id,
            )
            .where(
                SubscriptionPauseCause.source_type
                == SubscriptionPauseSource.automation_workflow.value,
                SubscriptionPauseCause.source_id == source,
            )
            .with_for_update()
        ).first()
        if prior is not None:
            prior_cause, prior_episode = prior
            return PauseTicketServiceForSlaBreachOutcome(
                ticket_id=ticket.id,
                subscription_id=prior_episode.subscription_id,
                pause_episode_id=prior_episode.id,
                pause_cause_id=prior_cause.id,
                replayed=True,
            )

        active_services = tuple(
            db.scalars(
                select(Subscription)
                .where(
                    Subscription.subscriber_id == account_id,
                    Subscription.status == SubscriptionStatus.active,
                )
                .order_by(Subscription.id.asc())
                .with_for_update()
            ).all()
        )
        if not active_services:
            raise _error(
                "active_service_not_found",
                "The linked customer has no active service to pause.",
                ticket_id=str(ticket.id),
                customer_account_id=str(account_id),
            )
        if len(active_services) != 1:
            raise _error(
                "active_service_ambiguous",
                "The linked customer has multiple active services; no service was paused.",
                ticket_id=str(ticket.id),
                customer_account_id=str(account_id),
                active_service_count=len(active_services),
            )

        effective_at = datetime.now(UTC)
        subscription = active_services[0]
        try:
            outcome = account_lifecycle.pause_subscription_for_cause(
                db,
                account_lifecycle.PauseSubscriptionCauseCommand(
                    subscription_id=subscription.id,
                    reason=SubscriptionPauseReason.ticket_resolution_sla_breach,
                    source_type=SubscriptionPauseSource.automation_workflow,
                    source_id=source,
                    selection_policy=command.selection_policy.value,
                    resume_policy=command.resume_policy,
                    billing_policy=command.billing_policy,
                    requested_at=effective_at,
                    effective_at=effective_at,
                    actor=command.context.actor,
                    idempotency_key=command.context.idempotency_key or source,
                    context=command.context,
                    ticket_id=ticket.id,
                    sla_clock_id=sla_clock.id,
                    sla_breach_id=sla_breach.id,
                    automation_event_id=command.event_id,
                    automation_rule_id=command.rule_id,
                    automation_rule_version_id=command.rule_version_id,
                    automation_step_index=command.step_index,
                ),
            )
        except ValueError as exc:
            raise _error(
                "pause_evidence_incomplete",
                "Subscription pause evidence is incomplete or inconsistent.",
                subscription_id=str(subscription.id),
            ) from exc
        return PauseTicketServiceForSlaBreachOutcome(
            ticket_id=ticket.id,
            subscription_id=outcome.subscription_id,
            pause_episode_id=outcome.episode_id,
            pause_cause_id=outcome.cause_id,
            replayed=outcome.replayed,
        )

    return execute_owner_command(
        db,
        definition=_PAUSE,
        context=command.context,
        operation=operation,
    )


def _resume_preview_fingerprint(parts: dict[str, object]) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def preview_ticket_service_resume(
    db: Session,
    *,
    cause_id: UUID,
    proposed_resumed_at: datetime | None = None,
) -> TicketServicePauseResumePreview:
    """Return a write-free, fingerprinted preview for one ticket pause cause."""

    resume_at = proposed_resumed_at or datetime.now(UTC)
    cause = db.get(SubscriptionPauseCause, cause_id)
    if cause is None or cause.ticket_id is None:
        raise _error("pause_cause_not_found", "Ticket pause cause was not found.")
    episode = db.get(SubscriptionPauseEpisode, cause.pause_episode_id)
    ticket = db.get(Ticket, cause.ticket_id)
    if episode is None or ticket is None:
        raise _error(
            "pause_evidence_incomplete",
            "Ticket pause evidence is incomplete and requires review.",
        )
    subscription = db.get(Subscription, episode.subscription_id)
    if subscription is None:
        raise _error(
            "active_service_not_found",
            "The paused subscription no longer exists.",
        )
    ticket_status = TicketStatus(ticket.status)
    blocking: list[str] = []
    if cause.status != "active" or episode.status != "active":
        blocking.append("pause_is_not_active")
    if ticket_status not in {
        TicketStatus.pending_confirmation,
        TicketStatus.closed,
    }:
        blocking.append("ticket_is_not_resolved")
    if subscription.status != SubscriptionStatus.paused:
        blocking.append("subscription_is_not_paused")
    effective_at = episode.effective_at
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=UTC)
    paused_seconds = max(0, int((resume_at - effective_at).total_seconds()))
    previous_anchor = episode.previous_next_billing_at
    projected_anchor = (
        previous_anchor + (resume_at - effective_at)
        if previous_anchor is not None
        else None
    )
    if previous_anchor is None:
        blocking.append("billing_anchor_missing")
    elif subscription.billing_mode == BillingMode.prepaid:
        compensation_preview = preview_pause_compensation_entitlement(
            db,
            PreviewPauseCompensationEntitlementQuery(
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
                pause_effective_at=effective_at,
                captured_billing_anchor=previous_anchor,
            ),
        )
        blocking.extend(compensation_preview.blocking_reasons)
    active_locks = account_lifecycle.get_active_locks(
        db, subscription_id=str(subscription.id)
    )
    active_cause_count = len(
        tuple(
            db.scalars(
                select(SubscriptionPauseCause.id).where(
                    SubscriptionPauseCause.pause_episode_id == episode.id,
                    SubscriptionPauseCause.status == "active",
                )
            ).all()
        )
    )
    fingerprint_parts: dict[str, object] = {
        "cause_id": str(cause.id),
        "cause_status": cause.status,
        "episode_id": str(episode.id),
        "episode_status": episode.status,
        "ticket_id": str(ticket.id),
        "ticket_status": ticket_status.value,
        "subscription_id": str(subscription.id),
        "subscription_status": subscription.status.value,
        "effective_at": effective_at.isoformat(),
        "previous_next_billing_at": (
            previous_anchor.isoformat() if previous_anchor else None
        ),
        "current_next_billing_at": (
            subscription.next_billing_at.isoformat()
            if subscription.next_billing_at
            else None
        ),
        "active_pause_cause_count": active_cause_count,
        "active_lock_ids": sorted(str(lock.id) for lock in active_locks),
    }
    return TicketServicePauseResumePreview(
        cause_id=cause.id,
        episode_id=episode.id,
        ticket_id=ticket.id,
        subscription_id=subscription.id,
        ticket_status=ticket_status,
        effective_at=effective_at,
        proposed_resumed_at=resume_at,
        paused_seconds=paused_seconds,
        previous_next_billing_at=previous_anchor,
        projected_next_billing_at=projected_anchor,
        access_will_be_active=not active_locks and active_cause_count == 1,
        eligible=not blocking,
        blocking_reasons=tuple(blocking),
        fingerprint=_resume_preview_fingerprint(fingerprint_parts),
    )


def preview_ticket_service_resume_for_subscription(
    db: Session,
    query: TicketServicePauseResumePreviewQuery,
) -> TicketServicePauseResumePreview | None:
    """Resolve the active ticket pause and preview resume eligibility."""

    cause_id = db.scalar(
        select(SubscriptionPauseCause.id)
        .join(
            SubscriptionPauseEpisode,
            SubscriptionPauseEpisode.id == SubscriptionPauseCause.pause_episode_id,
        )
        .where(
            SubscriptionPauseEpisode.subscription_id == query.subscription_id,
            SubscriptionPauseEpisode.status
            == SubscriptionPauseEpisodeStatus.active.value,
            SubscriptionPauseCause.status == SubscriptionPauseCauseStatus.active.value,
            SubscriptionPauseCause.ticket_id.is_not(None),
        )
        .order_by(SubscriptionPauseCause.created_at.asc())
        .limit(1)
    )
    if cause_id is None:
        return None
    return preview_ticket_service_resume(
        db,
        cause_id=cause_id,
        proposed_resumed_at=query.proposed_resumed_at,
    )


def preview_ticket_paused_prepaid_reconciliation(
    db: Session,
    query: TicketServicePauseResumePreviewQuery,
) -> TicketPausedPrepaidReconciliationPreview:
    """Preview a missed funded renewal that caused a pause-resume deadlock."""

    resume_preview = preview_ticket_service_resume_for_subscription(db, query)
    if resume_preview is None:
        raise _error(
            "pause_cause_not_found",
            "No active ticket-linked pause was found for the subscription.",
        )
    blocking = [
        reason
        for reason in resume_preview.blocking_reasons
        if reason
        not in {
            "prepaid_pause_coverage_ambiguous",
            "prepaid_pause_anchor_mismatch",
        }
    ]
    starts_at = resume_preview.previous_next_billing_at
    if starts_at is None:
        blocking.append("billing_anchor_missing")
        starts_at = resume_preview.effective_at

    from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
    from app.services.billing_automation import _period_end
    from app.services.prepaid_service_renewals import (
        PrepaidRenewalEligibilityContext,
        preview_prepaid_service_renewal,
        resolve_prepaid_monthly_charge,
    )

    subscription = db.get(Subscription, resume_preview.subscription_id)
    if subscription is None or subscription.billing_mode != BillingMode.prepaid:
        blocking.append("subscription_not_prepaid")
        amount = Decimal("0.00")
        currency = "NGN"
        ends_at = starts_at
        renewal_fingerprint = ""
    else:
        charge = resolve_prepaid_monthly_charge(db, subscription, starts_at)
        if charge is None:
            blocking.append("prepaid_renewal_terms_missing")
            amount = Decimal("0.00")
            currency = "NGN"
            ends_at = starts_at
            renewal_fingerprint = ""
        else:
            amount, currency, cycle = charge
            ends_at = _period_end(starts_at, cycle)
            renewal = preview_prepaid_service_renewal(
                db,
                subscription_id=subscription.id,
                starts_at=starts_at,
                ends_at=ends_at,
                amount=amount,
                currency=currency,
                eligibility_context=(
                    PrepaidRenewalEligibilityContext.ticket_pause_reconciliation
                ),
            )
            renewal_fingerprint = renewal.fingerprint
            if not renewal.allowed:
                blocking.append("prepaid_reconciliation_insufficient_funding")

            entitlements = tuple(
                db.scalars(
                    select(ServiceEntitlement).where(
                        ServiceEntitlement.subscription_id == subscription.id,
                        ServiceEntitlement.account_id == subscription.subscriber_id,
                        ServiceEntitlement.status == ServiceEntitlementStatus.active,
                    )
                ).all()
            )
            exact_prior_coverage = tuple(
                item for item in entitlements if item.ends_at == starts_at
            )
            if len(exact_prior_coverage) != 1:
                blocking.append("prepaid_reconciliation_anchor_evidence_ambiguous")
            if starts_at >= resume_preview.effective_at:
                blocking.append("prepaid_reconciliation_period_invalid")

    fingerprint = _resume_preview_fingerprint(
        {
            "resume": resume_preview.fingerprint,
            "renewal_starts_at": starts_at,
            "renewal_ends_at": ends_at,
            "renewal_amount": amount,
            "renewal_currency": currency,
            "renewal_preview_fingerprint": renewal_fingerprint,
        }
    )
    return TicketPausedPrepaidReconciliationPreview(
        resume_preview=resume_preview,
        renewal_starts_at=starts_at,
        renewal_ends_at=ends_at,
        renewal_amount=amount,
        renewal_currency=currency,
        renewal_preview_fingerprint=renewal_fingerprint,
        eligible=not blocking,
        blocking_reasons=tuple(blocking),
        fingerprint=fingerprint,
    )


def reconcile_ticket_paused_prepaid_service(
    db: Session,
    command: ReconcileTicketPausedPrepaidServiceCommand,
) -> ReconcileTicketPausedPrepaidServiceOutcome:
    """Settle one reviewed missed cycle and resume its ticket-linked pause."""

    def operation() -> ReconcileTicketPausedPrepaidServiceOutcome:
        if not command.reason.strip():
            raise _error(
                "resume_reason_required",
                "An operational reason is required for reconciliation.",
            )
        if not command.evidence_ref.strip():
            raise _error(
                "pause_evidence_incomplete",
                "A reviewed renewal evidence reference is required.",
            )
        cause = db.scalar(
            select(SubscriptionPauseCause)
            .where(SubscriptionPauseCause.id == command.cause_id)
            .with_for_update()
        )
        if cause is None or cause.ticket_id is None:
            raise _error("pause_cause_not_found", "Ticket pause cause was not found.")
        db.scalar(select(Ticket).where(Ticket.id == cause.ticket_id).with_for_update())
        episode = db.scalar(
            select(SubscriptionPauseEpisode)
            .where(SubscriptionPauseEpisode.id == cause.pause_episode_id)
            .with_for_update()
        )
        if episode is None or episode.subscription_id != command.subscription_id:
            raise _error(
                "pause_subscription_mismatch",
                "The pause cause does not belong to the requested subscription.",
            )
        db.scalar(
            select(Subscription)
            .where(Subscription.id == command.subscription_id)
            .with_for_update()
        )
        preview = preview_ticket_paused_prepaid_reconciliation(
            db,
            TicketServicePauseResumePreviewQuery(
                subscription_id=command.subscription_id,
                proposed_resumed_at=command.resumed_at,
            ),
        )
        if preview.fingerprint != command.preview_fingerprint:
            raise _error(
                "stale_resume_preview",
                "Pause or billing evidence changed after the reconciliation preview.",
            )
        if not preview.eligible:
            raise _error(
                "resume_ineligible",
                "The paused prepaid service is not eligible for reconciliation.",
                blocking_reasons=preview.blocking_reasons,
            )
        from app.services.prepaid_service_renewals import (
            ExecuteReviewedPrepaidServiceRenewalCommand,
            PrepaidRenewalEligibilityContext,
            execute_reviewed_prepaid_service_renewal_in_coordinator,
        )

        renewal = execute_reviewed_prepaid_service_renewal_in_coordinator(
            db,
            ExecuteReviewedPrepaidServiceRenewalCommand(
                context=command.context,
                subscription_id=command.subscription_id,
                starts_at=preview.renewal_starts_at,
                ends_at=preview.renewal_ends_at,
                amount=preview.renewal_amount,
                currency=preview.renewal_currency,
                expected_preview_fingerprint=preview.renewal_preview_fingerprint,
                evidence_ref=command.evidence_ref,
                eligibility_context=(
                    PrepaidRenewalEligibilityContext.ticket_pause_reconciliation
                ),
            ),
        )
        outcome = account_lifecycle.release_pause_cause_and_resume_subscription(
            db,
            account_lifecycle.ResumePausedSubscriptionCauseCommand(
                cause_id=command.cause_id,
                preview_fingerprint=preview.fingerprint,
                resumed_at=command.resumed_at,
                actor=command.actor,
                reason=command.reason,
                context=command.context,
                reconciled_renewal_period_start=preview.renewal_starts_at,
                reconciled_renewal_period_end=preview.renewal_ends_at,
            ),
        )
        return ReconcileTicketPausedPrepaidServiceOutcome(
            resume=ResumeTicketPausedServiceOutcome(
                ticket_id=preview.resume_preview.ticket_id,
                subscription_id=outcome.subscription_id,
                pause_episode_id=outcome.episode_id,
                pause_cause_id=outcome.cause_id,
                resulting_status=outcome.resulting_status,
                paused_seconds=outcome.paused_seconds,
                previous_next_billing_at=outcome.previous_next_billing_at,
                resulting_next_billing_at=outcome.resulting_next_billing_at,
                access_restored=outcome.resulting_status == SubscriptionStatus.active,
                replayed=outcome.replayed,
            ),
            renewal_entitlement_id=renewal.renewal.entitlement.id,
            renewal_invoice_id=(
                renewal.renewal.invoice.id if renewal.renewal.invoice else None
            ),
            renewal_ledger_entry_id=(
                renewal.renewal.ledger_entry.id
                if renewal.renewal.ledger_entry
                else None
            ),
            renewal_amount=renewal.renewal.preview.amount,
            renewal_currency=renewal.renewal.preview.currency,
        )

    return execute_owner_command(
        db,
        definition=_RESUME,
        context=command.context,
        operation=operation,
    )


def resume_ticket_paused_service(
    db: Session,
    command: ResumeTicketPausedServiceCommand,
) -> ResumeTicketPausedServiceOutcome:
    """Resume a ticket-linked pause after locking and verifying its preview."""

    def operation() -> ResumeTicketPausedServiceOutcome:
        if not command.reason.strip():
            raise _error(
                "resume_reason_required",
                "An operational reason is required to resume a paused service.",
            )
        cause = db.scalar(
            select(SubscriptionPauseCause)
            .where(SubscriptionPauseCause.id == command.cause_id)
            .with_for_update()
        )
        if cause is None or cause.ticket_id is None:
            raise _error("pause_cause_not_found", "Ticket pause cause was not found.")
        db.scalar(select(Ticket).where(Ticket.id == cause.ticket_id).with_for_update())
        episode = db.scalar(
            select(SubscriptionPauseEpisode)
            .where(SubscriptionPauseEpisode.id == cause.pause_episode_id)
            .with_for_update()
        )
        if episode is None:
            raise _error(
                "pause_evidence_incomplete",
                "Ticket pause evidence is incomplete and requires review.",
            )
        subscription = db.scalar(
            select(Subscription)
            .where(Subscription.id == episode.subscription_id)
            .with_for_update()
        )
        if subscription is None:
            raise _error(
                "active_service_not_found", "The paused subscription no longer exists."
            )
        if subscription.id != command.subscription_id:
            raise _error(
                "pause_subscription_mismatch",
                "The pause cause does not belong to the requested subscription.",
                requested_subscription_id=str(command.subscription_id),
                pause_subscription_id=str(subscription.id),
            )
        if cause.status != "active" and episode.status == "resumed":
            paused_seconds = episode.effective_duration_seconds
            if paused_seconds is None:
                raise _error(
                    "pause_evidence_incomplete",
                    "The completed pause has no effective-duration evidence.",
                )
            return ResumeTicketPausedServiceOutcome(
                ticket_id=cause.ticket_id,
                subscription_id=subscription.id,
                pause_episode_id=episode.id,
                pause_cause_id=cause.id,
                resulting_status=subscription.status,
                paused_seconds=paused_seconds,
                previous_next_billing_at=episode.previous_next_billing_at,
                resulting_next_billing_at=episode.resulting_next_billing_at,
                access_restored=subscription.status == SubscriptionStatus.active,
                replayed=True,
            )
        if cause.status != "active" and episode.status == "active":
            released_at = cause.released_at
            effective_at = episode.effective_at
            if released_at is None:
                raise _error(
                    "pause_evidence_incomplete",
                    "The released pause cause has no release time.",
                )
            if released_at.tzinfo is None:
                released_at = released_at.replace(tzinfo=UTC)
            if effective_at.tzinfo is None:
                effective_at = effective_at.replace(tzinfo=UTC)
            return ResumeTicketPausedServiceOutcome(
                ticket_id=cause.ticket_id,
                subscription_id=subscription.id,
                pause_episode_id=episode.id,
                pause_cause_id=cause.id,
                resulting_status=subscription.status,
                paused_seconds=max(
                    0, int((released_at - effective_at).total_seconds())
                ),
                previous_next_billing_at=episode.previous_next_billing_at,
                resulting_next_billing_at=None,
                access_restored=False,
                replayed=True,
            )
        preview = preview_ticket_service_resume(
            db,
            cause_id=cause.id,
            proposed_resumed_at=command.resumed_at,
        )
        if preview.fingerprint != command.preview_fingerprint:
            raise _error(
                "stale_resume_preview",
                "Pause or ticket evidence changed after the resume preview.",
            )
        if not preview.eligible:
            raise _error(
                "resume_ineligible",
                "The paused service is not eligible for resume.",
                blocking_reasons=preview.blocking_reasons,
            )
        try:
            outcome = account_lifecycle.release_pause_cause_and_resume_subscription(
                db,
                account_lifecycle.ResumePausedSubscriptionCauseCommand(
                    cause_id=cause.id,
                    preview_fingerprint=command.preview_fingerprint,
                    resumed_at=command.resumed_at,
                    actor=command.actor,
                    reason=command.reason,
                    context=command.context,
                ),
            )
        except ValueError as exc:
            raise _error(
                "pause_evidence_incomplete",
                "Subscription pause or billing evidence requires review.",
                subscription_id=str(subscription.id),
            ) from exc
        return ResumeTicketPausedServiceOutcome(
            ticket_id=preview.ticket_id,
            subscription_id=outcome.subscription_id,
            pause_episode_id=outcome.episode_id,
            pause_cause_id=outcome.cause_id,
            resulting_status=outcome.resulting_status,
            paused_seconds=outcome.paused_seconds,
            previous_next_billing_at=outcome.previous_next_billing_at,
            resulting_next_billing_at=outcome.resulting_next_billing_at,
            access_restored=outcome.resulting_status == SubscriptionStatus.active,
            replayed=outcome.replayed,
        )

    return execute_owner_command(
        db,
        definition=_RESUME,
        context=command.context,
        operation=operation,
    )


__all__ = [
    "PauseTicketServiceForSlaBreachCommand",
    "PauseTicketServiceForSlaBreachOutcome",
    "ReconcileTicketPausedPrepaidServiceCommand",
    "ReconcileTicketPausedPrepaidServiceOutcome",
    "ResumeTicketPausedServiceCommand",
    "ResumeTicketPausedServiceOutcome",
    "TicketPausedPrepaidReconciliationPreview",
    "TicketServicePauseResumePreview",
    "TicketServicePauseResumePreviewQuery",
    "TicketSlaServiceSelectionPolicy",
    "TicketSlaServiceAutomationError",
    "pause_unique_active_service_for_ticket_sla_breach",
    "preview_ticket_service_resume",
    "preview_ticket_service_resume_for_subscription",
    "preview_ticket_paused_prepaid_reconciliation",
    "reconcile_ticket_paused_prepaid_service",
    "resume_ticket_paused_service",
]
