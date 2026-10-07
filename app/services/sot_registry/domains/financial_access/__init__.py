"""Assemble the canonical financial_access SOT domain from capability shards."""

from __future__ import annotations

from app.services.automation_contracts import (
    AutomationCatalogItem,
    AutomationCatalogState,
    AutomationDomainCapabilities,
)
from app.services.sot_registry.domains.financial_access.billing import (
    SERVICES as BILLING_SERVICES,
)
from app.services.sot_registry.domains.financial_access.collection_operations import (
    SERVICES as COLLECTION_OPERATIONS_SERVICES,
)
from app.services.sot_registry.domains.financial_access.collections import (
    SERVICES as COLLECTIONS_SERVICES,
)
from app.services.sot_registry.domains.financial_access.customer_subledger import (
    SERVICES as CUSTOMER_SUBLEDGER_SERVICES,
)
from app.services.sot_registry.domains.financial_access.durable_timers import (
    SERVICES as DURABLE_TIMERS_SERVICES,
)
from app.services.sot_registry.domains.financial_access.erp_billing import (
    SERVICES as ERP_BILLING_SERVICES,
)
from app.services.sot_registry.domains.financial_access.financial_core import (
    SERVICES as FINANCIAL_CORE_SERVICES,
)
from app.services.sot_registry.domains.financial_access.invoicing_tax import (
    SERVICES as INVOICING_TAX_SERVICES,
)
from app.services.sot_registry.domains.financial_access.payment_intents import (
    SERVICES as PAYMENT_INTENTS_SERVICES,
)
from app.services.sot_registry.domains.financial_access.prepaid import (
    SERVICES as PREPAID_SERVICES,
)
from app.services.sot_registry.domains.financial_access.provider_payments import (
    SERVICES as PROVIDER_PAYMENTS_SERVICES,
)
from app.services.sot_registry.domains.financial_access.sales_funding import (
    SERVICES as SALES_FUNDING_SERVICES,
)
from app.services.sot_registry.domains.financial_access.test_connections import (
    SERVICES as TEST_CONNECTION_SERVICES,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="financial_access",
    setting_domains=(
        "billing",
        "collections",
    ),
    services=(
        *BILLING_SERVICES,
        *CUSTOMER_SUBLEDGER_SERVICES,
        *DURABLE_TIMERS_SERVICES,
        *COLLECTIONS_SERVICES,
        *SALES_FUNDING_SERVICES,
        *ERP_BILLING_SERVICES,
        *FINANCIAL_CORE_SERVICES,
        *PAYMENT_INTENTS_SERVICES,
        *INVOICING_TAX_SERVICES,
        *PREPAID_SERVICES,
        *COLLECTION_OPERATIONS_SERVICES,
        *PROVIDER_PAYMENTS_SERVICES,
        *TEST_CONNECTION_SERVICES,
    ),
    entrypoints=(
        "app.services.billing_automation",
        "app.services.collections.*",
        "app.web.admin.billing_*",
        "app.web.admin.reports",
        "app.api.billing",
        "app.services.payment_proofs",
        "app.services.web_reports_extended",
        "app.api.me",
        "mobile",
        "app.tasks.billing",
        "app.tasks.collections",
        "app.tasks.enforcement",
        "app.tasks.payment_reconciliation",
    ),
    rule="No caller infers access or balances from draft invoices, imported "
    "legacy fields, or ad hoc sums when ledger/access resolvers exist. "
    "Tax reports consume the tax-accounting projection, never label "
    "issued tax as collected cash, and never add different currencies. "
    "Tax account mappings and double-entry consequences are written only "
    "by Dotmac ERP from Sub's bounded source-fact feeds.",
    automation=AutomationDomainCapabilities(
        catalog_items=(
            AutomationCatalogItem(
                key="billing.recurring_invoice_cycle",
                label="Recurring invoice cycle",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="Invoice generation follows the existing billing schedule; no Automation Center trigger or safe invoice-creation action is registered.",
            ),
            AutomationCatalogItem(
                key="billing.invoice_reminders",
                label="Invoice reminders",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="Reminder timing and customer notification safeguards remain in the billing owners; configurable Center actions are not registered.",
            ),
            AutomationCatalogItem(
                key="billing.mark_invoices_overdue",
                label="Mark invoices overdue",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="The overdue schedule continues under billing ownership; the Center has no approved overdue-state action.",
            ),
            AutomationCatalogItem(
                key="billing.automatic_payment_collection",
                label="Automatic payment collection",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="Payment collection stays under mandate, invoice, and provider safeguards; no Center action is registered.",
            ),
            AutomationCatalogItem(
                key="billing.payment_webhook_settlement",
                label="Payment webhook settlement",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="Verified payment webhooks use the existing settlement flow; external payment settlement is not an editable rule action.",
            ),
            AutomationCatalogItem(
                key="billing.stranded_topup_reconciliation",
                label="Stranded top-up reconciliation",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="Gateway reconciliation remains a protected scheduled process and has no Center trigger or action.",
            ),
            AutomationCatalogItem(
                key="billing.prepaid_service_renewal",
                label="Prepaid service renewal after funding",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="Renewal and billing-anchor changes remain in the prepaid owner; a safe Center contract is not registered.",
            ),
            AutomationCatalogItem(
                key="billing.funding_reversal_correction",
                label="Funding reversal correction",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="Reversal handling changes financial coverage through its existing owner and is not an approved configurable action.",
            ),
            AutomationCatalogItem(
                key="billing.prepaid_balance_enforcement",
                label="Prepaid balance enforcement",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="Balance enforcement can restrict service and remains governed by the existing access policy; no Center action is registered.",
            ),
            AutomationCatalogItem(
                key="billing.dunning",
                label="Billing enforcement (dunning)",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="Dunning can restrict service and continues through existing billing and access safeguards; it is not yet a Center rule.",
            ),
            AutomationCatalogItem(
                key="billing.restore_service_after_payment",
                label="Restore service after payment",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="Restoration checks payment, debt, and other access blocks through linked owners; those steps are not yet exposed as safe Center actions.",
            ),
            AutomationCatalogItem(
                key="billing.payment_arrangements",
                label="Payment arrangements",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="Arrangement payment and overdue decisions remain in the billing owner; no Center trigger or action is registered.",
            ),
            AutomationCatalogItem(
                key="billing.bundle_consistency_repair",
                label="Bundle consistency repair",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="The existing reconciliation keeps bundle states consistent; it is not a configurable customer rule.",
            ),
            AutomationCatalogItem(
                key="billing.billing_approval_repair",
                label="Billing approval repair",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="This repair uses account lifecycle rules and remains a protected scheduled process.",
            ),
            AutomationCatalogItem(
                key="billing.health_snapshot",
                label="Billing health snapshot",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="Health snapshots are operational monitoring work, not a configurable business-rule action.",
            ),
            AutomationCatalogItem(
                key="billing.safety_audits",
                label="Billing safety audits",
                group="Billing",
                state=AutomationCatalogState.unavailable,
                explanation="These read-only audits remain in their existing schedule; no Center schedule trigger is available.",
            ),
            AutomationCatalogItem(
                key="subscriptions.expiration",
                label="Subscription expiration",
                group="Subscriptions",
                state=AutomationCatalogState.unavailable,
                explanation="The existing expiration process uses subscription lifecycle safeguards; a Center trigger and action are not registered.",
            ),
            AutomationCatalogItem(
                key="subscriptions.expiry_reminders",
                label="Subscription expiry reminders",
                group="Subscriptions",
                state=AutomationCatalogState.unavailable,
                explanation="Reminder timing remains under the subscription and notification owners; configurable Center support is not registered.",
            ),
            AutomationCatalogItem(
                key="subscriptions.scheduled_plan_changes",
                label="Scheduled plan changes",
                group="Subscriptions",
                state=AutomationCatalogState.unavailable,
                explanation="Approved plan changes execute through the subscription lifecycle; no Center schedule or action contract is registered.",
            ),
            AutomationCatalogItem(
                key="subscriptions.scheduled_status_changes",
                label="Scheduled subscription status changes",
                group="Subscriptions",
                state=AutomationCatalogState.unavailable,
                explanation="Status transitions remain governed by subscription lifecycle commands; they cannot yet be assembled in Center rules.",
            ),
            AutomationCatalogItem(
                key="subscriptions.vacation_hold_resumption",
                label="Vacation-hold resumption",
                group="Subscriptions",
                state=AutomationCatalogState.unavailable,
                explanation="Hold expiry and resumption remain in the existing subscription lifecycle flow.",
            ),
            AutomationCatalogItem(
                key="subscriptions.paid_service_change_completion",
                label="Paid service-change completion",
                group="Subscriptions",
                state=AutomationCatalogState.unavailable,
                explanation="Payment and service-order evidence are checked by existing owners; no Center trigger/action is registered.",
            ),
            AutomationCatalogItem(
                key="reports.invoice_pdf_generation",
                label="Invoice PDF generation",
                group="Reports and exports",
                state=AutomationCatalogState.unavailable,
                explanation="PDF generation remains a permission-checked invoice export job; the Center has no approved document-generation action.",
            ),
            AutomationCatalogItem(
                key="financial.mrr_snapshot",
                label="MRR snapshot",
                group="Reports and exports",
                state=AutomationCatalogState.unavailable,
                explanation="The monthly recurring revenue snapshot remains an existing reporting job and has no Center schedule trigger.",
            ),
        ),
    ),
)
