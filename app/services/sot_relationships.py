"""System-wide single-source-of-truth relationship registry.

This registry names the service boundaries that should own domain decisions.
It is intentionally declarative: routes, APIs, Celery tasks, and event handlers
can use it as an architectural map while each domain is migrated incrementally.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SOTService:
    name: str
    module: str
    owns: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    notes: str | None = None


@dataclass(frozen=True)
class DomainSOT:
    domain: str
    services: tuple[SOTService, ...]
    entrypoints: tuple[str, ...]
    rule: str


DOMAIN_SOT_RELATIONSHIPS: tuple[DomainSOT, ...] = (
    DomainSOT(
        domain="party_identity",
        services=(
            SOTService(
                name="party.registry",
                module="app.services.party",
                owns=(
                    "native person and organization party identity",
                    "party data classification and quarantine",
                    "party merge policy and canonical redirect",
                    "external identity-reference provenance",
                    "concurrent party role lifecycle",
                    "reseller versus partner role contract",
                    "partner agreement type vocabulary",
                    "directional person and organization relationships",
                    "relationship type and effective-date contract",
                    "person-to-organization membership lifecycle",
                    "bounded organization membership access scope",
                    "canonical party contact-point lifecycle",
                    "contact-point verification and consent evidence",
                    "provider-scoped immutable social contact identity",
                    "subscriber-account canonical party binding",
                    "organization role-profile canonical party binding",
                    "native Vendor and FieldVendor paired party binding",
                    "SystemUser principal to Person Party binding",
                    "ResellerUser Person and reseller membership binding",
                    "organization membership canonical context binding",
                    "FieldVendorUser explicit vendor membership binding",
                    "SubscriberContact canonical Person Party binding",
                    "reviewed SubscriberContact relationship projection",
                    "reviewed SubscriberContact source-field contact-point projection",
                ),
                depends_on=("auth.rbac", "auth.permission_gate"),
                notes=(
                    "One native owner keeps identity, roles, descriptive "
                    "relationships, memberships, and contact evidence coherent. "
                    "A reseller is a specific commercial channel role; a partner "
                    "is an explicitly typed collaboration agreement with no "
                    "implicit permission. CRM identifiers are import provenance "
                    "only. Migrations 339 through 344 are additive foundations; "
                    "the subscriber binding is nullable and existing domain "
                    "reads cut over only in later verified slices."
                ),
            ),
            SOTService(
                name="party.identity_audit",
                module="app.services.party_identity_audit",
                owns=(
                    "read-only subscriber identity cleanup classification",
                    "duplicate candidate evidence grouping",
                    "subscriber cleanup worklist contract",
                ),
                depends_on=(
                    "party.registry",
                    "sales.service",
                    "sales.orders",
                    "access.subscription_lifecycle",
                    "operations.provisioning_workflow",
                    "financial.invoices",
                    "financial.payments",
                    "support.ticket_lifecycle",
                ),
                notes=(
                    "Observes native Sub facts and produces private UUID-only "
                    "artifacts. It never writes source state, calls CRM, or "
                    "authorizes an automatic merge."
                ),
            ),
            SOTService(
                name="party.identity_adjudication",
                module="app.services.party_identity_adjudication",
                owns=(
                    "reviewed subscriber identity decision contract",
                    "medium and high duplicate adjudication closure",
                    "Party backfill dry-run plan digest",
                    "PII-free Party backfill plan artifact contract",
                ),
                depends_on=("party.identity_audit", "party.registry"),
                notes=(
                    "Validates explicit decisions against current audit and row "
                    "digests, then produces a non-executable plan. It has no DB "
                    "writer or apply mode and never authorizes automatic merge."
                ),
            ),
            SOTService(
                name="party.identity_backfill_executor",
                module="app.services.party_identity_backfill",
                owns=(
                    "approved Subscriber Party backfill execution gate",
                    "Party identity backfill execution receipt",
                    "Party identity backfill idempotent replay verification",
                ),
                depends_on=(
                    "party.identity_audit",
                    "party.identity_adjudication",
                    "party.registry",
                ),
                notes=(
                    "Consumes one exact, expiring, separately approved plan in a "
                    "SERIALIZABLE transaction and calls party.registry for "
                    "predetermined Party creation and Subscriber binding. It "
                    "records a PII-free receipt, never commits inside the owner, "
                    "and cannot merge, repoint, assign roles, copy contacts, or "
                    "change lifecycle, billing, access, or authorization state."
                ),
            ),
            SOTService(
                name="party.organization_profile_audit",
                module="app.services.party_organization_audit",
                owns=(
                    "read-only organization role-profile convergence audit",
                    "Vendor and FieldVendor bridge debt classification",
                    "organization profile Party-role coverage report",
                ),
                depends_on=("party.registry",),
                notes=(
                    "Reports aggregate schema, binding, role-coverage, and "
                    "Vendor/FieldVendor bridge counts without identity values. "
                    "It never binds a profile, assigns a role, repairs a twin, "
                    "calls CRM, or changes a legacy read path."
                ),
            ),
            SOTService(
                name="party.principal_context_audit",
                module="app.services.party_principal_audit",
                owns=(
                    "read-only Person principal convergence audit",
                    "reseller and organization membership context audit",
                    "FieldVendorUser vendor context debt classification",
                ),
                depends_on=("party.registry", "auth.rbac", "auth.permission_gate"),
                notes=(
                    "Reports aggregate schema, principal-binding, membership-"
                    "context, and field-vendor-user counts without identity values. "
                    "It never binds a principal, creates or activates a membership, "
                    "changes a credential or permission, calls CRM, or changes a "
                    "login/read path."
                ),
            ),
            SOTService(
                name="party.contact_inbox_audit",
                module="app.services.party_contact_audit",
                owns=(
                    "read-only SubscriberContact Person convergence audit",
                    "legacy contact relationship and contact-point projection audit",
                    "Party contact-point verification and consent coverage report",
                    "Team Inbox canonical contact-point projection debt report",
                ),
                depends_on=("party.registry", "communications.team_inbox"),
                notes=(
                    "Reports only aggregate schema, identity, contact-point, and "
                    "Inbox routing-projection counts. It never emits identity "
                    "values, creates or binds a Party/relationship/contact point, "
                    "changes an Inbox route, copies verification or consent, or "
                    "changes authentication or authorization."
                ),
            ),
        ),
        entrypoints=(
            "scripts.migration.audit_subscriber_identity",
            "scripts.migration.plan_subscriber_party_backfill",
            "scripts.migration.execute_subscriber_party_backfill",
            "scripts.migration.audit_party_organization_profiles",
            "scripts.migration.audit_party_principal_contexts",
            "scripts.migration.audit_party_contact_inbox",
            "future party backfills",
            "future subscriber/reseller/vendor cutovers",
            "future Team Inbox contact resolution",
            "future authentication principal cutovers",
        ),
        rule=(
            "One real-world person or organization has one canonical Party and "
            "may hold several independent roles. Domain records and security "
            "principals link to Party; they do not create parallel identity. "
            "No adapter treats the additive foundation as a completed cutover."
        ),
    ),
    DomainSOT(
        domain="customer_context",
        services=(
            SOTService(
                name="customer.accounts",
                module="app.services.subscriber",
                owns=(
                    "Subscriber account creation",
                    "transaction-neutral Subscriber account initialization",
                ),
                depends_on=(
                    "access.subscription_lifecycle",
                    "events.dispatcher",
                ),
                notes=(
                    "Cross-domain coordinators may prepare an account through "
                    "this owner, but new/cut-over callers must not construct "
                    "Subscriber rows or decide account lifecycle state themselves. "
                    "Existing direct writers remain shrink-only migration debt."
                ),
            ),
            SOTService(
                name="customer.identity_scope",
                module="app.services.customer_context",
                owns=(
                    "portal/customer principal resolution",
                    "allowed account/subscriber scope",
                    "customer ownership checks",
                ),
            ),
            SOTService(
                name="customer.profile_commands",
                module="app.services.web_customer_actions",
                owns=(
                    "admin customer profile edits",
                    "person-to-business customer conversion",
                ),
                depends_on=("customer.identity_scope",),
                notes=(
                    "Business conversion is an explicit command. Generic "
                    "person edits and form category controls must not change "
                    "the customer account type."
                ),
            ),
            SOTService(
                name="customer.network_context",
                module="app.services.customer_network_context",
                owns=(
                    "customer network footprint",
                    "ONT/CPE/IP/session summary",
                ),
                depends_on=("customer.identity_scope", "network.access_path"),
            ),
            SOTService(
                name="customer.financial_position",
                module="app.services.customer_financial_position",
                owns=(
                    "distinct invoice-receivable and prepaid-funding summaries",
                    "customer-visible financial position",
                    "bounded cohort financial projections",
                ),
                depends_on=(
                    "financial.ledger",
                    "financial.prepaid_funding_reconstruction",
                ),
            ),
            SOTService(
                name="customer.service_status",
                module="app.services.service_status",
                owns=(
                    "customer-visible service health",
                    "customer financial action hints",
                    "payment-restores-service claims",
                ),
                depends_on=(
                    "financial.access_resolution",
                    "customer.financial_position",
                    "financial.grace_policy",
                ),
            ),
            SOTService(
                name="customer.usage_summary",
                module="app.services.usage_summary",
                owns=(
                    "customer usage window definitions",
                    "customer usage headline totals",
                    "customer usage total provenance",
                ),
                depends_on=("sessions.radius_reconciliation",),
                notes=(
                    "Authoritative zero is a valid total. Customer clients do "
                    "not replace server totals with loaded-session pages or "
                    "retention-limited chart series."
                ),
            ),
            SOTService(
                name="subscriber.growth_reports",
                module="app.services.subscriber_growth",
                owns=(
                    "admin subscriber growth and churn report figures",
                    "monthly subscriber growth and churn series",
                    "derived subscriber-status report counts",
                ),
                notes=(
                    "Domain read owner for the admin /reports growth, churn, "
                    "and status figures. The web report layer composes these "
                    "reads and owns presentation only."
                ),
            ),
            SOTService(
                name="customer.data_completeness",
                module="app.services.subscriber_data_completeness",
                owns=(
                    "purpose-specific subscriber data requirements",
                    "derived completeness and revalidation state",
                    "subscriber capture backlog and filing-readiness counts",
                ),
                depends_on=("customer.identity_scope",),
                notes=(
                    "Read-only policy owner. It reports absent, inferred, "
                    "captured, and stale state; it never fills a field or "
                    "writes a capture fact."
                ),
            ),
            SOTService(
                name="customer.location_verification",
                module="app.services.geocode_reconciler",
                owns=(
                    "subscriber location verification ledger writes",
                    "reconciliation of a captured pin against claimed location",
                ),
                depends_on=("customer.identity_scope",),
                notes=(
                    "Captured location facts flow through this owner. The "
                    "reconciler adjudicates a GPS pin against what was claimed "
                    "and writes ledger rows only for what agrees; a "
                    "disagreement is flagged for a human, never auto-applied. "
                    "It never writes Subscriber columns — projecting a captured "
                    "fact onto the profile stays the subscriber owner's job. "
                    "Only the location-capture owner invokes this writer."
                ),
            ),
            SOTService(
                name="customer.location_capture",
                module="app.services.location_capture",
                owns=(
                    "location-capture rollout and source authorization",
                    "location prompt eligibility and snooze lifecycle",
                    "field, portal, and agent capture orchestration",
                ),
                depends_on=(
                    "customer.identity_scope",
                    "customer.data_completeness",
                    "customer.location_verification",
                ),
                notes=(
                    "The field-arrival, portal, and agent adapters call this "
                    "owner. It enforces the default-off controls before "
                    "delegating adjudication and ledger writes to location "
                    "verification; it never writes Subscriber columns."
                ),
            ),
            SOTService(
                name="customer.branding",
                module="app.services.brand_profiles",
                owns=(
                    "platform/reseller/organization brand profiles",
                    "customer-facing brand precedence",
                    "brand primary, secondary, and semantic UI color roles",
                    "runtime web theme token generation",
                    "legacy branding convergence",
                ),
                depends_on=("customer.identity_scope", "control.domain_settings"),
            ),
        ),
        entrypoints=(
            "app.web.customer",
            "app.api.me",
            "app.api.subscribers",
            "app.web.admin.customers",
            "mobile",
            "app.services.customer_portal_*",
            "app.services.crm_api",
        ),
        rule=(
            "Customer-facing surfaces resolve scope once through customer context "
            "and compose network/financial summaries through services. Clients "
            "consume service-status action hints instead of inferring restoration "
            "policy from subscription status or invoice rows, and consume usage "
            "totals with their server-owned provenance instead of reconstructing "
            "headlines from partial client data."
        ),
    ),
    DomainSOT(
        domain="financial_access",
        services=(
            SOTService(
                name="financial.ledger",
                module="app.services.billing.ledger",
                owns=(
                    "append-only ledger record lifecycle",
                    "ledger reversal invariants",
                    "financial transaction history",
                ),
            ),
            SOTService(
                name="financial.prepaid_funding_reconstruction",
                module="app.services.prepaid_funding_reconstruction",
                owns=(
                    "reviewed full-cohort prepaid funding manifests",
                    "prepaid opening-position baselines and supersession",
                    "final prepaid funding authority cutover",
                    "opening balance plus post-cutover native funding projection",
                ),
                depends_on=("financial.ledger",),
                notes=(
                    "The first approved batch permanently retires Splynx funding "
                    "authority. Missing pre-cutover opening balances fail closed; "
                    "later corrections are reviewed append-only supersessions. "
                    "Splynx exports and bank statements are migration evidence, "
                    "never runtime money sources or fallbacks."
                ),
            ),
            SOTService(
                name="financial.account_adjustments",
                module="app.services.billing.adjustments",
                owns=(
                    "prepaid account-debit eligibility and preview",
                    "locked account-debit confirmation",
                    "account-adjustment idempotency and audit evidence",
                    "exact account-adjustment ledger links",
                    "previewed account-adjustment reversal evidence",
                ),
                depends_on=("financial.ledger", "customer.financial_position"),
                notes=(
                    "This owner accepts debits only. Customer credits remain "
                    "owned by financial.credit_notes, and account adjustments "
                    "do not decide service-access state."
                ),
            ),
            SOTService(
                name="financial.billing_accounts",
                module="app.services.billing.billing_accounts",
                owns=(
                    "billing account identity and configuration",
                    "consolidated billing account statement projection",
                ),
                depends_on=("financial.ledger",),
            ),
            SOTService(
                name="financial.consolidated_payments",
                module="app.services.billing.consolidated_payments",
                owns=(
                    "consolidated payment settlement preview and confirmation",
                    "consolidated payment idempotency and actor audit evidence",
                    "historical consolidated settlement evidence reconciliation",
                    "exact consolidated settlement cash provenance links",
                    "exact member-invoice allocation ledger links",
                    "exact consolidated-credit ledger links",
                    "consolidated-credit allocation preview and confirmation",
                    "exact source-credit consumption and subscriber-ledger links",
                    "consolidated-credit allocation idempotency and actor audit",
                    "historical consolidated-credit consumption reconciliation",
                    "exact billing-account projection-debit repair evidence",
                    "consolidated payment refund eligibility and preview",
                    "billing-account refund confirmation and exact ledger evidence",
                    "consolidated payment reversal eligibility and preview",
                    "billing-account reversal confirmation and exact ledger evidence",
                    "consolidated return idempotency and actor audit evidence",
                    "historical consolidated refund/reversal evidence reconciliation",
                    "exact historical consolidated return provenance links",
                    "historical consolidated return document reconstruction",
                    "reviewed historical return source references",
                    "consolidated payment access-reconciliation handoff",
                ),
                depends_on=(
                    "financial.ledger",
                    "financial.billing_accounts",
                    "financial.payments",
                ),
                notes=(
                    "Subscriber invoice receivable credits remain subscriber "
                    "ledger rows; reseller-held surplus is recorded in the "
                    "billing-account ledger and never assigned to a fake "
                    "subscriber. Moving held credit to a member receivable is a "
                    "separate preview-bound transfer with exact source and result "
                    "links. Payment state and access state remain separate."
                ),
            ),
            SOTService(
                name="financial.account_credit_applications",
                module="app.services.billing.account_credit",
                owns=(
                    "eligible invoice selection for evidenced account credit",
                    "deterministic payment-credit source selection",
                    "oldest-payable-debt application orchestration",
                    "invoice-void release of exact account-credit allocations",
                    "account-credit application invariant monitoring",
                ),
                depends_on=("financial.payments", "financial.invoices"),
                notes=(
                    "Account credit is derived from exact unconsumed settlement "
                    "evidence, never a wallet counter. This owner composes the "
                    "payment-allocation owner and does not write money directly."
                ),
            ),
            SOTService(
                name="financial.account_credit_deposits",
                module="app.services.account_credit_deposits",
                owns=(
                    "Deposit Account Credit eligibility and preview",
                    "typed deposit intent lifecycle and provider correlation",
                    "atomic deposit settlement composition",
                    "deposit-to-payment evidence link",
                    "deposit settlement outbox event",
                ),
                depends_on=(
                    "financial.payments",
                    "financial.account_credit_applications",
                    "financial.access_resolution",
                ),
                notes=(
                    "A deposit first records the whole confirmed receipt as "
                    "unallocated account credit, grants no service duration, and "
                    "then asks the canonical applicator to settle eligible debt."
                ),
            ),
            SOTService(
                name="financial.payments",
                module="app.services.billing.payments",
                owns=(
                    "payment document lifecycle",
                    "payment intent and observation lifecycle",
                    "confirmed payment settlement preview and evidence",
                    "payment creation and settlement idempotency and audit",
                    "exact settlement allocation and unallocated-credit links",
                    "previewed prepaid renewal consequence and exact debit link",
                    "settled account-credit allocation preview and confirmation",
                    "exact invoice-credit and account-credit-consumption links",
                    "native unallocated-credit reconciliation transactions",
                    "historical payment settlement evidence reconciliation",
                    "payment settlement access-reconciliation handoff",
                    "payment-originated ledger postings",
                    "payment refund eligibility and preview",
                    "payment refund confirmation and exact ledger evidence",
                    "payment refund idempotency and audit evidence",
                    "historical payment refund evidence reconciliation",
                    "payment refund access-reconciliation handoff",
                    "payment reversal eligibility and preview",
                    "payment reversal confirmation and exact ledger evidence",
                    "payment reversal idempotency and audit evidence",
                    "normalized provider reversal evidence",
                    "historical payment reversal evidence reconciliation",
                    "payment reversal access-reconciliation handoff",
                ),
                depends_on=("financial.ledger", "financial.billing_accounts"),
            ),
            SOTService(
                name="financial.import_payment_batch_reversals",
                module="app.services.financial_import_batch_reversals",
                owns=(
                    "payment import creation provenance",
                    "imported-payment batch reversal eligibility and preview",
                    "locked imported-payment batch reversal confirmation",
                    "batch reversal idempotency and actor audit evidence",
                    "exact import-row-to-settlement-to-reversal ledger links",
                    "imported-payment reversal access-reconciliation handoff",
                ),
                depends_on=(
                    "financial.payments",
                    "customer.financial_position",
                ),
                notes=(
                    "Only payments structurally proven to have been created by "
                    "one durable apply run can be reversed. Reused or historical "
                    "rows without provenance are never inferred from JSON, "
                    "external IDs, amounts, or memos. Confirmation composes the "
                    "payment reversal owner and keeps every source and result row."
                ),
            ),
            SOTService(
                name="financial.invoices",
                module="app.services.billing.invoices",
                owns=(
                    "invoice document lifecycle",
                    "invoice status transitions",
                    "invoice adjustment and reversal postings",
                    "automation invoice creation and draft issuance",
                    "automation invoice-line construction and source-fact replay",
                    "usage-charge invoice and invoice-line construction",
                    "overdue invoice state and observation event",
                    "unfunded prepaid invoice return-to-draft eligibility",
                    "invoice-originated ledger postings",
                    "invoice receivable settlement summary",
                    "invoice void eligibility preview and confirmation",
                    "invoice write-off eligibility preview and confirmation",
                    "exact invoice closure ledger evidence",
                    "invoice closure idempotency and audit evidence",
                    "historical invoice closure evidence reconciliation",
                    "invoice settlement access-reconciliation handoff",
                ),
                depends_on=("financial.ledger", "financial.billing_accounts"),
            ),
            SOTService(
                name="financial.credit_notes",
                module="app.services.billing.credit_notes",
                owns=(
                    "credit-note lifecycle",
                    "credit-note issuance and void preview/confirmation",
                    "credit-note funding and void ledger evidence",
                    "historical credit-note funding reconciliation",
                    "credit-note application eligibility and preview",
                    "credit-note application idempotency",
                    "credit-note application-to-ledger evidence",
                    "funded credit-note application consumption evidence",
                    "credit-note ledger-posting requests",
                    "referral reward account credits",
                ),
                depends_on=("financial.ledger", "financial.invoices"),
            ),
            SOTService(
                name="financial.tax_configuration",
                module="app.services.billing.tax",
                owns=("configurable tax-rate records", "tax-rate activation lifecycle"),
            ),
            SOTService(
                name="financial.payment_proofs",
                module="app.services.payment_proofs",
                owns=(
                    "payment-proof review lifecycle",
                    "proof-backed payment request",
                    "withholding-tax receivable source records",
                ),
                depends_on=("financial.payments",),
            ),
            SOTService(
                name="financial.tax_accounting",
                module="app.services.tax_accounting",
                owns=(
                    "tax report semantics",
                    "output-tax invoice projection",
                    "withholding-tax receivable projection",
                    "tax report period and currency aggregation",
                    "credit-note tax recognition point",
                    "withholding-tax lifecycle",
                    "withholding-tax official timeline",
                    "net output-tax liability projection",
                ),
                depends_on=(
                    "financial.invoices",
                    "financial.tax_configuration",
                    "financial.payment_proofs",
                ),
                notes=(
                    "Issued output tax less issued credit-note tax adjustments is "
                    "the source-document liability, not cash collected, and "
                    "currencies remain separate. This owner also enforces legal "
                    "pending/certified/reclaimed/written-off WHT transitions and an "
                    "immutable evidence timeline. Dotmac ERP exclusively owns tax "
                    "account mappings, balanced journals, tax transactions, and "
                    "financial statements; Sub exports line tax treatment and WHT "
                    "facts through bounded sync feeds and has no local posting path."
                ),
            ),
            SOTService(
                name="financial.billing_profile",
                module="app.services.billing_profile",
                owns=(
                    "prepaid/postpaid profile resolution",
                    "billing-mode transition policy",
                ),
            ),
            SOTService(
                name="financial.prepaid_threshold",
                module="app.services.prepaid_threshold",
                owns=(
                    "prepaid enforcement threshold",
                    "unfunded prepaid renewal requirement",
                ),
                depends_on=("financial.billing_profile",),
            ),
            SOTService(
                name="financial.grace_policy",
                module="app.services.collections.grace_policy",
                owns=(
                    "account/policy/billing-default grace precedence",
                    "grace provenance and deadline",
                    "post-grace elapsed-day decision",
                ),
                depends_on=("financial.billing_profile",),
            ),
            SOTService(
                name="financial.prepaid_enforcement",
                module="app.services.prepaid_enforcement_planner",
                owns=(
                    "prepaid enforcement candidate cohort",
                    "prepaid warn/suspend/restore planning",
                    "prepaid policy projection consumed by dry-run and execution",
                ),
                depends_on=(
                    "financial.prepaid_funding_reconstruction",
                    "financial.access_resolution",
                    "financial.billing_profile",
                    "financial.prepaid_threshold",
                    "financial.grace_policy",
                ),
            ),
            SOTService(
                name="financial.prepaid_enforcement_readiness",
                module="app.services.prepaid_enforcement_readiness",
                owns=(
                    "prepaid independent-funding cutover comparison",
                    "prepaid enforcement activation prerequisite",
                    "prepaid funding readiness evidence",
                ),
                depends_on=(
                    "financial.prepaid_funding_reconstruction",
                    "financial.prepaid_enforcement",
                    "financial.access_resolution",
                ),
                notes=(
                    "Readiness proves full-cohort parity and gates activation. It "
                    "never supplies a runtime balance; live suspension and restore "
                    "always resolve funding from Sub's financial owners."
                ),
            ),
            SOTService(
                name="financial.prepaid_plan_change",
                module="app.services.prepaid_plan_changes",
                owns=(
                    "prepaid plan-change proration decision",
                    "prepaid plan-change funding affordability",
                    "human preview fingerprint and locked confirmation",
                    "plan-change confirmation idempotency and actor audit",
                    "exact change-request-to-financial-evidence links",
                    "idempotent plan-change debit and credit staging",
                ),
                depends_on=(
                    "financial.account_adjustments",
                    "financial.credit_notes",
                    "customer.financial_position",
                ),
                notes=(
                    "Immediate changes bind the displayed owner preview to a "
                    "durable change request, lock and recompute at write time, "
                    "then commit the request, exact financial evidence, and "
                    "subscription together. Bulk changes remain next-cycle only "
                    "until they have per-subscription previews."
                ),
            ),
            SOTService(
                name="financial.prepaid_service_renewals",
                module="app.services.prepaid_service_renewals",
                owns=(
                    "due prepaid service-cycle funding preview",
                    "locked and idempotent prepaid renewal debit",
                    "exact debit-to-entitlement evidence",
                    "prepaid subscription paid-through advancement",
                    "bounded scheduled renewal catch-up",
                ),
                depends_on=(
                    "financial.account_adjustments",
                    "financial.prepaid_funding_reconstruction",
                ),
            ),
            SOTService(
                name="financial.addon_purchases",
                module="app.services.customer_portal_flow_addons",
                owns=(
                    "customer add-on purchase eligibility and preview",
                    "add-on price and subscription-state confirmation",
                    "add-on purchase idempotency and audit evidence",
                    "exact add-on entitlement-to-adjustment link",
                ),
                depends_on=(
                    "financial.account_adjustments",
                    "customer.financial_position",
                ),
                notes=(
                    "Paid purchases request one exact debit from the adjustment "
                    "owner. Free add-ons explicitly produce no ledger transaction."
                ),
            ),
            SOTService(
                name="financial.payment_arrangements",
                module="app.services.payment_arrangements",
                owns=(
                    "payment-arrangement eligibility and lifecycle",
                    "installment schedule and payment application",
                    "active-arrangement collection shield state",
                ),
                depends_on=(
                    "customer.financial_position",
                    "financial.invoices",
                    "financial.payments",
                ),
            ),
            SOTService(
                name="financial.access_resolution",
                module="app.services.access_resolution",
                owns=(
                    "billable service classification",
                    "RADIUS access decision",
                    "financial suspension/restoration eligibility",
                    "currency-bound prepaid funding decision",
                ),
                depends_on=(
                    "financial.billing_profile",
                    "financial.prepaid_threshold",
                    "customer.financial_position",
                ),
            ),
            SOTService(
                name="financial.dunning",
                module="app.services.collections._core",
                owns=(
                    "postpaid collection lifecycle",
                    "dunning action execution",
                    "financial access consequence preview and confirmation",
                    "financial suspension and restoration idempotency and audit",
                    "exact enforcement-lock, throttle, and case evidence",
                    "financial access restoration reconciliation",
                ),
                depends_on=(
                    "financial.access_resolution",
                    "financial.ledger",
                    "financial.payment_arrangements",
                    "financial.billing_health",
                    "access.subscription_lifecycle",
                    "access.walled_garden_policy",
                ),
            ),
            SOTService(
                name="financial.billing_health",
                module="app.services.billing_health",
                owns=(
                    "billing health snapshot",
                    "billing anomaly classification",
                    "bounded billing health observations",
                    "account-credit invariant observation publication",
                ),
                depends_on=(
                    "customer.financial_position",
                    "financial.access_resolution",
                    "financial.billing_profile",
                    "financial.account_credit_applications",
                ),
                notes=(
                    "Billing health is monitoring evidence, never a financial "
                    "balance owner or direct suspension/restoration decision."
                ),
            ),
            SOTService(
                name="financial.billing_reporting",
                module="app.services.billing.reporting",
                owns=(
                    "billing statistics and dashboard report read models",
                    "admin revenue report figure definitions",
                    "payments-basis revenue definitions",
                    "subscription movement and per-offer report counts",
                ),
                depends_on=("financial.invoices", "financial.payments"),
                notes=(
                    "Read owner only: aggregates invoice/payment/subscription "
                    "facts for dashboards and the admin reports. It decides no "
                    "financial consequences."
                ),
            ),
            SOTService(
                name="financial.billing_scheduled",
                module="app.services.billing.scheduled",
                owns=(
                    "scheduled invoice and overdue execution",
                    "billing health and audit execution",
                    "scheduled billing notification execution",
                ),
                depends_on=(
                    "financial.ledger",
                    "financial.access_resolution",
                    "financial.billing_health",
                ),
            ),
            SOTService(
                name="financial.collections_scheduled",
                module="app.services.collections.scheduled",
                owns=(
                    "scheduled billing enforcement execution",
                    "scheduled prepaid balance enforcement execution",
                    "scheduled bundle-state reconciliation execution",
                ),
                depends_on=(
                    "financial.dunning",
                    "financial.access_resolution",
                    "financial.prepaid_enforcement",
                    "financial.prepaid_enforcement_readiness",
                ),
            ),
            SOTService(
                name="financial.payment_provider_events",
                module="app.services.billing.providers",
                owns=(
                    "payment-provider event ingestion",
                    "normalized provider monetary observations",
                    "provider-event idempotency",
                    "incomplete provider settlement resumption",
                ),
                depends_on=("financial.payments",),
            ),
            SOTService(
                name="financial.payment_webhooks",
                module="app.services.api_billing_webhooks",
                owns=(
                    "verified payment webhook projection",
                    "inbound payment dead-letter lifecycle",
                    "payment dead-letter replay",
                ),
                depends_on=("financial.payment_provider_events",),
            ),
            SOTService(
                name="financial.payment_reconciliation",
                module="app.services.payment_reconciliation",
                owns=(
                    "stranded top-up reconciliation",
                    "scheduled top-up reconciliation execution",
                ),
                depends_on=("financial.ledger", "financial.payment_provider_events"),
            ),
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
        rule=(
            "No caller infers access or balances from draft invoices, imported "
            "legacy fields, or ad hoc sums when ledger/access resolvers exist. "
            "Tax reports consume the tax-accounting projection, never label "
            "issued tax as collected cash, and never add different currencies. "
            "Tax account mappings and double-entry consequences are written only "
            "by Dotmac ERP from Sub's bounded source-fact feeds."
        ),
    ),
    DomainSOT(
        domain="network",
        services=(
            SOTService(
                name="network.identity",
                module="app.services.network.identity",
                owns=("cross-model network links", "device/entity identity"),
            ),
            SOTService(
                name="network.monitoring_inventory",
                module="app.services.network_monitoring",
                owns=(
                    "monitoring inventory mutations",
                    "monitoring metric records",
                    "alert rule and alert state mutations",
                ),
                depends_on=("network.identity",),
            ),
            SOTService(
                name="network.fiber_source_staging",
                module="app.services.network.fiber_topology_staging",
                owns=(
                    "immutable fiber source manifests",
                    "normalized staged fiber source facts",
                    "non-authoritative duplicate and canonical-match suggestions",
                ),
                depends_on=("gis.spatial_sync",),
                notes=(
                    "Staged map rows are observations with provenance. Match "
                    "suggestions never mutate or retire canonical assets."
                ),
            ),
            SOTService(
                name="network.fiber_topology",
                module="app.services.fiber_topology",
                owns=(
                    "fiber asset identity and connectivity graph",
                    "OLT-to-customer topology integrity",
                    "ordered validated subscription fiber traces",
                    "bounded fiber fault-candidate ranking",
                    "fiber import and customer-trace cutover gates",
                ),
                depends_on=(
                    "network.identity",
                    "gis.spatial_sync",
                    "network.fiber_source_staging",
                ),
                notes=(
                    "Electronic inventory and telemetry remain observations. "
                    "Imported geometry is staged evidence until this owner "
                    "validates asset identity and connectivity. Trace resolution "
                    "fails closed on missing or ambiguous edges; fault candidates "
                    "never declare incidents or redefine topology."
                ),
            ),
            SOTService(
                name="network.fiber_asset_changes",
                module="app.services.fiber_change_requests",
                owns=(
                    "reviewed passive-fiber asset change requests",
                    "approved passive-fiber asset mutations",
                ),
                depends_on=("network.fiber_topology",),
            ),
            SOTService(
                name="network.fiber_identity_decisions",
                module="app.services.network.fiber_topology_identity",
                owns=(
                    "reviewed fiber source identity decisions",
                    "canonical fiber source identity links",
                    "fiber source identity change-request projection",
                ),
                depends_on=(
                    "network.fiber_topology",
                    "network.fiber_asset_changes",
                ),
                notes=(
                    "Identity decisions are bound to immutable staged content. "
                    "Creates emit reviewed fiber change requests; a canonical "
                    "source link is written only after the asset exists."
                ),
            ),
            SOTService(
                name="network.fiber_identity_review",
                module="app.services.network.fiber_topology_review",
                owns=(
                    "fiber identity review queue projection",
                    "immutable fiber identity proposal batch manifests",
                    "fiber identity batch review attestations",
                    "bounded fiber identity execution-run evidence",
                    "fiber identity change-request finalization sweep",
                ),
                depends_on=("network.fiber_identity_decisions",),
                notes=(
                    "Batch review binds an independent attestation to the exact "
                    "proposal manifest and delegates every decision transition to "
                    "the identity owner. Bounded execution records exact outcomes. "
                    "Neither execution nor reconciliation approves a fiber change "
                    "request."
                ),
            ),
            SOTService(
                name="network.fiber_field_observations",
                module="app.services.network.fiber_topology_field_observations",
                owns=(
                    "immutable staged fiber field observations",
                    "exact field-observation and claim evidence digests",
                    "field observation agreement, conflict, and drift projection",
                ),
                depends_on=(
                    "network.fiber_source_staging",
                    "operations.work_orders",
                ),
                notes=(
                    "Every observation binds exact staged content, work order, "
                    "technician identity, explicit labels/references, measurement "
                    "facts, and active same-job attachment pointers. Contradictory "
                    "observations remain evidence. This owner cannot infer identity "
                    "or endpoints, generate decisions, approve changes, mutate "
                    "canonical topology, or establish cutover thresholds."
                ),
            ),
            SOTService(
                name="network.fiber_field_verification_worklist",
                module="app.services.network.fiber_topology_field_worklist",
                owns=(
                    "exhaustive latest-source fiber field-verification worklist",
                    "deterministic field-evidence triage priority projection",
                    "exact field-worklist row and report evidence digests",
                ),
                depends_on=(
                    "network.fiber_source_staging",
                    "network.fiber_field_observations",
                ),
                notes=(
                    "This read-only owner keeps every latest staged feature in "
                    "the cohort and orders evidence gathering without hiding "
                    "current agreement. Existing native work-order references "
                    "are context only. It cannot create or assign jobs, record "
                    "observations, infer identity/endpoints, generate decisions, "
                    "mutate topology, or establish cutover eligibility."
                ),
            ),
            SOTService(
                name="network.fiber_field_verification_map",
                module="app.services.network.fiber_topology_field_map",
                owns=(
                    "complete exact-GeoJSON fiber field-verification overlay",
                    "field-map presentation geometry classification and bounds",
                    "exact field-map feature and overlay evidence digests",
                ),
                depends_on=(
                    "network.fiber_source_staging",
                    "network.fiber_field_verification_worklist",
                ),
                notes=(
                    "This read-only projection attaches exact staged GeoJSON to "
                    "the complete owner-produced worklist. Color represents only "
                    "worklist priority; blocked source geometry remains visible "
                    "without repair. It cannot snap or infer topology, create jobs "
                    "or observations, mutate source/canonical state, establish "
                    "thresholds, or claim cutover eligibility."
                ),
            ),
            SOTService(
                name="network.fiber_identity_coverage",
                module="app.services.network.fiber_topology_identity_coverage",
                owns=(
                    "exhaustive latest staged point-identity coverage reconciliation",
                    "fiber point-identity lineage and provenance drift projection",
                    "fiber point-identity cutover-review readiness evidence",
                ),
                depends_on=(
                    "network.fiber_source_staging",
                    "network.fiber_asset_changes",
                    "network.fiber_field_observations",
                    "network.fiber_identity_decisions",
                    "network.fiber_identity_review",
                ),
                notes=(
                    "One repeatable read-only snapshot keeps canonical-model support, "
                    "source coverage, decision lifecycle, change-request state, and "
                    "provenance validity separate. Cabinets, FATs, closures, and "
                    "buildings use their current canonical models; poles/supports "
                    "remain explicitly reject-only. The owner cannot infer identity, "
                    "create or advance decisions, approve change requests, mutate "
                    "assets, or authorize production cutover."
                ),
            ),
            SOTService(
                name="network.fiber_connectivity_decisions",
                module="app.services.network.fiber_topology_connectivity",
                owns=(
                    "reviewed staged-path connectivity decisions",
                    "typed endpoint termination resolution",
                    "canonical fiber segment source provenance",
                    "fiber connectivity change-request reconciliation",
                ),
                depends_on=(
                    "network.fiber_topology",
                    "network.fiber_asset_changes",
                    "network.fiber_identity_decisions",
                ),
                notes=(
                    "Route geometry remains source evidence. Operational edges "
                    "require two explicit typed canonical endpoint references, "
                    "independent review, applied termination records, and an "
                    "applied segment change request."
                ),
            ),
            SOTService(
                name="network.fiber_connectivity_review",
                module=("app.services.network.fiber_topology_connectivity_review"),
                owns=(
                    "immutable fiber connectivity proposal batch manifests",
                    "fiber connectivity batch review attestations",
                    "bounded fiber connectivity execution evidence",
                    "bounded fiber connectivity reconciliation evidence",
                ),
                depends_on=("network.fiber_connectivity_decisions",),
                notes=(
                    "Every manifest row binds exact staged content to explicit "
                    "canonical endpoint IDs. Batch review and runs delegate every "
                    "transition to the connectivity-decision owner; geometry never "
                    "selects endpoints and the batch owner never approves canonical "
                    "termination or segment change requests."
                ),
            ),
            SOTService(
                name="network.fiber_connectivity_coverage",
                module=("app.services.network.fiber_topology_connectivity_coverage"),
                owns=(
                    "exhaustive latest staged-path coverage reconciliation",
                    "fiber connectivity lineage and evidence drift projection",
                    "fiber connectivity cutover-review readiness evidence",
                ),
                depends_on=(
                    "network.fiber_source_staging",
                    "network.fiber_asset_changes",
                    "network.fiber_field_observations",
                    "network.fiber_connectivity_decisions",
                    "network.fiber_connectivity_review",
                ),
                notes=(
                    "One repeatable read-only snapshot keeps source coverage, "
                    "decision lifecycle, canonical mutation state, and provenance "
                    "validity separate. It cannot infer endpoints, create or advance "
                    "decisions, approve change requests, mutate topology, or authorize "
                    "production cutover."
                ),
            ),
            SOTService(
                name="network.ont_topology_observations",
                module="app.services.network.ont_topology_observations",
                owns=(
                    "durable allowlisted electronic-topology observations",
                    "non-destructive initialization of empty ONT OLT/PON edges",
                    "exact observed PON inventory initialization with provenance",
                    "observation agreement and manual-review evidence",
                ),
                depends_on=("network.fiber_topology",),
                notes=(
                    "UISP collectors submit exact OLT and numeric PON evidence; "
                    "Huawei authorization submits exact modeled F/S/P evidence. "
                    "Only UISP numeric evidence may initialize missing PON "
                    "inventory. Both sources may fill empty ONT edges, but never "
                    "overwrite or merge an existing identity edge. Conflicts "
                    "remain durable review evidence for "
                    "network.ont_assignment_identity."
                ),
            ),
            SOTService(
                name="network.ont_assignment_commands",
                module="app.services.network.ont_assignment_commands",
                owns=(
                    "normal explicit ONT-to-subscription assignments",
                    "normal assignment release transitions",
                    "verified physical PON move projections",
                    "exact normal assignment audit results",
                ),
                depends_on=(
                    "network.identity",
                    "network.ont_topology_observations",
                ),
                notes=(
                    "Normal provisioning requires an exact ONT, subscription, "
                    "and modeled PON. The subscriber is derived only through the "
                    "subscription bridge. MAC, name, address, work-order, map, "
                    "and registration inference cannot select identity. Existing "
                    "disagreements fail closed into reviewed repair."
                ),
            ),
            SOTService(
                name="network.ont_assignment_identity",
                module="app.services.network.ont_assignment_identity",
                owns=(
                    "reviewed exceptional ONT-to-subscription identity repairs",
                    "exact subscription/subscriber and ONT/PON/OLT repair projection",
                    "duplicate assignment deactivation audit evidence",
                    "exact electronic identity repair result evidence",
                ),
                depends_on=(
                    "network.fiber_topology",
                    "network.ont_topology_observations",
                    "network.ont_assignment_commands",
                ),
                notes=(
                    "Repairs bind exact assignment, subscription, PON, OLT, and "
                    "conflict IDs. Subscriber, address, name, and registration "
                    "inference are forbidden. Preview is read-only, review is "
                    "independent, and execution revalidates under lock. This "
                    "does not replace normal explicit service provisioning."
                ),
            ),
            SOTService(
                name="network.ont_assignment_cutover",
                module="app.services.network.ont_assignment_cutover",
                owns=(
                    "read-only active ONT assignment invariant audit",
                    "stable exact assignment blocker evidence",
                    "assignment database-constraint cutover readiness gate",
                ),
                depends_on=(
                    "network.ont_assignment_commands",
                    "network.ont_assignment_identity",
                ),
                notes=(
                    "The exhaustive audit reports persisted identifiers and "
                    "routes every repair to independent identity review. It "
                    "never chooses replacement identity, mutates assignments, "
                    "or enables constraints. A clean report is necessary but "
                    "does not itself authorize cutover."
                ),
            ),
            SOTService(
                name="network.ont_assignment_cutover_batches",
                module="app.services.network.ont_assignment_cutover_batches",
                owns=(
                    "immutable operator-selected assignment cleanup manifests",
                    "exact cutover report and finding evidence binding",
                    "atomic independent batch review attestations",
                    "delegated identity decision staging provenance",
                ),
                depends_on=(
                    "network.ont_assignment_cutover",
                    "network.ont_assignment_identity",
                ),
                notes=(
                    "A batch binds one complete audit digest and each selected "
                    "finding digest to explicit actions, targets, and conflict "
                    "IDs. It stages and reviews identity-owner decisions "
                    "atomically but has no execution operation; approved repairs "
                    "remain individual locked identity commands."
                ),
            ),
            SOTService(
                name="network.ont_assignment_cutover_verification",
                module=("app.services.network.ont_assignment_cutover_verification"),
                owns=(
                    "immutable post-execution cleanup verification attestations",
                    "exact terminal identity-decision result snapshots",
                    "fresh exhaustive assignment audit evidence binding",
                    "batch-scope residual and global cutover-readiness evidence",
                ),
                depends_on=(
                    "network.ont_assignment_cutover",
                    "network.ont_assignment_cutover_batches",
                    "network.ont_assignment_identity",
                ),
                notes=(
                    "Verification copies exact terminal result payloads and "
                    "hashes into an immutable evidence snapshot, then binds a "
                    "fresh exhaustive audit. Pending decisions cannot be "
                    "attested. The owner never executes repairs, mutates "
                    "assignments, or enables constraints."
                ),
            ),
            SOTService(
                name="network.ont_assignment_cutover_coverage",
                module="app.services.network.ont_assignment_cutover_coverage",
                owns=(
                    "read-only current cleanup-finding lineage reconciliation",
                    "exact, superseded, and overlapping coverage classification",
                    "current decision-result and verification-drift projection",
                    "constraint-authorization review readiness evidence",
                ),
                depends_on=(
                    "network.ont_assignment_cutover",
                    "network.ont_assignment_cutover_batches",
                    "network.ont_assignment_cutover_verification",
                    "network.ont_assignment_identity",
                ),
                notes=(
                    "One repeatable snapshot joins every current finding to all "
                    "immutable proposal, review, result, and verification "
                    "evidence. It keeps decision, current-scope, and verification "
                    "state separate. Readiness is conservative evidence for a "
                    "separate authorization review; this owner cannot execute "
                    "repairs or authorize or enable constraints."
                ),
            ),
            SOTService(
                name="network.ont_assignment_constraint_authorization",
                module=("app.services.network.ont_assignment_constraint_authorization"),
                owns=(
                    "immutable constraint-cutover authorization requests",
                    "independent approve or decline authorization attestations",
                    "authorization expiry and current-evidence projection",
                    "exact target, coverage, and cutover evidence binding",
                ),
                depends_on=("network.ont_assignment_cutover_coverage",),
                notes=(
                    "A request stores an explicitly named target, caller-chosen "
                    "expiry, and the complete clean coverage snapshot. A different "
                    "actor reviews the unchanged request. Approval becomes stale "
                    "or expired by derivation and is only evidence for a separate "
                    "reviewed DDL change; this owner has no DDL authority."
                ),
            ),
            SOTService(
                name="network.ont_inventory_release",
                module="app.services.network.ont_inventory_release",
                owns=(
                    "return-to-inventory electronic identity release transition",
                    "closure and de-identification of all ONT assignments",
                    "post-cleanup ONT OLT/PON/F/S/P identity clearing",
                ),
                depends_on=(
                    "network.ont_assignment_commands",
                    "network.ont_assignment_identity",
                    "network.ont_topology_observations",
                ),
                notes=(
                    "The broader inventory orchestrator must complete external "
                    "OLT/ACS cleanup first. This narrow owner locks the ONT and "
                    "every assignment, selects no replacement, closes active "
                    "assignments, and clears customer/subscription and electronic "
                    "identity in one local transaction."
                ),
            ),
            SOTService(
                name="network.fiber_access_attachments",
                module="app.services.network.fiber_access_attachments",
                owns=(
                    "reviewed PON-to-splitter input attachments",
                    "reviewed ONT-to-splitter output attachments",
                    "canonical ONT splitter parent projection",
                    "fiber access attachment audit results",
                ),
                depends_on=(
                    "network.fiber_topology",
                    "network.fiber_connectivity_decisions",
                    "network.ont_assignment_commands",
                    "network.ont_assignment_identity",
                ),
                notes=(
                    "Only exact directed ports with agreeing ONT/PON/OLT identity "
                    "can be attached. Preview is read-only, review is independent, "
                    "execution revalidates under lock, and stale inputs close "
                    "without mutation. Geometry and legacy assignments never create "
                    "an access edge."
                ),
            ),
            SOTService(
                name="network.access_path",
                module="app.services.network.access_path",
                owns=("subscription access path", "last-mile path summary"),
                depends_on=(
                    "network.identity",
                    "network.fiber_topology",
                    "network.ont_assignment_commands",
                    "network.ont_assignment_identity",
                    "network.fiber_access_attachments",
                ),
            ),
            SOTService(
                name="network.radius_sessions",
                module="app.services.network.radius_sessions",
                owns=(
                    "online-now session state",
                    "primary NAS session",
                    "bounded historical NAS evidence",
                ),
                depends_on=("network.identity",),
            ),
            SOTService(
                name="network.ont_runtime_status",
                module="app.services.network.ont_runtime_status",
                owns=(
                    "Huawei ONT runtime-status poll observations",
                    "Huawei OLT bulk-status pollability predicate",
                    "Huawei OLT bulk-status poll task admission",
                ),
                depends_on=("runtime.infrastructure_polling",),
                notes=(
                    "Owns recurring and stale-read-triggered Huawei bulk status "
                    "polls as retry-safe infrastructure observations. These polls "
                    "do not create tracked device operations; operator-requested "
                    "single-ONT commands remain owned by operation dispatch."
                ),
            ),
            SOTService(
                name="network.device_state",
                module="app.services.device_operational_status",
                owns=(
                    "NOC-facing device operational status",
                    "device operational status vocabulary",
                    "device retry-pending and alarm classification",
                ),
                depends_on=(
                    "runtime.infrastructure_polling",
                    "network.ont_runtime_status",
                ),
            ),
            SOTService(
                name="network.ont_status_refresh",
                module="app.services.network.ont_status_refresh",
                owns=(
                    "stale ONT runtime-status refresh admission",
                    "OLT-level status refresh rate limiting",
                    "safe background refresh request projection",
                ),
                depends_on=(
                    "network.device_state",
                    "network.ont_runtime_status",
                ),
                notes=(
                    "Read surfaces may request refresh through this owner, but "
                    "must not poll OLTs directly. Huawei refreshes are admitted "
                    "through the infrastructure observation polling owner as "
                    "bounded OLT-level jobs; UISP-managed ONTs remain owned by "
                    "the UISP topology sync source."
                ),
            ),
            SOTService(
                name="network.device_projection",
                module="app.services.device_projection_reconcile",
                owns=(
                    "device_projections materialised table",
                    "unified cross-type device row (OLT/core/ONT/CPE)",
                    "projected operational status and freshness",
                    "device projection orphan pruning",
                ),
                depends_on=(
                    "network.device_state",
                    "network.monitoring_inventory",
                    "network.identity",
                ),
                notes=(
                    "Sole canonical writer of device_projections. Delegates the "
                    "multi-source device derivation to collect_devices and "
                    "projects one materialised row per device so the admin device "
                    "list can search/filter/sort/paginate in SQL. The table is a "
                    "rebuildable cache: reconcile is idempotent, stamps "
                    "refreshed_at, and prunes rows whose source device is gone. "
                    "Readers never write it; they request a reconcile rather than "
                    "maintaining a parallel derivation path."
                ),
            ),
            SOTService(
                name="network.operation_ledger",
                module="app.services.network_operations",
                owns=(
                    "tracked device operation lifecycle and status vocabulary",
                    "operation terminal-transition guard",
                    "correlation-key duplicate suppression",
                    "stale-active operation reclamation",
                    "parent/child operation status rollup",
                    "device operation re-execution eligibility",
                    "immutable redrive lineage and reviewed-head evidence",
                    "typed recovery eligibility and retry limits",
                ),
                depends_on=("network.identity",),
                notes=(
                    "Owns whether a tracked device operation may run, resume, or "
                    "be re-executed. Celery tasks are transport adapters that "
                    "report progress through this ledger; they do not decide "
                    "retry eligibility. app.services.task_reliability declares "
                    "each task's contract and is a projection of this owner, not "
                    "a parallel authority — a task whose contract claims operator "
                    "redrive requires a redrive path here first. Failed attempts "
                    "remain immutable; approved retries create linked operations "
                    "through app.services.network_operation_recovery. Unregistered "
                    "device writes fail closed."
                ),
            ),
            SOTService(
                name="network.operation_dispatch",
                module="app.services.network_operation_dispatch",
                owns=(
                    "transactional network command outbox",
                    "typed operation-to-task command registry",
                    "broker publication attempts and acknowledgement state",
                    "single-admission worker execution claims",
                    "unknown-delivery and interrupted-execution classification",
                ),
                depends_on=("network.operation_ledger",),
                notes=(
                    "Stages the exact registered command in the same transaction "
                    "as its operation. A scheduled publisher is the only broker "
                    "writer for managed commands, and a worker envelope claims the "
                    "dispatch row before entering device code. Operation status "
                    "remains the device/business outcome; transport uncertainty is "
                    "preserved separately and fails closed for reviewed recovery."
                ),
            ),
            SOTService(
                name="network.ont_provisioning_commands",
                module="app.services.network.ont_provisioning_commands",
                owns=(
                    "ONT authorization and baseline-repair command acceptance",
                    "provisioning operation and dispatch atomicity",
                    "bootstrap child-operation and delayed-attempt staging",
                    "provisioning command duplicate responses",
                ),
                depends_on=(
                    "network.identity",
                    "network.operation_ledger",
                    "network.operation_dispatch",
                ),
                notes=(
                    "Admin, API, and bulk adapters submit typed intent here. "
                    "They never publish provisioning device tasks directly, and "
                    "workers never create their own operation after broker delivery."
                ),
            ),
            SOTService(
                name="network.ont_provisioning_execution",
                module="app.services.network.ont_provisioning_execution",
                owns=(
                    "tracked ONT authorization execution transitions",
                    "tracked baseline-repair execution transitions",
                    "DB-only ONT baseline preview execution",
                    "TR-069 bootstrap verification and retry policy",
                    "bootstrap parent and bulk-item outcome projection",
                ),
                depends_on=(
                    "network.ont_provisioning_commands",
                    "network.operation_ledger",
                ),
                notes=(
                    "Celery tasks claim a durable dispatch and delegate here. "
                    "Inform-driven confirmation and scheduled verification share "
                    "the same parent/child completion projection. A pre-cutover "
                    "broker envelope may only re-submit intent to the command "
                    "owner and cannot enter device code."
                ),
            ),
            SOTService(
                name="network.control_plane_intent",
                module="app.services.control_plane_intent",
                owns=(
                    "shared desired-state delivery lifecycle",
                    "control-plane target and revision identity",
                    "vendor status projections and transition guards",
                ),
                depends_on=("network.identity",),
                notes=(
                    "Vendor adapters retain native persistence models but project "
                    "through one desired-to-readback lifecycle. Verified always "
                    "requires device evidence for the current intent revision."
                ),
            ),
            SOTService(
                name="network.routeros_sot",
                module="app.services.router_management.sot_policy",
                owns=(
                    "typed RouterOS desired-state contract",
                    "managed RouterOS resource and field policy",
                    "Dotmac RouterOS resource ownership identity",
                ),
                depends_on=(
                    "network.identity",
                    "runtime.db_sessions",
                    "observability.recording",
                ),
                notes=(
                    "Vendor-specific RouterOS desired state projects through the "
                    "shared network.control_plane_intent lifecycle."
                ),
            ),
            SOTService(
                name="network.nas_inventory",
                module="app.services.nas.devices",
                owns=("NAS administrative lifecycle state", "NAS inventory reads"),
                depends_on=("network.identity",),
            ),
            SOTService(
                name="network.nas_lifecycle",
                module="app.services.nas_lifecycle",
                owns=(
                    "NAS lifecycle reconciliation plans",
                    "subscription NAS relink decisions",
                    "NAS lifecycle RADIUS projection commands",
                ),
                depends_on=(
                    "network.identity",
                    "network.access_path",
                    "network.radius_sessions",
                    "network.nas_inventory",
                    "service_intent.subscription_nas_assignment",
                    "access.radius_state",
                    "runtime.db_sessions",
                    "observability.recording",
                ),
            ),
            SOTService(
                name="network.nas_access_path_evidence",
                module="app.services.nas_access_path_evidence",
                owns=(
                    "manual NAS lifecycle evidence reports",
                    "historical access-path review recommendations",
                ),
                depends_on=(
                    "network.radius_sessions",
                    "network.nas_lifecycle",
                    "runtime.db_sessions",
                ),
            ),
            SOTService(
                name="network.outage_impact",
                module="app.services.network.outage_impact",
                owns=("affected-customer impact", "outage scope impact"),
                depends_on=("network.access_path",),
            ),
            SOTService(
                name="network.device_groups",
                module="app.services.network.device_groups",
                owns=(
                    "network device group mutations",
                    "device group membership",
                    "device group bulk action queueing",
                ),
                depends_on=("network.identity",),
            ),
            SOTService(
                name="network.ip_pool_utilization",
                module="app.services.ip_pool_utilization_snapshot",
                owns=(
                    "IP pool utilization snapshot capture and retention",
                    "IP pool utilization history reads",
                    "live IP pool used/total report counts",
                ),
                notes=(
                    "Snapshot rows are point-in-time capacity observations; "
                    "the live report counts are counted address rows. Both "
                    "definitions live here so readers do not maintain "
                    "parallel counting paths."
                ),
            ),
            SOTService(
                name="network.outage_lifecycle",
                module="app.services.topology.outage",
                owns=(
                    "persisted outage incident status vocabulary",
                    "outage incident lifecycle",
                    "outage event emission and escalation planning",
                ),
                depends_on=(
                    "network.outage_impact",
                    "events.dispatcher",
                ),
            ),
            SOTService(
                name="network.connection_health",
                module="app.services.topology.connection_status",
                owns=(
                    "customer-safe connection health vocabulary",
                    "customer-safe last-mile and area-outage verdict",
                    "customer connection headline, message, and advice",
                ),
                depends_on=(
                    "network.access_path",
                    "network.radius_sessions",
                    "network.outage_impact",
                    "network.outage_lifecycle",
                ),
                notes=(
                    "This customer diagnostic vocabulary is separate from "
                    "network.device_state and raw RADIUS session observations."
                ),
            ),
        ),
        entrypoints=(
            "app.services.topology.*",
            "app.services.infrastructure_*",
            "app.services.router_management.*",
            "app.tasks.network_*",
            "app.tasks.router_sync",
            "scripts.network.audit_fiber_topology",
            "scripts.network.review_fiber_topology_identity",
            "scripts.network.review_fiber_topology_connectivity",
            "scripts.network.stage_fiber_topology_kmz",
            "app.web.admin.network_*",
            "app.web.customer.connection",
            "app.api.me",
            "app.services.reseller_portal",
            "mobile",
        ),
        rule=(
            "Pollers and map collectors write observations; the fiber-topology "
            "owner validates identity and connectivity; network resolvers decide "
            "state; event services decide consequences."
        ),
    ),
    DomainSOT(
        domain="subscriber_sessions",
        services=(
            SOTService(
                name="sessions.radius_reconciliation",
                module="app.services.radius_session_reconcile",
                owns=(
                    "external radacct open-session discovery",
                    "RADIUS active-session mirror writes",
                    "live-session mirror pruning",
                ),
                depends_on=("network.identity",),
            ),
            SOTService(
                name="sessions.radius_accounting_health",
                module="app.services.radius_accounting_health",
                owns=(
                    "RADIUS accounting source freshness policy",
                    "accounting source health classification",
                ),
                depends_on=("control.domain_settings", "runtime.db_sessions"),
            ),
            SOTService(
                name="sessions.radius_resolution",
                module="app.services.network.radius_sessions",
                owns=("customer online-now resolution", "primary NAS session"),
                depends_on=("sessions.radius_reconciliation", "network.identity"),
            ),
            SOTService(
                name="sessions.enforcement",
                module="app.services.enforcement",
                owns=(
                    "CoA/disconnect execution",
                    "session refresh after access-state changes",
                ),
                depends_on=(
                    "financial.access_resolution",
                    "sessions.radius_resolution",
                ),
            ),
        ),
        entrypoints=(
            "app.tasks.radius",
            "app.tasks.enforcement",
            "app.services.events.handlers.enforcement",
            "app.web.admin.network_radius",
            "app.services.web_customer_details",
        ),
        rule=(
            "RADIUS accounting imports write session facts; session resolvers "
            "answer online state; enforcement applies disconnect/CoA outcomes."
        ),
    ),
    DomainSOT(
        domain="application_sessions",
        services=(
            SOTService(
                name="app_sessions.store",
                module="app.services.session_store",
                owns=(
                    "Redis-backed session storage",
                    "session principal indexes",
                    "session revocation epochs",
                ),
            ),
            SOTService(
                name="app_sessions.customer_portal",
                module="app.services.customer_portal_session",
                owns=(
                    "customer portal session creation",
                    "customer portal session refresh/revoke",
                    "impersonation/read-only portal session policy",
                ),
                depends_on=("app_sessions.store", "customer.identity_scope"),
            ),
            SOTService(
                name="app_sessions.auth",
                module="app.services.session_manager",
                owns=(
                    "database auth-session listing",
                    "database auth-session revocation",
                ),
                depends_on=("app_sessions.store",),
            ),
        ),
        entrypoints=(
            "app.web.customer.auth",
            "app.web.customer.routes",
            "app.api.auth",
            "app.web.admin.auth",
        ),
        rule=(
            "Routes authenticate and authorize; session services own storage, "
            "refresh, listing, revocation, and impersonation session policy."
        ),
    ),
    DomainSOT(
        domain="secrets_credentials",
        services=(
            SOTService(
                name="secrets.reference_store",
                module="app.services.secrets",
                owns=(
                    "secret reference parsing and resolution",
                    "OpenBao read/write boundary",
                    "bounded secret cache lifecycle",
                ),
            ),
            SOTService(
                name="secrets.settings_policy",
                module="app.services.domain_settings",
                owns=(
                    "secret setting classification",
                    "secret setting reference persistence",
                ),
                depends_on=("secrets.reference_store",),
            ),
            SOTService(
                name="secrets.credential_crypto",
                module="app.services.credential_crypto",
                owns=(
                    "database credential encryption",
                    "credential field inventory",
                    "current and previous decryption key resolution",
                ),
                depends_on=("secrets.reference_store",),
            ),
            SOTService(
                name="secrets.access_credential_format",
                module="app.services.access_credential_secret",
                owns=(
                    "access credential representation classification",
                    "one-way RADIUS hash preservation policy",
                    "explicit cleartext marker normalization",
                ),
            ),
            SOTService(
                name="secrets.credential_integrity",
                module="app.services.credential_key_rotation",
                owns=(
                    "credential integrity classification",
                    "plaintext credential remediation",
                    "credential integrity observability projection",
                    "credential re-encryption convergence",
                ),
                depends_on=(
                    "secrets.access_credential_format",
                    "secrets.credential_crypto",
                    "observability.recording",
                    "runtime.db_sessions",
                ),
            ),
            SOTService(
                name="secrets.rotation",
                module="app.services.credential_rotation_schedule",
                owns=(
                    "scheduled credential key lifecycle",
                    "rotation grace period",
                ),
                depends_on=(
                    "secrets.reference_store",
                    "secrets.credential_integrity",
                    "runtime.db_sessions",
                ),
            ),
            SOTService(
                name="secrets.credential_recovery",
                module="app.services.credential_lifecycle_cleanup",
                owns=(
                    "lost-key credential recovery planning",
                    "lifecycle-safe unrecoverable credential cleanup",
                    "reviewed cleanup plan digest enforcement",
                ),
                depends_on=(
                    "secrets.credential_integrity",
                    "network.identity",
                    "network.radius_sessions",
                    "access.radius_state",
                    "runtime.db_sessions",
                    "observability.recording",
                ),
            ),
            SOTService(
                name="secrets.settings_migration",
                module="app.services.settings_secret_cleanup",
                owns=(
                    "noncanonical secret-setting discovery",
                    "OpenBao secret-setting migration",
                    "secret-setting reference replacement",
                ),
                depends_on=(
                    "secrets.reference_store",
                    "secrets.settings_policy",
                    "secrets.credential_crypto",
                ),
            ),
        ),
        entrypoints=(
            "app.tasks.security",
            "app.web.admin.system",
            "scripts.one_off.migrate_secret_settings_to_openbao",
            "app.services.*",
        ),
        rule=(
            "Bootstrap secrets use environment or mounted files; application "
            "secrets use references; high-cardinality credentials use the "
            "declared encrypted-field inventory. Callers never choose storage."
        ),
    ),
    DomainSOT(
        domain="notifications_communications",
        services=(
            SOTService(
                name="communications.channel_policy",
                module="app.services.notification_channel_policy",
                owns=("channel eligibility", "channel preference resolution"),
            ),
            SOTService(
                name="communications.customer_policy",
                module="app.services.customer_notification_policy",
                owns=("customer notification eligibility",),
                depends_on=("customer.identity_scope",),
            ),
            SOTService(
                name="communications.event_policy",
                module="app.services.event_notification_policy",
                owns=(
                    "event notification enablement",
                    "balance notification suppression",
                ),
                depends_on=("communications.channel_policy",),
            ),
            SOTService(
                name="communications.eligibility",
                module="app.services.communication_eligibility",
                owns=(
                    "recipient suppression ledger",
                    "transactional versus marketing send eligibility",
                ),
            ),
            SOTService(
                name="communications.intents",
                module="app.services.communication_intents",
                owns=(
                    "communication intent lifecycle",
                    "recipient and channel delivery expansion",
                    "intent delivery outcome projection",
                ),
                depends_on=(
                    "communications.channel_policy",
                    "communications.customer_policy",
                    "communications.eligibility",
                    "communications.notification_service",
                ),
            ),
            SOTService(
                name="communications.ephemeral_actions",
                module="app.services.ephemeral_communication_actions",
                owns=(
                    "typed non-secret ephemeral communication action envelope",
                    "just-in-time sensitive message materialization orchestration",
                    "secret-free transport outcome persistence contract",
                ),
                depends_on=(
                    "communications.intents",
                    "communications.eligibility",
                    "communications.notification_service",
                ),
                notes=(
                    "Calling domains own capability purpose, claims, lifetime, "
                    "and consequences. The communications worker materializes an "
                    "allowlisted action immediately before transport and never "
                    "persists or logs its rendered bearer content."
                ),
            ),
            SOTService(
                name="communications.notification_service",
                module="app.services.notification",
                owns=("notification row lifecycle", "delivery state"),
                depends_on=(
                    "communications.channel_policy",
                    "communications.event_policy",
                ),
            ),
            SOTService(
                name="communications.customer_read_state",
                module="app.services.customer_portal_notifications",
                owns=(
                    "customer notification read/unread state",
                    "customer notification unread counts",
                    "legacy device read-state migration boundary",
                ),
                depends_on=(
                    "customer.identity_scope",
                    "communications.customer_policy",
                    "communications.notification_service",
                ),
            ),
            SOTService(
                name="communications.staff_notifications",
                module="app.services.staff_notifications",
                owns=("admin/staff notification creation",),
                depends_on=("communications.notification_service",),
            ),
            SOTService(
                name="communications.campaigns",
                module="app.services.comms_campaigns",
                owns=(
                    "native communication campaign lifecycle",
                    "campaign sender and sequence lifecycle",
                    "campaign audience and recipient delivery state",
                ),
                depends_on=(
                    "communications.eligibility",
                    "communications.intents",
                    "communications.team_inbox_campaigns",
                ),
                notes=(
                    "Owns Sub outbound communication campaigns, not external "
                    "advertising campaigns. External provider campaign IDs are "
                    "lead-origin provenance owned by sales.lead_lifecycle."
                ),
            ),
            SOTService(
                name="communications.team_inbox",
                module="app.services.team_inbox_commands",
                owns=(
                    "conversation collaboration",
                    "conversation assignment",
                    "inbox reply and contact-link workflows",
                    "inbound channel ingestion",
                    "admin inbox mutation transactions",
                    "InboxContactLink canonical contact-point routing projection",
                ),
                depends_on=(
                    "party.registry",
                    "customer.identity_scope",
                    "communications.channel_policy",
                    "communications.notification_service",
                ),
            ),
            SOTService(
                name="communications.team_inbox_campaigns",
                module="app.services.team_inbox_campaigns",
                owns=(
                    "campaign-sourced inbox conversation materialization",
                    "campaign outbound inbox message rows",
                ),
                depends_on=("communications.team_inbox",),
                notes=(
                    "Campaigns decide audience, sequence, and content; inbox "
                    "rows stay inside the team-inbox family. comms_campaigns "
                    "requests materialization here instead of writing inbox "
                    "ORM rows itself."
                ),
            ),
        ),
        entrypoints=(
            "app.services.events.handlers.notification",
            "app.tasks.notifications",
            "app.api.me",
            "app.web.customer.routes",
            "app.web.admin.notifications",
            "app.web.admin.inbox",
            "app.services.team_inbox_*",
        ),
        rule=(
            "Domain services request communication outcomes; channel choice, "
            "notification rows, and recipient read state stay inside "
            "communication services. Admin inbox mutation routes delegate to "
            "the committed team-inbox command boundary."
        ),
    ),
    DomainSOT(
        domain="events_webhooks",
        services=(
            SOTService(
                name="events.dispatcher",
                module="app.services.events.dispatcher",
                owns=("event routing", "handler orchestration"),
                depends_on=("control.relationships",),
            ),
            SOTService(
                name="events.store",
                module="app.services.event_store",
                owns=("event persistence", "handler attempt tracking"),
                depends_on=("events.dispatcher",),
            ),
            SOTService(
                name="events.webhook_deliveries",
                module="app.services.webhook_deliveries",
                owns=("webhook delivery rows", "webhook queueing"),
                depends_on=("events.dispatcher",),
            ),
        ),
        entrypoints=(
            "app.services.events.handlers.*",
            "app.tasks.webhooks",
            "app.web.admin.integrations",
        ),
        rule=(
            "Handlers orchestrate; persistence, retry, and delivery bookkeeping "
            "live in event/webhook services."
        ),
    ),
    DomainSOT(
        domain="runtime_infrastructure",
        services=(
            SOTService(
                name="runtime.db_sessions",
                module="app.services.db_session_adapter",
                owns=(
                    "background DB session lifecycle",
                    "read/write task session boundaries",
                    "Postgres advisory lock ownership",
                ),
            ),
            SOTService(
                name="runtime.task_idempotency",
                module="app.services.task_idempotency",
                owns=("task idempotency keys", "duplicate task suppression"),
                depends_on=("runtime.db_sessions",),
            ),
            SOTService(
                name="runtime.task_heartbeat",
                module="app.services.task_heartbeat",
                owns=("task success heartbeat", "single-flight skip streaks"),
                depends_on=("observability.recording",),
            ),
            SOTService(
                name="runtime.infrastructure_polling",
                module="app.services.infrastructure_polling",
                owns=(
                    "shared native reachability poll observations",
                    "generic network-device pollable predicate",
                    "poll heartbeat result counters",
                ),
                depends_on=("runtime.db_sessions",),
            ),
            SOTService(
                name="runtime.infrastructure_health",
                module="app.services.infrastructure_health",
                owns=(
                    "dependency health checks",
                    "Postgres/Redis/VM/Celery infrastructure status",
                ),
                depends_on=("runtime.db_sessions",),
            ),
        ),
        entrypoints=(
            "app.tasks.*",
            "app.main",
            "app.services.scheduler_config",
            "app.web.admin.system",
        ),
        rule=(
            "Infrastructure tasks use shared DB/session/lock and heartbeat "
            "helpers; polling writes observations while network/device resolvers "
            "interpret state."
        ),
    ),
    DomainSOT(
        domain="observability",
        services=(
            SOTService(
                name="observability.audit_log",
                module="app.services.audit",
                owns=(
                    "audit event persistence and queries",
                    "request audit payload redaction",
                    "staged and deferred audit recording",
                ),
            ),
            SOTService(
                name="observability.recording",
                module="app.services.observability",
                owns=(
                    "task/job run recording",
                    "operational findings",
                    "bounded state snapshot publication",
                ),
            ),
            SOTService(
                name="observability.channel_health_contracts",
                module="app.services.channel_health_contracts",
                owns=(
                    "sensitive channel monitoring activation",
                    "channel active-window interpretation",
                    "natural and synthetic silence thresholds",
                    "channel alert severity contract",
                ),
                depends_on=(
                    "communications.team_inbox",
                    "observability.recording",
                ),
            ),
            SOTService(
                name="observability.task_reliability",
                module="app.services.task_reliability",
                owns=("task reliability classification", "stale-run alerts"),
                depends_on=("observability.recording",),
            ),
            SOTService(
                name="observability.metrics",
                module="app.metrics",
                owns=(
                    "runtime counters",
                    "runtime gauges",
                    "state snapshot scrape export",
                ),
                depends_on=("observability.recording",),
            ),
        ),
        entrypoints=("app.tasks.*", "app.main", "app.services.*"),
        rule=(
            "Tasks and service loops record lifecycle through observability "
            "helpers instead of writing heartbeat/run state directly. Metrics "
            "collectors read counters or bounded snapshots; unbounded business "
            "queries run only in scheduled single-flight producers."
        ),
    ),
    DomainSOT(
        domain="support_operations",
        services=(
            SOTService(
                name="support.ticket_lifecycle",
                module="app.services.support",
                owns=(
                    "ticket status vocabulary",
                    "guarded ticket status transitions",
                    "ticket lifecycle timestamps and consequences",
                ),
            ),
            SOTService(
                name="support.ticket_configuration",
                module="app.services.support_ticket_settings",
                owns=(
                    "operator-visible ticket status subset",
                    "ticket priority and type options",
                    "ticket routing and SLA policy",
                ),
                depends_on=("support.ticket_lifecycle",),
                notes=(
                    "Configured status choices are constrained to the lifecycle "
                    "vocabulary and do not own semantic colors or tones."
                ),
            ),
            SOTService(
                name="support.ticket_bulk_commands",
                module="app.services.web_support_ticket_bulk",
                owns=(
                    "selected support-ticket bulk membership resolution",
                    "support-ticket bulk change normalization",
                    "support-ticket bulk update eligibility preview",
                    "support-ticket bulk confirmation drift detection",
                    "structured support-ticket bulk update outcomes",
                ),
                depends_on=(
                    "support.ticket_lifecycle",
                    "support.ticket_configuration",
                    "ui.bulk_action_contracts",
                ),
                notes=(
                    "Execution delegates each eligible mutation to "
                    "app.services.support.Tickets.update through Tickets.bulk_update "
                    "so SLA, automation, assignment, work-order, notification, "
                    "event, audit, and workqueue consequences have one owner."
                ),
            ),
        ),
        entrypoints=(
            "app.api.support",
            "app.api.me.support",
            "app.web.admin.support",
            "app.web.customer.support",
            "mobile",
        ),
        rule=(
            "Support adapters request ticket mutations through the ticket service. "
            "The lifecycle owner validates raw statuses; settings may expose a "
            "subset but cannot add states or define their semantic presentation."
        ),
    ),
    DomainSOT(
        domain="ai_advisory",
        services=(
            SOTService(
                name="ai.insights",
                module="app.services.ai_operations",
                owns=(
                    "AI insight rows",
                    "insight lifecycle: create, acknowledge, expire",
                    "per-scope AI intake configuration",
                ),
                notes=(
                    "The canonical writer of AIInsight. Generated insights "
                    "land here and nowhere else; AiIntakeConfig owns the "
                    "per-scope/channel decision to run AI at all. AI is "
                    "advisory: it never mutates domain state — acting on a "
                    "recommendation means calling the domain's declared "
                    "owner. See docs/designs/AI_SOT.md."
                ),
            ),
            SOTService(
                name="ai.generation",
                module="app.services.ai.engine",
                owns=(
                    "the report-advisory generation path",
                    "advisor lookup, token budget, and prompt assembly",
                ),
                depends_on=("ai.insights",),
                notes=(
                    "advise() takes the CALLER's owned report projection and "
                    "never queries a domain model, so the AI boundary holds by "
                    "construction. It persists only through ai.insights (the "
                    "single AIInsight writer). Behind the default-OFF "
                    "ai.generation control. Called on demand from the admin "
                    "report surface (app.web.admin.reports)."
                ),
            ),
        ),
        entrypoints=(
            "app.api.ai_operations",
            "app.tasks.ai_operations",
            "app.web.admin.reports",
        ),
        rule=(
            "AI observes, derives, and recommends; it never decides domain "
            "state. Insight consequences are requested from the owning domain "
            "service, which applies its own guards, events, and audit. No "
            "app/services/ai* module writes a non-AI ORM row."
        ),
    ),
    DomainSOT(
        domain="provisioning_operations",
        services=(
            SOTService(
                name="operations.provisioning_context",
                module="app.services.provisioning_context",
                owns=("subscriber provisioning context", "ONT/CPE service link"),
                depends_on=("customer.identity_scope", "network.access_path"),
            ),
            SOTService(
                name="operations.provisioning_workflow",
                module="app.services.provisioning_managers",
                owns=("provisioning workflow execution", "provisioning step state"),
                depends_on=("operations.provisioning_context",),
            ),
            SOTService(
                name="operations.work_order_status",
                module="app.services.field.work_order_status",
                owns=(
                    "persisted work-order status vocabulary",
                    "open, assignable, and terminal work-order status sets",
                ),
            ),
            SOTService(
                name="operations.work_orders",
                module="app.services.work_order_views",
                owns=("work-order read models", "customer work-order linkage"),
                depends_on=(
                    "customer.identity_scope",
                    "operations.work_order_status",
                ),
                notes=(
                    "This registration owns reads only. Native work-order create, "
                    "assignment, and assignment-queue mutation still run through "
                    "dispatch CRUD without a named SOT mutation owner. New "
                    "cross-domain actions must not adopt that path as authority; "
                    "the writer boundary requires an explicit decision."
                ),
            ),
            SOTService(
                name="operations.field_completion",
                module="app.services.field.transitions",
                owns=(
                    "field job completion eligibility",
                    "field completion evidence requirements",
                    "field job completion transitions",
                ),
                depends_on=(
                    "operations.work_orders",
                    "operations.work_order_status",
                    "control.domain_settings",
                ),
                notes=(
                    "Authenticated field job detail projects the same completion "
                    "requirements consumed by transition validation. Field clients "
                    "do not reconstruct this policy."
                ),
            ),
            SOTService(
                name="operations.project_lifecycle",
                module="app.services.projects",
                owns=(
                    "native project field and status mutations",
                    "project SLA clock synchronization",
                    "project lifecycle event and notification requests",
                ),
                depends_on=(
                    "events.dispatcher",
                    "communications.staff_notifications",
                ),
                notes=(
                    "Customer and reseller read authority remains controlled by "
                    "projects.native_read until the CRM mirror cutover is complete."
                ),
            ),
        ),
        entrypoints=(
            "app.services.events.handlers.provisioning",
            "app.tasks.ont_provisioning",
            "app.web.admin.provisioning",
            "app.web.admin.projects",
            "app.api.projects",
            "app.api.field.*",
            "app.services.web_projects",
            "app.services.web_dispatch_work_orders",
            "field_mobile",
        ),
        rule=(
            "Provisioning callers resolve customer/network context through the "
            "shared context layer before executing workflow steps. Native project "
            "mutation adapters delegate to Projects.update for lifecycle consequences. "
            "Field clients consume completion_requirements from authenticated job "
            "detail and leave completion eligibility to the field transition service."
        ),
    ),
    DomainSOT(
        domain="feature_control_plane",
        services=(
            SOTService(
                name="control.feature_registry",
                module="app.services.control_registry",
                owns=(
                    "module/feature/safety control resolution",
                    "legacy feature-flag alias mapping",
                    "feature-to-module composition",
                ),
                depends_on=("control.module_manager", "control.domain_settings"),
            ),
            SOTService(
                name="control.module_manager",
                module="app.services.module_manager",
                owns=("product module enablement", "module labels and feature states"),
            ),
            SOTService(
                name="control.domain_settings",
                module="app.services.domain_settings",
                owns=("domain setting persistence", "setting update validation"),
            ),
            SOTService(
                name="control.settings_spec",
                module="app.services.settings_spec",
                owns=(
                    "setting schema and validation bounds",
                    "setting value coercion",
                    "DB-authoritative runtime setting resolution",
                    "registered setting defaults",
                ),
                depends_on=("control.domain_settings",),
                notes=(
                    "Runtime precedence is Redis cache, active database row, then "
                    "the registered default. SettingSpec.env_var is bootstrap and "
                    "migration metadata, never an implicit live override."
                ),
            ),
            SOTService(
                name="control.settings_bootstrap",
                module="app.services.settings_seed",
                owns=(
                    "startup default-setting materialization",
                    "environment-to-setting bootstrap",
                    "default notification-template seeding",
                ),
                depends_on=("control.domain_settings", "control.settings_spec"),
                notes=(
                    "Environment inputs are materialized one way into stored "
                    "settings and do not override runtime database decisions."
                ),
            ),
            SOTService(
                name="control.relationships",
                module="app.services.control_relationships",
                owns=(
                    "setting exclusivity and migration-chain validation",
                    "event handler stage and capability ownership",
                    "control relationship diagnostics",
                ),
                depends_on=("control.domain_settings", "control.settings_spec"),
            ),
        ),
        entrypoints=(
            "app.services.scheduler_config",
            "app.tasks.*",
            "app.web.admin.system",
            "app.api.settings",
        ),
        rule=(
            "Settings are inputs, not decision owners. Callers ask the named "
            "owner or resolver for a decision; they do not independently compose "
            "module, environment, database, and legacy state. Business and "
            "operational tuning is database-authoritative unless a separately "
            "registered, visible emergency override says otherwise."
        ),
    ),
    DomainSOT(
        domain="authorization_control_plane",
        services=(
            SOTService(
                name="auth.permission_gate",
                module="app.services.auth_dependencies",
                owns=(
                    "route permission dependencies",
                    "request principal permission checks",
                ),
                depends_on=("auth.rbac",),
            ),
            SOTService(
                name="auth.rbac",
                module="app.services.rbac",
                owns=("roles", "permissions", "role/user assignments"),
            ),
            SOTService(
                name="auth.token_signing",
                module="app.services.context_signing",
                owns=(
                    "configured JWT signing key and algorithm resolution",
                    "cryptographic signing and verification of typed capability envelopes",
                ),
                notes=(
                    "Calling domain owners define token purpose, claims, lifetime, "
                    "and authorization consequences. Auth owns only the signed "
                    "envelope and never turns a domain capability into identity proof."
                ),
            ),
            SOTService(
                name="auth.customer_credential_enrollment",
                module="app.services.customer_credential_enrollment",
                owns=(
                    "referral-created customer local credential enrollment",
                    "credential enrollment capability purpose claims and lifetime",
                    "single-use enrollment and account email verification consequence",
                ),
                depends_on=(
                    "auth.token_signing",
                    "customer.accounts",
                    "referrals.account_conversion",
                    "communications.ephemeral_actions",
                    "observability.audit_log",
                ),
                notes=(
                    "Creates no placeholder credential. The local credential and "
                    "Subscriber email verification are committed together only "
                    "after the emailed capability is redeemed. Party quarantine, "
                    "Party contact verification, and account/subscription state "
                    "remain with their existing owners."
                ),
            ),
            SOTService(
                name="auth.staff_provisioning",
                module="app.services.staff_provisioning",
                owns=("staff account provisioning", "staff identity bootstrap"),
                depends_on=("auth.rbac",),
            ),
        ),
        entrypoints=(
            "app.api.*",
            "app.web.admin.*",
            "app.web.auth.*",
            "app.web.customer.auth",
        ),
        rule=(
            "Routes declare permission requirements; RBAC services own role and "
            "permission mutation. Business services should receive an authorized "
            "principal, not perform route-level permission wiring."
        ),
    ),
    DomainSOT(
        domain="scheduler_control_plane",
        services=(
            SOTService(
                name="scheduler.registry",
                module="app.services.scheduler_config",
                owns=(
                    "effective scheduled-task registration",
                    "task toggle synchronization",
                    "Celery runtime schedule config",
                ),
                depends_on=("control.feature_registry", "runtime.db_sessions"),
            ),
            SOTService(
                name="scheduler.operations",
                module="app.services.scheduler",
                owns=("ScheduledTask CRUD", "manual task enqueue operations"),
                depends_on=("scheduler.registry",),
            ),
            SOTService(
                name="scheduler.worker_control",
                module="app.services.worker_control",
                owns=("worker restart targets", "worker control actions"),
                depends_on=("scheduler.registry",),
            ),
        ),
        entrypoints=("app.tasks.*", "app.web.admin.system", "app.main"),
        rule=(
            "Task cadence and enablement flow through scheduler config and the "
            "feature control plane; task bodies execute work and report status."
        ),
    ),
    DomainSOT(
        domain="network_access_control_plane",
        services=(
            SOTService(
                name="access.subscription_lifecycle",
                module="app.services.account_lifecycle",
                owns=(
                    "enforcement lock lifecycle",
                    "persisted access restriction intent",
                    "subscription access-status transitions",
                    "subscriber access-status projection",
                ),
                depends_on=("events.dispatcher",),
            ),
            SOTService(
                name="access.control_resolution",
                module="app.services.access_resolution",
                owns=(
                    "access-state command resolution",
                    "billable-service access eligibility",
                ),
                depends_on=("financial.billing_profile",),
            ),
            SOTService(
                name="access.event_policy",
                module="app.services.enforcement_event_policy",
                owns=(
                    "event-driven enforcement feature policy",
                    "FUP enforcement action settings",
                    "overdue suspension event policy",
                ),
                depends_on=("control.settings_spec",),
            ),
            SOTService(
                name="access.walled_garden_policy",
                module="app.services.walled_garden_policy",
                owns=(
                    "captive account eligibility",
                    "captive network readiness",
                    "effective hard-reject/captive restriction",
                    "most-restrictive-active-lock resolution",
                ),
                depends_on=(
                    "access.subscription_lifecycle",
                    "control.settings_spec",
                ),
            ),
            SOTService(
                name="access.radius_state",
                module="app.services.radius_access_state",
                owns=("desired RADIUS state mapping", "RADIUS group/profile actions"),
                depends_on=(
                    "access.control_resolution",
                    "access.walled_garden_policy",
                ),
            ),
            SOTService(
                name="access.radius_reject",
                module="app.services.radius_reject",
                owns=("reject address allocation", "reject IP lifecycle"),
                depends_on=("access.radius_state",),
            ),
            SOTService(
                name="access.radius_target_registry",
                module="app.services.external_radius_targets",
                owns=(
                    "configured external RADIUS database target selection",
                    "per-target capability and schema configuration",
                    "legacy environment bootstrap and cutover verification",
                ),
                depends_on=("control.settings_spec", "runtime.db_sessions"),
                notes=(
                    "Active RadiusSyncJob and encrypted ConnectorConfig rows are "
                    "the runtime authority. The environment DSN is bootstrap and "
                    "cutover-shadow input only, never a runtime fallback."
                ),
            ),
            SOTService(
                name="access.radius_projection",
                module="app.services.radius_population",
                owns=(
                    "radcheck/radreply/radusergroup customer projection",
                    "radcheck_admin/radreply_admin device-login projection",
                    "idempotent per-target advisory-locked RADIUS auth projection",
                    "walled-garden/reject radreply on blocked/suspended access",
                ),
                depends_on=(
                    "access.radius_state",
                    "access.radius_reject",
                    "access.radius_target_registry",
                ),
                notes=(
                    "Single writer of the FreeRADIUS auth tables across every "
                    "configured runtime target. Event-time and per-user callers "
                    "request a full or scoped projection; they do not write auth "
                    "tables directly."
                ),
            ),
            SOTService(
                name="access.session_enforcement",
                module="app.services.enforcement",
                owns=("access-state CoA/disconnect execution",),
                depends_on=("access.radius_state", "sessions.radius_resolution"),
            ),
        ),
        entrypoints=(
            "app.services.events.handlers.enforcement",
            "app.tasks.enforcement",
            "app.services.collections.*",
            "app.services.usage",
        ),
        rule=(
            "Billing, FUP, and admin actions resolve the desired access outcome "
            "once, map it to RADIUS state once, then let enforcement apply the "
            "network-side change."
        ),
    ),
    DomainSOT(
        domain="service_intent_control_plane",
        services=(
            SOTService(
                name="service_intent.catalog_policy",
                module="app.services.catalog.policies",
                owns=("catalog policy lookup", "offer policy interpretation"),
            ),
            SOTService(
                name="service_intent.catalog_validation",
                module="app.services.catalog.validation",
                owns=("catalog mutation validation", "offer/profile consistency"),
                depends_on=("service_intent.catalog_policy",),
            ),
            SOTService(
                name="service_intent.catalog_billing_governance",
                module="app.services.catalog_billing_governance",
                owns=(
                    "billing-critical catalog mutation policy",
                    "live pricing and cadence immutability",
                    "billing catalog audit and operator alerting",
                ),
                depends_on=(
                    "service_intent.catalog_validation",
                    "auth.permission_gate",
                    "observability.recording",
                ),
            ),
            SOTService(
                name="service_intent.subscription_nas_assignment",
                module="app.services.catalog.subscriptions",
                owns=(
                    "subscription provisioning NAS assignment",
                    "nonterminal services grouped by NAS",
                ),
                depends_on=("service_intent.catalog_policy",),
            ),
            SOTService(
                name="service_intent.subscription_billing_cadence",
                module="app.services.catalog.subscriptions",
                owns=(
                    "subscription billing cadence",
                    "subscription cadence resolution "
                    "(subscription -> offer price -> monthly)",
                    "next-billing anchor computation",
                ),
                depends_on=("service_intent.catalog_policy",),
                notes=(
                    "The subscription is the source of truth for a customer's "
                    "contracted billing cadence, captured from the sales-order "
                    "line and read by billing_automation. The offer/version "
                    "price cadence is fallback-only when the subscription's is "
                    "unset. Catalog offer-cadence immutability stays with "
                    "service_intent.catalog_billing_governance."
                ),
            ),
            SOTService(
                name="service_intent.subscription_lifecycle",
                module="app.services.subscription_lifecycle",
                owns=(
                    "subscription lifecycle state projection",
                    "subscription command eligibility and preview",
                    "billing and access impact projection",
                    "subscription command and outcome contracts",
                ),
                depends_on=(
                    "service_intent.catalog_policy",
                    "financial.access_resolution",
                    "financial.prepaid_plan_change",
                    "access.radius_state",
                ),
                notes=(
                    "Execution remains with the established billing, account "
                    "lifecycle, catalog, and RADIUS owners. UI, API, scheduled, "
                    "and bulk callers consume this preview before execution."
                ),
            ),
            SOTService(
                name="service_intent.subscription_lifecycle_execution",
                module="app.services.subscription_lifecycle_commands",
                owns=(
                    "single-subscription command orchestration",
                    "subscription command locking and reviewed-head enforcement",
                    "subscription command idempotent replay",
                    "structured subscription command outcomes",
                    "independently committed subscription command batches",
                ),
                depends_on=(
                    "service_intent.subscription_lifecycle",
                    "service_intent.catalog_policy",
                    "financial.prepaid_plan_change",
                    "access.radius_state",
                ),
                notes=(
                    "Delegates mutations and side effects to the established "
                    "account lifecycle, catalog, billing, scheduler, and RADIUS "
                    "owners. Renewal execution remains billing-owned and fails "
                    "closed. Deferred status execution is owned by "
                    "service_intent.subscription_lifecycle_scheduling. Admin "
                    "single and bulk adapters delegate here instead of writing "
                    "subscription lifecycle fields directly."
                ),
            ),
            SOTService(
                name="service_intent.subscription_lifecycle_scheduling",
                module="app.services.subscription_lifecycle_schedules",
                owns=(
                    "durable deferred subscription status intent",
                    "deferred command execution leases and bounded retry",
                    "scheduled lifecycle cancellation",
                    "deferred lifecycle execution evidence",
                ),
                depends_on=(
                    "service_intent.subscription_lifecycle",
                    "service_intent.subscription_lifecycle_execution",
                    "scheduler.registry",
                ),
                notes=(
                    "Revalidates the reviewed subscription head at execution "
                    "time and delegates every mutation to the canonical command "
                    "executor. Plan scheduling remains with the catalog change "
                    "request owner."
                ),
            ),
            SOTService(
                name="service_intent.ont",
                module="app.services.network.ont_service_intent",
                owns=("ONT service intent projection",),
            ),
        ),
        entrypoints=(
            "app.services.provisioning_*",
            "app.tasks.tr069.*",
            "app.web.admin.catalog",
            "app.web.admin.provisioning",
        ),
        rule=(
            "Catalog policy and subscription services define commercial intent; "
            "network owners project configured intent without a parallel adapter."
        ),
    ),
    DomainSOT(
        domain="integration_control_plane",
        services=(
            SOTService(
                name="integration.registry",
                module="app.services.integrations.registry",
                owns=("integration connector registry", "connector capabilities"),
            ),
            SOTService(
                name="integration.jobs",
                module="app.services.integration",
                owns=("integration targets", "integration jobs", "integration runs"),
                depends_on=("integration.registry",),
            ),
            SOTService(
                name="integration.sync",
                module="app.services.integration_sync",
                owns=("integration sync orchestration", "sync run lifecycle"),
                depends_on=("integration.jobs",),
            ),
            SOTService(
                name="integration.hooks",
                module="app.services.integration_hooks",
                owns=("integration hook dispatch", "hook subscriptions"),
                depends_on=("events.dispatcher", "integration.registry"),
            ),
        ),
        entrypoints=(
            "app.web.admin.integrations",
            "app.api.*_webhooks",
            "app.tasks.integrations",
            "app.services.events.handlers.integration_hook",
        ),
        rule=(
            "Integration routes and webhooks validate and enqueue; registry, job, "
            "sync, and hook services own connector behavior and delivery flow."
        ),
    ),
    DomainSOT(
        domain="ui_list_projection",
        services=(
            SOTService(
                name="ui.list_contracts",
                module="app.services.list_query",
                owns=(
                    "list query normalization",
                    "page metadata derivation",
                    "canonical list URL serialization",
                    "list capability declarations",
                ),
            ),
            SOTService(
                name="ui.form_contracts",
                module="app.services.form_contracts",
                owns=(
                    "editor/form contract vocabulary",
                    "rendered prerequisite and consequence disclosure shape",
                ),
                notes=(
                    "Declarative contract for editor pages per the UI "
                    "information/action standard: current vs proposed state, "
                    "prerequisites near the control, impact preview, named "
                    "consequences. The owning domain service evaluates "
                    "prerequisites and computes impact; the command owner "
                    "re-checks everything at execution — the rendered contract "
                    "is disclosure, never enforcement. Pilot consumer: the "
                    "customer plan-change editor (PLAN_CHANGE_FORM in "
                    "customer_portal_flow_changes)."
                ),
            ),
            SOTService(
                name="ui.customer_list_projection",
                module="app.services.web_customer_lists",
                owns=(
                    "admin customer searchable fields",
                    "admin customer filter semantics",
                    "admin customer stable sort semantics",
                    "admin customer row and page projection",
                    "legacy customer offset API compatibility mapping",
                ),
                depends_on=("ui.list_contracts",),
            ),
            SOTService(
                name="ui.subscriber_list_projection",
                module="app.services.web_subscriber_lists",
                owns=(
                    "subscriber table searchable fields",
                    "subscriber table filter semantics",
                    "subscriber table stable sort semantics",
                    "subscriber table page projection",
                    "legacy subscriber offset API compatibility mapping",
                ),
                depends_on=("ui.list_contracts",),
                notes=(
                    "Subscriber scope and full-text search delegate to "
                    "app.services.subscriber.Subscribers.query. List reads never "
                    "generate or persist subscriber identifiers."
                ),
            ),
            SOTService(
                name="ui.invoice_list_projection",
                module="app.services.web_billing_overview",
                owns=(
                    "admin invoice searchable fields",
                    "admin invoice filter semantics",
                    "admin invoice stable sort semantics",
                    "admin invoice page and status-summary projection",
                    "admin invoice export scope",
                ),
                depends_on=("ui.list_contracts", "financial.invoices"),
                notes=(
                    "The full page and HTMX response share one list partial. "
                    "Exports consume the same canonical scope without a page cap."
                ),
            ),
            SOTService(
                name="ui.payments_list_projection",
                module="app.services.web_billing_payments",
                owns=(
                    "admin payments searchable fields",
                    "admin payments filter semantics",
                    "admin payments stable sort and default-order semantics",
                    "admin payments list pagination normalization",
                ),
                depends_on=("ui.list_contracts", "financial.payments"),
                notes=(
                    "PAYMENTS_LIST_DEFINITION declares the list capabilities and "
                    "build_payments_list_query normalizes/validates request state; "
                    "build_payments_list_data remains the read owner that issues the "
                    "SQL, status totals, and enrichment. The route validates through "
                    "the contract and delegates. The CSV export intentionally reuses "
                    "the read owner without a page cap (same canonical filter scope, "
                    "no pagination). Gated by the existing granular "
                    "billing:payment:read. Read-only: no admin bulk command declared, "
                    "so no selection or bulk. Follow-up: decompose the read owner so "
                    "list and export share a hoisted filter helper."
                ),
            ),
            SOTService(
                name="ui.support_ticket_list_projection",
                module="app.services.web_support_tickets",
                owns=(
                    "admin support-ticket searchable fields",
                    "admin support-ticket filter semantics",
                    "admin support-ticket stable sort semantics",
                    "admin support-ticket page and status-summary projection",
                    "admin support-ticket export scope",
                ),
                depends_on=(
                    "ui.list_contracts",
                    "support.ticket_lifecycle",
                    "support.ticket_configuration",
                ),
                notes=(
                    "app.services.support.Tickets owns the canonical filtered "
                    "domain query. The web projection declares list capabilities, "
                    "normalizes request state, and renders full-page and HTMX "
                    "reads through one partial. Exports consume the same complete "
                    "scope without a silent row cap."
                ),
            ),
            SOTService(
                name="ui.reseller_list_projection",
                module="app.services.web_admin_resellers",
                owns=(
                    "admin reseller list filter and stable sort semantics",
                    "admin reseller list pagination normalization",
                ),
                depends_on=("ui.list_contracts",),
                notes=(
                    "web_admin_resellers owns the reseller read; this projection "
                    "declares the list capabilities (status filter, name sort, "
                    "pagination) so the route derives no pagination or filter rules. "
                    "The admin reseller surface is granularly gated by reseller:read "
                    "(list) and reseller:write (create/edit), split off the shared "
                    "customer:read/write."
                ),
            ),
            SOTService(
                name="ui.work_order_list_projection",
                module="app.services.web_dispatch_work_orders",
                owns=(
                    "admin work-order searchable fields",
                    "admin work-order status filter and stable sort semantics",
                    "admin work-order list pagination normalization",
                ),
                depends_on=(
                    "ui.list_contracts",
                    "operations.work_orders",
                ),
                notes=(
                    "work_order_views.query_work_orders owns the canonical filtered "
                    "and sorted work-order query; this projection declares list "
                    "capabilities, normalizes request state, and delegates the read "
                    "(it issues no SQL of its own). Read-only: work orders are a CRM "
                    "mirror with no Sub-owned admin bulk command, so no selection or "
                    "bulk is declared. Each dispatch route is granularly gated "
                    "(operations:dispatch:read/write/assign)."
                ),
            ),
            SOTService(
                name="ui.project_list_projection",
                module="app.services.web_projects",
                owns=(
                    "admin project searchable fields",
                    "admin project filter and stable sort semantics",
                    "admin project list pagination normalization",
                ),
                depends_on=(
                    "ui.list_contracts",
                    "operations.project_lifecycle",
                ),
                notes=(
                    "projects_service.projects.list (operations.project_lifecycle) "
                    "owns the canonical filtered/sorted project query; this "
                    "projection declares the list capabilities and normalizes "
                    "request state, then delegates the read. It issues no query of "
                    "its own. Gated by the existing granular project:read."
                ),
            ),
            SOTService(
                name="ui.audit_events_list_projection",
                module="app.services.web_system_audit",
                owns=(
                    "admin audit-log filterable fields",
                    "admin audit-log sort and default-order semantics",
                    "admin audit-log list pagination normalization",
                ),
                depends_on=(
                    "ui.list_contracts",
                    "observability.audit_log",
                ),
                notes=(
                    "audit_service.audit_events.list (observability.audit_log) owns "
                    "the canonical filtered/sorted audit query; this projection "
                    "declares the list capabilities (filter by actor/action/entity, "
                    "sort on occurred_at) and normalizes request state, then "
                    "delegates the read and count. It issues no query of its own. "
                    "Read-only: audit events are immutable observations with no admin "
                    "bulk command, so no selection or bulk is declared. Gated by the "
                    "existing granular audit:read."
                ),
            ),
            SOTService(
                name="ui.nas_list_projection",
                module="app.services.nas.web_builders",
                owns=(
                    "admin NAS dashboard searchable fields",
                    "admin NAS dashboard filter semantics",
                    "admin NAS dashboard sort and default-order semantics",
                    "admin NAS dashboard list pagination normalization",
                ),
                depends_on=("ui.list_contracts", "network.nas_inventory"),
                notes=(
                    "NAS_LIST_DEFINITION declares the list capabilities and "
                    "build_nas_list_query normalizes/validates request state; "
                    "build_nas_dashboard_data is the read owner. SQL-expressible "
                    "filters (vendor/nas_type/status/pop_site/search) paginate and "
                    "count in the database via NasDevices.list/count; partner_org_id "
                    "(tag) and olt_status (ping cache) are post-query filters that "
                    "page over a bounded in-memory scan (logged if the bound is hit) "
                    "rather than the prior unconditional 1000-row load-then-slice. "
                    "Gated by the router-level granular network:nas:read/write. "
                    "Read-only list: no admin bulk command declared."
                ),
            ),
            SOTService(
                name="ui.notification_list_projection",
                module="app.services.web_notifications",
                owns=(
                    "admin notification-template list searchable/filterable fields",
                    "admin notification-queue list filterable fields",
                    "admin notification-history list filterable fields",
                    "admin notification list sort and default-order semantics",
                    "admin notification list pagination normalization",
                ),
                depends_on=("ui.list_contracts", "communications.notification_service"),
                notes=(
                    "One projection owner for the three admin notification lists "
                    "(templates, queue, delivery history). "
                    "NOTIFICATION_{TEMPLATES,QUEUE,HISTORY}_LIST_DEFINITION declare "
                    "the per-list capabilities (search + filter channel/status, sort "
                    "name; filter status/channel, sort created_at; filter status, "
                    "sort occurred_at); templates_list_context / queue_context / "
                    "history_context normalize request state and delegate the read + "
                    "count to communications.notification_service. Gated by the "
                    "granular notification:read/notification:write (split off the "
                    "coarse system:read/write in migration 323). Read-only lists: "
                    "mutations have their own routes; no bulk selection declared."
                ),
            ),
            SOTService(
                name="ui.ip_address_list_projection",
                module="app.services.web_network_ip",
                owns=(
                    "admin IP-address list searchable/filterable fields",
                    "admin IP-address list sort and default-order semantics",
                    "admin IP-address list page-size normalization",
                ),
                depends_on=("ui.list_contracts",),
                notes=(
                    "IP_ADDRESS_LIST_DEFINITION declares the addresses-tab list "
                    "capabilities (search, filter by pool, sort by address) and "
                    "build_ip_address_list_query normalizes/validates request state; "
                    "build_ip_management_data remains the read owner. Gated by the "
                    "existing granular network:ip:read. The addresses list pages "
                    "across the concatenated IPv4-then-IPv6 ordering: the page window "
                    "is applied to the merged sequence (per-family offset/take), so a "
                    "page shows at most one page size and pages align across the two "
                    "families. Read-only list: no bulk selection declared."
                ),
            ),
            SOTService(
                name="ui.network_device_list_projection",
                module="app.services.web_network_core_devices_inventory",
                owns=(
                    "admin network-device list searchable/filterable fields",
                    "admin network-device list sort and default-order semantics",
                    "admin network-device list pagination normalization",
                ),
                depends_on=("ui.list_contracts", "network.device_projection"),
                notes=(
                    "NETWORK_DEVICE_LIST_DEFINITION declares the list capabilities "
                    "(search, filter type/status/vendor, sort name/last_seen) and "
                    "build_network_device_list_query normalizes request state; the "
                    "list reads the materialised device_projections table via "
                    "device_projection_views (SQL search/filter/sort/paginate), the "
                    "rebuildable read model owned by network.device_projection, "
                    "instead of aggregating every device in memory. Projected "
                    "operational_status is last-known state as of the projection's "
                    "refreshed_at, surfaced as freshness (not live truth); live "
                    "status stays on the monitoring/detail views. collect_devices is "
                    "retired from the request path and remains the reconciler's "
                    "derivation input. Gated by the existing granular "
                    "network:device:read. Read-only list: no bulk command declared."
                ),
            ),
        ),
        entrypoints=(
            "app.api.tables",
            "app.services.subscriber",
            "app.services.table_config",
            "app.web.admin.customers",
            "app.web.admin.billing_invoices",
            "app.web.admin.support_tickets",
            "templates.admin.billing.invoices",
            "templates.admin.customers",
            "templates.admin.support.tickets",
        ),
        rule=(
            "List routes normalize request parameters through one declared list "
            "contract. Owners filter before pagination and apply a stable unique "
            "tie-breaker. Compatibility APIs delegate row selection to a named "
            "resource owner and list reads do not mutate domain records. Templates "
            "consume ListQuery and PageMeta, preserve the canonical URL, and do not "
            "rebuild pagination or sort semantics."
        ),
    ),
    DomainSOT(
        domain="ui_bulk_actions",
        services=(
            SOTService(
                name="ui.bulk_action_contracts",
                module="app.services.bulk_actions",
                owns=(
                    "bulk selection mode normalization",
                    "bulk action capability presentation",
                    "bulk preview and confirmation declarations",
                    "bulk execution-mode presentation",
                ),
                depends_on=("ui.list_contracts",),
                notes=(
                    "These are read-side interaction contracts. Domain command "
                    "owners re-check permission, eligibility, scope, and impact "
                    "when executing a mutation."
                ),
            ),
            SOTService(
                name="ui.customer_bulk_action_projection",
                module="app.services.web_customer_bulk_actions",
                owns=(
                    "admin customer bulk action visibility",
                    "admin customer bulk selection presentation",
                    "admin customer filtered-selection promotion",
                ),
                depends_on=(
                    "ui.bulk_action_contracts",
                    "ui.customer_list_projection",
                ),
            ),
            SOTService(
                name="ui.invoice_bulk_action_projection",
                module="app.services.web_billing_invoice_bulk_actions",
                owns=(
                    "admin invoice bulk action visibility",
                    "admin invoice page-selection presentation",
                    "admin invoice bulk eligibility presentation",
                ),
                depends_on=(
                    "ui.bulk_action_contracts",
                    "ui.invoice_list_projection",
                    "financial.invoices",
                ),
                notes=(
                    "app.services.web_billing_invoice_bulk remains the command "
                    "eligibility, preview, mutation, audit, and outcome owner."
                ),
            ),
            SOTService(
                name="ui.support_ticket_bulk_action_projection",
                module="app.services.web_support_ticket_bulk_actions",
                owns=(
                    "admin support-ticket bulk action visibility",
                    "admin support-ticket page-selection presentation",
                    "admin support-ticket row eligibility presentation",
                ),
                depends_on=(
                    "ui.bulk_action_contracts",
                    "ui.support_ticket_list_projection",
                    "support.ticket_bulk_commands",
                ),
                notes=(
                    "Selection is page-only. The command owner previews exact "
                    "membership, proposed changes, and eligibility before execution."
                ),
            ),
        ),
        entrypoints=(
            "app.web.admin.customers",
            "app.web.admin.billing_invoice_bulk",
            "app.web.admin.billing_invoices",
            "app.web.admin.support_tickets",
            "app.services.web_customer_actions",
            "app.services.web_billing_invoice_bulk",
            "app.services.web_support_ticket_bulk",
            "templates.admin.billing.invoices",
            "templates.admin.customers",
            "templates.admin.support.tickets",
        ),
        rule=(
            "No selection means no bulk action. Page select-all selects only the "
            "visible page; all-filtered scope requires an explicit promotion. "
            "Adapters submit selected IDs or a canonical filtered query, and "
            "command owners resolve the scope again, require impact preview and "
            "confirmation, reject membership or eligibility drift, and report "
            "structured outcomes."
        ),
    ),
    DomainSOT(
        domain="ui_display_formatting",
        services=(
            SOTService(
                name="ui.display_formatting",
                module="app.services.display_format",
                owns=(
                    "display currency-code normalization",
                    "single-value money formatting",
                    "multi-currency summary grouping and ordering",
                    "display-timezone resolution",
                    "timestamp display formatting",
                    "missing-value display marker",
                ),
                depends_on=("control.settings_spec",),
                notes=(
                    "Domain services own amount, currency, unit, timestamp, and "
                    "missing-value facts. Web and mobile renderers consume this "
                    "projection and do not invent default currency or timezone."
                ),
            ),
        ),
        entrypoints=(
            "app.services.web_billing_overview",
            "app.services.web_billing_payments",
            "app.services.web_billing_ledger",
            "app.services.web_billing_reconciliation",
            "app.web.brand_globals",
            "mobile.lib.src.core.formatters",
        ),
        rule=(
            "Domain owners provide typed amount, currency, unit, timestamp, and "
            "availability facts. Display owners normalize and format them once. "
            "Mixed currencies remain separate and explicitly labeled; UI callers "
            "do not maintain local currency defaults or formatter copies."
        ),
    ),
    DomainSOT(
        domain="ui_action_forms",
        services=(
            SOTService(
                name="ui.action_form_contracts",
                module="app.services.action_forms",
                owns=(
                    "action visibility and disabled-reason projection",
                    "action impact and confirmation presentation",
                    "action field and option metadata",
                    "submitted action values and structured error binding",
                ),
                notes=(
                    "Domain command services still own authorization, eligibility, "
                    "validation, locking, execution, and audit consequences."
                ),
            ),
            SOTService(
                name="ui.payment_proof_review_projection",
                module="app.services.web_billing_payment_proofs",
                owns=(
                    "payment-proof review action visibility",
                    "payment-proof verify and reject form projection",
                    "payment-proof failed-submission presentation",
                ),
                depends_on=(
                    "ui.action_form_contracts",
                    "financial.payment_proofs",
                ),
            ),
        ),
        entrypoints=(
            "app.web.admin.billing_payment_proofs",
            "templates.admin.billing.payment_proof_detail",
            "templates.components.forms.action_form",
        ),
        rule=(
            "Action forms render owner-provided eligibility, impact, confirmation, "
            "declared fields, submitted values, and structured errors. Unauthorized "
            "actions are omitted. Routes remain adapters, and command owners lock "
            "and recheck permission and eligibility before mutation."
        ),
    ),
    DomainSOT(
        domain="ui_semantic_presentation",
        services=(
            SOTService(
                name="ui.status_presentation",
                module="app.services.status_presentation",
                owns=(
                    "account status labels, semantic tones, and icon keys",
                    "subscription status labels, semantic tones, and icon keys",
                    "invoice status labels, semantic tones, and icon keys",
                    "payment status labels, semantic tones, and icon keys",
                    "outage incident status labels, semantic tones, and icon keys",
                    "device operational status labels, semantic tones, and icon keys",
                    "customer connection health labels, semantic tones, and icon keys",
                    "RADIUS access-session observation labels, semantic tones, and icon keys",
                    "support-ticket status labels, semantic tones, and icon keys",
                    "field work-order status labels, semantic tones, and icon keys",
                    "status presentation fallback semantics",
                ),
                depends_on=(
                    "financial.invoices",
                    "financial.payments",
                    "network.device_state",
                    "network.connection_health",
                    "network.outage_lifecycle",
                    "support.ticket_lifecycle",
                    "operations.work_order_status",
                ),
                notes=(
                    "Domain services own lifecycle or derived operational state. "
                    "This read projection owns its cross-client semantic meaning; "
                    "customer.branding owns the concrete color behind each tone. "
                    "Clients render the tone through brand/theme tokens and do not "
                    "keep local tone-to-color maps."
                ),
            ),
        ),
        entrypoints=(
            "app.schemas.catalog.SubscriptionRead",
            "app.schemas.billing.InvoiceRead",
            "app.schemas.billing.PaymentRead",
            "app.schemas.service_status.ServiceStatusItem",
            "app.schemas.support.TicketRead",
            "app.schemas.network_monitoring.NetworkDeviceRead",
            "app.services.crm_api.outage_incident_row",
            "app.services.web_customer_lists",
            "app.services.web_customer_details",
            "app.services.customer_portal_context",
            "app.schemas.field.FieldJobSummary",
            "app.schemas.field.FieldManagerJob",
            "app.services.field.map_search",
            "templates.admin.customers",
            "templates.admin.billing",
            "templates.admin.network.outages",
            "templates.admin.network.core-devices",
            "templates.admin.network.network-devices",
            "templates.admin.network.monitoring",
            "templates.customer.connection",
            "templates.reseller.dashboard",
            "templates.customer.dashboard.restricted",
            "templates.customer.billing",
            "templates.admin.support.tickets",
            "templates.customer.support",
            "mobile",
            "field_mobile",
        ),
        rule=(
            "Domain state owners provide raw or derived status values. Server read "
            "projections add one StatusPresentation label/tone/icon contract. "
            "Templates and mobile clients render that contract and do not map "
            "the same domain values independently."
        ),
    ),
    DomainSOT(
        domain="vpn_remote_access",
        services=(
            SOTService(
                name="vpn.key_material",
                module="app.services.wireguard_crypto",
                owns=(
                    "WireGuard keypair generation",
                    "private-key at-rest encryption",
                ),
            ),
            SOTService(
                name="vpn.system_interface",
                module="app.services.wireguard_system",
                owns=(
                    "VPS-local WireGuard interface state",
                    "system peer projection to the running interface",
                ),
                depends_on=("vpn.key_material",),
            ),
            SOTService(
                name="vpn.wireguard",
                module="app.services.wireguard",
                owns=(
                    "WireGuard server and peer lifecycle",
                    "peer config and MikroTik RouterOS script generation",
                ),
                depends_on=("vpn.system_interface", "vpn.key_material"),
            ),
            SOTService(
                name="vpn.routing_readiness",
                module="app.services.vpn_routing",
                owns=("VPN interface readiness for device access",),
                depends_on=("vpn.system_interface",),
            ),
        ),
        entrypoints=(
            "app.api.wireguard",
            "app.tasks.wireguard",
            "app.services.web_vpn_servers",
            "app.services.web_vpn_peers",
            "app.services.web_vpn_management",
        ),
        rule=(
            "Admin VPN routes and device-access callers resolve WireGuard "
            "server/peer lifecycle, config and RouterOS script generation, key "
            "material, and interface readiness through these owners. web_vpn_* "
            "adapters and device-access code do not build WireGuard config, "
            "mutate peers, or write the system interface directly. The Redis "
            "vpn_cache is a rebuildable projection, never a source of truth."
        ),
    ),
    DomainSOT(
        domain="geospatial",
        services=(
            SOTService(
                name="gis.geocoding",
                module="app.services.geocoding",
                owns=(
                    "address and coordinate resolution",
                    "geocode lookup and result caching",
                ),
            ),
            SOTService(
                name="gis.spatial_sync",
                module="app.services.gis_sync",
                owns=(
                    "GIS/spatial data synchronization",
                    "spatial feature import and projection",
                ),
            ),
        ),
        entrypoints=(
            "app.api.geocoding",
            "app.api.gis",
            "app.tasks.gis",
            "app.services.web_system_geocode_tool",
            "app.services.web_gis",
        ),
        rule=(
            "Address/coordinate resolution and spatial data synchronization "
            "resolve through these owners. API, web, and task callers request a "
            "geocode or a sync outcome; they do not embed their own geocode "
            "lookups or spatial write logic."
        ),
    ),
    DomainSOT(
        domain="sales_referrals",
        services=(
            SOTService(
                name="sales.orders",
                module="app.services.sales_orders",
                owns=("sales order lifecycle",),
                depends_on=("sales.service", "sales.lead_lifecycle"),
            ),
            SOTService(
                name="sales.selfserve",
                module="app.services.sales.selfserve",
                owns=("self-serve quote and signup flow",),
            ),
            SOTService(
                name="sales.lead_lifecycle",
                module="app.services.sales.lifecycle",
                owns=(
                    "Party-first Lead identity lifecycle",
                    "immutable structured Lead origin capture",
                    "reviewed Lead to Subscriber account attachment",
                    "Lead-to-Quote and Lead-to-Ticket Party alignment",
                ),
                depends_on=("party.registry", "communications.campaigns"),
                notes=(
                    "Native Sub campaign responses and external ad-provider "
                    "identifiers are deliberately distinct. dotmac_mkt and CRM "
                    "have no lead, customer, attribution, or lifecycle authority."
                ),
            ),
            SOTService(
                name="sales.service",
                module="app.services.sales.service",
                owns=("sales pipeline and quote lifecycle",),
                depends_on=("sales.lead_lifecycle",),
            ),
            SOTService(
                name="customer.lifecycle_audit",
                module="app.services.customer_lifecycle_audit",
                owns=(
                    "PII-free customer lifecycle link convergence report",
                    "Lead origin and downstream alignment debt classification",
                    "Party-first referral capture and conversion debt classification",
                ),
                depends_on=(
                    "party.registry",
                    "communications.campaigns",
                    "sales.lead_lifecycle",
                    "sales.service",
                    "sales.orders",
                    "access.subscription_lifecycle",
                    "support.ticket_lifecycle",
                ),
            ),
            SOTService(
                name="referrals.program",
                module="app.services.referrals",
                owns=(
                    "Party-first Refer & Earn capture",
                    "reviewed Referral to Subscriber account conversion",
                    "referral qualification and reward decisions",
                ),
                depends_on=(
                    "party.registry",
                    "sales.lead_lifecycle",
                    "access.subscription_lifecycle",
                    "financial.credit_notes",
                ),
                notes=(
                    "Contact observations never establish identity or attach an "
                    "account. New capture creates no Subscriber and stores no "
                    "contact PII in Referral metadata or Lead origin."
                ),
            ),
            SOTService(
                name="referrals.account_conversion",
                module="app.services.referral_account_conversion",
                owns=(
                    "stable Referral Party Lead conversion context validation",
                    "atomic referral account creation and adjudication orchestration",
                ),
                depends_on=(
                    "customer.accounts",
                    "party.registry",
                    "sales.lead_lifecycle",
                    "referrals.program",
                    "auth.token_signing",
                ),
                notes=(
                    "The coordinator carries exact UUID context under a row lock "
                    "and mints the expiring public signup capability through the "
                    "auth signing owner. It never selects identity by name, email, "
                    "phone, or other contact observations."
                ),
            ),
        ),
        entrypoints=(
            "app.api.me",
            "app.api.crm_referrals",
            "app.api.crm_webhooks",
            "app.web.customer.referrals",
            "app.tasks.referrals",
            "app.services.events.handlers.referral",
            "app.services.web_sales",
            "app.services.web_referrals",
            "scripts.migration.audit_customer_lifecycle",
        ),
        rule=(
            "A prospect enters as a Party-bound Lead with captured origin, not a "
            "fake Subscriber. Quote, order, subscription, and support owners keep "
            "their domain state while the account-conversion coordinator validates "
            "the stable Referral/Party/Lead context and calls each owner. web_sales/"
            "web_referrals adapters and API/task callers request an outcome; CRM "
            "and dotmac_mkt have no customer-lifecycle or attribution authority."
        ),
    ),
)


def domain_order() -> list[str]:
    return [domain.domain for domain in DOMAIN_SOT_RELATIONSHIPS]


def domain_relationship(domain_name: str) -> DomainSOT:
    for domain in DOMAIN_SOT_RELATIONSHIPS:
        if domain.domain == domain_name:
            return domain
    raise KeyError(domain_name)


def services_for_domain(domain_name: str) -> tuple[SOTService, ...]:
    return domain_relationship(domain_name).services


def service_names_for_domain(domain_name: str) -> tuple[str, ...]:
    return tuple(service.name for service in services_for_domain(domain_name))


def dependencies_for(service_name: str) -> tuple[str, ...]:
    for domain in DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            if service.name == service_name:
                return service.depends_on
    raise KeyError(service_name)


def owning_service_for(concern: str) -> SOTService | None:
    needle = concern.strip().lower()
    if not needle:
        return None
    for domain in DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            if any(needle in owned.lower() for owned in service.owns):
                return service
    return None
