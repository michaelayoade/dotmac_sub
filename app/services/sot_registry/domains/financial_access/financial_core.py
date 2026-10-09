"""financial_access SOT declarations: financial core."""

from __future__ import annotations

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    EventContract,
    MigrationContract,
    OwnerRole,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
)

SERVICES: tuple[SOTService, ...] = (
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
        depends_on=(
            "billing.opening_balance_history",
            "financial.ledger",
        ),
        notes=(
            "The first approved batch permanently retires carried-in funding "
            "authority. The complete-history migration supersession requires "
            "one target for every funding candidate and aborts on any source "
            "integrity defect; later corrections are reviewed append-only "
            "supersessions. The frozen opening-balance snapshot is one-time migration "
            "evidence, never a runtime money source or fallback. The separate "
            "customer.financial_position verifier owns the post-activation "
            "composition of an approved subledger opening with later native "
            "facts. For opening verification only, a customer created after "
            "the fixed handoff with no carried-in identity has a typed "
            "zero history component plus canonical native facts; runtime "
            "money actions remain quarantined until approved immutable "
            "opening capture. A post-cutover single-account review bounds "
            "those native facts at the immutable original authority cutoff so "
            "later authoritative postings are not absorbed twice. This "
            "reconstruction owner never rewrites an "
            "opening or posts money."
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
                    "change, add-on, renewal, or reviewed legacy-renewal "
                    "tax-invoice correction coordinator transactions."
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
                    "financial.account_adjustments.participant_owner_required",
                ),
                mapping_owner="API and enclosing financial coordinator adapters",
                retryable_codes=("financial.account_adjustments.write_conflict",),
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
            "approved-opening exclusion of already absorbed payment sources",
            "oldest-payable-debt application orchestration",
            "exact invoice payment-backed funding preview",
            "pre-issuance payment-credit reservation and atomic application",
            "all-or-nothing exact invoice credit application",
            "invoice-void release of exact account-credit allocations",
            "account-credit application invariant monitoring",
            "bounded account-credit invariant summary",
            "unallocated account-credit creation",
            "offer of settled account credit to open receivables",
            "automatic verified customer-payment application to eligible invoices",
        ),
        depends_on=("financial.payments", "financial.invoices", "financial.ledger"),
        notes=(
            "Account credit is derived from exact unconsumed settlement "
            "evidence, never a wallet counter. This owner composes the "
            "payment-allocation owner for application. It gained the creation "
            "half in record_credit, so credit can no longer be minted without "
            "this owner knowing it exists — which is how it got stranded while "
            "the invoice it should have settled was dunned. The ledger row is "
            "still written through financial.ledger; this owner supplies the "
            "decision, not its own persistence. Minting and offering are "
            "separate commands because credit is spendable only once its "
            "settlement evidence exists; the settlement path calls "
            "offer_available_credit once it does."
            " Verified customer settlement is always offered to eligible invoices; "
            "customer and reviewer adapters cannot opt out. Explicitly reserved, "
            "reviewed-correction, refund, reversal, and consolidated flows retain "
            "their bounded consequence modes."
            " Invoice issuance reserves eligible payment credit while the document "
            "is still a draft, then consumes that exact reservation after the "
            "receivable is issued in the same transaction. The new invoice's own "
            "debit therefore cannot hide the funding that must settle it."
            " A reviewed account opening bounds payment source selection even"
            " when a generic caller omits an explicit funding boundary;"
            " pre-opening payment room is historical evidence, not new credit."
        ),
    ),
    SOTService(
        name="financial.account_credit_invoice_reconciliation",
        module="app.services.account_credit_invoice_reconciliation",
        owns=("reviewed stranded account-credit invoice reconciliation",),
        depends_on=(
            "financial.account_credit_applications",
            "financial.invoices",
            "financial.ledger",
            "financial.payments",
            "events.dispatcher",
            "observability.audit_log",
        ),
        notes=(
            "This correction-only coordinator never records a payment. It binds "
            "one completed account-credit deposit intent to its existing succeeded "
            "settlement and the account's oldest eligible non-service invoice, then "
            "delegates creation of the missing allocation and paired ledger entries "
            "to financial.account_credit_applications."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="reviewed stranded account-credit invoice reconciliation",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "reviewed reconciliation command",
                        "canonical invoice debt",
                        "canonical deposit intent",
                        "canonical settled payment credit",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="reviewed reconciliation command",
                    owner="financial.account_credit_invoice_reconciliation",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed account, invoice, payment, deposit-intent, amount, "
                        "currency, permission, actor, reason, preview fingerprint, "
                        "command identity, and idempotency evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical invoice debt",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked active financial invoice, active lines, exact balance, "
                        "status, and canonical oldest-payable-debt order"
                    ),
                ),
                AuthorityInput(
                    name="canonical deposit intent",
                    owner="financial.account_credit_deposits",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "completed account-credit deposit intent whose credit-only "
                        "policy requires application to eligible invoices"
                    ),
                ),
                AuthorityInput(
                    name="canonical settled payment credit",
                    owner="financial.payments",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "existing succeeded Payment, PaymentSettlement, unallocated "
                        "credit ledger evidence, refund/reversal state, and exact "
                        "payment-allocation room"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "The public reconciliation command enters execute_owner_command "
                    "exactly once on a transaction-free session; reservation, "
                    "allocation, paired ledger entries, invoice settlement, audit, "
                    "and event commit together."
                ),
                locking=(
                    "Locks the customer account, then the reviewed invoice, payment, "
                    "and deposit intent before re-previewing and delegating to the "
                    "account-credit allocation participant."
                ),
                idempotency=(
                    "A bounded command key reserves the one resulting allocation; "
                    "the preview fingerprints every named record and exact amount."
                ),
                retries=(
                    "Exact replay returns the recorded allocation. Stale, partial, "
                    "refunded, reversed, service-linked, non-oldest, or ambiguous "
                    "evidence fails closed."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes(
                        "financial.account_credit_invoice_reconciliation"
                    ),
                    "financial.account_credit_invoice_reconciliation.amount_invalid",
                    "financial.account_credit_invoice_reconciliation.application_rejected",
                    "financial.account_credit_invoice_reconciliation.currency_invalid",
                    "financial.account_credit_invoice_reconciliation.idempotency_conflict",
                    "financial.account_credit_invoice_reconciliation.idempotency_key_required",
                    "financial.account_credit_invoice_reconciliation.incomplete_reconciliation",
                    "financial.account_credit_invoice_reconciliation.invoice_missing",
                    "financial.account_credit_invoice_reconciliation.not_actionable",
                    "financial.account_credit_invoice_reconciliation.permission_denied",
                    "financial.account_credit_invoice_reconciliation.preview_invalid",
                    "financial.account_credit_invoice_reconciliation.reason_invalid",
                    "financial.account_credit_invoice_reconciliation.replay_conflict",
                    "financial.account_credit_invoice_reconciliation.scope_invalid",
                    "financial.account_credit_invoice_reconciliation.stale_preview",
                ),
                mapping_owner="reviewed account-credit reconciliation CLI adapter",
                fail_closed_on=(
                    "missing or ambiguous invoice, deposit, settlement, or payment evidence",
                    "stale preview, permission failure, or idempotency conflict",
                    "any amount, currency, policy, account, ordering, refund, or reversal mismatch",
                ),
            ),
            events=EventContract(
                event_types=("account_credit.invoice_reconciled",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries only bounded invoice, payment, settlement, "
                    "intent, allocation, money, currency, and preview identifiers."
                ),
                replay=(
                    "The command reservation and deterministic participant allocation "
                    "prevent duplicate allocations, ledger rows, audits, or events."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner="none; stranded credit required manual database intervention",
                new_owner="financial.account_credit_invoice_reconciliation",
                verification=(
                    "Focused eligibility, exact settlement, replay, drift, registry, "
                    "and architecture-boundary tests."
                ),
                cutover_gate=(
                    "Only fingerprinted CLI confirmation may reconcile one explicitly "
                    "named evidence chain."
                ),
                fallback_retirement=(
                    "No raw SQL, payment re-recording, balance override, generic "
                    "adjustment, or direct adapter allocation path exists."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/designs/ACCOUNT_CREDIT_INVOICE_RECONCILIATION.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_account_credit_invoice_reconciliation.py",
                "tests/architecture/test_account_credit_invoice_reconciliation_boundary.py",
            ),
        ),
    ),
)
