"""Assemble the canonical sales_referrals SOT domain from capability shards."""

from __future__ import annotations

from app.services.automation_contracts import (
    AutomationActionCapability,
    AutomationActionInput,
    AutomationCatalogItem,
    AutomationCatalogState,
    AutomationConditionField,
    AutomationDomainCapabilities,
    AutomationOperator,
    AutomationScriptTargetCapability,
    AutomationTriggerCapability,
    AutomationValueType,
)
from app.services.custom_field_contracts import (
    CustomFieldDomainCapabilities,
    CustomFieldTargetCapability,
)
from app.services.sot_registry.domains.sales_referrals.acquisition import (
    SERVICES as ACQUISITION_SERVICES,
)
from app.services.sot_registry.domains.sales_referrals.customer_audit import (
    SERVICES as CUSTOMER_AUDIT_SERVICES,
)
from app.services.sot_registry.domains.sales_referrals.customer_handoff import (
    SERVICES as CUSTOMER_HANDOFF_SERVICES,
)
from app.services.sot_registry.domains.sales_referrals.lifecycle import (
    SERVICES as LIFECYCLE_SERVICES,
)
from app.services.sot_registry.domains.sales_referrals.referrals import (
    SERVICES as REFERRALS_SERVICES,
)
from app.services.sot_registry.model import DomainSOT

_LEAD_STATUS_VALUES = (
    "new",
    "contacted",
    "qualified",
    "proposal",
    "negotiation",
    "won",
    "lost",
)
_QUOTE_STATUS_VALUES = ("draft", "sent", "accepted", "rejected", "expired")
_ORDER_STATUS_VALUES = ("draft", "confirmed", "paid", "fulfilled", "cancelled")
_PAYMENT_REVIEW_VALUES = ("pending", "approved", "rejected")


def _enum_field(
    key: str,
    label: str,
    values: tuple[str, ...],
) -> AutomationConditionField:
    return AutomationConditionField(
        key=key,
        label=label,
        value_type=AutomationValueType.enum,
        operators=(
            AutomationOperator.equals,
            AutomationOperator.not_equals,
            AutomationOperator.in_values,
            AutomationOperator.not_in_values,
        ),
        enum_values=values,
    )


def _text_field(key: str, label: str) -> AutomationConditionField:
    return AutomationConditionField(
        key=key,
        label=label,
        value_type=AutomationValueType.string,
        operators=(
            AutomationOperator.equals,
            AutomationOperator.not_equals,
            AutomationOperator.contains,
            AutomationOperator.is_empty,
            AutomationOperator.is_not_empty,
        ),
    )


def _uuid_field(key: str, label: str) -> AutomationConditionField:
    return AutomationConditionField(
        key=key,
        label=label,
        value_type=AutomationValueType.uuid,
        operators=(AutomationOperator.equals, AutomationOperator.not_equals),
    )


def _decimal_field(key: str, label: str) -> AutomationConditionField:
    return AutomationConditionField(
        key=key,
        label=label,
        value_type=AutomationValueType.decimal,
        operators=(
            AutomationOperator.equals,
            AutomationOperator.greater_than,
            AutomationOperator.greater_than_or_equal,
            AutomationOperator.less_than,
            AutomationOperator.less_than_or_equal,
        ),
    )


DOMAIN = DomainSOT(
    domain="sales_referrals",
    setting_domains=("workflow",),
    services=(
        *ACQUISITION_SERVICES,
        *CUSTOMER_HANDOFF_SERVICES,
        *LIFECYCLE_SERVICES,
        *CUSTOMER_AUDIT_SERVICES,
        *REFERRALS_SERVICES,
    ),
    entrypoints=(
        "app.api.me",
        "app.api.crm_referrals",
        "app.api.crm_webhooks",
        "app.api.crm_sales",
        "app.api.lead_capture_webhooks",
        "app.api.customer_experience",
        "app.web.customer.referrals",
        "app.tasks.referrals",
        "app.services.events.handlers.referral",
        "app.services.events.handlers.sales_lifecycle_projection",
        "app.services.web_sales",
        "app.services.web_referrals",
        "scripts.migration.audit_customer_lifecycle",
        "scripts.migration.reconcile_sales_lifecycle",
    ),
    rule="A prospect enters as a Party-bound Lead with captured origin, not a "
    "fake Subscriber. Staff author Lead-backed Quotes without conversion; "
    "Accepted Quote is the sole atomic account, SalesOrder, Project, Task, "
    "and configured WorkOrder conversion event. SalesOrder structurally "
    "owns one Project and installation scope; verified "
    "implementation requests service-order release after its evidence "
    "commits; successful provisioning activates service and its committed "
    "completion requests the CX handoff. Routes, webhooks, jobs, and "
    "handlers request outcomes from these owners and translate domain "
    "errors at the boundary. CRM and dotmac_mkt have no customer-lifecycle "
    "or attribution authority.",
    automation=AutomationDomainCapabilities(
        target_types=("sales.lead", "sales.quote", "sales.sales_order"),
        triggers=(
            AutomationTriggerCapability(
                key="sales.lead.created",
                label="Lead created",
                event_type="lead.created",
                event_schema_version=1,
                entity_type="sales.lead",
                tenant_id_field="tenant_id",
                entity_id_field="lead_id",
                fields=(
                    _enum_field("status", "Lead status", _LEAD_STATUS_VALUES),
                    _text_field("lead_source", "Lead source"),
                    _uuid_field("pipeline_id", "Pipeline"),
                ),
                author_permission="crm:lead:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="sales.lead.updated",
                label="Lead updated",
                event_type="lead.updated",
                event_schema_version=1,
                entity_type="sales.lead",
                tenant_id_field="tenant_id",
                entity_id_field="lead_id",
                fields=(
                    _enum_field("status", "Lead status", _LEAD_STATUS_VALUES),
                    _uuid_field("pipeline_id", "Pipeline"),
                ),
                author_permission="crm:lead:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="sales.lead.account_converted",
                label="Lead converted to customer account",
                event_type="lead.account_converted",
                event_schema_version=1,
                entity_type="sales.lead",
                tenant_id_field="tenant_id",
                entity_id_field="lead_id",
                fields=(_text_field("outcome", "Conversion outcome"),),
                author_permission="crm:lead:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="sales.quote.created",
                label="Quote created",
                event_type="quote.created",
                event_schema_version=1,
                entity_type="sales.quote",
                tenant_id_field="tenant_id",
                entity_id_field="quote_id",
                fields=(
                    _enum_field("status", "Quote status", _QUOTE_STATUS_VALUES),
                    _text_field("currency", "Currency"),
                    _decimal_field("total", "Quote total"),
                    _uuid_field("lead_id", "Lead"),
                    _uuid_field("subscriber_id", "Customer account"),
                ),
                author_permission="crm:quote:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="sales.quote.accepted",
                label="Quote accepted",
                event_type="quote.accepted",
                event_schema_version=1,
                entity_type="sales.quote",
                tenant_id_field="tenant_id",
                entity_id_field="quote_id",
                fields=(
                    _decimal_field("total", "Quote total"),
                    _text_field("currency", "Currency"),
                    _uuid_field("sales_order_id", "Sales order"),
                    _uuid_field("project_id", "Project"),
                    _uuid_field("subscriber_id", "Customer account"),
                ),
                author_permission="crm:quote:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="sales.quote.payment_review_requested",
                label="Quote payment review requested",
                event_type="quote.payment_review_requested",
                event_schema_version=1,
                entity_type="sales.quote",
                tenant_id_field="tenant_id",
                entity_id_field="quote_id",
                fields=(
                    _enum_field(
                        "payment_review_status",
                        "Payment review status",
                        _PAYMENT_REVIEW_VALUES,
                    ),
                    _uuid_field("subscriber_id", "Customer account"),
                ),
                author_permission="crm:quote:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="sales.quote.payment_approved",
                label="Quote payment approved",
                event_type="quote.payment_approved",
                event_schema_version=1,
                entity_type="sales.quote",
                tenant_id_field="tenant_id",
                entity_id_field="quote_id",
                fields=(
                    _uuid_field("subscriber_id", "Customer account"),
                    _uuid_field("reviewer_system_user_id", "Reviewer"),
                    _text_field("reason", "Review reason"),
                ),
                author_permission="crm:quote:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="sales.quote.payment_rejected",
                label="Quote payment rejected",
                event_type="quote.payment_rejected",
                event_schema_version=1,
                entity_type="sales.quote",
                tenant_id_field="tenant_id",
                entity_id_field="quote_id",
                fields=(
                    _uuid_field("subscriber_id", "Customer account"),
                    _uuid_field("reviewer_system_user_id", "Reviewer"),
                    _text_field("reason", "Review reason"),
                ),
                author_permission="crm:quote:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="sales.sales_order.funding_satisfied",
                label="Sales order funding satisfied",
                event_type="sales_order.funding_satisfied",
                event_schema_version=1,
                entity_type="sales.sales_order",
                tenant_id_field="tenant_id",
                entity_id_field="sales_order_id",
                fields=(
                    _decimal_field("total", "Order total"),
                    _decimal_field("amount_paid", "Amount paid"),
                    _text_field("currency", "Currency"),
                    _enum_field(
                        "from_payment_status",
                        "Previous payment status",
                        ("pending", "partial", "paid", "waived"),
                    ),
                    _enum_field(
                        "to_payment_status",
                        "New payment status",
                        ("pending", "partial", "paid", "waived"),
                    ),
                ),
                author_permission="crm:sales_order:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="sales.sales_order.paid",
                label="Sales order paid",
                event_type="sales_order.paid",
                event_schema_version=1,
                entity_type="sales.sales_order",
                tenant_id_field="tenant_id",
                entity_id_field="sales_order_id",
                fields=(
                    _decimal_field("total", "Order total"),
                    _decimal_field("amount_paid", "Amount paid"),
                    _text_field("currency", "Currency"),
                ),
                author_permission="crm:sales_order:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="sales.sales_order.fulfilled",
                label="Sales order fulfilled",
                event_type="sales_order.fulfilled",
                event_schema_version=1,
                entity_type="sales.sales_order",
                tenant_id_field="tenant_id",
                entity_id_field="sales_order_id",
                fields=(
                    _enum_field(
                        "from_status", "Previous order status", _ORDER_STATUS_VALUES
                    ),
                    _enum_field("to_status", "New order status", _ORDER_STATUS_VALUES),
                    _uuid_field("cx_handoff_id", "Customer-experience handoff"),
                ),
                author_permission="crm:sales_order:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="sales.lead.scheduled",
                label="Lead scheduled evaluation",
                event_type="sales.lead.scheduled",
                event_schema_version=1,
                entity_type="sales.lead",
                tenant_id_field="tenant_id",
                entity_id_field="lead_id",
                fields=(
                    _enum_field("status", "Lead status", _LEAD_STATUS_VALUES),
                    _uuid_field("pipeline_id", "Pipeline"),
                ),
                author_permission="crm:lead:read",
                runtime_enabled=True,
                scheduled=True,
                schedule_adapter_key="sales.lead",
            ),
            AutomationTriggerCapability(
                key="sales.quote.scheduled",
                label="Quote scheduled evaluation",
                event_type="sales.quote.scheduled",
                event_schema_version=1,
                entity_type="sales.quote",
                tenant_id_field="tenant_id",
                entity_id_field="quote_id",
                fields=(
                    _enum_field("status", "Quote status", _QUOTE_STATUS_VALUES),
                    _enum_field(
                        "payment_review_status",
                        "Payment review status",
                        _PAYMENT_REVIEW_VALUES,
                    ),
                ),
                author_permission="crm:quote:read",
                runtime_enabled=True,
                scheduled=True,
                schedule_adapter_key="sales.quote",
            ),
            AutomationTriggerCapability(
                key="sales.sales_order.scheduled",
                label="Sales order scheduled evaluation",
                event_type="sales.sales_order.scheduled",
                event_schema_version=1,
                entity_type="sales.sales_order",
                tenant_id_field="tenant_id",
                entity_id_field="sales_order_id",
                fields=(
                    _enum_field("status", "Sales order status", _ORDER_STATUS_VALUES),
                    _enum_field(
                        "payment_status",
                        "Payment status",
                        ("pending", "partial", "paid", "waived"),
                    ),
                ),
                author_permission="crm:sales_order:read",
                runtime_enabled=True,
                scheduled=True,
                schedule_adapter_key="sales.sales_order",
            ),
        ),
        actions=(
            AutomationActionCapability(
                key="sales.lead.set_status",
                label="Set lead status",
                entity_type="sales.lead",
                command_owner="sales.lead_authoring",
                command_name="update_status",
                input_schema_version=1,
                inputs=(
                    AutomationActionInput(
                        key="status",
                        label="Status",
                        value_type=AutomationValueType.enum,
                        enum_values=_LEAD_STATUS_VALUES,
                    ),
                ),
                author_permission="crm:lead:write",
                runtime_scope="one lead",
                idempotency="tenant/lead/status/version",
                runtime_enabled=True,
            ),
            AutomationActionCapability(
                key="sales.quote.set_status",
                label="Set quote status",
                entity_type="sales.quote",
                command_owner="sales.quote_authoring",
                command_name="update_status",
                input_schema_version=1,
                inputs=(
                    AutomationActionInput(
                        key="status",
                        label="Status",
                        value_type=AutomationValueType.enum,
                        enum_values=_QUOTE_STATUS_VALUES,
                    ),
                ),
                author_permission="crm:quote:write",
                runtime_scope="one quote",
                idempotency="tenant/quote/status/version",
                runtime_enabled=True,
            ),
            AutomationActionCapability(
                key="sales.sales_order.set_status",
                label="Set sales-order status",
                entity_type="sales.sales_order",
                command_owner="sales.orders",
                command_name="transition_status",
                input_schema_version=1,
                inputs=(
                    AutomationActionInput(
                        key="status",
                        label="Status",
                        value_type=AutomationValueType.enum,
                        enum_values=_ORDER_STATUS_VALUES,
                    ),
                ),
                author_permission="crm:sales_order:write",
                runtime_scope="one sales order",
                idempotency="tenant/sales-order/status/version",
                runtime_enabled=True,
            ),
        ),
        script_targets=(
            AutomationScriptTargetCapability(
                key="sales.lead",
                label="Lead",
                entity_type="sales.lead",
                client_events=("form.load", "field.change", "form.validate"),
                server_events=(
                    "lead.created",
                    "lead.updated",
                    "lead.account_converted",
                ),
                read_permission="crm:lead:read",
                write_permission="crm:lead:write",
                tenant_id_field="tenant_id",
                entity_id_field="lead_id",
            ),
            AutomationScriptTargetCapability(
                key="sales.quote",
                label="Quote",
                entity_type="sales.quote",
                client_events=("form.load", "field.change", "form.validate"),
                server_events=(
                    "quote.created",
                    "quote.accepted",
                    "quote.payment_review_requested",
                    "quote.payment_approved",
                    "quote.payment_rejected",
                ),
                read_permission="crm:quote:read",
                write_permission="crm:quote:write",
                tenant_id_field="tenant_id",
                entity_id_field="quote_id",
            ),
            AutomationScriptTargetCapability(
                key="sales.sales_order",
                label="Sales order",
                entity_type="sales.sales_order",
                client_events=("form.load", "field.change", "form.validate"),
                server_events=(
                    "sales_order.paid",
                    "sales_order.funding_satisfied",
                    "sales_order.fulfilled",
                ),
                read_permission="crm:sales_order:read",
                write_permission="crm:sales_order:write",
                tenant_id_field="tenant_id",
                entity_id_field="sales_order_id",
            ),
        ),
        catalog_items=(
            AutomationCatalogItem(
                key="sales.funding_to_implementation_handoff",
                label="Funding-to-implementation handoff",
                group="Sales",
                state=AutomationCatalogState.unavailable,
                explanation="Funding and sales-order transitions remain in their current owners; no Center trigger/action contract is registered.",
            ),
            AutomationCatalogItem(
                key="sales.verified_implementation_release",
                label="Verified implementation release",
                group="Sales",
                state=AutomationCatalogState.unavailable,
                explanation="Release requires verified project evidence and remains governed by the sales fulfilment owner.",
            ),
            AutomationCatalogItem(
                key="sales.service_order_release",
                label="Service-order release",
                group="Sales",
                state=AutomationCatalogState.unavailable,
                explanation="The release step changes provisioning state and remains in the existing sales-to-provisioning workflow.",
            ),
            AutomationCatalogItem(
                key="sales.customer_experience_handoff",
                label="Customer-experience handoff",
                group="Sales",
                state=AutomationCatalogState.unavailable,
                explanation="Customer acceptance handoff follows the service-order completion owner; Center support is not registered.",
            ),
            AutomationCatalogItem(
                key="sales.customer_acceptance_completion",
                label="Customer acceptance completion",
                group="Sales",
                state=AutomationCatalogState.unavailable,
                explanation="Fulfilment completion requires the canonical handoff evidence and cannot yet be configured as a Center action.",
            ),
            AutomationCatalogItem(
                key="sales.overdue_acceptance_flag",
                label="Overdue acceptance flag",
                group="Sales",
                state=AutomationCatalogState.unavailable,
                explanation="The durable timer remains the owner of overdue acceptance; Center timer triggers are not available.",
            ),
            AutomationCatalogItem(
                key="sales.referral_qualification",
                label="Referral qualification",
                group="Sales",
                state=AutomationCatalogState.unavailable,
                explanation="Referral eligibility and rewards remain in the referral owner; no Center trigger or reward action is registered.",
            ),
            AutomationCatalogItem(
                key="sales.retired_crm_quote_referral_refresh",
                label="Legacy quote and referral CRM refresh",
                group="Sales",
                state=AutomationCatalogState.retired,
                explanation="These legacy mirror tasks no longer contact CRM; developers must restore and review the integration before use.",
            ),
        ),
    ),
    custom_fields=CustomFieldDomainCapabilities(
        targets=(
            CustomFieldTargetCapability(
                key="lead",
                label="Leads",
                entity_id_type="uuid",
                read_permission="crm:lead:read",
                write_permission="crm:lead:write",
                create_permission="crm:lead:write",
                detail_path_template="/admin/sales/leads/{target_id}",
                maximum_active_fields=50,
            ),
            CustomFieldTargetCapability(
                key="quote",
                label="Quotes",
                entity_id_type="uuid",
                read_permission="crm:quote:read",
                write_permission="crm:quote:write",
                create_permission="crm:quote:write",
                detail_path_template="/admin/sales/quotes/{target_id}",
                maximum_active_fields=50,
            ),
            CustomFieldTargetCapability(
                key="sales_order",
                label="Sales orders",
                entity_id_type="uuid",
                read_permission="crm:sales_order:read",
                write_permission="crm:sales_order:write",
                create_permission="crm:sales_order:write",
                detail_path_template="/admin/sales/sales-order/{target_id}",
                maximum_active_fields=50,
            ),
        ),
    ),
)
