"""System-wide single-source-of-truth relationship registry.

This registry names the service boundaries that should own domain decisions.
It is intentionally declarative: routes, APIs, Celery tasks, and event handlers
can use it as an architectural map while each domain is migrated incrementally.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    EventContract,
    MigrationContract,
    OwnerRole,
    ProjectionContract,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    contract_validation_errors,
)


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
                depends_on=("auth.subscriber_assignments", "auth.permission_gate"),
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
                depends_on=(
                    "party.registry",
                    "auth.subscriber_assignments",
                    "auth.permission_gate",
                ),
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
                    "Reseller record creation",
                    "transaction-neutral Reseller record initialization",
                ),
                depends_on=(
                    "access.subscription_lifecycle",
                    "events.dispatcher",
                ),
                notes=(
                    "Cross-domain coordinators may prepare an account through "
                    "this owner, but new/cut-over callers must not construct "
                    "Subscriber or Reseller rows or decide account lifecycle "
                    "state themselves. "
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
                    "currency-typed complete billing headline projection",
                ),
                depends_on=(
                    "financial.ledger",
                    "financial.prepaid_funding_reconstruction",
                ),
            ),
            SOTService(
                name="customer.reseller_status_actions",
                module="app.services.reseller_portal",
                owns=(
                    "reseller-scoped account-action impact preview",
                    "lock-aware account-action eligibility",
                    "account-action stale-preview fingerprint",
                    "account-bound idempotent status confirmation",
                ),
                depends_on=(
                    "customer.identity_scope",
                    "access.subscription_lifecycle",
                ),
                notes=(
                    "The reseller adapter renders a distinct confirmation step "
                    "bound to this preview and an account-scoped idempotency key. "
                    "Subscription and account lifecycle mutation remains owned by "
                    "access.subscription_lifecycle."
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
                depends_on=(
                    "financial.ledger",
                    "customer.financial_position",
                    "customer.accounts",
                    "control.settings_spec",
                    "events.dispatcher",
                    "observability.audit_log",
                ),
                notes=(
                    "This owner accepts debits only. Customer credits remain "
                    "owned by financial.credit_notes, and account adjustments "
                    "do not decide service-access state."
                ),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name="prepaid account-debit eligibility and preview",
                            role=OwnerRole.POLICY,
                            input_names=(
                                "canonical Subscriber account state",
                                "canonical append-only ledger state",
                                "resolved customer financial position",
                                "billing default-currency setting",
                            ),
                        ),
                        ConcernContract(
                            name="locked account-debit confirmation",
                            role=OwnerRole.COMMAND_WRITER,
                            input_names=(
                                "account-adjustment command evidence",
                                "canonical Subscriber account state",
                                "canonical append-only ledger state",
                                "resolved customer financial position",
                                "billing default-currency setting",
                            ),
                            canonical_writer="financial.account_adjustments",
                        ),
                        ConcernContract(
                            name="account-adjustment idempotency and audit evidence",
                            role=OwnerRole.AUTHORITATIVE_RECORD,
                            input_names=(
                                "account-adjustment command evidence",
                                "canonical Subscriber account state",
                                "canonical append-only ledger state",
                            ),
                            canonical_writer="financial.account_adjustments",
                        ),
                        ConcernContract(
                            name="exact account-adjustment ledger links",
                            role=OwnerRole.AUTHORITATIVE_RECORD,
                            input_names=(
                                "account-adjustment command evidence",
                                "canonical append-only ledger state",
                            ),
                            canonical_writer="financial.account_adjustments",
                        ),
                        ConcernContract(
                            name="previewed account-adjustment reversal evidence",
                            role=OwnerRole.COMMAND_WRITER,
                            input_names=(
                                "account-adjustment command evidence",
                                "canonical Subscriber account state",
                                "canonical append-only ledger state",
                                "resolved customer financial position",
                            ),
                            canonical_writer="financial.account_adjustments",
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="account-adjustment command evidence",
                            owner="financial.account_adjustments",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "typed command context, confirmed preview fingerprint, "
                                "and origin-scoped idempotency key"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical Subscriber account state",
                            owner="customer.accounts",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source="subscribers account identity",
                        ),
                        AuthorityInput(
                            name="canonical append-only ledger state",
                            owner="financial.ledger",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source="ledger_entries and structural reversal links",
                        ),
                        AuthorityInput(
                            name="resolved customer financial position",
                            owner="customer.financial_position",
                            kind=AuthorityKind.DERIVED_PROJECTION,
                            source=(
                                "prepaid availability, receivables, and "
                                "collection-blocking balance resolver"
                            ),
                        ),
                        AuthorityInput(
                            name="billing default-currency setting",
                            owner="control.settings_spec",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source="billing.default_currency",
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.OWNER_MANAGED,
                        boundary=(
                            "Public debit and reversal commands enter one "
                            "manifest-verified owner transaction. Explicit nested "
                            "staging collaborators flush only inside approved plan-"
                            "change, add-on, or renewal coordinator transactions."
                        ),
                        locking=(
                            "Debit confirmation locks the Subscriber account before "
                            "re-preview and append. Reversal locks the account, "
                            "AccountAdjustment, and original ledger entry in that order."
                        ),
                        idempotency=(
                            "Database uniqueness scopes debit and reversal keys by "
                            "origin; exact account, preview, effective-date, and "
                            "structural ledger evidence are revalidated on replay."
                        ),
                        retries=(
                            "Exact replay is safe. Only write_conflict is retryable "
                            "after the owner rolls back; stale previews require a new "
                            "preview and insufficient funding requires new source state."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(
                            "financial.account_adjustments.invalid_command",
                            "financial.account_adjustments.invalid_configuration",
                            "financial.account_adjustments.account_not_found",
                            "financial.account_adjustments.adjustment_not_found",
                            "financial.account_adjustments.insufficient_funding",
                            "financial.account_adjustments.idempotency_conflict",
                            "financial.account_adjustments.stale_preview",
                            "financial.account_adjustments.already_reversed",
                            "financial.account_adjustments.incomplete_evidence",
                            "financial.account_adjustments.write_conflict",
                            "financial.account_adjustments.active_caller_transaction",
                            "financial.account_adjustments.command_contract_violation",
                            "financial.account_adjustments.invalid_command_context",
                            "financial.account_adjustments.nested_owner_command",
                            "financial.account_adjustments.nested_transaction_completion",
                        ),
                        mapping_owner="API and enclosing financial coordinator adapters",
                        retryable_codes=(
                            "financial.account_adjustments.write_conflict",
                        ),
                        fail_closed_on=(
                            "stale or mismatched preview",
                            "insufficient prepaid funding",
                            "ambiguous idempotency evidence",
                            "incomplete or inconsistent structural ledger evidence",
                            "active caller transaction",
                        ),
                    ),
                    events=EventContract(
                        event_types=(
                            "account_adjustment.confirmed",
                            "account_adjustment.reversed",
                        ),
                        schema_version=1,
                        delivery_owner="events.dispatcher",
                        compatibility=(
                            "PII-free versioned payloads retain aggregate, account, "
                            "money, origin, exact ledger, and command evidence fields."
                        ),
                        replay=(
                            "Idempotent command replay emits no duplicate event; the "
                            "durable dispatcher retries each staged event."
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "generic ledger API plus plan-change and add-on debit paths"
                        ),
                        new_owner="financial.account_adjustments",
                        verification=(
                            "The billing alignment audit recorded zero historical "
                            "adjustment-debit drift; structural evidence inspection and "
                            "focused replay, stale-preview, funding, and reversal tests "
                            "remain the cutover proof."
                        ),
                        cutover_gate=(
                            "All application debits use a public command or an approved "
                            "nested staging collaborator and carry exact ledger evidence."
                        ),
                        fallback_retirement=(
                            "Generic ledger posting/reversal stays gated; direct "
                            "AccountAdjustment construction and legacy commit flags are "
                            "forbidden by architecture tests."
                        ),
                    ),
                    steward="finance operations",
                    design_refs=(
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/CODING_STANDARD.md",
                        "docs/audits/BILLING_ALIGNMENT_RUN_2026-07-12.md",
                        "docs/adr/0002-owner-command-transaction-boundary.md",
                        "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                    ),
                    test_refs=(
                        "tests/test_account_adjustment_evidence.py",
                        "tests/architecture/test_account_adjustment_boundary.py",
                        "tests/architecture/test_financial_action_boundaries.py",
                        "tests/architecture/test_financial_ownership.py",
                    ),
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
                    "cash-first verified provider settlement evidence",
                    "payment allocation reconciliation exception lifecycle",
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
                name="financial.prepaid_enforcement_state",
                module="app.services.prepaid_enforcement_state",
                owns=(
                    "prepaid low-balance timer state",
                    "prepaid deactivation timer state",
                    "funded and terminal prepaid timer cleanup",
                ),
                depends_on=("events.dispatcher",),
                notes=(
                    "Writes prepared timer observations and cleanup requests in "
                    "the caller transaction. It owns no eligibility, threshold, "
                    "grace, suspension, restoration, or commit decision."
                ),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name="prepaid low-balance timer state",
                            role=OwnerRole.AUTHORITATIVE_RECORD,
                            input_names=(
                                "resolved prepaid enforcement transition",
                                "canonical prepaid enforcement timers",
                            ),
                            canonical_writer="financial.prepaid_enforcement_state",
                        ),
                        ConcernContract(
                            name="prepaid deactivation timer state",
                            role=OwnerRole.AUTHORITATIVE_RECORD,
                            input_names=(
                                "resolved prepaid enforcement transition",
                                "canonical prepaid enforcement timers",
                            ),
                            canonical_writer="financial.prepaid_enforcement_state",
                        ),
                        ConcernContract(
                            name="funded and terminal prepaid timer cleanup",
                            role=OwnerRole.COMMAND_WRITER,
                            input_names=(
                                "resolved prepaid enforcement transition",
                                "resolved account lifecycle transition",
                                "canonical prepaid enforcement timers",
                            ),
                            canonical_writer="financial.prepaid_enforcement_state",
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="resolved prepaid enforcement transition",
                            owner="financial.prepaid_enforcement",
                            kind=AuthorityKind.DERIVED_PROJECTION,
                            source=(
                                "locked prepaid enforcement plan and successful "
                                "suspend, restore, or funding consequence"
                            ),
                        ),
                        AuthorityInput(
                            name="resolved account lifecycle transition",
                            owner="access.subscription_lifecycle",
                            kind=AuthorityKind.DERIVED_PROJECTION,
                            source="terminal account status derived from subscription facts",
                        ),
                        AuthorityInput(
                            name="canonical prepaid enforcement timers",
                            owner="financial.prepaid_enforcement_state",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "Subscriber.prepaid_low_balance_at and "
                                "Subscriber.prepaid_deactivation_at"
                            ),
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.PARTICIPANT,
                        boundary=(
                            "Only contracted enforcement and lifecycle owners call "
                            "the participant. It locks the Subscriber row and "
                            "flushes timer plus event evidence without committing."
                        ),
                        locking=(
                            "Every transition selects the canonical Subscriber row "
                            "FOR UPDATE before inspecting or changing timer state."
                        ),
                        idempotency=(
                            "Arm and deactivation preserve the first timestamp; "
                            "equivalent repeats and already-clear cleanup are no-ops."
                        ),
                        retries=(
                            "The surrounding owner retries its complete transaction; "
                            "the participant never retries or commits independently."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(
                            "financial.prepaid_enforcement_state.invalid_account_id",
                            "financial.prepaid_enforcement_state.account_not_found",
                        ),
                        mapping_owner=(
                            "financial.prepaid_enforcement and "
                            "access.subscription_lifecycle coordinators"
                        ),
                        fail_closed_on=(
                            "malformed or missing canonical account",
                            "unlocked or ambiguous timer state",
                        ),
                    ),
                    events=EventContract(
                        event_types=("prepaid_enforcement.timer_changed",),
                        schema_version=1,
                        delivery_owner="events.dispatcher",
                        compatibility=(
                            "Version 1 is additive and contains only the account "
                            "identifier and transition vocabulary."
                        ),
                        replay=(
                            "Equivalent commands are no-ops. Current timer fields "
                            "are authoritative; events retain transition evidence."
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "collections and account-lifecycle call sites mutating "
                            "Subscriber prepaid timer fields directly"
                        ),
                        new_owner="financial.prepaid_enforcement_state",
                        verification=(
                            "Focused state, sweep, lifecycle, atomic-event, and "
                            "single-writer architecture tests."
                        ),
                        cutover_gate=(
                            "Every prepaid timer transition calls the locked, "
                            "flush-only participant from a named owner."
                        ),
                        fallback_retirement=(
                            "Direct prepaid timer assignments outside the owner and "
                            "silent missing-account behavior are removed."
                        ),
                    ),
                    steward="billing operations",
                    design_refs=(
                        "docs/designs/PREPAID_FUNDING_RECONSTRUCTION.md",
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/adr/0002-owner-command-transaction-boundary.md",
                    ),
                    test_refs=(
                        "tests/test_prepaid_enforcement_state_owner.py",
                        "tests/architecture/test_prepaid_enforcement_state_boundary.py",
                        "tests/test_prepaid_balance_sweep.py",
                        "tests/test_account_lifecycle.py",
                    ),
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
                    "financial.prepaid_enforcement_state",
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
                    "financial.prepaid_enforcement_state",
                    "financial.prepaid_enforcement_readiness",
                ),
            ),
            SOTService(
                name="financial.provider_payment_settlements",
                module="app.services.provider_payment_settlements",
                owns=(
                    "verified invoice-payment cash-first orchestration",
                    "post-settlement invoice-allocation request",
                    "allocation-failure exception handoff",
                ),
                depends_on=("financial.payments", "financial.invoices"),
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
                depends_on=(
                    "financial.payments",
                    "financial.provider_payment_settlements",
                ),
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
                    "verified provider settlement then allocation orchestration",
                ),
                depends_on=(
                    "financial.ledger",
                    "financial.payment_provider_events",
                    "financial.provider_payment_settlements",
                ),
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
                    "cross-customer exact shared-cable fault candidates",
                    "customer-trace evidence completeness",
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
                    "never declare incidents or redefine topology. Numeric cutover "
                    "review readiness is owned by network.fiber_cutover_readiness."
                ),
            ),
            SOTService(
                name="network.fiber_plant_integrity",
                module="app.services.network.fiber_plant_integrity",
                owns=(
                    "active passive-cable endpoint, geometry, and size validation",
                    "serving PON/OLT rootedness and safe cable retirement",
                    "exact numbered cable-core materialization and capacity projection",
                    "splitter ratio, port-count, and declared-capacity validation",
                ),
                depends_on=("network.fiber_topology",),
                notes=(
                    "This is the invariant and exact-capacity owner called by "
                    "reviewed asset changes and splitter commands. Cable names are "
                    "display metadata only; name or proximity matching never creates "
                    "an endpoint, core assignment, or rooted topology edge."
                ),
            ),
            SOTService(
                name="network.splitter_inventory",
                module="app.services.network.splitters",
                owns=(
                    "splitter identity and declared ratio/capacity mutations",
                    "splitter port identity and bounded port mutations",
                    "splitter utilization and spare-output projection",
                ),
                depends_on=("network.fiber_plant_integrity",),
                notes=(
                    "API and admin form adapters delegate here. Reviewed attachment "
                    "owners remain authoritative for PON inputs, cascades, and ONT "
                    "outputs; this inventory owner does not infer those edges."
                ),
            ),
            SOTService(
                name="network.fiber_physical_continuity",
                module="app.services.network.fiber_physical_continuity",
                owns=(
                    "fiber rack, ODF/patch-panel, and connector-port inventory invariants",
                    "reviewed exact core-splice, strand-termination, and patch-cord decisions",
                    "canonical active physical optical links and immutable result evidence",
                    "exact ordered cable-core continuity and evidence hash",
                ),
                depends_on=(
                    "network.fiber_topology",
                    "network.fiber_plant_integrity",
                ),
                notes=(
                    "Every connector represents one optical channel; duplex uses "
                    "two explicit links sharing an assembly label, while MPO/MTP "
                    "fails closed until an exact lane model exists. Cable names, "
                    "labels, geometry, proximity, legacy FiberSplice rows, and the "
                    "legacy FiberSegment.fiber_strand_id scalar never create exact "
                    "continuity. Links require preview, independent review, locked "
                    "execution, and exact result evidence."
                ),
            ),
            SOTService(
                name="network.fiber_asset_changes",
                module="app.services.fiber_change_requests",
                owns=(
                    "reviewed passive-fiber asset change requests",
                    "approved passive-fiber asset mutations",
                    "reviewed requests for operational cable size and lifecycle state",
                    "review transport for rack, panel, connector, and exact splice decisions",
                ),
                depends_on=(
                    "network.fiber_topology",
                    "network.fiber_plant_integrity",
                    "network.splitter_inventory",
                    "network.fiber_support_structures",
                    "network.fiber_physical_continuity",
                ),
                notes=(
                    "This workflow owns review and application. It delegates cable "
                    "and splitter invariants, exact core materialization, physical "
                    "inventory, and splice execution to their named owners instead "
                    "of maintaining parallel mutation rules."
                ),
            ),
            SOTService(
                name="network.fiber_support_structures",
                module="app.services.network.fiber_support_structures",
                owns=(
                    "canonical fiber support identity and operational state",
                    "support lifecycle, inspection, ownership, and lease projection",
                    "reviewed exact passive-asset-to-support mount decisions",
                    "canonical ordered support mount edges and result evidence",
                ),
                depends_on=(
                    "network.fiber_topology",
                    "observability.audit_log",
                ),
                notes=(
                    "Imported poles remain staged observations. Canonical support "
                    "creates and state changes are applied here only after reviewed "
                    "passive-asset requests. Mounts require exact asset/support IDs, "
                    "preview confirmation, independent review, locked revalidation, "
                    "and audit evidence; geometry and proximity never create an edge."
                ),
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
                    "network.fiber_support_structures",
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
                    "network.fiber_field_verification_job_scope",
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
                name="network.fiber_field_verification_job_scope",
                module=("app.services.network.fiber_field_verification_job_scope"),
                owns=(
                    "fiber field-verification work-order scope metadata contract",
                    "exact planned staged-feature observation boundary",
                ),
                notes=(
                    "Legacy jobs without an explicit plan retain their existing "
                    "observation behavior. Once a plan is present, observations "
                    "must name one of its exact staged feature IDs with unchanged "
                    "content; names, labels, geometry, and proximity never expand "
                    "the scope."
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
                name="network.fiber_field_verification_jobs",
                module=("app.services.network.fiber_field_verification_job_plans"),
                owns=(
                    "exact fiber field-verification job-plan previews",
                    "confirmed staged-source-to-native-job plan execution",
                    "fiber field-verification job-plan audit evidence",
                ),
                depends_on=(
                    "network.fiber_field_verification_worklist",
                    "network.fiber_field_verification_job_scope",
                    "operations.work_order_commands",
                    "observability.audit_log",
                ),
                notes=(
                    "The owner binds at most 100 explicitly selected current "
                    "worklist rows, exact row/content/geometry hashes, and the "
                    "complete worklist report hash. Execute re-previews and "
                    "requires the exact plan digest, then delegates create and "
                    "optional assignment to operations.work_order_commands in one "
                    "transaction. It never writes work-order tables directly and "
                    "does not add actions to the read-only worklist or map."
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
                name="network.fiber_work_order_evidence_map",
                module=("app.services.network.fiber_topology_work_order_evidence_map"),
                owns=(
                    "technician-scoped native work-order fiber evidence overlay",
                    "exact work-order observation-to-map lineage projection",
                    "work-order fiber evidence feature and report digests",
                    "work-order evidence and geometry presentation semantics",
                ),
                depends_on=(
                    "operations.work_orders",
                    "network.fiber_field_observations",
                    "network.fiber_field_verification_map",
                ),
                notes=(
                    "This read-only projection selects only field-verification "
                    "map features represented by immutable observations for one explicitly "
                    "scoped native Sub work order. Every observation must map "
                    "exactly once; other jobs' evidence is removed. Current and "
                    "superseded source context remains distinct. It cannot assign "
                    "work, record observations, repair geometry, infer topology, "
                    "mutate state, establish thresholds, or decide customer impact."
                    " The field mobile client renders this contract and may cache "
                    "only one exact public-work-order/report-hash snapshot per "
                    "authenticated principal; its offline cache is explicitly "
                    "stale and never an authority."
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
                    "network.fiber_support_structures",
                ),
                notes=(
                    "One repeatable read-only snapshot keeps canonical-model support, "
                    "source coverage, decision lifecycle, change-request state, and "
                    "provenance validity separate. Cabinets, FATs, closures, "
                    "buildings, and supports use their current canonical models. "
                    "The owner cannot infer identity, "
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
                name="network.fiber_cutover_readiness",
                module=("app.services.network.fiber_topology_cutover_readiness"),
                owns=(
                    "versioned numeric fiber cutover-readiness policy",
                    "complete global fiber cutover cohort evidence projection",
                    "fiber topology cutover-review readiness decision",
                ),
                depends_on=(
                    "network.fiber_topology",
                    "network.fiber_identity_coverage",
                    "network.fiber_connectivity_coverage",
                    "network.fiber_field_verification_worklist",
                ),
                notes=(
                    "The initial policy accepts only the complete global cohort, "
                    "requires exact current identity/connectivity/result/provenance "
                    "and customer traces, and applies zero-tolerance blockers. All "
                    "latest staged rows remain mandatory until an authoritative "
                    "dormant-low-risk classifier exists. Missing POP/OLT, splitter, "
                    "and customer-endpoint field contracts fail closed. A passing "
                    "report is evidence for independent review and cannot authorize "
                    "or execute a production cutover."
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
                    "reviewed splitter-output-to-downstream-input cascade links",
                    "reviewed ONT-to-splitter output attachments",
                    "canonical ONT splitter parent projection",
                    "splitter stage and cumulative optical-loss evidence",
                    "fiber access attachment result evidence",
                ),
                depends_on=(
                    "network.fiber_topology",
                    "network.fiber_connectivity_decisions",
                    "network.ont_assignment_commands",
                    "network.ont_assignment_identity",
                ),
                notes=(
                    "Only exact directed ports in one rooted, acyclic splitter "
                    "tree with agreeing ONT/PON/OLT identity can be attached. "
                    "Cascade construction is root-first, removal is leaf-first, "
                    "and each participating splitter has explicit insertion loss. "
                    "Preview is read-only, review is independent, execution "
                    "revalidates under lock, and stale inputs close without "
                    "mutation. Geometry, cabinets, ratios, names, and legacy "
                    "assignments never create an access edge."
                ),
            ),
            SOTService(
                name="network.access_path",
                module="app.services.network.access_path",
                owns=(
                    "subscription access path",
                    "last-mile path summary",
                    "composed ONT-to-passive-fiber-to-NAS-to-core/border path",
                    "typed cross-domain path gaps and combined evidence hash",
                    "distinct provisioning-NAS and live-session-NAS evidence",
                ),
                depends_on=(
                    "network.identity",
                    "network.fiber_topology",
                    "network.ont_assignment_commands",
                    "network.ont_assignment_identity",
                    "network.fiber_access_attachments",
                    "network.fiber_physical_continuity",
                    "network.forwarding_topology",
                ),
            ),
            SOTService(
                name="network.radius_sessions",
                module="app.services.network.radius_sessions",
                owns=(
                    "online-now session state",
                    "active-session NAS observation evidence",
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
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name="device_projections materialised table",
                            role=OwnerRole.PROJECTION_WRITER,
                            input_names=(
                                "canonical device identity",
                                "monitoring inventory observations",
                                "resolved operational device state",
                            ),
                            canonical_writer="network.device_projection",
                        ),
                        ConcernContract(
                            name="unified cross-type device row (OLT/core/ONT/CPE)",
                            role=OwnerRole.PROJECTION_WRITER,
                            input_names=(
                                "canonical device identity",
                                "monitoring inventory observations",
                            ),
                            canonical_writer="network.device_projection",
                        ),
                        ConcernContract(
                            name="projected operational status and freshness",
                            role=OwnerRole.PROJECTION_WRITER,
                            input_names=(
                                "resolved operational device state",
                                "monitoring inventory observations",
                            ),
                            canonical_writer="network.device_projection",
                        ),
                        ConcernContract(
                            name="device projection orphan pruning",
                            role=OwnerRole.RECONCILER,
                            input_names=("canonical device identity",),
                            canonical_writer="network.device_projection",
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="canonical device identity",
                            owner="network.identity",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "OLTDevice, NetworkDevice, OntUnit, and CpeDevice "
                                "natural identities"
                            ),
                        ),
                        AuthorityInput(
                            name="monitoring inventory observations",
                            owner="network.monitoring_inventory",
                            kind=AuthorityKind.OBSERVATION,
                            source=(
                                "active device inventory, address, vendor, model, "
                                "and last-seen facts consumed by collect_devices"
                            ),
                        ),
                        AuthorityInput(
                            name="resolved operational device state",
                            owner="network.device_state",
                            kind=AuthorityKind.DERIVED_PROJECTION,
                            source=(
                                "collect_devices operational status and reason "
                                "derivation"
                            ),
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.OWNER_MANAGED,
                        boundary=(
                            "reconcile_device_projections enters the verified "
                            "owner-command boundary on a transaction-free session; "
                            "the projection and outbox event commit atomically "
                            "before return."
                        ),
                        locking=(
                            "A PostgreSQL transaction advisory lock serializes full "
                            "rebuilds; uq_device_projection_source arbitrates each "
                            "device_type/source_id natural key."
                        ),
                        idempotency=(
                            "The natural-key upsert and orphan-pruning pass converges "
                            "to the authoritative input set; one Celery delivery "
                            "keeps its task-derived command/idempotency key across "
                            "retries without duplicating rows."
                        ),
                        retries=(
                            "The task retries SQLAlchemy OperationalError up to three "
                            "times with bounded exponential backoff and a fresh "
                            "session; a later scheduled pass repairs stale state."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(
                            "network.device_projection.invalid_command",
                            "network.device_projection.invalid_command_context",
                            "network.device_projection.command_contract_violation",
                            "network.device_projection.nested_owner_command",
                            "network.device_projection.active_caller_transaction",
                            "network.device_projection.nested_transaction_completion",
                        ),
                        mapping_owner="app.tasks.device_projection",
                        fail_closed_on=(
                            "invalid command metadata",
                            "active caller transaction",
                            "nested command or transaction completion",
                            "manifest mismatch",
                        ),
                    ),
                    events=EventContract(
                        event_types=("device_projection.reconciled",),
                        schema_version=1,
                        delivery_owner="events.dispatcher",
                        compatibility=(
                            "Additive payload evolution within schema version 1; "
                            "breaking changes require a new version."
                        ),
                        replay=(
                            "Event-store delivery is retryable; consumers key side "
                            "effects by event_id and treat reconciliation counts as "
                            "immutable evidence."
                        ),
                    ),
                    projections=(
                        ProjectionContract(
                            name="device_projections",
                            input_names=(
                                "canonical device identity",
                                "monitoring inventory observations",
                                "resolved operational device state",
                            ),
                            writer="network.device_projection",
                            freshness=(
                                "Celery beat targets a 60-second rebuild interval; "
                                "every row carries reconciled refreshed_at."
                            ),
                            stale_behavior=(
                                "Readers may show the last committed projection and "
                                "its refreshed_at; they never synthesize or write a "
                                "replacement row."
                            ),
                            drift_signal=(
                                "Reconcile logs inserted, updated, and pruned counts; "
                                "latest_refreshed_at exposes projection age."
                            ),
                            rebuild_operation=(
                                "app.services.device_projection_reconcile."
                                "reconcile_device_projections"
                            ),
                            repair_owner="network.device_projection",
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.NATIVE,
                        new_owner="network.device_projection",
                    ),
                    steward="network operations",
                    design_refs=(
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/adr/0002-owner-command-transaction-boundary.md",
                    ),
                    test_refs=(
                        "tests/test_owner_commands.py",
                        "tests/test_device_projection_reconcile.py",
                        "tests/test_device_projection_task.py",
                        "tests/architecture/test_owner_command_boundary.py",
                    ),
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
                name="network.forwarding_topology",
                module="app.services.network.forwarding_topology",
                owns=(
                    "reviewed downstream-to-upstream forwarding declarations",
                    "normalized BGP-peer and routing-table observations",
                    "forwarding declaration agreement and drift projection",
                    "authoritative core, border, NAS, site, interface, and VRF graph",
                    "official customer upstream path and outage ancestry",
                ),
                depends_on=(
                    "network.identity",
                    "network.monitoring_inventory",
                    "network.radius_sessions",
                    "network.control_plane_intent",
                    "network.routeros_sot",
                ),
                notes=(
                    "Declarations bind exact devices, interfaces, sites, roles, "
                    "VRFs, configuration intent, and where applicable peer, "
                    "route, and NAS identity. Preview is write-free; proposal "
                    "and review are separated; execution locks and revalidates "
                    "exact evidence. LLDP, BGP, routing-table, and RADIUS data "
                    "remain observations and cannot create or retire official "
                    "path. Configuration remains owned by control-plane intent "
                    "and RouterOS SOT. Customer paths, reachability, and outage "
                    "blast radius consume only agreeing declarations. The "
                    "RouterOS collector is a GET-only, declaration-scoped "
                    "observation adapter behind the fail-closed "
                    "network.forwarding_observation_collection control; enabling "
                    "it starts evidence shadowing, not authority cutover."
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
                depends_on=(
                    "network.access_path",
                    "network.forwarding_topology",
                ),
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
            "scripts.network.review_forwarding_topology",
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
                owns=(
                    "customer online-now resolution",
                    "primary NAS session resolution",
                ),
                depends_on=("sessions.radius_reconciliation", "network.identity"),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name="customer online-now resolution",
                            role=OwnerRole.RESOLVER,
                            input_names=("active RADIUS session projection",),
                        ),
                        ConcernContract(
                            name="primary NAS session resolution",
                            role=OwnerRole.RESOLVER,
                            input_names=(
                                "active RADIUS session projection",
                                "network identity registry",
                            ),
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="active RADIUS session projection",
                            owner="sessions.radius_reconciliation",
                            kind=AuthorityKind.DERIVED_PROJECTION,
                            source="radius_active_sessions",
                        ),
                        AuthorityInput(
                            name="network identity registry",
                            owner="network.identity",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source="NetworkDevice and NAS identity mappings",
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.READ_ONLY,
                        boundary=(
                            "Caller creates and closes the session; resolver "
                            "performs no writes or transaction completion."
                        ),
                        locking=(
                            "No row lock; the result reflects database visibility "
                            "at query time."
                        ),
                        idempotency=(
                            "The same subscriber, limit, and visible input snapshot "
                            "produce the same ordered resolution."
                        ),
                        retries=(
                            "Adapters may retry transient read failures; the resolver "
                            "has no side effects."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(),
                        mapping_owner="web, API, task, and service adapters",
                        fail_closed_on=("invalid subscriber identifier",),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.NATIVE,
                        new_owner="sessions.radius_resolution",
                    ),
                    steward="network operations",
                    design_refs=(
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/DASHBOARD_OVERVIEW_PAGE_CONTRACT.md",
                    ),
                    test_refs=(
                        "tests/test_network_sot_services.py",
                        "tests/test_sot_relationships.py",
                    ),
                ),
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
                    "exact open, response, assignment, mute, and snooze queue cohorts",
                    "failed outbound queue count and worklist",
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
                name="operations.work_order_commands",
                module="app.services.work_order_commands",
                owns=(
                    "native work-order creation and header commands",
                    "work-order assignment decisions and projection",
                    "work-order assignment-queue transitions",
                ),
                depends_on=(
                    "customer.identity_scope",
                    "operations.work_order_status",
                    "observability.audit_log",
                ),
                notes=(
                    "Dispatch API/web and field-manager adapters authorize and "
                    "delegate here. The owner validates a read-only assignment "
                    "preview, locks the work order, changes queue and assignee "
                    "projection atomically, records exact actor audit evidence, "
                    "and treats equivalent retries as replays. CRM mirror ingest "
                    "remains a provenance importer; field execution statuses remain "
                    "owned by operations.field_completion."
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
                    "This registration owns reads only. Native mutations delegate "
                    "to operations.work_order_commands; imported CRM identifiers "
                    "remain provenance and never become native command authority."
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
            SOTService(
                name="operations.vendor_project_lifecycle",
                module="app.services.vendor_portal_operations",
                owns=(
                    "vendor start/complete installation-project transitions",
                    "durable vendor lifecycle actor/time/event evidence",
                    "typed vendor project lifecycle outbox events",
                    "vendor installation-project quote lifecycle",
                    "quote submission eligibility and impact snapshot",
                    "as-built evidence lifecycle and impact snapshot",
                ),
                depends_on=("events.dispatcher", "operations.project_lifecycle"),
                notes=(
                    "This is the sole writer for approved -> in_progress -> "
                    "completed vendor work transitions and owns the related quote "
                    "and as-built workflow in the same implementation module. It "
                    "raises transport-neutral domain errors; adapters translate "
                    "them for HTTP delivery."
                ),
            ),
            SOTService(
                name="operations.vendor_purchase_invoices",
                module="app.services.vendor_purchase_invoices",
                owns=(
                    "vendor purchase-invoice lifecycle",
                    "purchase-invoice submission eligibility and financial preview",
                ),
                depends_on=("operations.vendor_project_lifecycle",),
            ),
            SOTService(
                name="operations.vendor_submission_confirmation",
                module="app.services.vendor_submission_proposals",
                owns=(
                    "short-lived signed vendor submission proposal",
                    "vendor submission stale-preview verification",
                    "vendor submission idempotency and replay result",
                ),
                depends_on=(
                    "auth.permission_gate",
                    "auth.token_signing",
                    "events.dispatcher",
                    "operations.vendor_project_lifecycle",
                    "operations.vendor_purchase_invoices",
                ),
                notes=(
                    "Web adapters only request a preview or confirm its signed "
                    "proposal. Domain owners recheck under lock and commit the "
                    "mutation with its idempotency result."
                ),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name="short-lived signed vendor submission proposal",
                            role=OwnerRole.POLICY,
                            input_names=(
                                "authenticated vendor principal context",
                                "vendor project submission preview",
                                "vendor purchase-invoice submission preview",
                                "capability signing envelope",
                                "vendor confirmation protocol invariants",
                            ),
                        ),
                        ConcernContract(
                            name="vendor submission stale-preview verification",
                            role=OwnerRole.POLICY,
                            input_names=(
                                "authenticated vendor principal context",
                                "vendor project submission preview",
                                "vendor purchase-invoice submission preview",
                                "capability signing envelope",
                            ),
                        ),
                        ConcernContract(
                            name="vendor submission idempotency and replay result",
                            role=OwnerRole.APPLICATION_COORDINATOR,
                            input_names=(
                                "authenticated vendor principal context",
                                "vendor project submission preview",
                                "vendor purchase-invoice submission preview",
                                "capability signing envelope",
                                "canonical vendor submission replay record",
                            ),
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="authenticated vendor principal context",
                            owner="auth.permission_gate",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "authenticated vendor, vendor-user, scope, reason, "
                                "command, and correlation identifiers"
                            ),
                        ),
                        AuthorityInput(
                            name="vendor project submission preview",
                            owner="operations.vendor_project_lifecycle",
                            kind=AuthorityKind.DERIVED_PROJECTION,
                            source=(
                                "locked quote, as-built, or project-lifecycle impact "
                                "and state fingerprint"
                            ),
                        ),
                        AuthorityInput(
                            name="vendor purchase-invoice submission preview",
                            owner="operations.vendor_purchase_invoices",
                            kind=AuthorityKind.DERIVED_PROJECTION,
                            source=(
                                "locked purchase-invoice financial impact and state "
                                "fingerprint"
                            ),
                        ),
                        AuthorityInput(
                            name="capability signing envelope",
                            owner="auth.token_signing",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source="configured context-signing key and algorithm",
                        ),
                        AuthorityInput(
                            name="vendor confirmation protocol invariants",
                            owner="operations.vendor_submission_confirmation",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "versioned purpose, issuer, claim allowlist, maximum "
                                "token size, ten-minute lifetime, and submission scopes"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical vendor submission replay record",
                            owner="operations.vendor_submission_confirmation",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "IdempotencyKey row keyed by signed proposal jti and "
                                "submission scope"
                            ),
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.COORDINATOR_MANAGED,
                        boundary=(
                            "A typed confirmation command enters one verified owner "
                            "transaction. Locked stale verification, replay reservation, "
                            "domain mutation, result evidence, and event commit together."
                        ),
                        locking=(
                            "The delegated domain preview locks the exact project, "
                            "quote, or invoice aggregate before the coordinator reserves "
                            "the signed proposal jti."
                        ),
                        idempotency=(
                            "The signed proposal jti and submission scope identify one "
                            "stable result. Exact replay returns that result without "
                            "rerunning the mutation."
                        ),
                        retries=(
                            "Expired, malformed, context-mismatched, or stale proposals "
                            "are terminal. Database concurrency failures retry the whole "
                            "owner command; delivery retries use the stable result."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(
                            "operations.vendor_submission_confirmation.unsupported_submission_type",
                            "operations.vendor_submission_confirmation.invalid_proposal",
                            "operations.vendor_submission_confirmation.expired_proposal",
                            "operations.vendor_submission_confirmation.proposal_context_mismatch",
                            "operations.vendor_submission_confirmation.confirmation_in_progress",
                            "operations.vendor_submission_confirmation.invalid_payload",
                            "operations.vendor_submission_confirmation.stale_proposal",
                            "operations.vendor_submission_confirmation.missing_result_evidence",
                            "operations.vendor_submission_confirmation.lifecycle_not_found",
                            "operations.vendor_submission_confirmation.lifecycle_not_assigned",
                            "operations.vendor_submission_confirmation.lifecycle_unsupported_action",
                            "operations.vendor_submission_confirmation.lifecycle_actor_required",
                            "operations.vendor_submission_confirmation.lifecycle_invalid_transition",
                            "operations.vendor_submission_confirmation.invalid_command_context",
                            "operations.vendor_submission_confirmation.command_contract_violation",
                            "operations.vendor_submission_confirmation.nested_owner_command",
                            "operations.vendor_submission_confirmation.active_caller_transaction",
                            "operations.vendor_submission_confirmation.nested_transaction_completion",
                        ),
                        mapping_owner="app.web.vendor_portal",
                        fail_closed_on=(
                            "invalid or expired signed proposal",
                            "vendor, user, project, or target mismatch",
                            "state fingerprint drift",
                            "ambiguous concurrent confirmation",
                            "missing stable result evidence",
                        ),
                    ),
                    events=EventContract(
                        event_types=("vendor_submission.confirmed",),
                        schema_version=1,
                        delivery_owner="events.dispatcher",
                        compatibility=(
                            "Version 1 is additive and contains submission, project, "
                            "stable result, command, and correlation identifiers only."
                        ),
                        replay=(
                            "The idempotency row is authoritative for command replay; "
                            "domain records and lifecycle events rebuild the outcome."
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "app.services.vendor_submission_proposals direct HTTP errors, "
                            "helper rollback, and service-owned commit"
                        ),
                        new_owner="operations.vendor_submission_confirmation",
                        verification=(
                            "Proposal scope, expiry, stale-state, replay, rollback, event, "
                            "web mapping, and architecture boundary tests."
                        ),
                        cutover_gate=(
                            "Vendor confirmation routes pass a typed command on a clean "
                            "session and all mutation branches return stable result evidence."
                        ),
                        fallback_retirement=(
                            "Transport-coded errors, direct commit/rollback, and mutation "
                            "before locked stale verification are removed."
                        ),
                    ),
                    steward="vendor operations",
                    design_refs=(
                        "docs/designs/UI_PROJECTION_CONTRACTS.md",
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/adr/0002-owner-command-transaction-boundary.md",
                    ),
                    test_refs=(
                        "tests/test_vendor_submission_proposals.py",
                        "tests/architecture/test_vendor_submission_confirmation_boundary.py",
                        "tests/test_vendor_lifecycle.py",
                    ),
                ),
            ),
        ),
        entrypoints=(
            "app.services.events.handlers.provisioning",
            "app.tasks.ont_provisioning",
            "app.web.admin.provisioning",
            "app.web.admin.projects",
            "app.web.vendor_portal",
            "app.api.vendor_portal",
            "app.api.projects",
            "app.api.field.*",
            "app.services.web_projects",
            "app.services.web_dispatch_work_orders",
            "app.services.work_order_commands",
            "field_mobile",
        ),
        rule=(
            "Provisioning callers resolve customer/network context through the "
            "shared context layer before executing workflow steps. Native project "
            "mutation adapters delegate to Projects.update for lifecycle consequences. "
            "Field clients consume completion_requirements from authenticated job "
            "detail and leave completion eligibility to the field transition service. "
            "Dispatch adapters delegate native work-order and assignment writes to "
            "operations.work_order_commands."
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
                depends_on=(
                    "auth.rbac_catalog",
                    "auth.subscriber_assignments",
                ),
            ),
            SOTService(
                name="auth.subscriber_assignments",
                module="app.services.subscriber_assignments",
                owns=("subscriber role and direct-permission assignments",),
                depends_on=(
                    "auth.rbac_catalog",
                    "events.dispatcher",
                    "observability.audit_log",
                ),
                notes=(
                    "This is the only application and seed writer for "
                    "subscriber_roles and subscriber_permissions. Public "
                    "commands own their complete transaction; reseller "
                    "onboarding and seed workflows use only flush-only "
                    "collaborators inside their coordinator transaction."
                ),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name=("subscriber role and direct-permission assignments"),
                            role=OwnerRole.COMMAND_WRITER,
                            input_names=(
                                "authorized subscriber assignment principal",
                                "active role and permission catalog",
                                "canonical subscriber assignment state",
                            ),
                            canonical_writer="auth.subscriber_assignments",
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="authorized subscriber assignment principal",
                            owner="auth.permission_gate",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=("rbac:assign evidence carried in CommandContext"),
                        ),
                        AuthorityInput(
                            name="active role and permission catalog",
                            owner="auth.rbac_catalog",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "active roles and active UI-assignable permissions"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical subscriber assignment state",
                            owner="auth.subscriber_assignments",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source="subscriber_roles and subscriber_permissions",
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.OWNER_MANAGED,
                        boundary=(
                            "Each public assignment command enters "
                            "execute_owner_command on a transaction-free session; "
                            "the grants, audit evidence, and versioned event commit "
                            "or roll back together. Reseller onboarding and seed "
                            "collaborators flush only."
                        ),
                        locking=(
                            "Target subscribers, active catalog references, and "
                            "existing grants are selected FOR UPDATE. Unique "
                            "constraints arbitrate concurrent duplicate grants."
                        ),
                        idempotency=(
                            "Duplicate grant and desired-state replacement "
                            "commands converge without parallel writes; adapter "
                            "intent keys are stored only as SHA-256 evidence."
                        ),
                        retries=(
                            "Adapters may retry a failed desired-state command "
                            "with the same intent key. Invalid scope, inactive "
                            "catalog, and conflict failures require changed input."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(
                            "auth.subscriber_assignments.invalid_command",
                            "auth.subscriber_assignments.invalid_scope",
                            "auth.subscriber_assignments.subscriber_not_found",
                            "auth.subscriber_assignments.role_not_found",
                            "auth.subscriber_assignments.permission_not_found",
                            "auth.subscriber_assignments.role_grant_not_found",
                            "auth.subscriber_assignments.permission_grant_not_found",
                            "auth.subscriber_assignments.assignment_conflict",
                            "auth.subscriber_assignments.invalid_command_context",
                            "auth.subscriber_assignments.command_contract_violation",
                            "auth.subscriber_assignments.nested_owner_command",
                            "auth.subscriber_assignments.active_caller_transaction",
                            (
                                "auth.subscriber_assignments."
                                "nested_transaction_completion"
                            ),
                        ),
                        mapping_owner=("app.api.rbac and app.web.admin.resellers"),
                        fail_closed_on=(
                            "missing rbac:assign evidence",
                            "inactive or non-assignable catalog references",
                            "invalid region or reseller grant scope",
                            "concurrent assignment conflicts",
                            "active caller transaction or manifest mismatch",
                        ),
                    ),
                    events=EventContract(
                        event_types=("subscriber.assignments_changed",),
                        schema_version=1,
                        delivery_owner="events.dispatcher",
                        compatibility=(
                            "Version 1 is additive and contains subscriber, role, "
                            "scope, and permission identifiers but no PII or raw "
                            "idempotency key."
                        ),
                        replay=(
                            "Events are immutable assignment-change evidence. "
                            "Canonical assignment and catalog tables remain the "
                            "rebuild inputs."
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "app.services.rbac subscriber assignment CRUD, "
                            "reseller onboarding role writes, and direct "
                            "scripts.seed.seed_rbac subscriber grant writes"
                        ),
                        new_owner="auth.subscriber_assignments",
                        verification=(
                            "Focused atomicity, scope, catalog-safety, API, "
                            "reseller, seed, cache, and architecture tests."
                        ),
                        cutover_gate=(
                            "Every application and seed subscriber assignment "
                            "write delegates to auth.subscriber_assignments."
                        ),
                        fallback_retirement=(
                            "The legacy app.services.rbac module and all direct "
                            "subscriber assignment writers are removed."
                        ),
                    ),
                    steward="platform security",
                    design_refs=(
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/adr/0002-owner-command-transaction-boundary.md",
                        "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                    ),
                    test_refs=(
                        "tests/test_subscriber_assignments.py",
                        ("tests/architecture/test_subscriber_assignment_boundary.py"),
                    ),
                ),
            ),
            SOTService(
                name="auth.rbac_catalog",
                module="app.services.rbac_catalog",
                owns=(
                    "role catalog and role-permission policy",
                    "permission catalog",
                ),
                depends_on=("events.dispatcher", "observability.audit_log"),
                notes=(
                    "This is the only application and seed writer for roles, "
                    "permissions, and role_permissions. Catalog identities are "
                    "case-normalized and protected by functional unique indexes. "
                    "Assigned identities cannot be renamed or deactivated, and "
                    "non-assignable permissions may be granted only to admin."
                ),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name="role catalog and role-permission policy",
                            role=OwnerRole.COMMAND_WRITER,
                            input_names=(
                                "authorized RBAC catalog principal",
                                "canonical role and role-permission catalog",
                                "system-user role grant references",
                                "subscriber role grant references",
                            ),
                            canonical_writer="auth.rbac_catalog",
                        ),
                        ConcernContract(
                            name="permission catalog",
                            role=OwnerRole.COMMAND_WRITER,
                            input_names=(
                                "authorized RBAC catalog principal",
                                "canonical permission catalog",
                                "system-user permission grant references",
                                "subscriber permission grant references",
                            ),
                            canonical_writer="auth.rbac_catalog",
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="authorized RBAC catalog principal",
                            owner="auth.permission_gate",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "rbac role/permission write or delete scope "
                                "evidence carried in CommandContext"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical role and role-permission catalog",
                            owner="auth.rbac_catalog",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source="roles and role_permissions",
                        ),
                        AuthorityInput(
                            name="canonical permission catalog",
                            owner="auth.rbac_catalog",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source="permissions",
                        ),
                        AuthorityInput(
                            name="system-user role grant references",
                            owner="auth.system_user_assignments",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source="system_user_roles references used by catalog safety policy",
                        ),
                        AuthorityInput(
                            name="subscriber role grant references",
                            owner="auth.subscriber_assignments",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source="subscriber_roles references used by catalog safety policy",
                        ),
                        AuthorityInput(
                            name="system-user permission grant references",
                            owner="auth.system_user_assignments",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "system_user_permissions references used by "
                                "catalog identity and deactivation safety policy"
                            ),
                        ),
                        AuthorityInput(
                            name="subscriber permission grant references",
                            owner="auth.subscriber_assignments",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "subscriber_permissions references used by "
                                "catalog identity and deactivation safety policy"
                            ),
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.OWNER_MANAGED,
                        boundary=(
                            "Each public catalog command enters "
                            "execute_owner_command on a transaction-free session; "
                            "the catalog row, complete role-permission policy, "
                            "audit evidence, and versioned event commit or roll "
                            "back together. Seed collaborators flush only."
                        ),
                        locking=(
                            "Existing catalog rows and relationship sets are "
                            "selected FOR UPDATE. Case-normalized PostgreSQL unique "
                            "indexes arbitrate concurrent natural-key creation, "
                            "while grant-reference checks fail closed before rename "
                            "or deactivation."
                        ),
                        idempotency=(
                            "Role-permission replacement and seed convergence use "
                            "desired sets; duplicate grants are no-ops. Adapter "
                            "intent keys are stored only as SHA-256 evidence."
                        ),
                        retries=(
                            "Adapters may retry failed desired-state commands with "
                            "the same intent key. Validation, protected catalog, "
                            "and in-use failures require changed authoritative input."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(
                            "auth.rbac_catalog.invalid_command",
                            "auth.rbac_catalog.invalid_role_name",
                            "auth.rbac_catalog.invalid_permission_key",
                            "auth.rbac_catalog.invalid_permissions",
                            "auth.rbac_catalog.role_not_found",
                            "auth.rbac_catalog.permission_not_found",
                            "auth.rbac_catalog.role_permission_not_found",
                            "auth.rbac_catalog.role_conflict",
                            "auth.rbac_catalog.permission_conflict",
                            "auth.rbac_catalog.catalog_conflict",
                            "auth.rbac_catalog.role_in_use",
                            "auth.rbac_catalog.permission_in_use",
                            "auth.rbac_catalog.protected_role",
                            "auth.rbac_catalog.protected_permission",
                            "auth.rbac_catalog.invalid_command_context",
                            "auth.rbac_catalog.command_contract_violation",
                            "auth.rbac_catalog.nested_owner_command",
                            "auth.rbac_catalog.active_caller_transaction",
                            "auth.rbac_catalog.nested_transaction_completion",
                        ),
                        mapping_owner=("app.api.rbac and app.web.admin.system"),
                        fail_closed_on=(
                            "missing catalog authorization evidence",
                            "case-normalized catalog collisions",
                            "rename or deactivation of assigned identities",
                            "protected admin role or permission changes",
                            "non-assignable permission grants outside admin",
                            "active caller transaction or manifest mismatch",
                        ),
                    ),
                    events=EventContract(
                        event_types=(
                            "rbac.role_catalog_changed",
                            "rbac.permission_catalog_changed",
                        ),
                        schema_version=1,
                        delivery_owner="events.dispatcher",
                        compatibility=(
                            "Version 1 is additive and contains authorization "
                            "identifiers but no PII or raw idempotency key."
                        ),
                        replay=(
                            "Events are immutable policy-change evidence. Canonical "
                            "catalog tables and checked-in seed desired sets remain "
                            "the rebuild inputs."
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "app.services.rbac catalog CRUD, "
                            "app.services.web_system_role_forms, and direct "
                            "scripts.seed.seed_rbac catalog writers"
                        ),
                        new_owner="auth.rbac_catalog",
                        verification=(
                            "Focused atomicity, normalization, protected-catalog, "
                            "API/web adapter, seed, migration, and architecture tests."
                        ),
                        cutover_gate=(
                            "Every application and seed catalog write delegates to "
                            "auth.rbac_catalog and subscriber grant references "
                            "are owned by auth.subscriber_assignments."
                        ),
                        fallback_retirement=(
                            "Multi-commit role forms and legacy role, permission, "
                            "and role-permission CRUD writers are removed."
                        ),
                    ),
                    steward="platform security",
                    design_refs=(
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/adr/0002-owner-command-transaction-boundary.md",
                        "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                    ),
                    test_refs=(
                        "tests/test_rbac_catalog_owner.py",
                        "tests/architecture/test_rbac_catalog_boundary.py",
                    ),
                ),
            ),
            SOTService(
                name="auth.system_user_assignments",
                module="app.services.system_user_assignments",
                owns=(
                    "system-user role and direct-permission assignments",
                    "source-scoped managed system-user role convergence",
                ),
                depends_on=(
                    "auth.rbac_catalog",
                    "auth.permission_gate",
                    "events.dispatcher",
                    "observability.audit_log",
                ),
                notes=(
                    "This is the only application writer for system_user_roles and "
                    "system_user_permissions. Local replacement preserves grants "
                    "owned by ERP or another source. Every role convergence locks "
                    "the active admin role before checking the final-active-admin "
                    "invariant. Public administrative replacement owns its complete "
                    "transaction; staff provisioning uses only flush-only "
                    "source-scoped collaborators inside its coordinator transaction."
                ),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name=("system-user role and direct-permission assignments"),
                            role=OwnerRole.COMMAND_WRITER,
                            input_names=(
                                "authorized system-user assignment principal",
                                "active role and permission catalog",
                                "canonical system-user assignment state",
                            ),
                            canonical_writer="auth.system_user_assignments",
                        ),
                        ConcernContract(
                            name=("source-scoped managed system-user role convergence"),
                            role=OwnerRole.COMMAND_WRITER,
                            input_names=(
                                "active role and permission catalog",
                                "canonical system-user assignment state",
                            ),
                            canonical_writer="auth.system_user_assignments",
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="authorized system-user assignment principal",
                            owner="auth.permission_gate",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "rbac:assign authorization evidence in the typed "
                                "CommandContext"
                            ),
                        ),
                        AuthorityInput(
                            name="active role and permission catalog",
                            owner="auth.rbac_catalog",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "active roles and active UI-assignable permissions"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical system-user assignment state",
                            owner="auth.system_user_assignments",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source="system_user_roles and system_user_permissions",
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.OWNER_MANAGED,
                        boundary=(
                            "The public replacement command enters "
                            "execute_owner_command on a transaction-free session; "
                            "roles, direct permissions, audit, and event evidence "
                            "commit or roll back together. Collaborator methods "
                            "flush but never complete a coordinator transaction."
                        ),
                        locking=(
                            "The target principal and existing grants are selected "
                            "FOR UPDATE. Every role change locks the active admin "
                            "role row before evaluating the final-active-admin "
                            "invariant, serializing competing removals and disables."
                        ),
                        idempotency=(
                            "Each source converges only its own global role grants; "
                            "direct permissions converge to the requested set. "
                            "Repeated desired state is a no-op and adapters carry a "
                            "stable intent key recorded only as a digest."
                        ),
                        retries=(
                            "Adapters may retry failed commands with the same intent "
                            "key. Validation and final-admin failures are not "
                            "retryable without a changed authoritative input."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(
                            "auth.system_user_assignments.invalid_command",
                            "auth.system_user_assignments.invalid_roles",
                            "auth.system_user_assignments.invalid_permissions",
                            "auth.system_user_assignments.system_user_not_found",
                            "auth.system_user_assignments.last_admin_required",
                            "auth.system_user_assignments.invalid_command_context",
                            "auth.system_user_assignments.command_contract_violation",
                            "auth.system_user_assignments.nested_owner_command",
                            "auth.system_user_assignments.active_caller_transaction",
                            "auth.system_user_assignments.nested_transaction_completion",
                        ),
                        mapping_owner="app.web.admin.system",
                        fail_closed_on=(
                            "missing assignment authorization evidence",
                            "inactive or unknown roles",
                            "inactive or non-assignable direct permissions",
                            "removal or deactivation of the final active admin",
                            "active caller transaction or nested completion",
                            "manifest mismatch",
                        ),
                    ),
                    events=EventContract(
                        event_types=("system_user.assignments_changed",),
                        schema_version=1,
                        delivery_owner="events.dispatcher",
                        compatibility=(
                            "Version 1 is additive and contains identifiers and "
                            "authorization keys but no raw idempotency key or PII."
                        ),
                        replay=(
                            "Events are immutable decision evidence; authoritative "
                            "assignment tables remain repairable by replaying the "
                            "source-specific desired grant command."
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "app.services.web_system_user_edit, "
                            "app.services.web_system_user_mutations, and legacy "
                            "app.services.rbac system-user grant helpers"
                        ),
                        new_owner="auth.system_user_assignments",
                        verification=(
                            "Focused atomicity, source preservation, final-admin, "
                            "adapter, and architecture boundary tests."
                        ),
                        cutover_gate=(
                            "All application-level system-user assignment writes "
                            "delegate to this owner and managed roles are read-only "
                            "in the local administrative editor."
                        ),
                        fallback_retirement=(
                            "Profile edits no longer write grants or active state; "
                            "legacy create and direct assignment mutation helpers "
                            "are removed."
                        ),
                    ),
                    steward="platform security",
                    design_refs=(
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/adr/0002-owner-command-transaction-boundary.md",
                        "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                    ),
                    test_refs=(
                        "tests/test_system_user_assignments.py",
                        "tests/architecture/test_system_user_assignment_boundary.py",
                    ),
                ),
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
                name="auth.credential_recovery",
                module="app.services.credential_recovery",
                owns=(
                    "password recovery request and delivery intent",
                    "password reset credential transition",
                    "credential recovery session projection invalidation",
                ),
                depends_on=(
                    "auth.token_signing",
                    "communications.intents",
                    "communications.ephemeral_actions",
                    "control.settings_spec",
                    "events.dispatcher",
                    "observability.audit_log",
                ),
                notes=(
                    "Recovery requests persist only PII-safe event context. "
                    "The communication consequence revalidates an exact local "
                    "principal and mints the bearer only at delivery time. "
                    "Capability redemption is the only password-reset writer "
                    "and atomically changes credentials, revokes database "
                    "sessions, stages audit evidence, and emits an event."
                ),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name="password recovery request and delivery intent",
                            role=OwnerRole.COMMAND_WRITER,
                            input_names=(
                                "credential recovery command evidence",
                                "canonical recoverable principal state",
                                "credential recovery policy settings",
                                "durable recovery delivery boundary",
                            ),
                            canonical_writer="auth.credential_recovery",
                        ),
                        ConcernContract(
                            name="password reset credential transition",
                            role=OwnerRole.COMMAND_WRITER,
                            input_names=(
                                "credential recovery command evidence",
                                "canonical recoverable principal state",
                                "credential recovery policy settings",
                                "verified recovery capability envelope",
                            ),
                            canonical_writer="auth.credential_recovery",
                        ),
                        ConcernContract(
                            name=(
                                "credential recovery session projection invalidation"
                            ),
                            role=OwnerRole.RECONCILER,
                            input_names=("canonical recoverable principal state",),
                            canonical_writer="auth.credential_recovery",
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="credential recovery command evidence",
                            owner="auth.credential_recovery",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "typed CommandContext carrying public-auth or "
                                "authorized-administrator actor, scope, reason, "
                                "correlation, and idempotency evidence"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical recoverable principal state",
                            owner="auth.credential_recovery",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "active Subscriber, SystemUser, or ResellerUser "
                                "identity and its active local user_credential, "
                                "password marker, and auth_sessions"
                            ),
                        ),
                        AuthorityInput(
                            name="credential recovery policy settings",
                            owner="control.settings_spec",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "declared password minimum and recovery lifetime "
                                "settings plus the owner-defined request rate policy"
                            ),
                        ),
                        AuthorityInput(
                            name="durable recovery delivery boundary",
                            owner="communications.intents",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "deduplicated communication intent and notification "
                                "outbox state created from the request event"
                            ),
                        ),
                        AuthorityInput(
                            name="verified recovery capability envelope",
                            owner="auth.token_signing",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "signature and expiry verified password_reset "
                                "claims minted for one exact principal"
                            ),
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.OWNER_MANAGED,
                        boundary=(
                            "Each public request or redemption enters "
                            "execute_owner_command on a transaction-free adapter "
                            "session. Request audit and outbox event, or credential "
                            "change, session revocation, audit, and completion "
                            "event commit or roll back together."
                        ),
                        locking=(
                            "Redemption selects the exact active principal and "
                            "local credential FOR UPDATE before comparing the "
                            "single-use password marker. Request rate limiting "
                            "precedes principal lookup."
                        ),
                        idempotency=(
                            "Each accepted request has one immutable event id and "
                            "its communication intent deduplicates on that id. A "
                            "redeemed capability is spent by password_updated_at, "
                            "so replay fails closed."
                        ),
                        retries=(
                            "Rolled-back commands may be retried with the same "
                            "intent evidence. Invalid or spent capabilities and "
                            "invalid passwords require changed input; event and "
                            "notification delivery retry independently."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(
                            "auth.credential_recovery.invalid_command",
                            "auth.credential_recovery.invalid_password",
                            "auth.credential_recovery.invalid_reset_capability",
                            "auth.credential_recovery.credential_not_found",
                            ("auth.credential_recovery.invalid_command_context"),
                            ("auth.credential_recovery.command_contract_violation"),
                            "auth.credential_recovery.nested_owner_command",
                            "auth.credential_recovery.active_caller_transaction",
                            ("auth.credential_recovery.nested_transaction_completion"),
                        ),
                        mapping_owner=(
                            "app.api.auth_flow, app.services.web_auth, and portal "
                            "or administrative web adapters"
                        ),
                        fail_closed_on=(
                            "invalid, expired, or spent capability",
                            "principal or recipient drift",
                            "inactive or missing local credential",
                            "active caller transaction or manifest mismatch",
                        ),
                    ),
                    events=EventContract(
                        event_types=(
                            "password_recovery.requested",
                            "password_recovery.completed",
                        ),
                        schema_version=1,
                        delivery_owner="events.dispatcher",
                        compatibility=(
                            "Version 1 contains identifiers, correlation evidence, "
                            "an email digest, and safe redirect context but never "
                            "raw email, password, hash, or bearer capability."
                        ),
                        replay=(
                            "Request-event replay converges on one communication "
                            "intent by event id. Completion events are immutable "
                            "evidence; credential state remains authoritative."
                        ),
                    ),
                    projections=(
                        ProjectionContract(
                            name=(
                                "recovery-invalidated authentication session "
                                "projections"
                            ),
                            input_names=("canonical recoverable principal state",),
                            writer="auth.credential_recovery",
                            freshness=(
                                "Completion-event dispatch invalidates auth and "
                                "portal session projections immediately after the "
                                "credential transaction commits."
                            ),
                            stale_behavior=(
                                "The event handler attempt remains failed and "
                                "retriable; durable auth_sessions revocation stays "
                                "authoritative while projection repair is pending."
                            ),
                            drift_signal=(
                                "A failed credential-session projection handler "
                                "attempt on the password_recovery.completed event."
                            ),
                            rebuild_operation=(
                                "Replay password_recovery.completed for the exact "
                                "principal to idempotently invalidate auth cache and "
                                "revoke customer or reseller portal sessions."
                            ),
                            repair_owner="auth.credential_recovery",
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "app.services.auth_flow forgot_password_flow, "
                            "request_password_reset, and reset_password plus "
                            "synchronous web and administrative email helpers"
                        ),
                        new_owner="auth.credential_recovery",
                        verification=(
                            "Focused request, materialization, redemption, replay, "
                            "session-revocation, adapter, and architecture tests."
                        ),
                        cutover_gate=(
                            "Public API, shared web, customer, reseller, admin, "
                            "staff-invite, and reseller-invite paths call only the "
                            "contracted owner or exact in-memory materializer."
                        ),
                        fallback_retirement=(
                            "Synchronous recovery email delivery, persisted bearer "
                            "content, adapter-owned credential mutation, service "
                            "HTTP exceptions, and service commits are removed."
                        ),
                    ),
                    steward="platform security",
                    design_refs=(
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/adr/0002-owner-command-transaction-boundary.md",
                        "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                    ),
                    test_refs=(
                        "tests/test_credential_recovery.py",
                        "tests/architecture/test_credential_recovery_boundary.py",
                    ),
                ),
            ),
            SOTService(
                name="auth.customer_credential_enrollment",
                module="app.services.customer_credential_enrollment",
                owns=(
                    "credential enrollment delivery request",
                    "referral-created customer local credential enrollment",
                    "credential enrollment capability purpose claims and lifetime",
                    "single-use enrollment and account email verification consequence",
                    "credential enrollment authentication cache projection",
                ),
                depends_on=(
                    "auth.token_signing",
                    "communications.intents",
                    "customer.accounts",
                    "referrals.account_conversion",
                    "communications.ephemeral_actions",
                    "control.settings_spec",
                    "events.dispatcher",
                    "observability.audit_log",
                ),
                notes=(
                    "Creates no placeholder credential. The local credential and "
                    "Subscriber email verification are committed together only "
                    "after the emailed capability is redeemed. Party quarantine, "
                    "Party contact verification, and account/subscription state "
                    "remain with their existing owners."
                ),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name="credential enrollment delivery request",
                            role=OwnerRole.COMMAND_WRITER,
                            input_names=(
                                "credential enrollment command evidence",
                                "canonical referral account context",
                                "canonical customer credential state",
                                "credential enrollment policy settings",
                                "durable enrollment delivery intent",
                            ),
                            canonical_writer="auth.customer_credential_enrollment",
                        ),
                        ConcernContract(
                            name=(
                                "referral-created customer local credential enrollment"
                            ),
                            role=OwnerRole.COMMAND_WRITER,
                            input_names=(
                                "credential enrollment command evidence",
                                "canonical referral account context",
                                "canonical customer credential state",
                                "credential enrollment policy settings",
                                "verified enrollment capability envelope",
                            ),
                            canonical_writer="auth.customer_credential_enrollment",
                        ),
                        ConcernContract(
                            name=(
                                "credential enrollment capability purpose claims "
                                "and lifetime"
                            ),
                            role=OwnerRole.POLICY,
                            input_names=(
                                "canonical referral account context",
                                "canonical customer credential state",
                                "credential enrollment policy settings",
                                "verified enrollment capability envelope",
                            ),
                        ),
                        ConcernContract(
                            name=(
                                "single-use enrollment and account email "
                                "verification consequence"
                            ),
                            role=OwnerRole.COMMAND_WRITER,
                            input_names=(
                                "credential enrollment command evidence",
                                "canonical customer credential state",
                                "verified enrollment capability envelope",
                            ),
                            canonical_writer="auth.customer_credential_enrollment",
                        ),
                        ConcernContract(
                            name=(
                                "credential enrollment authentication cache projection"
                            ),
                            role=OwnerRole.RECONCILER,
                            input_names=("canonical customer credential state",),
                            canonical_writer="auth.customer_credential_enrollment",
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="credential enrollment command evidence",
                            owner="auth.customer_credential_enrollment",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "typed CommandContext carrying the public referral "
                                "or capability actor, scope, reason, command, "
                                "correlation, and idempotency evidence"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical referral account context",
                            owner="referrals.account_conversion",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "active Referral, referred Party and Lead binding, "
                                "and the exact converted Subscriber identifier"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical customer credential state",
                            owner="auth.customer_credential_enrollment",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "active converted Subscriber identity, email and "
                                "email_verified state plus its local user_credential"
                            ),
                        ),
                        AuthorityInput(
                            name="credential enrollment policy settings",
                            owner="control.settings_spec",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "database-authoritative password minimum, user invite "
                                "lifetime, credential enrollment request limit, and "
                                "request window settings"
                            ),
                        ),
                        AuthorityInput(
                            name="verified enrollment capability envelope",
                            owner="auth.token_signing",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "signature and expiry verified referral enrollment "
                                "claims for one exact referral, Party, Lead, "
                                "Subscriber, and email digest"
                            ),
                        ),
                        AuthorityInput(
                            name="durable enrollment delivery intent",
                            owner="communications.intents",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "referral-deduplicated communication intent and "
                                "notification outbox with a non-secret ephemeral "
                                "action descriptor"
                            ),
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.OWNER_MANAGED,
                        boundary=(
                            "Each request or redemption enters "
                            "execute_owner_command on a transaction-free adapter "
                            "session. Request intent, audit, and event, or local "
                            "credential, Subscriber email verification, audit, and "
                            "completion event commit or roll back together."
                        ),
                        locking=(
                            "Requests and redemption lock the exact Referral, Lead, "
                            "and Subscriber in canonical order. Redemption rechecks "
                            "the absence of a local credential; the normalized local "
                            "username unique index arbitrates cross-principal races."
                        ),
                        idempotency=(
                            "A referral has one communication intent dedupe key. "
                            "Delivery retries remint the bearer from canonical "
                            "context. Credential existence spends every outstanding "
                            "capability, so replay fails closed."
                        ),
                        retries=(
                            "Rolled-back commands may retry after transient database "
                            "failures. Rate-limited requests return a typed outcome; "
                            "delivery and cache projection retry independently."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(
                            "auth.customer_credential_enrollment.invalid_command",
                            (
                                "auth.customer_credential_enrollment."
                                "invalid_configuration"
                            ),
                            "auth.customer_credential_enrollment.context_not_found",
                            "auth.customer_credential_enrollment.stale_context",
                            "auth.customer_credential_enrollment.inactive_account",
                            "auth.customer_credential_enrollment.invalid_capability",
                            "auth.customer_credential_enrollment.invalid_password",
                            "auth.customer_credential_enrollment.invalid_username",
                            (
                                "auth.customer_credential_enrollment."
                                "username_unavailable"
                            ),
                            (
                                "auth.customer_credential_enrollment."
                                "invalid_command_context"
                            ),
                            (
                                "auth.customer_credential_enrollment."
                                "command_contract_violation"
                            ),
                            (
                                "auth.customer_credential_enrollment."
                                "nested_owner_command"
                            ),
                            (
                                "auth.customer_credential_enrollment."
                                "active_caller_transaction"
                            ),
                            (
                                "auth.customer_credential_enrollment."
                                "nested_transaction_completion"
                            ),
                        ),
                        mapping_owner=(
                            "app.api.crm_referrals, app.api.auth_flow, and "
                            "app.services.web_customer_auth adapters"
                        ),
                        fail_closed_on=(
                            "invalid, expired, or spent capability",
                            "referral, Party, Lead, Subscriber, or recipient drift",
                            "inactive account or existing local credential",
                            "username collision",
                            "missing or invalid canonical policy configuration",
                            "active caller transaction or manifest mismatch",
                        ),
                    ),
                    events=EventContract(
                        event_types=(
                            "customer_credential_enrollment.requested",
                            "customer_credential_enrollment.completed",
                        ),
                        schema_version=1,
                        delivery_owner="events.dispatcher",
                        compatibility=(
                            "Version 1 contains canonical identifiers, command and "
                            "correlation evidence, delivery outcome, and an email "
                            "digest but never raw email, password, hash, rendered "
                            "content, or bearer capability."
                        ),
                        replay=(
                            "Request replay converges on the referral-deduplicated "
                            "communication intent. Completion replay leaves the "
                            "existing credential authoritative and repairs its auth "
                            "cache projection idempotently."
                        ),
                    ),
                    projections=(
                        ProjectionContract(
                            name="enrolled customer authentication cache",
                            input_names=("canonical customer credential state",),
                            writer="auth.customer_credential_enrollment",
                            freshness=(
                                "Completion-event dispatch invalidates the exact "
                                "subscriber authentication cache immediately after "
                                "the credential transaction commits."
                            ),
                            stale_behavior=(
                                "The handler attempt remains failed and retriable; "
                                "the committed credential and Subscriber email "
                                "verification remain authoritative."
                            ),
                            drift_signal=(
                                "A failed CredentialSessionProjectionHandler attempt "
                                "on customer_credential_enrollment.completed."
                            ),
                            rebuild_operation=(
                                "Replay customer_credential_enrollment.completed for "
                                "the exact subscriber to invalidate its auth cache."
                            ),
                            repair_owner="auth.customer_credential_enrollment",
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "uncontracted request_referral_enrollment and "
                            "complete_referral_enrollment service functions with "
                            "helper commits, nested transactions, transport-coded "
                            "errors, and best-effort cache invalidation"
                        ),
                        new_owner="auth.customer_credential_enrollment",
                        verification=(
                            "Focused request, suppression, dedupe, materialization, "
                            "redemption, replay, drift, event, projection, adapter, "
                            "and architecture tests."
                        ),
                        cutover_gate=(
                            "Referral signup, public auth API, and customer portal "
                            "form submit only typed commands on transaction-free "
                            "sessions; materialization remains transport-only."
                        ),
                        fallback_retirement=(
                            "Service commits, savepoints, status-coded domain errors, "
                            "adapter keyword mutation calls, direct best-effort cache "
                            "invalidation, and duplicate delivery intents are removed."
                        ),
                    ),
                    steward="platform security",
                    design_refs=(
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/REFERRAL_CREDENTIAL_ENROLLMENT.md",
                        "docs/adr/0002-owner-command-transaction-boundary.md",
                        "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                    ),
                    test_refs=(
                        "tests/test_referral_credential_enrollment.py",
                        (
                            "tests/architecture/"
                            "test_customer_credential_enrollment_boundary.py"
                        ),
                    ),
                ),
            ),
            SOTService(
                name="auth.staff_provisioning",
                module="app.services.staff_provisioning",
                owns=("staff account provisioning", "staff identity bootstrap"),
                depends_on=(
                    "auth.rbac_catalog",
                    "auth.system_user_assignments",
                    "auth.permission_gate",
                    "communications.intents",
                    "communications.ephemeral_actions",
                    "events.dispatcher",
                    "observability.audit_log",
                ),
                notes=(
                    "ERP HR commands enter one verified coordinator transaction. "
                    "This owner writes staff identity and credential bootstrap, "
                    "delegates managed grants to auth.system_user_assignments, "
                    "stages audit and "
                    "versioned events atomically, and leaves invite delivery to a "
                    "deduplicated communication consequence. Invite capabilities "
                    "are minted only at transport time and are never stored in the "
                    "event or notification outbox."
                ),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name="staff account provisioning",
                            role=OwnerRole.APPLICATION_COORDINATOR,
                            input_names=(
                                "ERP HR staff lifecycle request",
                                "authorized RBAC assignment principal",
                                "active role catalog",
                                "managed role grant state",
                                "canonical staff identity and credential state",
                            ),
                        ),
                        ConcernContract(
                            name="staff identity bootstrap",
                            role=OwnerRole.COMMAND_WRITER,
                            input_names=(
                                "ERP HR staff lifecycle request",
                                "canonical staff identity and credential state",
                            ),
                            canonical_writer="auth.staff_provisioning",
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="ERP HR staff lifecycle request",
                            owner="external:dotmac_erp",
                            kind=AuthorityKind.EXTERNAL_OBSERVATION,
                            source=(
                                "typed provision, managed-role, and active-state "
                                "commands received by app.api.staff_sync"
                            ),
                        ),
                        AuthorityInput(
                            name="authorized RBAC assignment principal",
                            owner="auth.permission_gate",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "rbac:assign authorization result carried in "
                                "CommandContext actor and scope evidence"
                            ),
                        ),
                        AuthorityInput(
                            name="active role catalog",
                            owner="auth.rbac_catalog",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source="active roles",
                        ),
                        AuthorityInput(
                            name="managed role grant state",
                            owner="auth.system_user_assignments",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "source-scoped rows in system_user_roles and the "
                                "final-active-admin invariant"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical staff identity and credential state",
                            owner="auth.staff_provisioning",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "system_users and staff-bound local user_credentials"
                            ),
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.COORDINATOR_MANAGED,
                        boundary=(
                            "Each public staff write enters execute_owner_command "
                            "on a transaction-free adapter session; identity, "
                            "credentials, RBAC grants, session revocation, audit, "
                            "and the outbox event commit together before return."
                        ),
                        locking=(
                            "A PostgreSQL advisory transaction lock serializes "
                            "provisioning by normalized email; existing principals "
                            "are selected FOR UPDATE and database unique constraints "
                            "arbitrate identity and grant keys."
                        ),
                        idempotency=(
                            "Email is the provision natural key; managed roles and "
                            "active state converge to requested sets. Adapters carry "
                            "a stable intent key, and invite expansion deduplicates "
                            "on the immutable provisioning event id."
                        ),
                        retries=(
                            "Adapters may retry a failed request with the same "
                            "idempotency key. Domain validation is not retryable; "
                            "event-store delivery retries consequences independently."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(
                            "auth.staff_provisioning.invalid_command",
                            "auth.staff_provisioning.unknown_roles",
                            "auth.staff_provisioning.staff_account_not_found",
                            "auth.staff_provisioning.identity_conflict",
                            "auth.system_user_assignments.last_admin_required",
                            "auth.staff_provisioning.invalid_command_context",
                            "auth.staff_provisioning.command_contract_violation",
                            "auth.staff_provisioning.nested_owner_command",
                            "auth.staff_provisioning.active_caller_transaction",
                            "auth.staff_provisioning.nested_transaction_completion",
                        ),
                        mapping_owner="app.api.staff_sync",
                        fail_closed_on=(
                            "missing authorization evidence",
                            "unknown or inactive roles",
                            "identity conflict",
                            "active caller transaction",
                            "nested command or transaction completion",
                            "manifest mismatch",
                        ),
                    ),
                    events=EventContract(
                        event_types=(
                            "staff_account.provisioned",
                            "staff_account.roles_changed",
                            "staff_account.activated",
                            "staff_account.deactivated",
                        ),
                        schema_version=1,
                        delivery_owner="events.dispatcher",
                        compatibility=(
                            "Version 1 is additive and PII-safe; breaking payload "
                            "changes require a new schema version."
                        ),
                        replay=(
                            "State events are immutable evidence. Staff invitation "
                            "intent expansion is idempotent by event_id and mints a "
                            "fresh short-lived capability only at delivery time."
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "app.services.web_system_user_mutations and the legacy "
                            "multi-commit staff provisioning path"
                        ),
                        new_owner="auth.staff_provisioning",
                        verification=(
                            "Focused API, transaction, event, audit, RBAC, and "
                            "ephemeral-delivery tests plus architecture guards."
                        ),
                        cutover_gate=(
                            "All staff-sync write routes call only typed owner "
                            "commands and contain no persistence mutation."
                        ),
                        fallback_retirement=(
                            "Staff sync no longer calls web_system_user_mutations "
                            "or synchronous email delivery."
                        ),
                    ),
                    steward="platform security",
                    design_refs=(
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/adr/0002-owner-command-transaction-boundary.md",
                        "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                    ),
                    test_refs=(
                        "tests/test_api_staff_sync.py",
                        "tests/test_staff_provisioning_owner.py",
                        "tests/architecture/test_staff_provisioning_boundary.py",
                    ),
                ),
            ),
            SOTService(
                name="auth.reseller_onboarding",
                module="app.services.reseller_onboarding",
                owns=("reseller portal principal onboarding",),
                depends_on=(
                    "customer.accounts",
                    "auth.subscriber_assignments",
                    "auth.permission_gate",
                    "communications.intents",
                    "communications.ephemeral_actions",
                    "control.feature_registry",
                    "events.dispatcher",
                    "observability.audit_log",
                ),
                notes=(
                    "Administrative reseller onboarding enters one verified "
                    "coordinator transaction. Canonical reseller and fallback "
                    "Subscriber initialization, portal identity and credential "
                    "bootstrap, assignment-owner grants, audit, and events commit "
                    "atomically. Invitations are deduplicated event consequences; "
                    "reset capabilities are minted only at transport time for the "
                    "exact principal and never persisted in the outbox."
                ),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name="reseller portal principal onboarding",
                            role=OwnerRole.APPLICATION_COORDINATOR,
                            input_names=(
                                "authorized reseller onboarding principal",
                                "canonical reseller and subscriber account state",
                                "canonical subscriber assignment state",
                                "reseller principal cutover gate",
                                "canonical reseller onboarding state",
                            ),
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="authorized reseller onboarding principal",
                            owner="auth.permission_gate",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "reseller:write and, when needed, rbac:assign "
                                "evidence carried in correlated CommandContexts"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical reseller and subscriber account state",
                            owner="customer.accounts",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "resellers, subscribers, and transaction-neutral "
                                "canonical initialization collaborators"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical subscriber assignment state",
                            owner="auth.subscriber_assignments",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source="subscriber_roles and active role catalog references",
                        ),
                        AuthorityInput(
                            name="reseller principal cutover gate",
                            owner="control.feature_registry",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source="reseller_user_principal_enabled feature setting",
                        ),
                        AuthorityInput(
                            name="canonical reseller onboarding state",
                            owner="auth.reseller_onboarding",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "reseller_users and reseller-bound local "
                                "user_credentials"
                            ),
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.COORDINATOR_MANAGED,
                        boundary=(
                            "Each public onboarding command enters "
                            "execute_owner_command on a transaction-free adapter "
                            "session; every record, grant, audit event, and outbox "
                            "event commits or rolls back together."
                        ),
                        locking=(
                            "Existing resellers and active role references are "
                            "selected FOR UPDATE. PostgreSQL advisory transaction "
                            "locks serialize normalized email and username keys, "
                            "with database constraints arbitrating remaining races."
                        ),
                        idempotency=(
                            "Adapters carry stable intent keys as hashed evidence. "
                            "Identity conflicts fail closed, assignment grants "
                            "converge, and invite expansion deduplicates by event id."
                        ),
                        retries=(
                            "Adapters may retry a rolled-back command with the same "
                            "intent key. Validation and identity conflicts require "
                            "changed input; event delivery retries independently."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(
                            "auth.reseller_onboarding.invalid_command",
                            (
                                "auth.reseller_onboarding."
                                "assignment_authorization_required"
                            ),
                            "auth.reseller_onboarding.identity_conflict",
                            "auth.reseller_onboarding.reseller_not_found",
                            "auth.reseller_onboarding.inactive_reseller",
                            "auth.reseller_onboarding.role_not_found",
                            "auth.reseller_onboarding.unsupported_role_target",
                            ("auth.reseller_onboarding.invalid_command_context"),
                            ("auth.reseller_onboarding.command_contract_violation"),
                            "auth.reseller_onboarding.nested_owner_command",
                            "auth.reseller_onboarding.active_caller_transaction",
                            ("auth.reseller_onboarding.nested_transaction_completion"),
                        ),
                        mapping_owner="app.web.admin.resellers",
                        fail_closed_on=(
                            "missing or mismatched authorization evidence",
                            "inactive reseller or role",
                            "identity collision",
                            "unsupported first-class principal role assignment",
                            "active caller transaction or manifest mismatch",
                        ),
                    ),
                    events=EventContract(
                        event_types=(
                            "reseller.created",
                            "reseller_user.provisioned",
                            "subscriber.created",
                        ),
                        schema_version=1,
                        delivery_owner="events.dispatcher",
                        compatibility=(
                            "Version 1 onboarding events contain identifiers, "
                            "role names, and an email digest but no PII, password, "
                            "or reset capability."
                        ),
                        replay=(
                            "Events are immutable evidence. Invitation expansion "
                            "is idempotent by event id and revalidates the exact "
                            "canonical principal before minting a capability."
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "app.services.web_admin_resellers and "
                            "app.services.reseller_portal multi-commit onboarding"
                        ),
                        new_owner="auth.reseller_onboarding",
                        verification=(
                            "Focused atomicity, delivery, reset, adapter, manifest, "
                            "and architecture-boundary tests."
                        ),
                        cutover_gate=(
                            "Admin reseller creation and add-user routes call only "
                            "typed coordinator commands."
                        ),
                        fallback_retirement=(
                            "Compensating deletion, direct onboarding commits, and "
                            "synchronous invite delivery are removed."
                        ),
                    ),
                    steward="platform security",
                    design_refs=(
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/adr/0002-owner-command-transaction-boundary.md",
                        "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                    ),
                    test_refs=(
                        "tests/test_reseller_onboarding.py",
                        "tests/architecture/test_reseller_onboarding_boundary.py",
                    ),
                ),
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
                depends_on=(
                    "events.dispatcher",
                    "financial.prepaid_enforcement_state",
                ),
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
            SOTService(
                name="access.fup_rule_engine",
                module="app.services.fup",
                owns=(
                    "FUP policy and rule definitions (CRUD)",
                    "FUP rule evaluation and simulation",
                ),
                depends_on=("access.fup_usage_windows",),
                notes=(
                    "Pure decision engine shared by the enforcement sweep and "
                    "the what-if simulator; it writes no enforcement state."
                ),
            ),
            SOTService(
                name="access.fup_runtime_state",
                module="app.services.fup_state",
                owns=("FUP per-subscription runtime state rows",),
                depends_on=("events.dispatcher",),
                notes=(
                    "State store only: get/apply/clear/list. Decisions live in "
                    "the rule engine and the enforcement sweep."
                ),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name="FUP per-subscription runtime state rows",
                            role=OwnerRole.PROJECTION_WRITER,
                            input_names=(
                                "canonical subscription offer state",
                                "resolved FUP enforcement consequence",
                                "applied access consequence evidence",
                            ),
                            canonical_writer="access.fup_runtime_state",
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="canonical subscription offer state",
                            owner="access.subscription_lifecycle",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source="Subscription.id and Subscription.offer_id",
                        ),
                        AuthorityInput(
                            name="resolved FUP enforcement consequence",
                            owner="access.fup_enforcement_sweep",
                            kind=AuthorityKind.DERIVED_PROJECTION,
                            source=(
                                "rule, action, cap-reset, and evaluation-time "
                                "evidence from the FUP sweep"
                            ),
                        ),
                        AuthorityInput(
                            name="applied access consequence evidence",
                            owner="access.session_enforcement",
                            kind=AuthorityKind.DERIVED_PROJECTION,
                            source=(
                                "successfully applied throttle, block, suspend, "
                                "restore, or reset consequence"
                            ),
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.PARTICIPANT,
                        boundary=(
                            "FUP enforcement owners pass a typed transition. The "
                            "participant locks the Subscription and FupState rows, "
                            "then flushes state and event evidence without commit."
                        ),
                        locking=(
                            "The canonical Subscription row serializes creation and "
                            "the FupState row is selected FOR UPDATE before change."
                        ),
                        idempotency=(
                            "An exact typed transition replay is a no-op; clear is a "
                            "no-op when the runtime projection is already neutral."
                        ),
                        retries=(
                            "The surrounding enforcement owner retries the complete "
                            "consequence transaction; the participant never retries "
                            "or commits independently."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(
                            "access.fup_runtime_state.invalid_subscription_id",
                            "access.fup_runtime_state.invalid_evaluated_at",
                            "access.fup_runtime_state.invalid_cap_resets_at",
                            "access.fup_runtime_state.invalid_before",
                            "access.fup_runtime_state.invalid_event_evidence",
                            "access.fup_runtime_state.subscription_not_found",
                            "access.fup_runtime_state.offer_required",
                            "access.fup_runtime_state.offer_mismatch",
                            "access.fup_runtime_state.state_offer_mismatch",
                        ),
                        mapping_owner=(
                            "access.fup_enforcement_sweep and enforcement event "
                            "consequence owners"
                        ),
                        fail_closed_on=(
                            "missing or mismatched subscription/offer identity",
                            "naive evaluation or reset time",
                            "runtime state persistence failure",
                        ),
                    ),
                    events=EventContract(
                        event_types=("fup.runtime_state_changed",),
                        schema_version=1,
                        delivery_owner="events.dispatcher",
                        compatibility=(
                            "Version 1 is additive and contains subscription, offer, "
                            "transition, and action vocabulary without usage values."
                        ),
                        replay=(
                            "Exact transition replay is idempotent. The current row "
                            "is rebuilt from subscription, rule, usage, and applied "
                            "access consequence evidence."
                        ),
                    ),
                    projections=(
                        ProjectionContract(
                            name="current per-subscription FUP enforcement posture",
                            input_names=(
                                "canonical subscription offer state",
                                "resolved FUP enforcement consequence",
                                "applied access consequence evidence",
                            ),
                            writer="access.fup_runtime_state",
                            freshness="FupState.last_evaluated_at",
                            stale_behavior=(
                                "Never relax access from stale state; expose the stale "
                                "projection and request enforcement reconciliation."
                            ),
                            drift_signal=(
                                "Compare runtime action/profile/reset evidence with "
                                "canonical rules, usage window, access locks, and "
                                "RADIUS projection."
                            ),
                            rebuild_operation=(
                                "Run the scoped FUP enforcement reconciliation for "
                                "the subscription and reapply or clear exact state."
                            ),
                            repair_owner="access.fup_enforcement_sweep",
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "event-handler and sweep-local free-form FupState writes "
                            "with implicit wall-clock time"
                        ),
                        new_owner="access.fup_runtime_state",
                        verification=(
                            "Typed transition, locking, idempotency, atomic-event, "
                            "reset, lift, sweep, and single-writer tests."
                        ),
                        cutover_gate=(
                            "All FupState mutations pass typed commands with owner-"
                            "supplied evaluation time through the participant."
                        ),
                        fallback_retirement=(
                            "Free-form state mutation, implicit datetime.now, silent "
                            "offer mismatch, and parallel FupState writers are removed."
                        ),
                    ),
                    steward="network access",
                    design_refs=(
                        "docs/designs/FUP_CONSUMPTION_WINDOWS.md",
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/adr/0002-owner-command-transaction-boundary.md",
                    ),
                    test_refs=(
                        "tests/test_fup_runtime_state_owner.py",
                        "tests/architecture/test_fup_runtime_state_boundary.py",
                        "tests/test_fup_lift_enforcement.py",
                        "tests/test_fup_evaluate_commits.py",
                    ),
                ),
            ),
            SOTService(
                name="access.fup_usage_windows",
                module="app.services.fup_usage",
                owns=(
                    "FUP consumption window bounds",
                    "windowed FUP usage aggregation",
                ),
                depends_on=("sessions.radius_reconciliation",),
                notes=(
                    "Single source of truth for FUP consumption windows and "
                    "windowed usage reads; read-only over usage facts."
                ),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name="FUP consumption window bounds",
                            role=OwnerRole.RESOLVER,
                            input_names=("FUP consumption period policy",),
                        ),
                        ConcernContract(
                            name="windowed FUP usage aggregation",
                            role=OwnerRole.RESOLVER,
                            input_names=(
                                "FUP consumption period policy",
                                "rated quota and session usage facts",
                            ),
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="FUP consumption period policy",
                            owner="access.fup_usage_windows",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "typed period argument normalized to the supported "
                                "daily, weekly, or monthly vocabulary"
                            ),
                        ),
                        AuthorityInput(
                            name="rated quota and session usage facts",
                            owner="sessions.radius_reconciliation",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "rated QuotaBucket totals and timestamped RADIUS "
                                "usage samples"
                            ),
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.READ_ONLY,
                        boundary=(
                            "Window resolution and aggregation read usage facts on "
                            "the caller session and never flush or complete a "
                            "transaction."
                        ),
                        locking="Read-only aggregation requires no mutation lock.",
                        idempotency=(
                            "The same period, timezone, timestamp, and usage facts "
                            "produce the same aligned window and total."
                        ),
                        retries=(
                            "Callers may retry reads; unavailable sample evidence "
                            "returns an explicit non-authoritative no-data result."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(),
                        mapping_owner="FUP enforcement and usage-summary adapters",
                        fail_closed_on=(
                            "missing or unavailable non-monthly usage evidence",
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "call-site-local FUP period arithmetic and direct usage "
                            "reads"
                        ),
                        new_owner="access.fup_usage_windows",
                        verification=(
                            "Window-boundary, authoritative-source, and no-data "
                            "behavior tests cover every supported period."
                        ),
                        cutover_gate=(
                            "FUP evaluation, usage summaries, and notifications use "
                            "the shared windowed reader."
                        ),
                        fallback_retirement=(
                            "Independent daily, weekly, and monthly window "
                            "calculations are removed from callers."
                        ),
                    ),
                    steward="network access",
                    design_refs=(
                        "docs/designs/FUP_CONSUMPTION_WINDOWS.md",
                        "docs/SOT_RELATIONSHIP_MAP.md",
                    ),
                    test_refs=(
                        "tests/test_fup_window_bounds.py",
                        "tests/test_fup_usage_reader.py",
                    ),
                ),
            ),
            SOTService(
                name="access.fup_enforcement_sweep",
                module="app.services.fup_enforcement",
                owns=(
                    "FUP sweep enforce/warn/reset decisions",
                    "FUP enforcement transition and cooldown hysteresis",
                    "FUP repeat-upsell nudge policy",
                    "FUP customer notification fan-out",
                ),
                depends_on=(
                    "access.session_enforcement",
                    "control.settings_spec",
                    "events.dispatcher",
                ),
                notes=(
                    "Celery tasks keep only the advisory-lock plumbing, task "
                    "names, and queue chaining; the sweep owns every "
                    "enforce/warn/reset/repeat-upsell decision."
                ),
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
                name="ui.referral_list_projection",
                module="app.services.web_referrals",
                owns=(
                    "admin referral filter and stable sort semantics",
                    "admin referral row and page projection",
                    "admin referral KPI values and exact cohort links",
                    "admin referral list canonical URL",
                ),
                depends_on=(
                    "ui.list_contracts",
                    "ui.projection_contracts",
                    "referrals.program",
                ),
                notes=(
                    "The route redirects stale or clamped request state to the "
                    "owner-provided canonical URL. Templates render ListQuery, "
                    "PageMeta, and Kpi contracts without deriving totals, cohort "
                    "links, sort rules, or pagination strings."
                ),
                contract=ServiceContract(
                    concerns=tuple(
                        ConcernContract(
                            name=concern,
                            role=OwnerRole.RESOLVER,
                            input_names=(
                                "canonical referral program state",
                                "normalized referral list query",
                                "UI projection vocabulary",
                            ),
                        )
                        for concern in (
                            "admin referral filter and stable sort semantics",
                            "admin referral row and page projection",
                            "admin referral KPI values and exact cohort links",
                            "admin referral list canonical URL",
                        )
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="canonical referral program state",
                            owner="referrals.program",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "Referral, ReferralCode, Party, Lead, Subscriber, "
                                "and resolved referral-program policy"
                            ),
                        ),
                        AuthorityInput(
                            name="normalized referral list query",
                            owner="ui.list_contracts",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source="REFERRAL_LIST_DEFINITION and normalized ListQuery",
                        ),
                        AuthorityInput(
                            name="UI projection vocabulary",
                            owner="ui.projection_contracts",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source="StateValue and Kpi contracts",
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.READ_ONLY,
                        boundary=(
                            "The projection reads referral and program facts on the "
                            "adapter session and never mutates or completes a "
                            "transaction."
                        ),
                        locking="Stable read projection requires no mutation lock.",
                        idempotency=(
                            "The same canonical query and referral facts produce the "
                            "same rows, counts, cohort URLs, and canonical URL."
                        ),
                        retries="Read-only projection calls are safe to retry.",
                    ),
                    errors=ErrorContract(
                        domain_codes=(),
                        mapping_owner="app.web.admin.crm_referrals",
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "app.web.admin.crm_referrals route-local filtering and "
                            "templates/admin/referrals list derivation"
                        ),
                        new_owner="ui.referral_list_projection",
                        verification=(
                            "List, stable-sort, exact-cohort KPI, canonicalization, "
                            "and template boundary tests."
                        ),
                        cutover_gate=(
                            "Admin referral routes and templates consume only the "
                            "owner-provided ListQuery, PageMeta, rows, and KPIs."
                        ),
                        fallback_retirement=(
                            "Route-local pagination and template-derived referral "
                            "totals, filters, and URLs are removed."
                        ),
                    ),
                    steward="subscriber growth",
                    design_refs=(
                        "docs/designs/LIST_QUERY_MIGRATION.md",
                        "docs/designs/UI_PROJECTION_CONTRACTS.md",
                        "docs/SOT_RELATIONSHIP_MAP.md",
                    ),
                    test_refs=(
                        "tests/test_web_referrals_list.py",
                        "tests/architecture/test_template_projection_boundary.py",
                    ),
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
                    "(it issues no SQL of its own). Native form mutations delegate to "
                    "operations.work_order_commands; no bulk command is declared. "
                    "Each dispatch route is granularly gated "
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
                name="ui.projection_contracts",
                module="app.services.ui_contracts",
                owns=(
                    "UI value availability and freshness contract",
                    "UI KPI exact-cohort contract",
                    "UI action eligibility and confirmation contract",
                ),
                depends_on=("ui.status_presentation",),
                notes=(
                    "Transport-neutral StateValue, Kpi, and Action shapes. Domain "
                    "read and command owners supply the facts and decisions; "
                    "templates and clients render them without deriving meaning."
                ),
                contract=ServiceContract(
                    concerns=tuple(
                        ConcernContract(
                            name=concern,
                            role=OwnerRole.POLICY,
                            input_names=("UI projection contract vocabulary",),
                        )
                        for concern in (
                            "UI value availability and freshness contract",
                            "UI KPI exact-cohort contract",
                            "UI action eligibility and confirmation contract",
                        )
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="UI projection contract vocabulary",
                            owner="ui.projection_contracts",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "StateKind, StateValue, Kpi, and Action typed "
                                "invariants"
                            ),
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.NOT_APPLICABLE,
                        boundary=(
                            "Typed projection value objects validate in memory and "
                            "never access a database session."
                        ),
                        locking="Immutable value objects require no lock.",
                        idempotency=(
                            "Construction is deterministic for the same typed inputs."
                        ),
                        retries="In-memory construction has no retry side effect.",
                    ),
                    errors=ErrorContract(
                        domain_codes=(),
                        mapping_owner="domain projection owners and UI adapters",
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "portal-specific dictionaries and template-derived "
                            "availability, KPI, and action semantics"
                        ),
                        new_owner="ui.projection_contracts",
                        verification=(
                            "Typed invariant, projection-boundary, and portal "
                            "adoption tests."
                        ),
                        cutover_gate=(
                            "Adopted projections return StateValue, Kpi, and Action "
                            "objects without template-side decision logic."
                        ),
                        fallback_retirement=(
                            "Adopted templates no longer derive unknown/stale state, "
                            "KPI cohorts, eligibility, or confirmation requirements."
                        ),
                    ),
                    steward="platform UI",
                    design_refs=(
                        "docs/designs/UI_PROJECTION_CONTRACTS.md",
                        "docs/SOT_RELATIONSHIP_MAP.md",
                    ),
                    test_refs=(
                        "tests/test_ui_contracts.py",
                        "tests/architecture/test_template_projection_boundary.py",
                    ),
                ),
            ),
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
                    "Party-first Refer & Earn capture policy",
                    "canonical Referral program record",
                    "Referral Subscriber attachment record",
                    "referral qualification and reward policy",
                    "atomic referral program transition orchestration",
                ),
                depends_on=(
                    "customer.accounts",
                    "party.registry",
                    "sales.lead_lifecycle",
                    "access.subscription_lifecycle",
                    "financial.credit_notes",
                    "control.settings_spec",
                    "events.dispatcher",
                    "observability.audit_log",
                    "communications.event_policy",
                ),
                notes=(
                    "Typed commands lock canonical Referral, ReferralCode, and "
                    "Subscriber rows, call transaction-neutral Party, Lead, and "
                    "credit-note collaborators, and stage PII-free audit/events "
                    "before one commit. Contact observations never establish "
                    "identity or attach an account."
                ),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name="Party-first Refer & Earn capture policy",
                            role=OwnerRole.POLICY,
                            input_names=(
                                "referral program policy settings",
                                "canonical referrer account state",
                                "canonical Party identity and reachability facts",
                            ),
                        ),
                        ConcernContract(
                            name="canonical Referral program record",
                            role=OwnerRole.AUTHORITATIVE_RECORD,
                            input_names=(
                                "referral program command evidence",
                                "referral program policy settings",
                                "canonical referrer account state",
                                "canonical Party identity and reachability facts",
                                "canonical attributed Lead state",
                            ),
                            canonical_writer="referrals.program",
                        ),
                        ConcernContract(
                            name="Referral Subscriber attachment record",
                            role=OwnerRole.AUTHORITATIVE_RECORD,
                            input_names=(
                                "canonical Referral program record",
                                "canonical referred account state",
                                "canonical Party identity and reachability facts",
                                "canonical attributed Lead state",
                            ),
                            canonical_writer="referrals.program",
                        ),
                        ConcernContract(
                            name="referral qualification and reward policy",
                            role=OwnerRole.POLICY,
                            input_names=(
                                "canonical Referral program record",
                                "referral program policy settings",
                                "canonical subscriber activation state",
                                "canonical referral reward credit evidence",
                            ),
                        ),
                        ConcernContract(
                            name="atomic referral program transition orchestration",
                            role=OwnerRole.APPLICATION_COORDINATOR,
                            input_names=(
                                "referral program command evidence",
                                "canonical Referral program record",
                                "referral program policy settings",
                                "canonical referrer account state",
                                "canonical referred account state",
                                "canonical Party identity and reachability facts",
                                "canonical attributed Lead state",
                                "canonical subscriber activation state",
                                "canonical referral reward credit evidence",
                            ),
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="referral program command evidence",
                            owner="referrals.program",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "typed CommandContext carrying actor, scope, reason, "
                                "command, correlation, causation, and idempotency evidence"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical Referral program record",
                            owner="referrals.program",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "active ReferralCode and Referral rows with Party, "
                                "Lead, Subscriber, lifecycle, reward snapshot, and "
                                "credit-link evidence"
                            ),
                        ),
                        AuthorityInput(
                            name="referral program policy settings",
                            owner="control.settings_spec",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "database-authoritative enablement, reward amount and "
                                "currency, qualification window, approval mode, and "
                                "share-base settings in the subscriber domain"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical referrer account state",
                            owner="customer.accounts",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source="the exact Subscriber that owns the active referral code",
                        ),
                        AuthorityInput(
                            name="canonical referred account state",
                            owner="customer.accounts",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "the exact reviewed Subscriber selected by conversion "
                                "or subscriber lifecycle evidence"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical Party identity and reachability facts",
                            owner="party.registry",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "quarantined Person Party and unverified contact-point "
                                "observations; contacts are risk inputs, never identity keys"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical attributed Lead state",
                            owner="sales.lead_lifecycle",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "Party-bound Lead, immutable referral origin, and exact "
                                "reviewed Subscriber attachment evidence"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical subscriber activation state",
                            owner="access.subscription_lifecycle",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "derived active Subscriber status or an exact active "
                                "Subscription observed from lifecycle events"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical referral reward credit evidence",
                            owner="financial.credit_notes",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "owner-previewed issued CreditNote, exact legacy-compatible "
                                "referral reference, and funding-ledger link"
                            ),
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.COORDINATOR_MANAGED,
                        boundary=(
                            "Each code, capture, qualification, rejection, or reward "
                            "command enters execute_owner_command on a transaction-free "
                            "adapter session. Referral state, collaborators, audit, and "
                            "events commit or roll back together."
                        ),
                        locking=(
                            "Code issuance locks the Subscriber; capture locks the exact "
                            "ReferralCode before retry comparison; transitions lock the "
                            "Referral before Subscriber or financial account state. "
                            "Database uniqueness arbitrates generated-code collisions."
                        ),
                        idempotency=(
                            "One active code per locked Subscriber, same-code plus exact "
                            "normalized contact-set capture replay, monotonic lifecycle "
                            "states, and the legacy referral:<UUID> credit reference "
                            "return stable outcomes without duplicate evidence or money."
                        ),
                        retries=(
                            "Rolled-back commands may retry with the same intent key. "
                            "Generated-code or serialization conflicts are retryable; "
                            "identity, lifecycle, policy, and financial conflicts require "
                            "review. Event delivery retries independently by event_id."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(
                            "referrals.program.invalid_command",
                            "referrals.program.invalid_configuration",
                            "referrals.program.program_disabled",
                            "referrals.program.subscriber_not_found",
                            "referrals.program.referral_not_found",
                            "referrals.program.code_not_found",
                            "referrals.program.contact_required",
                            "referrals.program.self_referral",
                            "referrals.program.existing_customer",
                            "referrals.program.incomplete_context",
                            "referrals.program.account_conflict",
                            "referrals.program.account_attachment_required",
                            "referrals.program.invalid_transition",
                            "referrals.program.invalid_reward",
                            "referrals.program.incomplete_reward_evidence",
                            "referrals.program.financial_conflict",
                            "referrals.program.collaboration_conflict",
                            "referrals.program.idempotency_conflict",
                            "referrals.program.invalid_filter",
                            "referrals.program.code_generation_exhausted",
                            "referrals.program.write_conflict",
                            "referrals.program.invalid_command_context",
                            "referrals.program.command_contract_violation",
                            "referrals.program.nested_owner_command",
                            "referrals.program.active_caller_transaction",
                            "referrals.program.nested_transaction_completion",
                        ),
                        mapping_owner=(
                            "app.api.crm_referrals, app.api.me, "
                            "app.web.admin.crm_referrals, app.web.customer.referrals, "
                            "and app.services.events.handlers.referral adapters"
                        ),
                        retryable_codes=(
                            "referrals.program.code_generation_exhausted",
                            "referrals.program.write_conflict",
                        ),
                        fail_closed_on=(
                            "missing or invalid canonical program settings",
                            "ambiguous identity or known self/existing-customer contact",
                            "incomplete Party, Lead, Subscriber, or reward evidence",
                            "invalid lifecycle transition or issued-credit conflict",
                            "active caller transaction or manifest mismatch",
                        ),
                    ),
                    events=EventContract(
                        event_types=(
                            "referral_code.issued",
                            "referral.captured",
                            "referral.subscriber_attached",
                            "referral.qualified",
                            "referral.expired",
                            "referral.rejected",
                            "referral.reward_issued",
                            "referral.reward_reconciled",
                        ),
                        schema_version=1,
                        delivery_owner="events.dispatcher",
                        compatibility=(
                            "Version 1 contains canonical UUIDs, lifecycle/reward "
                            "outcome, bounded financial evidence, and command tracing. "
                            "It contains no name, email, phone, address, notes, reason "
                            "text, referral code, or bearer capability."
                        ),
                        replay=(
                            "Command replay emits no duplicate transition event. "
                            "The reward-issued event resolves through the canonical "
                            "notification template/channel policy and communication "
                            "intents deduplicate each event and channel."
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "CRM referral mutation and an uncontracted service that "
                            "mixed HTTP errors, helper commits, direct push transport, "
                            "raw runtime environment fallback, and keyword mutations"
                        ),
                        new_owner="referrals.program",
                        verification=(
                            "Focused code, capture, identity-risk, attachment, "
                            "qualification, reward, rollback, idempotency, audit, event, "
                            "adapter, policy, manifest, and architecture tests."
                        ),
                        cutover_gate=(
                            "Staff, public, customer API/web, and lifecycle-event writes "
                            "construct typed owner commands on transaction-free sessions."
                        ),
                        fallback_retirement=(
                            "CRM/write-through authority, service HTTP/commit/rollback, "
                            "direct push delivery, raw share-base environment reads, "
                            "and public keyword mutation entry points are removed."
                        ),
                    ),
                    steward="customer operations",
                    design_refs=(
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/PARTY_FIRST_REFERRAL_CAPTURE.md",
                        "docs/REFERRAL_ACCOUNT_CONVERSION.md",
                        "docs/adr/0002-owner-command-transaction-boundary.md",
                        "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                    ),
                    test_refs=(
                        "tests/test_referrals_native.py",
                        "tests/test_admin_referrals_web.py",
                        "tests/test_customer_portal_referrals.py",
                        "tests/architecture/test_referrals_program_boundary.py",
                    ),
                ),
            ),
            SOTService(
                name="referrals.account_conversion",
                module="app.services.referral_account_conversion",
                owns=(
                    "stable Referral Party Lead conversion context validation",
                    "atomic referral account creation and adjudication orchestration",
                    "public referral signup capability purpose claims and lifetime",
                ),
                depends_on=(
                    "customer.accounts",
                    "party.registry",
                    "sales.lead_lifecycle",
                    "referrals.program",
                    "auth.token_signing",
                    "control.settings_spec",
                    "events.dispatcher",
                    "observability.audit_log",
                ),
                notes=(
                    "Typed public and staff commands enter one verified coordinator "
                    "transaction. The owner locks and revalidates exact UUID context, "
                    "calls transaction-neutral record-owner collaborators, and stages "
                    "PII-free audit and events before one commit. Public capability "
                    "lifetime resolves only through the settings owner. Identity is "
                    "never selected by contact observations."
                ),
                contract=ServiceContract(
                    concerns=(
                        ConcernContract(
                            name=(
                                "stable Referral Party Lead conversion context "
                                "validation"
                            ),
                            role=OwnerRole.POLICY,
                            input_names=(
                                "canonical Referral conversion record",
                                "canonical referred Party identity",
                                "canonical attributed Lead state",
                            ),
                        ),
                        ConcernContract(
                            name=(
                                "atomic referral account creation and adjudication "
                                "orchestration"
                            ),
                            role=OwnerRole.APPLICATION_COORDINATOR,
                            input_names=(
                                "referral account conversion command evidence",
                                "canonical Referral conversion record",
                                "canonical referred Party identity",
                                "canonical attributed Lead state",
                                "canonical Subscriber account state",
                            ),
                        ),
                        ConcernContract(
                            name=(
                                "public referral signup capability purpose claims "
                                "and lifetime"
                            ),
                            role=OwnerRole.POLICY,
                            input_names=(
                                "canonical Referral conversion record",
                                "referral signup capability policy settings",
                                "verified public signup capability envelope",
                            ),
                        ),
                    ),
                    authoritative_inputs=(
                        AuthorityInput(
                            name="referral account conversion command evidence",
                            owner="referrals.account_conversion",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "typed CommandContext carrying actor, scope, reason, "
                                "command, correlation, causation, and idempotency "
                                "evidence"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical Referral conversion record",
                            owner="referrals.program",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "active Referral Party, Lead, referrer, attached "
                                "Subscriber, status, and complete binding evidence"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical referred Party identity",
                            owner="party.registry",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "the exact active or quarantined Party row and its "
                                "canonical Subscriber binding"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical attributed Lead state",
                            owner="sales.lead_lifecycle",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "the exact Party-bound Lead and its canonical "
                                "Subscriber attachment evidence"
                            ),
                        ),
                        AuthorityInput(
                            name="canonical Subscriber account state",
                            owner="customer.accounts",
                            kind=AuthorityKind.AUTHORITATIVE_RECORD,
                            source=(
                                "transaction-neutral account initialization and the "
                                "exact existing or newly prepared Subscriber"
                            ),
                        ),
                        AuthorityInput(
                            name="referral signup capability policy settings",
                            owner="control.settings_spec",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "database-authoritative bounded referral signup "
                                "context expiry in the subscriber settings domain"
                            ),
                        ),
                        AuthorityInput(
                            name="verified public signup capability envelope",
                            owner="auth.token_signing",
                            kind=AuthorityKind.CONTROL_INPUT,
                            source=(
                                "configured signing key and algorithm verification "
                                "for exact purpose, version, UUID, issued-at, and "
                                "expiry claims"
                            ),
                        ),
                    ),
                    transaction=TransactionContract(
                        mode=TransactionMode.COORDINATOR_MANAGED,
                        boundary=(
                            "Each create or attach command enters "
                            "execute_owner_command on a transaction-free adapter "
                            "session. Subscriber preparation, Party binding, Lead "
                            "and Referral attachment, audit, subscriber.created, and "
                            "referral_account.converted commit or roll back together."
                        ),
                        locking=(
                            "The exact Referral, Party, Lead, and any existing "
                            "Subscriber are selected FOR UPDATE in canonical order. "
                            "Referral serialization and database identity constraints "
                            "arbitrate concurrent account creation and attachment."
                        ),
                        idempotency=(
                            "The Referral row is the natural conversion key. An exact "
                            "replay returns its already attached Subscriber without a "
                            "second account, audit row, or conversion event; a "
                            "different account or Party fails closed."
                        ),
                        retries=(
                            "Adapters may retry a rolled-back command with the same "
                            "intent key after transient database failure. Canonical "
                            "context conflicts require review; outbox delivery retries "
                            "independently."
                        ),
                    ),
                    errors=ErrorContract(
                        domain_codes=(
                            "referrals.account_conversion.invalid_command",
                            "referrals.account_conversion.invalid_configuration",
                            "referrals.account_conversion.invalid_capability",
                            "referrals.account_conversion.context_not_found",
                            "referrals.account_conversion.incomplete_context",
                            "referrals.account_conversion.stale_context",
                            "referrals.account_conversion.context_not_convertible",
                            "referrals.account_conversion.subscriber_not_found",
                            "referrals.account_conversion.account_conflict",
                            "referrals.account_conversion.self_referral",
                            ("referrals.account_conversion.invalid_command_context"),
                            ("referrals.account_conversion.command_contract_violation"),
                            "referrals.account_conversion.nested_owner_command",
                            ("referrals.account_conversion.active_caller_transaction"),
                            (
                                "referrals.account_conversion."
                                "nested_transaction_completion"
                            ),
                        ),
                        mapping_owner=(
                            "app.api.crm_referrals and "
                            "app.web.admin.crm_referrals adapters"
                        ),
                        fail_closed_on=(
                            "missing or altered Referral, Party, or Lead context",
                            "incomplete binding evidence",
                            "different Party, Subscriber, or self-referral",
                            "invalid or expired public capability",
                            "missing or invalid canonical lifetime policy",
                            "active caller transaction or manifest mismatch",
                        ),
                    ),
                    events=EventContract(
                        event_types=(
                            "subscriber.created",
                            "referral_account.converted",
                        ),
                        schema_version=1,
                        delivery_owner="events.dispatcher",
                        compatibility=(
                            "Version 1 contains canonical UUIDs, conversion outcome, "
                            "and command/correlation evidence only. It never contains "
                            "name, email, phone, address, reason text, or bearer "
                            "capability."
                        ),
                        replay=(
                            "Events are immutable committed evidence. Consumers "
                            "deduplicate by event_id; command replay converges on the "
                            "Referral's canonical attached Subscriber."
                        ),
                    ),
                    migration=MigrationContract(
                        state=AuthorityMigrationState.COMPLETE,
                        old_owner=(
                            "uncontracted keyword service functions using savepoints, "
                            "helper commits, status-coded errors, and adapter-owned "
                            "transaction handoff"
                        ),
                        new_owner="referrals.account_conversion",
                        verification=(
                            "Focused create, attach, public capability, stale-context, "
                            "self-referral, idempotency, rollback, event, audit, "
                            "adapter, policy, manifest, and architecture tests."
                        ),
                        cutover_gate=(
                            "Public API, staff API, and admin web conversion surfaces "
                            "construct only typed owner commands on transaction-free "
                            "sessions."
                        ),
                        fallback_retirement=(
                            "Service commits, savepoints, FastAPI errors, keyword "
                            "mutation entry points, hardcoded capability lifetime, and "
                            "post-conversion adapter transaction completion are removed."
                        ),
                    ),
                    steward="customer operations",
                    design_refs=(
                        "docs/SOT_RELATIONSHIP_MAP.md",
                        "docs/REFERRAL_ACCOUNT_CONVERSION.md",
                        "docs/adr/0002-owner-command-transaction-boundary.md",
                        "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                    ),
                    test_refs=(
                        "tests/test_referral_account_conversion.py",
                        "tests/test_referral_self_service_signup.py",
                        (
                            "tests/architecture/"
                            "test_referral_account_conversion_boundary.py"
                        ),
                    ),
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


def all_services() -> tuple[SOTService, ...]:
    """Return registered services in domain and dependency declaration order."""

    return tuple(
        service for domain in DOMAIN_SOT_RELATIONSHIPS for service in domain.services
    )


def registry_validation_errors() -> tuple[str, ...]:
    """Return structural errors that make ownership resolution ambiguous."""

    errors: list[str] = []
    services = all_services()

    duplicate_domains = sorted(
        name
        for name, count in Counter(
            domain.domain.strip().casefold() for domain in DOMAIN_SOT_RELATIONSHIPS
        ).items()
        if count > 1
    )
    errors.extend(f"duplicate domain name: {name}" for name in duplicate_domains)

    duplicate_services = sorted(
        name
        for name, count in Counter(
            service.name.strip().casefold() for service in services
        ).items()
        if count > 1
    )
    errors.extend(f"duplicate service name: {name}" for name in duplicate_services)

    concern_owners: dict[str, list[str]] = {}
    for service in services:
        if not service.name.strip():
            errors.append("service has an empty name")
        if not service.module.strip():
            errors.append(f"service {service.name!r} has an empty module")
        if not service.owns:
            errors.append(f"service {service.name!r} has no owned concerns")
        for concern in service.owns:
            normalized = concern.strip().casefold()
            if not normalized:
                errors.append(f"service {service.name!r} has an empty concern")
                continue
            concern_owners.setdefault(normalized, []).append(service.name)

    errors.extend(
        f"duplicate exact concern {concern!r}: {', '.join(sorted(owners))}"
        for concern, owners in sorted(concern_owners.items())
        if len(owners) > 1
    )

    service_names = {service.name for service in services}
    for service in services:
        duplicate_dependencies = sorted(
            name for name, count in Counter(service.depends_on).items() if count > 1
        )
        errors.extend(
            f"service {service.name!r} repeats dependency {dependency!r}"
            for dependency in duplicate_dependencies
        )
        errors.extend(
            f"service {service.name!r} has unknown dependency {dependency!r}"
            for dependency in service.depends_on
            if dependency not in service_names
        )
        errors.extend(contract_validation_errors(service, service_names=service_names))

    dependency_graph = {service.name: set(service.depends_on) for service in services}
    try:
        tuple(TopologicalSorter(dependency_graph).static_order())
    except CycleError as exc:
        cycle = " -> ".join(str(item) for item in exc.args[1])
        errors.append(f"service dependency cycle: {cycle}")

    return tuple(sorted(errors))


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


def service_relationship(service_name: str) -> SOTService:
    """Return one exactly named service from the canonical registry."""

    for service in all_services():
        if service.name == service_name:
            return service
    raise KeyError(service_name)


def owning_service_for(concern: str) -> SOTService | None:
    """Return the owner of one exact, normalized concern string."""

    needle = concern.strip().lower()
    if not needle:
        return None
    for domain in DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            if any(needle == owned.strip().lower() for owned in service.owns):
                return service
    return None
