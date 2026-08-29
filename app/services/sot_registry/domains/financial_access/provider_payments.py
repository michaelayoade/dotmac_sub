"""financial_access SOT declarations: provider payments."""

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
    ProjectionContract,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
)

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="financial.provider_payment_settlements",
        module="app.services.provider_payment_settlements",
        owns=(
            "verified invoice-payment cash-first orchestration",
            "post-settlement invoice-allocation request",
            "allocation-failure exception handoff",
            "idempotent post-allocation funding-change outbox event",
        ),
        depends_on=(
            "financial.payments",
            "financial.invoices",
            "financial.prepaid_service_renewals",
        ),
    ),
    SOTService(
        name="financial.payment_provider_events",
        module="app.services.payment_provider_events",
        owns=(
            "payment-provider event ingestion",
            "normalized provider monetary observations",
            "provider-event idempotency",
            "incomplete provider settlement resumption",
        ),
        depends_on=(
            "events.dispatcher",
            "financial.consolidated_payments",
            "financial.invoices",
            "financial.payments",
            "financial.payment_routing",
            "financial.provider_payment_settlements",
            "observability.audit_log",
        ),
        notes=(
            "Signature verification and gateway verification remain external "
            "observation admission boundaries. This owner persists the exact "
            "normalized identity, source, digest, status, money, processing "
            "result, audit, and event before delegating financial consequences "
            "to named participants. Administrative observations cannot change "
            "payment state. Provider fee policy is intentionally not decided "
            "here; the unresolved route policy remains with financial.payments."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="payment-provider event ingestion",
                    role=OwnerRole.OBSERVATION_COLLECTOR,
                    input_names=(
                        "verified external provider observation",
                        "administrative provider observation",
                        "active provider identity",
                        "provider-event command context",
                    ),
                    canonical_writer="financial.payment_provider_events",
                ),
                ConcernContract(
                    name="normalized provider monetary observations",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "verified external provider observation",
                        "active provider identity",
                    ),
                    canonical_writer="financial.payment_provider_events",
                ),
                ConcernContract(
                    name="provider-event idempotency",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "canonical provider-event record",
                        "provider-event command context",
                    ),
                    canonical_writer="financial.payment_provider_events",
                ),
                ConcernContract(
                    name="incomplete provider settlement resumption",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical provider-event record",
                        "canonical payment participant protocol",
                        "canonical consolidated-payment participant protocol",
                        "canonical invoice-settlement participant protocol",
                        "provider-event command context",
                    ),
                    canonical_writer="financial.payment_provider_events",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="verified external provider observation",
                    owner="external:payment_provider",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "signature-verified webhook or gateway-verified transaction "
                        "identity, event type, gross, fee, net observation, currency, "
                        "provider reference, status, and bounded raw payload"
                    ),
                ),
                AuthorityInput(
                    name="administrative provider observation",
                    owner="financial.payment_provider_events",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "authenticated operator-supplied informational event with no "
                        "payment-state or refund/reversal authority"
                    ),
                ),
                AuthorityInput(
                    name="active provider identity",
                    owner="financial.payment_routing",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "configured provider identity and active eligibility admitted "
                        "for the verified receipt or reconciliation path"
                    ),
                ),
                AuthorityInput(
                    name="canonical provider-event record",
                    owner="financial.payment_provider_events",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked provider/event identity, source, observation digest, "
                        "normalized money/status/effect, processing state, error code, "
                        "and linked payment/invoice"
                    ),
                ),
                AuthorityInput(
                    name="canonical payment participant protocol",
                    owner="financial.payments",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only settlement, fee observation, payment status, "
                        "allocation, refund, and reversal participants"
                    ),
                ),
                AuthorityInput(
                    name="canonical consolidated-payment participant protocol",
                    owner="financial.consolidated_payments",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only consolidated settlement, refund, and reversal "
                        "participants"
                    ),
                ),
                AuthorityInput(
                    name="canonical invoice-settlement participant protocol",
                    owner="financial.provider_payment_settlements",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "cash-first verified invoice-payment and allocation-exception "
                        "participant"
                    ),
                ),
                AuthorityInput(
                    name="provider-event command context",
                    owner="financial.payment_provider_events",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed actor, trust scope, reason, command, idempotency, "
                        "correlation, and causation evidence"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "The administrative command enters execute_owner_command once and "
                    "can record informational observations only. Verified webhook and "
                    "reconciliation methods are flush-only participants of their named "
                    "coordinator transactions; all record, payment, audit, event, and "
                    "processed-receipt consequences commit or roll back together."
                ),
                locking=(
                    "After the wider coordinator locks its receipt or top-up intent, "
                    "this owner locks the exact provider row, then matching provider "
                    "event, invoice/payment, and downstream participant scopes. The "
                    "provider lock serializes first insert for both unique identities."
                ),
                idempotency=(
                    "Provider plus idempotency key or external transaction identifies "
                    "one row. A canonical SHA-256 digest includes the admission trust "
                    "class and all normalized decision evidence. Signature-verified "
                    "webhook and gateway-verified observations may converge only when "
                    "those normalized fields match exactly; administrative evidence "
                    "cannot converge with either. A legacy incomplete row may resume "
                    "once from verified evidence and receives canonical provenance."
                ),
                retries=(
                    "Retry the complete owning coordinator with the same normalized "
                    "observation and context. Conflicting identity evidence never "
                    "retries as the existing event and no participant commits alone."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.payment_provider_events.currency_invalid",
                    "financial.payment_provider_events.currency_required",
                    "financial.payment_provider_events.event_not_found",
                    "financial.payment_provider_events.financial_consequence_rejected",
                    "financial.payment_provider_events.financial_effect_conflict",
                    "financial.payment_provider_events.financial_effect_required",
                    "financial.payment_provider_events.identity_collision",
                    "financial.payment_provider_events.identity_required",
                    "financial.payment_provider_events.invoice_account_mismatch",
                    "financial.payment_provider_events.invoice_not_found",
                    "financial.payment_provider_events.money_invalid",
                    "financial.payment_provider_events.observation_invalid",
                    "financial.payment_provider_events.pagination_invalid",
                    "financial.payment_provider_events.payment_not_found",
                    "financial.payment_provider_events.provider_not_found",
                    "financial.payment_provider_events.replay_conflict",
                    "financial.payment_provider_events.status_conflict",
                    "financial.payment_provider_events.untrusted_financial_effect",
                    "financial.payment_provider_events.untrusted_financial_observation",
                    "financial.payment_provider_events.invalid_command_context",
                    "financial.payment_provider_events.command_contract_violation",
                    "financial.payment_provider_events.command_scope_mismatch",
                    "financial.payment_provider_events.nested_owner_command",
                    "financial.payment_provider_events.active_caller_transaction",
                    "financial.payment_provider_events.nested_transaction_completion",
                    "financial.payment_provider_events.net_amount_required",
                ),
                mapping_owner=(
                    "billing API, payment-webhook coordinator, and payment-"
                    "reconciliation coordinator adapters"
                ),
                retryable_codes=(
                    "financial.payment_provider_events.financial_consequence_rejected",
                ),
                fail_closed_on=(
                    "missing verified identity, malformed money or currency, and "
                    "contradictory event/status/effect evidence",
                    "identity reuse with a different trust class or normalized digest",
                    "administrative attempts to change payment, refund, or reversal state",
                    "admission source invoked with the wrong command scope",
                    "invoice settlement without an explicit normalized net amount",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=(
                    "payment_provider_event.processed",
                    "payment_provider_event.failed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 exposes canonical event/payment/invoice/provider "
                    "identities, trust source, normalized status/effect, processing "
                    "state, stable error code, and command lineage without raw provider "
                    "payloads, credentials, or customer PII."
                ),
                replay=(
                    "Consumers project committed observation facts only and never "
                    "re-enter settlement, refund, reversal, or allocation commands."
                ),
            ),
            projections=(),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "billing.providers mixed provider configuration with a mutable "
                    "event helper, caller-selected trust boolean, helper commit, "
                    "non-exact replay, and administrative financial consequences"
                ),
                new_owner="financial.payment_provider_events",
                verification=(
                    "Typed admission, exact replay/conflict, provenance, rollback, "
                    "audit/event, adapter-boundary, manifest, and PostgreSQL concurrent "
                    "identity tests."
                ),
                cutover_gate=(
                    "Only payment_webhooks and payment_reconciliation call verified "
                    "participants with typed context; the API root admits only "
                    "non-financial administrative observations."
                ),
                fallback_retirement=(
                    "The trusted_financial_effects boolean, event helper commit, "
                    "billing_adapter gateway ingress, mutable transport payload, "
                    "source-free record, and return-on-identity-without-proof paths "
                    "are absent."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_payment_provider_events.py",
                "tests/test_api_billing_webhooks.py",
                "tests/test_payment_webhook_settlement.py",
                "tests/architecture/test_payment_provider_event_ownership.py",
                "tests/architecture/test_payment_settlement_participants.py",
                "tests/integration/test_payment_provider_event_concurrency.py",
            ),
        ),
    ),
    SOTService(
        name="financial.payment_webhooks",
        module="app.services.payment_webhook_commands",
        owns=(
            "verified payment webhook projection",
            "Integrator settlement observation projection",
            "billing consequence submission from verified receipts",
        ),
        depends_on=(
            "integration.inbox",
            "financial.account_credit_deposits",
            "financial.payment_gateway_finance",
            "financial.payment_provider_events",
            "financial.topup_intents",
        ),
        notes=(
            "Signature adapters persist and claim the verified receipt through "
            "integration.inbox, then submit only its typed identity. The "
            "independently deployed Integrator submits a typed provider-neutral "
            "observation plus an opaque installation UUID; the locally approved "
            "PaymentProvider mapping selects the financial identity. This "
            "coordinator commits all billing consequences with processed-receipt "
            "evidence once. An unobserved provider fee fails closed rather than "
            "becoming zero."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="verified payment webhook projection",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "claimed signature-verified payment receipt",
                        "external provider payment observation",
                        "canonical provider-event settlement protocol",
                    ),
                ),
                ConcernContract(
                    name="Integrator settlement observation projection",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "claimed Integrator settlement receipt",
                        "Integrator installation provider mapping",
                        "external provider payment observation",
                        "canonical provider-event settlement protocol",
                    ),
                ),
                ConcernContract(
                    name=("billing consequence submission from verified receipts"),
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "claimed signature-verified payment receipt",
                        "canonical provider-event settlement protocol",
                        "canonical account-credit deposit protocol",
                        "canonical top-up completion protocol",
                        "canonical inbox consequence protocol",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="claimed signature-verified payment receipt",
                    owner="integration.inbox",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked IntegrationInbox identity, verified payload digest, "
                        "provider header, processing state, and attempt evidence"
                    ),
                ),
                AuthorityInput(
                    name="external provider payment observation",
                    owner="external:payment_provider",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "signature-verified Paystack or Flutterwave event type, "
                        "transaction identity, amount, fee, currency, status, and "
                        "provider-reflected checkout metadata"
                    ),
                ),
                AuthorityInput(
                    name="claimed Integrator settlement receipt",
                    owner="integration.inbox",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked ProductObservation v1 receipt, payload digest, "
                        "opaque source installation UUID, connector provenance, "
                        "processing state, and attempt evidence"
                    ),
                ),
                AuthorityInput(
                    name="Integrator installation provider mapping",
                    owner="financial.payment_gateway_finance",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "nullable unique PaymentProvider correlation to one "
                        "Integrator installation; never selected from provider data"
                    ),
                ),
                AuthorityInput(
                    name="canonical provider-event settlement protocol",
                    owner="financial.payment_provider_events",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only idempotent provider-event, payment, allocation, "
                        "refund, reversal, status, and exception participants"
                    ),
                ),
                AuthorityInput(
                    name="canonical account-credit deposit protocol",
                    owner="financial.account_credit_deposits",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only typed deposit correlation, settlement, credit "
                        "application, intent projection, audit, and event protocol"
                    ),
                ),
                AuthorityInput(
                    name="canonical top-up completion protocol",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only locked payment-to-intent completion projection"
                    ),
                ),
                AuthorityInput(
                    name="canonical inbox consequence protocol",
                    owner="integration.inbox",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only processed consequence projection and separate "
                        "owner-managed failure/dead-letter recording"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "After integration.inbox durably records and claims the receipt, "
                    "execute_owner_command admits a transaction-free session and "
                    "commits or rolls back provider event, money, allocation, intent, "
                    "audit/event, and processed-receipt evidence exactly once."
                ),
                locking=(
                    "The coordinator locks the exact claimed inbox receipt first; "
                    "participants then use their canonical account, invoice, payment, "
                    "billing-account, and top-up intent lock order."
                ),
                idempotency=(
                    "IntegrationInbox binding/provider-event identity owns receipt "
                    "replay. Provider-event and monetary participants reuse that "
                    "identity or exact provider transaction evidence; a processed "
                    "receipt returns its stored consequence."
                ),
                retries=(
                    "Retryable failure rolls back the complete billing consequence, "
                    "then the adapter separately records inbox retry evidence. "
                    "Deterministic payload/correlation rejection is dead-lettered."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.payment_webhooks.payload_invalid",
                    "financial.payment_webhooks.receipt_not_found",
                    "financial.payment_webhooks.receipt_not_claimed",
                    "financial.payment_webhooks.receipt_provider_mismatch",
                    "financial.payment_webhooks.topup_intent_mismatch",
                    "financial.payment_webhooks.provider_not_configured",
                    "financial.payment_webhooks.integrator_provider_not_configured",
                    "financial.payment_webhooks.provider_fee_unobserved",
                    "financial.payment_webhooks.receipt_source_mismatch",
                    "financial.payment_webhooks.receipt_consequence_invalid",
                    "financial.payment_webhooks.deposit_correlation_unavailable",
                    "financial.payment_webhooks.deposit_rejected",
                    "financial.payment_webhooks.provider_event_rejected",
                    "financial.payment_webhooks.settlement_unlinked",
                    "financial.payment_webhooks.topup_projection_rejected",
                    "financial.payment_webhooks.invalid_command_context",
                    "financial.payment_webhooks.command_contract_violation",
                    "financial.payment_webhooks.nested_owner_command",
                    "financial.payment_webhooks.active_caller_transaction",
                    "financial.payment_webhooks.nested_transaction_completion",
                ),
                mapping_owner="Paystack and Flutterwave webhook HTTP adapters",
                retryable_codes=(
                    "financial.payment_webhooks.provider_not_configured",
                    "financial.payment_webhooks.integrator_provider_not_configured",
                    "financial.payment_webhooks.provider_fee_unobserved",
                    "financial.payment_webhooks.settlement_unlinked",
                    "financial.payment_webhooks.topup_projection_rejected",
                ),
                fail_closed_on=(
                    "unclaimed, missing, provider-mismatched, or malformed receipt",
                    "invalid amount, fee, currency, top-up, invoice, or deposit "
                    "correlation evidence",
                    "unmapped Integrator source or provider fee not observed by "
                    "the connector contract",
                    "provider success without structurally linked payment evidence",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "app.services.api_billing_webhooks mixed signature transport, "
                    "provider normalization, direct receipt/payment/intent writes, "
                    "savepoints, commits, rollbacks, and best-effort access decisions"
                ),
                new_owner="financial.payment_webhooks",
                verification=(
                    "Signature, identity collision, provider normalization, invoice, "
                    "deposit, top-up, fee, replay, dead-letter, atomic rollback, "
                    "manifest, and adapter-boundary tests."
                ),
                cutover_gate=(
                    "Adapters verify signatures, serialize HTTP, and invoke one typed "
                    "coordinator on a transaction-free session; every business write "
                    "uses a named flush-only participant."
                ),
                fallback_retirement=(
                    "Adapter-owned ORM writes, provider mappings, savepoints, "
                    "commit/rollback calls, direct provider-fee edits, and synchronous "
                    "service-restoration fallback are absent."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_api_billing_webhooks.py",
                "tests/test_payment_webhook_settlement.py",
                "tests/test_integrator_settlement_port.py",
                "tests/architecture/test_payment_webhook_ownership.py",
                "tests/architecture/test_payment_settlement_participants.py",
                "tests/architecture/test_integrator_settlement_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="financial.payment_reconciliation",
        module="app.services.payment_reconciliation",
        owns=(
            "stranded top-up reconciliation",
            "scheduled top-up reconciliation execution",
            "outside-window exact-reference recovery preview and classification",
            "finance-reviewed exact-reference outside-window top-up recovery",
            "finance-reviewed exact-reference recovery evidence",
            "verified provider settlement then allocation orchestration",
            "top-up reconciliation backlog, progress, and age projection",
        ),
        depends_on=(
            "control.settings_spec",
            "events.dispatcher",
            "integration.installations",
            "integration.runtime",
            "financial.account_credit_deposits",
            "financial.payment_gateway_finance",
            "financial.payments",
            "financial.payment_provider_events",
            "financial.topup_intents",
            "observability.audit_log",
        ),
        notes=(
            "The bounded sweep uses a work-conserving two-lane policy with reserved "
            "capacity for both stale pending customer payments and terminal late-"
            "success recovery. A batch has room for both lanes, unused reservations "
            "flow to the other lane, and provider cohorts interleave within each "
            "lane starting with the least recently served provider. Every successfully "
            "claimed intent durably advances typed attempt progress before provider "
            "I/O, which rotates provider priority across sweeps, then gateway "
            "verification is treated as an "
            "external fact. "
            "The automatic maximum-age policy is never widened for recovery: one "
            "outside-window Paystack intent whose canonical status is failed, "
            "abandoned, canceled, or expired may be rechecked only through the same "
            "owner's exact-reference preview and a fingerprint-bound finance confirmation "
            "carrying durable actor, reason, and evidence-reference provenance. "
            "Pending and other non-terminal outside-window intents are ineligible for "
            "this operator command. "
            "Each successful reviewed command writes one immutable, append-only "
            "recovery-run record that owns its review, idempotency, normalized money, "
            "canonical result, and command-lineage evidence. "
            "Each consequence is a separate typed coordinator transaction that "
            "composes canonical financial participants."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="stranded top-up reconciliation",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical top-up reconciliation policy",
                        "canonical reconcilable top-up intent",
                        "typed gateway reconciliation progress",
                        "external gateway verification observation",
                        "canonical gateway observation lifecycle protocol",
                    ),
                ),
                ConcernContract(
                    name="scheduled top-up reconciliation execution",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical top-up reconciliation policy",
                        "canonical reconcilable top-up intent",
                        "typed gateway reconciliation progress",
                        "external gateway verification observation",
                    ),
                ),
                ConcernContract(
                    name=(
                        "outside-window exact-reference recovery preview and "
                        "classification"
                    ),
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical top-up reconciliation policy",
                        "canonical reconcilable top-up intent",
                        "typed gateway reconciliation progress",
                        "typed exact-reference recovery request",
                        "enabled version-pinned reconciliation capability binding",
                        "canonical payment provider identity",
                        "external gateway verification observation",
                        "canonical payment replay evidence",
                        "canonical provider-event replay evidence",
                        "canonical exact-reference recovery evidence",
                    ),
                ),
                ConcernContract(
                    name=(
                        "finance-reviewed exact-reference outside-window top-up "
                        "recovery"
                    ),
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical top-up reconciliation policy",
                        "canonical reconcilable top-up intent",
                        "typed gateway reconciliation progress",
                        "finance-reviewed exact-reference recovery confirmation",
                        "enabled version-pinned reconciliation capability binding",
                        "canonical payment provider identity",
                        "external gateway verification observation",
                        "canonical payment replay evidence",
                        "canonical provider-event replay evidence",
                        "canonical exact-reference recovery evidence",
                        "canonical account-credit deposit protocol",
                        "canonical provider-event settlement protocol",
                        "canonical top-up completion protocol",
                        "canonical gateway observation lifecycle protocol",
                    ),
                ),
                ConcernContract(
                    name="finance-reviewed exact-reference recovery evidence",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "canonical reconcilable top-up intent",
                        "finance-reviewed exact-reference recovery confirmation",
                        "external gateway verification observation",
                        "canonical payment replay evidence",
                        "canonical provider-event replay evidence",
                    ),
                    canonical_writer="financial.payment_reconciliation",
                ),
                ConcernContract(
                    name=("verified provider settlement then allocation orchestration"),
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical reconcilable top-up intent",
                        "external gateway verification observation",
                        "canonical account-credit deposit protocol",
                        "canonical provider-event settlement protocol",
                        "canonical top-up completion protocol",
                    ),
                ),
                ConcernContract(
                    name=(
                        "top-up reconciliation backlog, progress, and age projection"
                    ),
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical top-up reconciliation policy",
                        "canonical reconcilable top-up intent",
                        "typed gateway reconciliation progress",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical top-up reconciliation policy",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed stale window, maximum age, batch size of at least two, "
                        "pending retry, processing retry, unavailable retry, and "
                        "terminal late-success retry settings with bounded defaults; "
                        "intent expiry itself is canonical"
                    ),
                ),
                AuthorityInput(
                    name="typed exact-reference recovery request",
                    owner="financial.payment_reconciliation",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "one exact failed, abandoned, canceled, or expired Paystack "
                        "top-up intent identity, stored reference, and explicit "
                        "observation time, older than the automatic maximum-age window; "
                        "pending and other non-terminal intents are ineligible, and the "
                        "request never names a cohort or changes that policy"
                    ),
                ),
                AuthorityInput(
                    name="finance-reviewed exact-reference recovery confirmation",
                    owner="financial.payment_reconciliation",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "the exact intent, provider and stored reference, reviewed "
                        "preview SHA-256 and normalized provider-evidence fingerprint, "
                        "plus actor, reason, evidence or change reference, command, "
                        "correlation, and idempotency identity"
                    ),
                ),
                AuthorityInput(
                    name="enabled version-pinned reconciliation capability binding",
                    owner="integration.installations",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "enabled installation and payments.reconcile.v1 capability "
                        "binding for the intent's pinned or provider-resolved connector"
                    ),
                ),
                AuthorityInput(
                    name="canonical payment provider identity",
                    owner="financial.payment_gateway_finance",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked local PaymentProvider row whose provider type matches "
                        "the exact intent and whose active state admits verified "
                        "reconciliation; resolved through the finance identity owner, "
                        "not gateway routing or presentment policy"
                    ),
                ),
                AuthorityInput(
                    name="canonical reconcilable top-up intent",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked intent identity, account scope, provider, reference, "
                        "purpose, currency, invoice instruction, lifecycle status, "
                        "expiry, normalized safe observation, and completion state"
                    ),
                ),
                AuthorityInput(
                    name="typed gateway reconciliation progress",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "durable selection-attempt count and time, latest normalized "
                        "observation count, time, outcome, and reason, plus the next "
                        "reconcile time on the locked top-up intent"
                    ),
                ),
                AuthorityInput(
                    name="external gateway verification observation",
                    owner="external:payment_provider",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "allowlisted Paystack or Flutterwave transaction status and "
                        "safe reason; successful observations additionally carry "
                        "transaction identity, gross amount, fee, and currency"
                    ),
                ),
                AuthorityInput(
                    name="canonical payment replay evidence",
                    owner="financial.payments",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active succeeded Payment identity, provider, external "
                        "transaction, gross amount, fee, currency, paid-at accounting "
                        "instant, settlement, and top-up completion link used to "
                        "classify exact replay"
                    ),
                ),
                AuthorityInput(
                    name="canonical provider-event replay evidence",
                    owner="financial.payment_provider_events",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "canonical provider-event trust source, normalized evidence "
                        "digest, processing result, and linked payment or invoice"
                    ),
                ),
                AuthorityInput(
                    name="canonical exact-reference recovery evidence",
                    owner="financial.payment_reconciliation",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "immutable recovery-run identity, intent/payment/provider-event "
                        "and binding links, idempotency and command fingerprints, review "
                        "reference, actor, reason, normalized external transaction and "
                        "gross/fee/net/currency facts, disposition, and command lineage"
                    ),
                ),
                AuthorityInput(
                    name="canonical account-credit deposit protocol",
                    owner="financial.account_credit_deposits",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only typed deposit correlation, payment, credit "
                        "application, audit, and event participant"
                    ),
                ),
                AuthorityInput(
                    name="canonical provider-event settlement protocol",
                    owner="financial.payment_provider_events",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only idempotent provider-event, payment, fee, invoice, "
                        "consolidated settlement, and allocation participants"
                    ),
                ),
                AuthorityInput(
                    name="canonical top-up completion protocol",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="flush-only locked payment-to-intent completion projection",
                ),
                AuthorityInput(
                    name="canonical gateway observation lifecycle protocol",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only locked terminal/non-terminal observation, "
                        "effective-expiry, blocker/retry, and late-success protocol"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "Scheduled candidate selection completes its read transaction, and "
                    "each candidate uses a dedicated pre-I/O claim root before the "
                    "existing definitive-observation root. Exact-reference preview is "
                    "read-only. Reviewed apply first replays matching immutable recovery "
                    "evidence without provider I/O; otherwise it obtains a second fresh "
                    "provider observation and local snapshot, releases that read "
                    "transaction, then enters one execute_owner_command root. That root "
                    "locks and recomputes the fingerprint and atomically commits or "
                    "rolls back settlement, provider event, intent completion, immutable "
                    "recovery evidence, audit, and domain events. A newly created "
                    "Payment uses that current confirmation as its paid-at accounting "
                    "instant, and any customer-subledger posting derives occurred-at "
                    "from it; exact link or replay preserves the existing canonical "
                    "Payment date. The command never backdates to intent creation or an "
                    "unauthenticated provider timestamp."
                ),
                locking=(
                    "The coordinator locks the canonical subscriber or billing-account "
                    "scope and top-up intent first; named participants then use their "
                    "account, invoice, payment, and settlement lock order."
                ),
                idempotency=(
                    "Provider type plus intent reference reuses the webhook provider-"
                    "event identity. Provider transaction identity and canonical "
                    "participant keys prevent duplicate cash, allocation, and intent "
                    "consequences. A reviewed recovery additionally binds one evidence "
                    "or change reference and idempotency key to the exact preview and "
                    "normalized provider-evidence fingerprints. Its immutable recovery "
                    "run is append-only; exact replay returns that result, while a reused "
                    "key with changed command or preview evidence conflicts. One successfully "
                    "claimed intent advances attempt progress once for its typed "
                    "attempt identity before transport is invoked."
                ),
                retries=(
                    "Unavailable or unknown evidence fails closed until canonical "
                    "expiry; failed or abandoned evidence terminalizes immediately. "
                    "Stale pending intents and failed, abandoned, canceled, or expired "
                    "late-success intents form separate lanes with reserved capacity "
                    "when the configured batch size is at least two. Unused capacity "
                    "is work-conserving, supported providers interleave within each "
                    "lane from the least recently served provider, and both provider "
                    "priority and due rows rotate by durable attempt progress so "
                    "neither a busy lane nor a repeated provider error can pin the "
                    "queue. Each candidate "
                    "is an independent transaction, so one rejection cannot roll back "
                    "or repeat another candidate's completed consequence. The reviewed "
                    "outside-window command is limited to failed, abandoned, canceled, "
                    "or expired Paystack intents and never admits pending or other "
                    "non-terminal work. Eligible work is never re-enqueued as a cohort: "
                    "a non-success, unavailable, unknown, stale, or conflicting result "
                    "is returned for that one reviewed reference and requires a new "
                    "current preview before any later operator confirmation."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.payment_reconciliation.policy_missing",
                    "financial.payment_reconciliation.provider_mismatch",
                    "financial.payment_reconciliation.reference_mismatch",
                    "financial.payment_reconciliation.provider_reference_mismatch",
                    "financial.payment_reconciliation.transaction_identity_invalid",
                    "financial.payment_reconciliation.amount_invalid",
                    "financial.payment_reconciliation.provider_fee_invalid",
                    "financial.payment_reconciliation.currency_invalid",
                    "financial.payment_reconciliation.currency_mismatch",
                    "financial.payment_reconciliation.invoice_correlation_invalid",
                    "financial.payment_reconciliation.authorized_net_mismatch",
                    "financial.payment_reconciliation.completion_conflict",
                    "financial.payment_reconciliation.provider_not_configured",
                    "financial.payment_reconciliation.provider_configuration_mismatch",
                    "financial.payment_reconciliation.provider_configuration_ambiguous",
                    "financial.payment_reconciliation.deposit_rejected",
                    "financial.payment_reconciliation.provider_event_rejected",
                    "financial.payment_reconciliation.settlement_unlinked",
                    "financial.payment_reconciliation.topup_projection_rejected",
                    "financial.payment_reconciliation.outcome_invalid",
                    "financial.payment_reconciliation.observation_incomplete",
                    "financial.payment_reconciliation.attempt_claim_rejected",
                    "financial.payment_reconciliation.recovery_reference_invalid",
                    "financial.payment_reconciliation.recovery_reference_mismatch",
                    "financial.payment_reconciliation.recovery_provider_invalid",
                    "financial.payment_reconciliation.recovery_status_invalid",
                    "financial.payment_reconciliation.recovery_status_ineligible",
                    "financial.payment_reconciliation.recovery_already_completed",
                    "financial.payment_reconciliation.recovery_scope_invalid",
                    "financial.payment_reconciliation.recovery_inside_automatic_window",
                    "financial.payment_reconciliation.recovery_intent_not_found",
                    "financial.payment_reconciliation.recovery_scope_forbidden",
                    "financial.payment_reconciliation.recovery_actor_invalid",
                    "financial.payment_reconciliation.recovery_confirmation_required",
                    "financial.payment_reconciliation.recovery_fingerprint_invalid",
                    "financial.payment_reconciliation.recovery_review_reference_invalid",
                    "financial.payment_reconciliation.recovery_reason_invalid",
                    "financial.payment_reconciliation.recovery_idempotency_key_invalid",
                    "financial.payment_reconciliation.recovery_idempotency_conflict",
                    "financial.payment_reconciliation.recovery_stale_preview",
                    "financial.payment_reconciliation.recovery_not_actionable",
                    "financial.payment_reconciliation.recovery_settlement_incomplete",
                    "financial.payment_reconciliation.invalid_command_context",
                    "financial.payment_reconciliation.command_contract_violation",
                    "financial.payment_reconciliation.nested_owner_command",
                    "financial.payment_reconciliation.active_caller_transaction",
                    "financial.payment_reconciliation.nested_transaction_completion",
                ),
                mapping_owner=(
                    "scheduled payment reconciliation task and finance-reviewed "
                    "exact-reference recovery adapters"
                ),
                retryable_codes=(
                    "financial.payment_reconciliation.provider_not_configured",
                    "financial.payment_reconciliation.settlement_unlinked",
                    "financial.payment_reconciliation.topup_projection_rejected",
                ),
                fail_closed_on=(
                    "provider, reference, transaction, amount, fee, or currency mismatch",
                    "missing or invalid explicit invoice instruction",
                    "successful observation without linked payment evidence",
                    "a selected intent whose lifecycle, provider, reference, or due "
                    "state changed before durable attempt progress",
                    "missing finance review, evidence reference, exact intent/reference "
                    "correlation, or current preview fingerprint",
                    "a reviewed intent that is pending, otherwise non-terminal, or not "
                    "outside the automatic window, or "
                    "reviewed provider/payment/event evidence that is stale, ambiguous, "
                    "or contradictory",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=(
                    "payment_reconciliation.paystack_outside_window_recovered",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries the immutable recovery-run, intent, Payment, "
                    "provider-event, finance-provider and optional capability-binding "
                    "identities; exact reference/external identity; gross, fee, "
                    "authorized net and currency; disposition; non-secret review "
                    "reference; preview fingerprint; and command lineage without raw "
                    "provider payloads or customer identity."
                ),
                replay=(
                    "Exact command replay returns the immutable recovery run and emits "
                    "no second event or financial consequence."
                ),
            ),
            projections=(
                ProjectionContract(
                    name=(
                        "top-up reconciliation backlog, progress, and age projection"
                    ),
                    input_names=(
                        "canonical top-up reconciliation policy",
                        "canonical reconcilable top-up intent",
                        "typed gateway reconciliation progress",
                    ),
                    writer="financial.payment_reconciliation",
                    freshness=(
                        "Recomputed on read at an explicit observation time "
                        "from intent creation, typed attempt and observation progress, "
                        "next reconcile times, and the effective stale, maximum-age, "
                        "retry, lane-reservation, and provider-interleave policy."
                    ),
                    stale_behavior=(
                        "Reports exhaustive, mutually exclusive pending fresh, due, "
                        "cooling-down, and outside-window partitions plus terminal "
                        "late-success due, cooling-down, and outside-window partitions. "
                        "It reports oldest due ages and attempt progress separately by "
                        "lifecycle lane and never treats a runner heartbeat or a full "
                        "checked batch as proof that the backlog is healthy."
                    ),
                    drift_signal=(
                        "A supported unresolved intent appears in zero or multiple "
                        "partitions, lane totals disagree with their exhaustive "
                        "partitions, oldest due age advances without durable attempt "
                        "progress, checked and attempt progress disagree, or a "
                        "selected-greater-than-checked gap persists beyond transient "
                        "concurrent claim loss."
                    ),
                    rebuild_operation=(
                        "Re-run topup_reconciliation_backlog from canonical "
                        "gateway intents, typed attempt and observation progress, and "
                        "effective reconciliation policy."
                    ),
                    repair_owner="financial.payment_reconciliation",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "app.services.payment_reconciliation mixed scheduled session "
                    "lifecycle, gateway calls, direct payment decisions, commits, "
                    "rollbacks, private provider lookup, allocation fallback, prepaid "
                    "invoice settlement, and synchronous access restoration"
                ),
                new_owner="financial.payment_reconciliation",
                verification=(
                    "Gateway outcome, deposit, invoice, consolidated, existing-payment, "
                    "expiry, reserved-lane and provider fairness, pre-I/O attempt "
                    "progress, exhaustive backlog partition, age/progress telemetry, "
                    "exact-reference preview/classification, finance-reviewed "
                    "outside-window replay and refusal, transaction-boundary, manifest, "
                    "and task/operator-adapter tests."
                ),
                cutover_gate=(
                    "The task owns only session lifecycle and serialization; the sweep "
                    "uses both reserved lanes, advances each successfully claimed "
                    "intent before provider I/O, and submits typed immutable evidence "
                    "to per-intent coordinator roots. Outside-window recovery accepts "
                    "only one exact current preview and finance confirmation for a "
                    "failed, abandoned, canceled, or expired Paystack intent; it rejects "
                    "pending and other non-terminal intents and never widens or bypasses "
                    "scheduled candidate policy."
                ),
                fallback_retirement=(
                    "Service commit/rollback/session creation, direct payment creation, "
                    "private provider lookup, guessed invoice allocation, synchronous "
                    "prepaid settlement, access restoration, direct SQL repair, and "
                    "unreviewed or bulk outside-window replay are absent."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                "docs/runbooks/PAYSTACK_AUTOMATIC_POSTING.md",
            ),
            test_refs=(
                "tests/test_payment_webhook_settlement.py",
                "tests/test_reconcile_honours_invoice_intent.py",
                "tests/test_paystack_outside_window_recovery.py",
                "tests/test_reconcile_paystack_reference_cli.py",
                "tests/architecture/test_payment_reconciliation_ownership.py",
                "tests/architecture/test_payment_settlement_participants.py",
            ),
        ),
    ),
)
