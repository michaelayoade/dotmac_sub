"""Single source of truth for customer billing/access decisions.

This module is the public boundary for decisions that affect whether a customer
is billable, counted as active service, or allowed through RADIUS. It delegates
to the existing customer-service-state implementation for now, but gives new
callers one explicit API instead of importing mixed helper names from multiple
modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from app.models.catalog import AccessState
from app.services.customer_service_state import (
    CustomerBillingAccessState,
    active_customer_subscription_filters,
    postpaid_invoice_eligible_filters,
    prepaid_enforcement_eligible_filters,
    resolve_customer_billing_access_state,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.subscriber import Subscriber


@dataclass(frozen=True)
class CustomerAccessDecision:
    """Named access decision returned by the shared resolver."""

    state: CustomerBillingAccessState

    @property
    def subscription_id(self):
        return self.state.subscription_id

    @property
    def subscriber_id(self):
        return self.state.subscriber_id

    @property
    def is_active_customer_service(self) -> bool:
        return self.state.active_customer_service

    @property
    def is_billable_account(self) -> bool:
        return self.state.billable_account

    @property
    def is_postpaid_invoice_eligible(self) -> bool:
        return self.state.postpaid_invoice_eligible

    @property
    def is_prepaid_enforcement_eligible(self) -> bool:
        return self.state.prepaid_enforcement_eligible

    @property
    def counts_for_customer_impact(self) -> bool:
        return self.state.counts_for_customer_impact

    @property
    def radius_access_state(self) -> AccessState | None:
        return self.state.radius_access_state

    @property
    def radius_allowed(self) -> bool:
        return self.state.radius_allowed

    @property
    def radius_blocked(self) -> bool:
        return self.state.radius_blocked

    @property
    def radius_mode(self) -> str:
        return self.state.radius_mode

    @property
    def access_block_reason(self) -> str | None:
        return self.state.access_block_reason

    @property
    def billing_block_reason(self) -> str | None:
        return self.state.billing_block_reason


@dataclass(frozen=True)
class PrepaidFundingDecision:
    """The one funding decision used to suspend and restore prepaid access."""

    account_id: str
    available_balance: Decimal
    required_balance: Decimal

    @property
    def funded(self) -> bool:
        return self.available_balance >= self.required_balance


def resolve_prepaid_available_balance(db: Session, account_id: object) -> Decimal:
    """Resolve the customer financial position used by prepaid access policy.

    Multi-currency accounts fail closed by using their least-funded currency.
    This preserves the existing enforcement semantics while moving the decision
    behind the declared ``financial.access_resolution`` boundary.
    """
    from app.services.customer_financial_position import prepaid_available_balance

    return prepaid_available_balance(db, account_id)


def resolve_prepaid_funding(
    db: Session,
    account: Subscriber,
    *,
    now: datetime | None = None,
) -> PrepaidFundingDecision:
    """Compare the access balance with the canonical prepaid threshold.

    Callers must consume ``funded`` rather than comparing against a locally
    chosen zero/minimum/invoice value. Suspension and restoration therefore
    use the same quantity and cannot oscillate between incompatible gates.
    """
    from app.services.prepaid_threshold import resolve_prepaid_threshold

    return PrepaidFundingDecision(
        account_id=str(account.id),
        available_balance=resolve_prepaid_available_balance(db, account.id),
        required_balance=resolve_prepaid_threshold(db, account, now=now),
    )


def resolve_customer_access(
    subscription,
    *,
    subscriber=None,
    captive_redirect_enabled: bool | None = None,
) -> CustomerAccessDecision:
    """Resolve one subscription's customer-facing access/billing decision."""
    return CustomerAccessDecision(
        resolve_customer_billing_access_state(
            subscription,
            subscriber=subscriber,
            captive_redirect_enabled=captive_redirect_enabled,
        )
    )


def active_customer_service_filters(subscription_model, subscriber_model) -> tuple:
    """SQL predicates for subscriptions counted as active customer service."""
    return active_customer_subscription_filters(subscription_model, subscriber_model)


def postpaid_billing_filters(subscription_model, subscriber_model) -> tuple:
    """SQL predicates for the postpaid invoice-cycle cohort."""
    return postpaid_invoice_eligible_filters(subscription_model, subscriber_model)


def prepaid_enforcement_filters(subscription_model, subscriber_model) -> tuple:
    """SQL predicates for prepaid balance enforcement/exposure cohorts."""
    return prepaid_enforcement_eligible_filters(subscription_model, subscriber_model)
