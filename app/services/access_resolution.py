"""Single source of truth for customer billing/access decisions.

This module is the public boundary for decisions that affect whether a customer
is billable, counted as active service, or allowed through RADIUS. It delegates
to the existing customer-service-state implementation for now, but gives new
callers one explicit API instead of importing mixed helper names from multiple
modules.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.catalog import AccessState
from app.services.customer_service_state import (
    CustomerBillingAccessState,
    active_customer_subscription_filters,
    postpaid_invoice_eligible_filters,
    prepaid_enforcement_eligible_filters,
    resolve_customer_billing_access_state,
)


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
