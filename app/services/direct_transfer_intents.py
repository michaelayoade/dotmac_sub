"""Typed root command for customer direct-transfer intent creation."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, TopupIntent
from app.models.domain_settings import SettingDomain
from app.services import topup_intents
from app.services.account_credit_deposits import (
    SUPPORTED_CURRENCY,
    AccountCreditDeposits,
    DepositEligibilityError,
)
from app.services.billing._common import lock_account
from app.services.common import round_money, to_decimal
from app.services.domain_errors import DomainError
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.settings_spec import resolve_value

CREATE_SCOPE = "direct-transfer-intent:create"

_CREATE_COMMAND = OwnerCommandDefinition(
    owner="financial.direct_transfer_intent_commands",
    concern="customer direct-transfer intent creation coordination",
    name="create_customer_direct_transfer_intent",
)
_MINIMUM_SETTING = "topup_min_amount"
_MAXIMUM_SETTING = "topup_max_amount"
_TTL_SETTING = "direct_bank_transfer_intent_ttl_days"
_WITHHOLDING_TAX_RATE_SETTING = "withholding_tax_rate_percent"


class DirectTransferIntentError(DomainError, ValueError):
    """Stable rejection from the direct-transfer creation coordinator."""


def _error(suffix: str, message: str, **details: object) -> DirectTransferIntentError:
    return DirectTransferIntentError(
        code=f"financial.direct_transfer_intent_commands.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class CreateDirectTransferIntentCommand:
    """Authenticated customer request admitted by a portal or API adapter."""

    account_id: UUID
    created_by: str
    requested_amount: Decimal | int | float | str | None = None
    invoice_id: UUID | None = None
    expected_preview_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class DirectTransferIntentResult:
    """Immutable command result serialized only by adapters."""

    intent_id: UUID
    account_id: UUID
    reference: str
    provider_type: str
    currency: str
    requested_amount: Decimal
    expires_at: datetime
    payment_flow: str
    invoice_id: UUID | None
    preview_fingerprint: str | None
    replayed: bool
    replaced_intent_ids: tuple[UUID, ...]

    def to_dict(self) -> dict[str, object | None]:
        return {
            "intent_id": str(self.intent_id),
            "account_id": str(self.account_id),
            "reference": self.reference,
            "provider_type": self.provider_type,
            "currency": self.currency,
            "requested_amount": self.requested_amount,
            "expires_at": self.expires_at,
            "payment_flow": self.payment_flow,
            "invoice_id": str(self.invoice_id) if self.invoice_id else None,
            "preview_fingerprint": self.preview_fingerprint,
            "replayed": self.replayed,
            "replaced_intent_ids": [
                str(intent_id) for intent_id in self.replaced_intent_ids
            ],
        }


def _withholding_tax_rate_percent(db: Session) -> Decimal:
    raw = resolve_value(db, SettingDomain.billing, _WITHHOLDING_TAX_RATE_SETTING)
    try:
        value = round_money(to_decimal(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise _error(
            "withholding_tax_configuration_invalid",
            "Configured withholding-tax percentage is invalid",
            setting=_WITHHOLDING_TAX_RATE_SETTING,
        ) from exc
    if value <= Decimal("0.00") or value >= Decimal("100.00"):
        raise _error(
            "withholding_tax_configuration_invalid",
            "Configured withholding-tax percentage must be greater than 0 and less than 100",
            setting=_WITHHOLDING_TAX_RATE_SETTING,
        )
    return value


def validate_invoice_transfer_selection(db: Session, *, intent: TopupIntent) -> None:
    """Reject obsolete WHT instructions; selection never changes an old snapshot."""
    metadata = intent.metadata_ or {}
    if not metadata.get("withholding_tax"):
        return
    invoice_reference = intent.invoice_id or metadata.get("invoice_id")
    try:
        invoice_id = UUID(str(invoice_reference))
    except (TypeError, ValueError) as exc:
        raise _error(
            "invoice_withholding_selection_changed",
            "Start a new invoice bank transfer payment.",
        ) from exc
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or not invoice.withholding_tax_selected:
        raise _error(
            "invoice_withholding_selection_changed",
            "This transfer used withholding tax that is not selected on the invoice. If you already transferred money, do not transfer again; contact staff to review the existing receipt. Otherwise restart the invoice bank transfer payment.",
            invoice_id=str(invoice_id),
        )
    from app.services.invoice_withholding import (
        InvoiceWithholdingQuery,
        snapshot_invoice_withholding_for_transfer,
    )

    try:
        snapshot_invoice_withholding_for_transfer(
            db, InvoiceWithholdingQuery(invoice_id)
        )
    except DomainError as exc:
        raise _error(
            "invoice_withholding_selection_changed",
            "These withholding transfer instructions require staff review. If you already transferred money, do not transfer again; contact staff with the existing receipt.",
            invoice_id=str(invoice_id),
            reason=exc.code,
        ) from exc


def _invoice_withholding_tax_snapshot(
    db: Session,
    *,
    account_id: UUID,
    invoice: Invoice,
) -> dict[str, object] | None:
    if not invoice.withholding_tax_selected:
        return None
    from app.services.invoice_withholding import (
        InvoiceWithholdingQuery,
        snapshot_invoice_withholding_for_transfer,
    )

    snapshot = snapshot_invoice_withholding_for_transfer(
        db, InvoiceWithholdingQuery(invoice.id)
    )
    return {
        "schema_version": 1,
        "account_id": str(account_id),
        "policy_version": snapshot.policy_version,
        "source_invoice_id": str(snapshot.invoice_id),
        "currency": snapshot.currency,
        "vat_exclusive_amount": str(snapshot.vat_exclusive_amount),
        "vat_amount": str(snapshot.vat_amount),
        "gross_amount": str(snapshot.gross_amount),
        "withholding_tax_rate_percent": str(snapshot.wht_rate),
        "withholding_tax_amount": str(snapshot.wht_amount),
        "net_amount": str(snapshot.net_amount),
    }


def _configured_integer(db: Session, key: str) -> int:
    value = resolve_value(db, SettingDomain.billing, key)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _error(
            "configuration_invalid",
            "Direct-transfer policy configuration is invalid",
            setting=key,
        )
    try:
        normalized = int(value)
    except ValueError as exc:
        raise _error(
            "configuration_invalid",
            "Direct-transfer policy configuration is invalid",
            setting=key,
        ) from exc
    if normalized <= 0:
        raise _error(
            "configuration_invalid",
            "Direct-transfer policy configuration must be positive",
            setting=key,
        )
    return normalized


def _idempotency_key(context: CommandContext, account_id: UUID) -> str:
    source = context.idempotency_key or str(context.command_id)
    digest = hashlib.sha256(f"{account_id}:{source}".encode()).hexdigest()
    return f"direct-transfer-{digest}"


def _reference() -> str:
    return f"TRF-{uuid.uuid4().hex[:12].upper()}"


def create_direct_transfer_intent(
    db: Session,
    command: CreateDirectTransferIntentCommand,
    *,
    context: CommandContext,
) -> DirectTransferIntentResult:
    """Create or replay one direct-transfer intent in a verified root transaction."""

    return execute_owner_command(
        db,
        definition=_CREATE_COMMAND,
        context=context,
        operation=lambda: _create_direct_transfer_intent(
            db,
            command=command,
            context=context,
        ),
    )


def _create_direct_transfer_intent(
    db: Session,
    *,
    command: CreateDirectTransferIntentCommand,
    context: CommandContext,
) -> DirectTransferIntentResult:
    configuration = topup_intents.direct_transfer_configuration(db)
    if not configuration.enabled:
        raise _error(
            "unavailable",
            "Direct bank transfer is not configured",
            enabled_account_count=len(configuration.enabled_accounts),
        )

    created_by = command.created_by.strip()
    if not created_by:
        raise _error("created_by_required", "Direct-transfer actor is required")
    ttl_days = _configured_integer(db, _TTL_SETTING)
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
    key = _idempotency_key(context, command.account_id)

    if command.invoice_id is not None:
        lock_account(db, str(command.account_id))
        invoice = lock_for_update(db, Invoice, command.invoice_id)
        if invoice is None or invoice.account_id != command.account_id:
            raise _error(
                "invoice_not_found",
                "Invoice was not found for this account",
                invoice_id=str(command.invoice_id),
            )
        if invoice.status in {
            InvoiceStatus.draft,
            InvoiceStatus.paid,
            InvoiceStatus.void,
            InvoiceStatus.written_off,
        }:
            raise _error(
                "invoice_not_payable",
                "Invoice is no longer payable by direct transfer",
                invoice_id=str(invoice.id),
                status=invoice.status.value,
            )
        currency = str(invoice.currency or "").strip().upper()
        if currency != SUPPORTED_CURRENCY:
            raise _error(
                "currency_unsupported",
                f"Direct bank transfer supports {SUPPORTED_CURRENCY} only",
                currency=currency,
            )
        withholding_tax = _invoice_withholding_tax_snapshot(
            db,
            account_id=command.account_id,
            invoice=invoice,
        )
        amount = round_money(invoice.balance_due or invoice.total)
        if withholding_tax is not None:
            amount = round_money(to_decimal(str(withholding_tax["net_amount"])))
        if amount <= Decimal("0.00"):
            raise _error(
                "invoice_not_payable",
                "Invoice has no outstanding balance",
                invoice_id=str(invoice.id),
            )
        staged = topup_intents.stage_invoice_direct_transfer_intent(
            db,
            account_id=command.account_id,
            invoice_id=invoice.id,
            amount=amount,
            currency=currency,
            reference=_reference(),
            expires_at=expires_at,
            idempotency_key=key,
            created_by=created_by,
            metadata=(
                {"withholding_tax": withholding_tax}
                if withholding_tax is not None
                else None
            ),
            context=context,
        )
        intent = staged.intent
        validate_invoice_transfer_selection(db, intent=intent)
        preview_fingerprint = None
        replayed = staged.replayed
        replaced_intent_ids = staged.replaced_intent_ids
        payment_flow = "invoice_payment"
    else:
        try:
            amount = round_money(to_decimal(command.requested_amount))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise _error(
                "amount_invalid",
                "Direct-transfer amount is invalid",
            ) from exc
        expected_preview_fingerprint = str(
            command.expected_preview_fingerprint or ""
        ).strip()
        if len(expected_preview_fingerprint) != 64:
            raise _error(
                "preview_required",
                "Review the latest account-credit allocation preview before checkout",
            )
        minimum = _configured_integer(db, _MINIMUM_SETTING)
        maximum = _configured_integer(db, _MAXIMUM_SETTING)
        if maximum < minimum:
            raise _error(
                "configuration_invalid",
                "Direct-transfer maximum is below the minimum",
                minimum=minimum,
                maximum=maximum,
            )
        try:
            intent, preview, replayed = AccountCreditDeposits.stage_intent(
                db,
                account_id=command.account_id,
                amount=amount,
                currency=SUPPORTED_CURRENCY,
                minimum=minimum,
                maximum=maximum,
                reference=_reference(),
                provider_type=topup_intents.DIRECT_TRANSFER_PROVIDER,
                provider_id=None,
                capability_binding_id=None,
                expires_at=expires_at,
                idempotency_key=key,
                channel=topup_intents.TopupIntentChannel.customer_selfcare,
                created_by=created_by,
                expected_preview_fingerprint=expected_preview_fingerprint,
                metadata={
                    "payment_method": "bank_transfer",
                    "payment_flow": "account_credit_deposit",
                },
            )
        except DepositEligibilityError as exc:
            raise _error(
                "deposit_rejected",
                str(exc),
                deposit_code=exc.code,
            ) from exc
        except IntegrityError as exc:
            raise _error(
                "intent_conflict",
                "Direct-transfer intent is already being created",
            ) from exc
        if not replayed:
            topup_intents.stage_direct_transfer_intent_created_event(
                db,
                intent=intent,
                context=context,
            )
        preview_fingerprint = preview.fingerprint
        replaced_intent_ids = ()
        payment_flow = "account_credit_deposit"

    if intent.account_id is None or intent.expires_at is None:
        raise _error(
            "record_invalid",
            "Direct-transfer intent evidence is incomplete",
            intent_id=str(intent.id),
        )
    return DirectTransferIntentResult(
        intent_id=intent.id,
        account_id=intent.account_id,
        reference=intent.reference,
        provider_type=intent.provider_type,
        currency=intent.currency,
        requested_amount=round_money(intent.requested_amount),
        expires_at=intent.expires_at,
        payment_flow=payment_flow,
        invoice_id=command.invoice_id,
        preview_fingerprint=preview_fingerprint,
        replayed=replayed,
        replaced_intent_ids=replaced_intent_ids,
    )
