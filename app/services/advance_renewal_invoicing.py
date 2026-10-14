"""Advance renewal invoices anchored to existing service coverage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    TaxApplication,
)
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.durable_timer import DurableTimer, TimerStatus
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import InvoiceCreate, SystemInvoiceLineCreate
from app.services import settings_spec
from app.services.audit import AuditEvents
from app.services.billing.invoices import InvoiceLines, Invoices
from app.services.billing_automation import (
    RecurringChargeComponentKind,
    preview_postpaid_recurring_charge,
)
from app.services.domain_errors import DomainError
from app.services.enforcement_window import to_local
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.form_contracts import FormConsequence, FormContract
from app.services.form_contracts import register as register_form_contract
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.prepaid_service_coverage import (
    PrepaidCoverageStatus,
    resolve_prepaid_service_coverage,
)
from app.services.prepaid_service_renewals import preview_prepaid_recurring_charge
from app.services.runtime_durable_timers import (
    ScheduleTimerCommand,
    cancel_timer,
    schedule_timer,
)

ADVANCE_RENEWAL_WRITE_SCOPE = "billing:advance-renewal:write"
MANUAL_SUBSCRIPTION_SERVICE_INVOICE_SCOPE = (
    "billing:invoice:create_subscription_service"
)
ADVANCE_RENEWAL_TIMER_PURPOSE = "advance_renewal_invoice_due"
ADVANCE_RENEWAL_TIMER_TRIGGER = "financial.advance_renewal_invoice_due"
ADVANCE_RENEWAL_TIMER_OWNER = "financial.advance_renewal_invoicing"
_GENERATE_COMMAND = OwnerCommandDefinition(
    owner="financial.advance_renewal_invoicing",
    concern="idempotent advance renewal invoice and notification request",
    name="generate_advance_renewal_invoice",
)
_SCHEDULE_COMMAND = OwnerCommandDefinition(
    owner=ADVANCE_RENEWAL_TIMER_OWNER,
    concern="per-subscription advance renewal timer",
    name="schedule_advance_renewal_timer",
)
_MANUAL_CREATE_COMMAND = OwnerCommandDefinition(
    owner=ADVANCE_RENEWAL_TIMER_OWNER,
    concern="reviewed next-period subscription service invoice",
    name="create_manual_subscription_service_invoice",
)

SUBSCRIPTION_SERVICE_INVOICE_FORM = register_form_contract(
    FormContract(
        key="admin.subscription_service_invoice",
        title="Issue next service-period invoice",
        entity="subscription",
        command_owner=ADVANCE_RENEWAL_TIMER_OWNER,
        consequences=(
            FormConsequence(
                "receivable",
                "Issues an immutable customer receivable for the exact next service period.",
            ),
            FormConsequence(
                "subscription_link",
                "Links every invoice line to this subscription and period.",
            ),
            FormConsequence(
                "prepaid_entitlement",
                "For prepaid service, full settlement creates one idempotent entitlement for this period; invoice creation alone does not extend access.",
            ),
            FormConsequence(
                "billing_anchor",
                "Does not advance the subscription billing anchor; settlement owns that consequence.",
            ),
        ),
    )
)


class RenewalNoticeConfigurationState(StrEnum):
    disabled = "disabled"
    configured = "configured"
    invalid = "invalid"


@dataclass(frozen=True, slots=True)
class RenewalNoticeConfiguration:
    state: RenewalNoticeConfigurationState
    days_before: int | None


class AdvanceRenewalInvoiceDisposition(StrEnum):
    created = "created"
    replayed = "replayed"


@dataclass(frozen=True, slots=True)
class GenerateAdvanceRenewalInvoiceCommand:
    context: CommandContext
    subscription_id: UUID
    evaluated_at: datetime
    timer_id: UUID
    timer_generation: int


@dataclass(frozen=True, slots=True)
class AdvanceRenewalInvoiceOutcome:
    subscription_id: UUID
    invoice_id: UUID
    period_start: datetime
    period_end: datetime
    disposition: AdvanceRenewalInvoiceDisposition
    notification_requested: bool


@dataclass(frozen=True, slots=True)
class ManualSubscriptionServiceInvoiceQuery:
    account_id: UUID
    subscription_id: UUID
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class ManualSubscriptionServiceInvoicePreview:
    account_id: UUID
    subscription_id: UUID
    account_name: str
    account_number: str | None
    plan_name: str
    billing_mode: BillingMode
    period_start: datetime
    period_end: datetime
    currency: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    component_count: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class CreateManualSubscriptionServiceInvoiceCommand:
    context: CommandContext
    query: ManualSubscriptionServiceInvoiceQuery
    expected_preview_fingerprint: str
    permission_granted: bool
    confirmation: bool
    notify_customer: bool = False


@dataclass(frozen=True, slots=True)
class ManualSubscriptionServiceInvoiceOutcome:
    subscription_id: UUID
    invoice_id: UUID
    period_start: datetime
    period_end: datetime
    disposition: AdvanceRenewalInvoiceDisposition
    notification_requested: bool


@dataclass(frozen=True, slots=True)
class ScheduleAdvanceRenewalTimerCommand:
    context: CommandContext
    subscription_id: UUID


@dataclass(frozen=True, slots=True)
class AdvanceRenewalTimerOutcome:
    subscription_id: UUID
    timer_id: UUID | None
    due_at: datetime | None
    scheduled: bool


class AdvanceRenewalInvoiceError(DomainError):
    """Stable failure from the advance renewal owner."""


def _error(suffix: str, message: str, **details: object) -> AdvanceRenewalInvoiceError:
    return AdvanceRenewalInvoiceError(
        code=f"financial.advance_renewal_invoicing.{suffix}",
        message=message,
        details=details,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def resolve_notice_configuration(db: Session) -> RenewalNoticeConfiguration:
    enabled = settings_spec.resolve_value(
        db, SettingDomain.billing, "renewal_invoice_notice_enabled"
    )
    if enabled is not True:
        return RenewalNoticeConfiguration(
            state=RenewalNoticeConfigurationState.disabled, days_before=None
        )
    raw_days = settings_spec.resolve_value(
        db, SettingDomain.billing, "renewal_invoice_notice_days"
    )
    if not isinstance(raw_days, int) or not 0 <= raw_days <= 90:
        return RenewalNoticeConfiguration(
            state=RenewalNoticeConfigurationState.invalid, days_before=None
        )
    return RenewalNoticeConfiguration(
        state=RenewalNoticeConfigurationState.configured,
        days_before=raw_days,
    )


def _renewal_boundary(
    db: Session, subscription: Subscription, *, evaluated_at: datetime
) -> datetime:
    projected = subscription.next_billing_at
    if projected is None:
        raise _error(
            "missing_renewal_boundary",
            "Advance renewal invoicing requires a billing anchor.",
            subscription_id=str(subscription.id),
        )
    boundary = _utc(projected)
    if subscription.billing_mode is BillingMode.prepaid:
        decision = resolve_prepaid_service_coverage(
            db, [subscription], as_of=evaluated_at
        )[subscription.id]
        if (
            decision.status is not PrepaidCoverageStatus.covered
            or decision.evidence is None
        ):
            raise _error(
                "coverage_ambiguous",
                "Prepaid renewal requires current authoritative coverage evidence.",
                subscription_id=str(subscription.id),
            )
        evidence_end = _utc(decision.evidence.ends_at)
        if evidence_end != boundary:
            raise _error(
                "coverage_anchor_drift",
                "Coverage evidence and the billing anchor disagree.",
                subscription_id=str(subscription.id),
                evidence_end=evidence_end.isoformat(),
                projected_boundary=boundary.isoformat(),
            )
    return boundary


def schedule_advance_renewal_timer(
    db: Session, command: ScheduleAdvanceRenewalTimerCommand
) -> AdvanceRenewalTimerOutcome:
    """Create or cancel one exact subscription's durable notice timer."""

    def operation() -> AdvanceRenewalTimerOutcome:
        subscription = db.scalar(
            select(Subscription)
            .where(Subscription.id == command.subscription_id)
            .with_for_update()
        )
        configuration = resolve_notice_configuration(db)
        if (
            subscription is None
            or subscription.status is not SubscriptionStatus.active
            or subscription.next_billing_at is None
            or configuration.state is not RenewalNoticeConfigurationState.configured
        ):
            cancel_timer(
                db,
                owner=ADVANCE_RENEWAL_TIMER_OWNER,
                entity_kind="subscription",
                entity_id=command.subscription_id,
                purpose=ADVANCE_RENEWAL_TIMER_PURPOSE,
            )
            return AdvanceRenewalTimerOutcome(
                subscription_id=command.subscription_id,
                timer_id=None,
                due_at=None,
                scheduled=False,
            )
        assert configuration.days_before is not None
        boundary = _utc(subscription.next_billing_at)
        due_at = boundary - timedelta(days=configuration.days_before)
        timer = schedule_timer(
            db,
            ScheduleTimerCommand(
                owner=ADVANCE_RENEWAL_TIMER_OWNER,
                entity_kind="subscription",
                entity_id=subscription.id,
                purpose=ADVANCE_RENEWAL_TIMER_PURPOSE,
                due_at=due_at,
                output_event_type=ADVANCE_RENEWAL_TIMER_TRIGGER,
            ),
            context=command.context,
        )
        return AdvanceRenewalTimerOutcome(
            subscription_id=subscription.id,
            timer_id=timer.id,
            due_at=due_at,
            scheduled=True,
        )

    return execute_owner_command(
        db,
        definition=_SCHEDULE_COMMAND,
        context=command.context,
        operation=operation,
    )


def _billing_line_key(
    subscription_id: UUID, period_start: datetime, period_end: datetime, component: str
) -> str:
    return (
        f"subscription:{subscription_id}:{period_start.isoformat()}:"
        f"{period_end.isoformat()}:advance-renewal:{component}"
    )


@dataclass(frozen=True, slots=True)
class _ChargeComponent:
    component_key: str
    kind: str
    quantity: Decimal
    unit_price: Decimal
    net_amount: Decimal
    tax_amount: Decimal
    gross_amount: Decimal
    tax_application: TaxApplication
    tax_rate_id: UUID | None


@dataclass(frozen=True, slots=True)
class _SubscriptionPeriodCharge:
    period_start: datetime
    period_end: datetime
    currency: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    components: tuple[_ChargeComponent, ...]


def _resolve_subscription_period_charge(
    db: Session,
    subscription: Subscription,
    *,
    evaluated_at: datetime,
) -> _SubscriptionPeriodCharge:
    period_start = _renewal_boundary(db, subscription, evaluated_at=evaluated_at)
    if subscription.billing_mode is BillingMode.prepaid:
        prepaid_source = preview_prepaid_recurring_charge(
            db, subscription_id=subscription.id, as_of=evaluated_at
        )
        if prepaid_source.excluded_recurring_addon_ids:
            raise _error(
                "charge_preview_incomplete",
                "The prepaid charge preview excludes recurring add-ons. Resolve them before issuing a manual service invoice.",
                subscription_id=str(subscription.id),
                excluded_addon_ids=tuple(
                    str(item) for item in prepaid_source.excluded_recurring_addon_ids
                ),
            )
        charge = _SubscriptionPeriodCharge(
            period_start=prepaid_source.period_start,
            period_end=prepaid_source.period_end,
            currency=prepaid_source.currency,
            subtotal=prepaid_source.gross_amount,
            tax_total=Decimal("0.00"),
            total=prepaid_source.gross_amount,
            components=(
                _ChargeComponent(
                    component_key="base",
                    kind="base_subscription",
                    quantity=Decimal("1"),
                    unit_price=prepaid_source.gross_amount,
                    net_amount=prepaid_source.gross_amount,
                    tax_amount=Decimal("0.00"),
                    gross_amount=prepaid_source.gross_amount,
                    tax_application=TaxApplication.exempt,
                    tax_rate_id=None,
                ),
            ),
        )
    else:
        postpaid_source = preview_postpaid_recurring_charge(
            db, subscription_id=subscription.id, as_of=evaluated_at
        )
        if postpaid_source.issues:
            raise _error(
                "charge_preview_incomplete",
                "The recurring charge preview has unresolved component issues. Resolve them before issuing a manual service invoice.",
                subscription_id=str(subscription.id),
                issue_kinds=tuple(item.kind.value for item in postpaid_source.issues),
            )
        charge = _SubscriptionPeriodCharge(
            period_start=postpaid_source.period_start,
            period_end=postpaid_source.period_end,
            currency=postpaid_source.currency,
            subtotal=postpaid_source.net_amount,
            tax_total=postpaid_source.tax_amount,
            total=postpaid_source.gross_amount,
            components=tuple(
                _ChargeComponent(
                    component_key=(
                        "base"
                        if component.kind is RecurringChargeComponentKind.base_service
                        else component.component_key
                    ),
                    kind=component.kind.value,
                    quantity=component.quantity,
                    unit_price=component.unit_price,
                    net_amount=component.net_amount,
                    tax_amount=component.tax_amount,
                    gross_amount=component.gross_amount,
                    tax_application=component.tax_application,
                    tax_rate_id=component.tax_rate_id,
                )
                for component in postpaid_source.components
            ),
        )
    if period_start != charge.period_start:
        raise _error(
            "period_drift",
            "The charge owner returned a different renewal boundary.",
            subscription_id=str(subscription.id),
        )
    return charge


def _manual_preview_fingerprint(
    subscription: Subscription, charge: _SubscriptionPeriodCharge
) -> str:
    payload = {
        "account_id": str(subscription.subscriber_id),
        "subscription_id": str(subscription.id),
        "subscription_updated_at": _utc(subscription.updated_at).isoformat(),
        "billing_mode": subscription.billing_mode.value,
        "period_start": charge.period_start.isoformat(),
        "period_end": charge.period_end.isoformat(),
        "currency": charge.currency.upper(),
        "subtotal": str(charge.subtotal),
        "tax_total": str(charge.tax_total),
        "total": str(charge.total),
        "components": [
            {
                "key": component.component_key,
                "kind": component.kind,
                "quantity": str(component.quantity),
                "unit_price": str(component.unit_price),
                "gross_amount": str(component.gross_amount),
                "tax_application": component.tax_application.value,
                "tax_rate_id": (
                    str(component.tax_rate_id) if component.tax_rate_id else None
                ),
            }
            for component in charge.components
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _existing_period_line(
    db: Session, subscription_id: UUID, charge: _SubscriptionPeriodCharge
) -> InvoiceLine | None:
    return db.scalar(
        select(InvoiceLine).where(
            InvoiceLine.billing_line_key
            == _billing_line_key(
                subscription_id,
                charge.period_start,
                charge.period_end,
                "base",
            ),
            InvoiceLine.is_active.is_(True),
        )
    )


def _validate_manual_candidate(
    db: Session,
    *,
    query: ManualSubscriptionServiceInvoiceQuery,
    subscription: Subscription,
    charge: _SubscriptionPeriodCharge,
    allow_existing_line: bool,
) -> None:
    evaluated_at = _utc(query.evaluated_at)
    _validate_manual_subscription_identity(subscription=subscription, query=query)
    if charge.period_start <= evaluated_at:
        raise _error(
            "period_not_future",
            "The next service period must start in the future. Use the reviewed historical workflow for an elapsed period.",
            subscription_id=str(subscription.id),
            period_start=charge.period_start.isoformat(),
        )
    terminal_end = _utc(subscription.end_at) if subscription.end_at else None
    if terminal_end is not None and terminal_end <= charge.period_start:
        raise _error(
            "terminal_subscription",
            "A terminal subscription cannot receive a next-period invoice.",
            subscription_id=str(subscription.id),
        )
    if not allow_existing_line and _existing_period_line(db, subscription.id, charge):
        raise _error(
            "period_already_invoiced",
            "This subscription service period already has an active invoice.",
            subscription_id=str(subscription.id),
            period_start=charge.period_start.isoformat(),
            period_end=charge.period_end.isoformat(),
        )
    conflicting_invoice = db.scalar(
        select(Invoice.id).where(
            Invoice.account_id == subscription.subscriber_id,
            Invoice.billing_period_start == charge.period_start,
            Invoice.billing_period_end == charge.period_end,
            Invoice.is_active.is_(True),
        )
    )
    if conflicting_invoice is not None:
        raise _error(
            "invoice_period_conflict",
            "The account already has an active invoice for this exact period. Review it before issuing another document.",
            invoice_id=str(conflicting_invoice),
        )


def _validate_manual_subscription_identity(
    *,
    subscription: Subscription,
    query: ManualSubscriptionServiceInvoiceQuery,
) -> None:
    if subscription.subscriber_id != query.account_id:
        raise _error(
            "account_mismatch",
            "The selected subscription does not belong to this billing account.",
            account_id=str(query.account_id),
            subscription_id=str(query.subscription_id),
        )
    if subscription.status is not SubscriptionStatus.active:
        raise _error(
            "subscription_not_eligible",
            "Next-period invoicing requires an active subscription.",
            subscription_id=str(subscription.id),
        )


def preview_manual_subscription_service_invoice(
    db: Session, query: ManualSubscriptionServiceInvoiceQuery
) -> ManualSubscriptionServiceInvoicePreview:
    evaluated_at = _utc(query.evaluated_at)
    subscription = db.get(Subscription, query.subscription_id)
    if subscription is None:
        raise _error(
            "subscription_not_found",
            "Subscription was not found.",
            subscription_id=str(query.subscription_id),
        )
    _validate_manual_subscription_identity(subscription=subscription, query=query)
    charge = _resolve_subscription_period_charge(
        db, subscription, evaluated_at=evaluated_at
    )
    _validate_manual_candidate(
        db,
        query=query,
        subscription=subscription,
        charge=charge,
        allow_existing_line=False,
    )
    account = subscription.subscriber
    return ManualSubscriptionServiceInvoicePreview(
        account_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        account_name=account.name,
        account_number=account.account_number,
        plan_name=subscription.offer.name if subscription.offer else "Service",
        billing_mode=subscription.billing_mode,
        period_start=charge.period_start,
        period_end=charge.period_end,
        currency=charge.currency.upper(),
        subtotal=charge.subtotal,
        tax_total=charge.tax_total,
        total=charge.total,
        component_count=len(charge.components),
        fingerprint=_manual_preview_fingerprint(subscription, charge),
    )


def generate_advance_renewal_invoice(
    db: Session, command: GenerateAdvanceRenewalInvoiceCommand
) -> AdvanceRenewalInvoiceOutcome:
    evaluated_at = _utc(command.evaluated_at)

    def operation() -> AdvanceRenewalInvoiceOutcome:
        configuration = resolve_notice_configuration(db)
        if configuration.state is not RenewalNoticeConfigurationState.configured:
            raise _error(
                "configuration_unavailable",
                "Advance renewal invoicing is disabled or unconfigured.",
            )
        subscription = db.scalar(
            select(Subscription)
            .where(Subscription.id == command.subscription_id)
            .with_for_update()
        )
        if subscription is None or subscription.status is not SubscriptionStatus.active:
            raise _error(
                "subscription_not_eligible",
                "Advance renewal requires an active subscription.",
                subscription_id=str(command.subscription_id),
            )
        timer = db.get(DurableTimer, command.timer_id)
        if (
            timer is None
            or timer.owner != ADVANCE_RENEWAL_TIMER_OWNER
            or timer.entity_kind != "subscription"
            or timer.entity_id != subscription.id
            or timer.purpose != ADVANCE_RENEWAL_TIMER_PURPOSE
            or timer.output_event_type != ADVANCE_RENEWAL_TIMER_TRIGGER
            or timer.generation != command.timer_generation
            or timer.status is not TimerStatus.fired
        ):
            raise _error(
                "stale_timer",
                "Advance renewal invoicing requires the current fired subscription timer.",
                subscription_id=str(subscription.id),
                timer_id=str(command.timer_id),
            )
        period_start = _renewal_boundary(db, subscription, evaluated_at=evaluated_at)
        assert configuration.days_before is not None
        expected_notice_date = to_local(db, period_start).date() - timedelta(
            days=configuration.days_before
        )
        if expected_notice_date != to_local(db, evaluated_at).date():
            raise _error(
                "outside_notice_date",
                "Subscription is outside the configured renewal notice date.",
                subscription_id=str(subscription.id),
            )
        terminal_end = _utc(subscription.end_at) if subscription.end_at else None
        if terminal_end is not None and terminal_end <= period_start:
            raise _error(
                "terminal_subscription",
                "A terminal subscription cannot receive a renewal invoice.",
                subscription_id=str(subscription.id),
            )

        if subscription.billing_mode is BillingMode.prepaid:
            prepaid_preview = preview_prepaid_recurring_charge(
                db, subscription_id=subscription.id, as_of=evaluated_at
            )
            preview_period_start = prepaid_preview.period_start
            period_end = prepaid_preview.period_end
            currency = prepaid_preview.currency
            components: tuple[
                tuple[str, str, Decimal, Decimal, TaxApplication, UUID | None], ...
            ] = (
                (
                    "base",
                    "base_subscription",
                    Decimal("1"),
                    prepaid_preview.gross_amount,
                    TaxApplication.exempt,
                    None,
                ),
            )
        else:
            postpaid_preview = preview_postpaid_recurring_charge(
                db, subscription_id=subscription.id, as_of=evaluated_at
            )
            preview_period_start = postpaid_preview.period_start
            period_end = postpaid_preview.period_end
            currency = postpaid_preview.currency
            components = tuple(
                (
                    "base"
                    if component.kind is RecurringChargeComponentKind.base_service
                    else component.component_key,
                    component.kind.value,
                    component.quantity,
                    component.unit_price,
                    component.tax_application,
                    component.tax_rate_id,
                )
                for component in postpaid_preview.components
            )
        if period_start != preview_period_start:
            raise _error(
                "period_drift",
                "The charge owner returned a different renewal boundary.",
                subscription_id=str(subscription.id),
            )

        base_key = _billing_line_key(subscription.id, period_start, period_end, "base")
        existing_line = db.scalar(
            select(InvoiceLine).where(
                InvoiceLine.billing_line_key == base_key,
                InvoiceLine.is_active.is_(True),
            )
        )
        if existing_line is not None:
            existing_invoice = db.get(Invoice, existing_line.invoice_id)
            if existing_invoice is None:
                raise _error("invoice_drift", "Existing billing line has no invoice.")
            return AdvanceRenewalInvoiceOutcome(
                subscription_id=subscription.id,
                invoice_id=existing_invoice.id,
                period_start=period_start,
                period_end=period_end,
                disposition=AdvanceRenewalInvoiceDisposition.replayed,
                notification_requested=False,
            )

        invoice = db.scalar(
            select(Invoice)
            .where(
                Invoice.account_id == subscription.subscriber_id,
                Invoice.billing_period_start == period_start,
                Invoice.billing_period_end == period_end,
                Invoice.is_active.is_(True),
            )
            .with_for_update()
        )
        if invoice is None:
            invoice = Invoices.stage_system_invoice(
                db,
                InvoiceCreate(
                    account_id=subscription.subscriber_id,
                    status=InvoiceStatus.issued,
                    currency=currency,
                    issued_at=evaluated_at,
                    due_at=period_start,
                    due_date_basis=InvoiceDueDateBasis.contract_terms,
                    due_date_basis_ref=(
                        f"subscription:{subscription.id}:period:{period_start.isoformat()}"
                    ),
                    due_date_policy_version="advance-renewal-terms-v1",
                    billing_period_start=period_start,
                    billing_period_end=period_end,
                ),
                reason="advance_renewal_invoice",
            )
        elif invoice.currency != currency:
            raise _error(
                "currency_conflict",
                "Existing future invoice has a different currency.",
                invoice_id=str(invoice.id),
            )

        offer_name = subscription.offer.name if subscription.offer else "Service"
        for (
            component_key,
            kind,
            quantity,
            unit_price,
            tax_application,
            tax_rate_id,
        ) in components:
            key = _billing_line_key(
                subscription.id, period_start, period_end, str(component_key)
            )
            description = (
                f"{offer_name} ({period_start.date()} - {period_end.date()})"
                if kind == RecurringChargeComponentKind.base_service.value
                or kind == "base_subscription"
                else f"{offer_name} add-on ({period_start.date()} - {period_end.date()})"
            )
            InvoiceLines.stage_system_line(
                db,
                SystemInvoiceLineCreate(
                    invoice_id=invoice.id,
                    subscription_id=subscription.id,
                    description=description,
                    quantity=quantity,
                    unit_price=unit_price,
                    tax_rate_id=tax_rate_id,
                    tax_application=tax_application,
                    metadata_={
                        "kind": kind,
                        "billing_period_start": period_start.isoformat(),
                        "billing_period_end": period_end.isoformat(),
                        "advance_renewal": True,
                    },
                    billing_line_key=key,
                ),
                reason="advance_renewal_invoice",
            )
        db.flush()
        emit_event(
            db,
            EventType.subscription_renewal_invoice_ready,
            {
                "subscription_id": str(subscription.id),
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number or "",
                "amount": str(invoice.total),
                "currency": invoice.currency,
                "due_date": period_start.date().isoformat(),
                "renewal_period_start": period_start.date().isoformat(),
                "renewal_period_end": period_end.date().isoformat(),
            },
            subscription_id=subscription.id,
            invoice_id=invoice.id,
            account_id=subscription.subscriber_id,
        )
        return AdvanceRenewalInvoiceOutcome(
            subscription_id=subscription.id,
            invoice_id=invoice.id,
            period_start=period_start,
            period_end=period_end,
            disposition=AdvanceRenewalInvoiceDisposition.created,
            notification_requested=True,
        )

    return execute_owner_command(
        db,
        definition=_GENERATE_COMMAND,
        context=command.context,
        operation=operation,
    )


def _audit_actor(context: CommandContext) -> tuple[AuditActorType, str | None]:
    prefix, separator, actor_id = context.actor.partition(":")
    actor_types = {
        "user": AuditActorType.user,
        "api_key": AuditActorType.api_key,
        "service": AuditActorType.service,
        "system": AuditActorType.system,
    }
    actor_type = actor_types.get(prefix, AuditActorType.service)
    if actor_type is AuditActorType.system:
        return actor_type, None
    return actor_type, actor_id if separator and actor_id.strip() else context.actor


def create_manual_subscription_service_invoice(
    db: Session, command: CreateManualSubscriptionServiceInvoiceCommand
) -> ManualSubscriptionServiceInvoiceOutcome:
    """Issue one reviewed invoice for the subscription's exact next period."""

    evaluated_at = _utc(command.query.evaluated_at)

    def operation() -> ManualSubscriptionServiceInvoiceOutcome:
        if (
            not command.permission_granted
            or command.context.scope != MANUAL_SUBSCRIPTION_SERVICE_INVOICE_SCOPE
        ):
            raise _error(
                "permission_denied",
                "Permission to issue subscription service invoices is required.",
            )
        if not command.confirmation:
            raise _error(
                "confirmation_required",
                "Confirm the disclosed receivable and service consequences.",
            )
        if not (command.context.reason or "").strip():
            raise _error(
                "reason_required",
                "A reason is required for this controlled financial action.",
            )
        subscription = db.scalar(
            select(Subscription)
            .where(Subscription.id == command.query.subscription_id)
            .with_for_update()
        )
        if subscription is None:
            raise _error(
                "subscription_not_found",
                "Subscription was not found.",
                subscription_id=str(command.query.subscription_id),
            )
        _validate_manual_subscription_identity(
            subscription=subscription, query=command.query
        )
        charge = _resolve_subscription_period_charge(
            db, subscription, evaluated_at=evaluated_at
        )
        existing_line = _existing_period_line(db, subscription.id, charge)
        if existing_line is not None:
            existing_invoice = db.get(Invoice, existing_line.invoice_id)
            if existing_invoice is None:
                raise _error(
                    "invoice_drift",
                    "Existing billing line has no invoice.",
                    invoice_line_id=str(existing_line.id),
                )
            return ManualSubscriptionServiceInvoiceOutcome(
                subscription_id=subscription.id,
                invoice_id=existing_invoice.id,
                period_start=charge.period_start,
                period_end=charge.period_end,
                disposition=AdvanceRenewalInvoiceDisposition.replayed,
                notification_requested=False,
            )
        _validate_manual_candidate(
            db,
            query=command.query,
            subscription=subscription,
            charge=charge,
            allow_existing_line=True,
        )
        actual_fingerprint = _manual_preview_fingerprint(subscription, charge)
        if actual_fingerprint != command.expected_preview_fingerprint:
            raise _error(
                "stale_preview",
                "Subscription, pricing, tax, or service-period facts changed. Review the invoice again.",
                expected_fingerprint=command.expected_preview_fingerprint,
                actual_fingerprint=actual_fingerprint,
            )

        invoice = Invoices.stage_system_invoice_for_owner(
            db,
            InvoiceCreate(
                account_id=subscription.subscriber_id,
                status=InvoiceStatus.issued,
                currency=charge.currency,
                issued_at=evaluated_at,
                due_at=charge.period_start,
                due_date_basis=InvoiceDueDateBasis.contract_terms,
                due_date_basis_ref=(
                    f"subscription:{subscription.id}:period:"
                    f"{charge.period_start.isoformat()}"
                ),
                due_date_policy_version="manual-next-service-period-v1",
                billing_period_start=charge.period_start,
                billing_period_end=charge.period_end,
                memo="Reviewed next service-period invoice",
            ),
            reason="manual_subscription_service_invoice",
        )
        invoice.metadata_ = {
            **(invoice.metadata_ or {}),
            "source_owner": ADVANCE_RENEWAL_TIMER_OWNER,
            "manual_subscription_service_invoice": True,
            "subscription_id": str(subscription.id),
            "preview_fingerprint": actual_fingerprint,
            "command_id": str(command.context.command_id),
        }
        offer_name = subscription.offer.name if subscription.offer else "Service"
        for component in charge.components:
            description = (
                f"{offer_name} ({charge.period_start.date()} - "
                f"{charge.period_end.date()})"
                if component.kind
                in {
                    RecurringChargeComponentKind.base_service.value,
                    "base_subscription",
                }
                else (
                    f"{offer_name} add-on ({charge.period_start.date()} - "
                    f"{charge.period_end.date()})"
                )
            )
            InvoiceLines.stage_system_line_for_owner(
                db,
                SystemInvoiceLineCreate(
                    invoice_id=invoice.id,
                    subscription_id=subscription.id,
                    description=description,
                    quantity=component.quantity,
                    unit_price=component.unit_price,
                    tax_rate_id=component.tax_rate_id,
                    tax_application=component.tax_application,
                    metadata_={
                        "kind": component.kind,
                        "billing_period_start": charge.period_start.isoformat(),
                        "billing_period_end": charge.period_end.isoformat(),
                        "advance_renewal": True,
                        "manual_subscription_service_invoice": True,
                    },
                    billing_line_key=_billing_line_key(
                        subscription.id,
                        charge.period_start,
                        charge.period_end,
                        component.component_key,
                    ),
                ),
                reason="manual_subscription_service_invoice",
            )
        invoice = Invoices.recalculate_totals_for_owner(db, invoice.id)
        actor_type, actor_id = _audit_actor(command.context)
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_type=actor_type,
                actor_id=actor_id,
                action="issue_subscription_service_invoice",
                entity_type="invoice",
                entity_id=str(invoice.id),
                metadata_={
                    "account_id": str(subscription.subscriber_id),
                    "subscription_id": str(subscription.id),
                    "billing_mode": subscription.billing_mode.value,
                    "period_start": charge.period_start.isoformat(),
                    "period_end": charge.period_end.isoformat(),
                    "currency": charge.currency,
                    "total": str(invoice.total),
                    "reason": command.context.reason,
                    "preview_fingerprint": actual_fingerprint,
                    "command_id": str(command.context.command_id),
                    "correlation_id": str(command.context.correlation_id),
                    "notification_requested": command.notify_customer,
                    "service_access_consequence": (
                        "entitlement_after_full_settlement"
                        if subscription.billing_mode is BillingMode.prepaid
                        else "documentary_period_only"
                    ),
                },
            ),
        )
        if command.notify_customer:
            emit_event(
                db,
                EventType.subscription_renewal_invoice_ready,
                {
                    "subscription_id": str(subscription.id),
                    "invoice_id": str(invoice.id),
                    "invoice_number": invoice.invoice_number or "",
                    "amount": str(invoice.total),
                    "currency": invoice.currency,
                    "due_date": charge.period_start.date().isoformat(),
                    "renewal_period_start": charge.period_start.date().isoformat(),
                    "renewal_period_end": charge.period_end.date().isoformat(),
                },
                subscription_id=subscription.id,
                invoice_id=invoice.id,
                account_id=subscription.subscriber_id,
            )
        return ManualSubscriptionServiceInvoiceOutcome(
            subscription_id=subscription.id,
            invoice_id=invoice.id,
            period_start=charge.period_start,
            period_end=charge.period_end,
            disposition=AdvanceRenewalInvoiceDisposition.created,
            notification_requested=command.notify_customer,
        )

    return execute_owner_command(
        db,
        definition=_MANUAL_CREATE_COMMAND,
        context=command.context,
        operation=operation,
    )
