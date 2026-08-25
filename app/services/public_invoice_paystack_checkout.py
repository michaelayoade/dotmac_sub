"""Public, invoice-scoped Paystack checkout hand-off.

The PDF contains only this application URL.  Opening it rechecks the current
invoice and creates the short-lived provider checkout immediately before the
browser is redirected, so cached documents never retain a stale provider URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Subscriber
from app.services import gateway_topup_intents, payment_routing
from app.services.integrations import payment_capability
from app.services.owner_commands import CommandContext
from app.services.payment_gateway_adapter import payment_gateway_adapter


class PublicInvoiceCheckoutError(ValueError):
    """A safe reason why an invoice cannot start a public checkout."""


@dataclass(frozen=True, slots=True)
class StartPublicInvoicePaystackCheckoutCommand:
    invoice_id: UUID
    return_url: str


@dataclass(frozen=True, slots=True)
class PublicInvoicePaystackCheckout:
    authorization_url: str
    reference: str


def _invoice_email(db: Session, invoice: Invoice) -> str:
    account = db.get(Subscriber, invoice.account_id)
    email = str(getattr(account, "email", "") or "").strip()
    if "@" not in email:
        raise PublicInvoiceCheckoutError(
            "An email address is required before this invoice can be paid online."
        )
    return email


def start_public_invoice_paystack_checkout(
    db: Session,
    command: StartPublicInvoicePaystackCheckoutCommand,
) -> PublicInvoicePaystackCheckout:
    """Create one current Paystack checkout for a public invoice-payment link.

    The durable payment intent remains owned by
    ``financial.gateway_topup_intent_commands``; this hand-off owns no invoice
    or payment state and the provider webhook/reconciler records settlement.
    """
    if not command.return_url.startswith("https://"):
        raise PublicInvoiceCheckoutError("A secure payment return URL is unavailable.")
    invoice = db.get(Invoice, command.invoice_id)
    if (
        invoice is None
        or invoice.is_proforma
        or invoice.status
        in {
            InvoiceStatus.draft,
            InvoiceStatus.paid,
            InvoiceStatus.void,
            InvoiceStatus.written_off,
        }
    ):
        raise PublicInvoiceCheckoutError("This invoice is no longer payable.")
    amount = Decimal(str(invoice.balance_due or invoice.total or "0"))
    if amount <= Decimal("0.00"):
        raise PublicInvoiceCheckoutError("This invoice no longer has a balance due.")

    email = _invoice_email(db, invoice)
    try:
        route = payment_routing.select_checkout_provider(db, "paystack")
        context = payment_gateway_adapter.build_context(
            db,
            provider_type="paystack",
            capability_binding_id=route.capability_binding_id,
            invoice_number=invoice.invoice_number,
        )
    except ValueError as exc:
        raise PublicInvoiceCheckoutError(
            "Paystack is unavailable for this invoice."
        ) from exc

    intent = gateway_topup_intents.create_customer_gateway_topup_intent(
        db,
        gateway_topup_intents.CreateCustomerGatewayTopupIntentCommand(
            flow=gateway_topup_intents.CustomerGatewayTopupFlow.invoice_payment,
            account_id=invoice.account_id,
            invoice_id=invoice.id,
            reference=context.reference,
            provider_type="paystack",
            provider_id=route.provider_id,
            capability_binding_id=route.capability_binding_id,
            created_by=f"invoice-pdf:{invoice.id}",
        ),
        context=CommandContext.system(
            actor=f"invoice-pdf:{invoice.id}",
            scope=gateway_topup_intents.CREATE_CUSTOMER_SCOPE,
            reason="Public invoice PDF Paystack checkout",
        ),
    )
    separator = "&" if "?" in command.return_url else "?"
    checkout = payment_capability.initialize_transaction(
        db,
        provider_type="paystack",
        email=email,
        amount=intent.requested_amount,
        reference=intent.reference,
        redirect_url=f"{command.return_url}{separator}reference={intent.reference}",
        metadata={
            "payment_flow": "invoice_payment",
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number or "",
            "account_id": str(invoice.account_id),
            "provider_id": str(route.provider_id),
        },
        currency=intent.currency,
        checkout_binding_id=route.capability_binding_id,
    )
    authorization_url = str(checkout.get("authorization_url") or "").strip()
    if not authorization_url.startswith("https://"):
        raise PublicInvoiceCheckoutError("Paystack could not start a secure checkout.")
    return PublicInvoicePaystackCheckout(
        authorization_url=authorization_url,
        reference=intent.reference,
    )
