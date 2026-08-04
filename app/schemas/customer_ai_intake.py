"""Closed transport contract for customer-facing Inbox AI intake."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CustomerIntent(StrEnum):
    technical_support = "technical_support"
    billing = "billing"
    payment_confirmation = "payment_confirmation"
    subscription = "subscription"
    account_access = "account_access"
    new_connection = "new_connection"
    general_complaint = "general_complaint"
    general_enquiry = "general_enquiry"
    unknown = "unknown"


class CustomerCategory(StrEnum):
    no_internet = "no_internet"
    slow_internet = "slow_internet"
    intermittent_connection = "intermittent_connection"
    router_issue = "router_issue"
    billing_issue = "billing_issue"
    payment_not_reflected = "payment_not_reflected"
    subscription_renewal = "subscription_renewal"
    plan_change = "plan_change"
    account_login_issue = "account_login_issue"
    coverage_request = "coverage_request"
    new_connection_request = "new_connection_request"
    general_complaint = "general_complaint"
    general_enquiry = "general_enquiry"
    unknown = "unknown"


class CustomerDepartment(StrEnum):
    technical_support = "technical_support"
    helpdesk = "helpdesk"
    sales = "sales"
    fallback = "fallback"


class CustomerPartyType(StrEnum):
    individual = "individual"
    organization = "organization"
    unknown = "unknown"


class FollowUpQuestionKey(StrEnum):
    request_type = "request_type"
    technical_problem = "technical_problem"
    billing_problem = "billing_problem"
    sales_location = "sales_location"
    customer_type = "customer_type"


class CustomerAiClassification(BaseModel):
    """Strict provider output; unknown values and extra keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    intent: CustomerIntent
    category: CustomerCategory
    confidence: float = Field(ge=0, le=1)
    department: CustomerDepartment
    requires_follow_up: bool
    follow_up_question: FollowUpQuestionKey | None = None
    summary: str = Field(min_length=1, max_length=500)
    party_type: CustomerPartyType = CustomerPartyType.unknown
    party_type_confidence: float = Field(default=0, ge=0, le=1)
