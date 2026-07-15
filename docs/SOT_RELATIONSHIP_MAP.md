# Single Source of Truth Relationship Map

This document names the service layers that should own decisions. Web/API
routes and Celery tasks should be thin wrappers around these services.

The executable registry is `app/services/sot_relationships.py`. When a domain
is harmonised, add or update its service boundary there and cover it with tests
before migrating more callers.

## UI Projection Boundary

The approved cross-Dotmac presentation contract is
`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`.

1. Domain read/context services own displayed facts, status meaning,
   provenance, freshness, and business action hints.
2. Domain command/transition services own action eligibility and execution.
3. RBAC owns authorization; event/timeline services own official history.
4. UI page contracts own relevance, ordering, progressive disclosure,
   responsive depth, and interaction shape.
5. Routes, templates, HTMX handlers, and mobile clients render the contract and
   submit commands; they do not derive business state, totals, or eligibility.

Rule: the UI is a projection boundary, not a new business source of truth. Web,
API, exports, and mobile surfaces may present different depths for their task,
but equivalent state and actions resolve through the same backend owners.

## Domain Order

1. `customer_context`
2. `financial_access`
3. `network`
4. `subscriber_sessions`
5. `application_sessions`
6. `secrets_credentials`
7. `notifications_communications`
8. `events_webhooks`
9. `runtime_infrastructure`
10. `observability`
11. `support_operations`
12. `provisioning_operations`
13. `feature_control_plane`
14. `authorization_control_plane`
15. `scheduler_control_plane`
16. `network_access_control_plane`
17. `service_intent_control_plane`
18. `integration_control_plane`
19. `ui_list_projection`
20. `ui_bulk_actions`
21. `ui_semantic_presentation`

Rule: each PR should finish one domain slice: define the owner service, migrate
the highest-risk callers, and add focused tests. Avoid broad mechanical rewrites
that obscure business behavior.

## Financial and Access

1. `financial.ledger` owns the append-only record lifecycle and reversal
   invariant. Domain owners decide why money moves.
2. `financial.payments`, `financial.consolidated_payments`,
   `financial.invoices`, and `financial.credit_notes` own their scoped document
   lifecycles, owner-produced previews, and the ledger postings those
   transitions require. Invoice read models expose payment, credit-note, and
   remaining-receivable amounts as distinct fields.
3. `financial.tax_configuration` owns configurable tax-rate records and their
   active lifecycle. Inclusive, exclusive, or exempt treatment belongs to the
   invoice/credit-note line, not to a second tax-rate vocabulary.
4. `financial.payment_proofs` owns proof review and creation of the source WHT
   receivable when a reseller pays net cash against a gross obligation.
5. `financial.tax_accounting` owns tax-report meaning, periods, currency
   separation, issued-output-tax and credit-note adjustment projection, net
   output-tax liability, WHT-receivable projection and lifecycle, its immutable
   official transition timeline, and the bounded tax-fact feeds consumed by
   Dotmac ERP. Issued output tax less issued credit-note tax adjustments is the
   source-document liability; it is not labelled as collected cash. Pending and
   certified WHT remain outstanding receivables; reclaimed and written-off
   records remain visible without inflating the outstanding amount. Dotmac ERP
   exclusively owns TaxCode account mappings, balanced journals, tax
   transactions, and financial statements. Sub has no tax posting or account-
   mapping table.
6. The VAS product is retired. Its database tables are immutable financial
   archives, not live balances or action owners. Revision
   `300_retire_vas_runtime` blocks cutover until wallet liabilities are zero and
   provider workflows are terminal; no route, task, setting, or service may
   resume writes to those tables. The cutover and fallback contract is
   `docs/designs/VAS_RETIREMENT.md`.
7. Customer financial position owns read-side financial summaries, including
   the bounded bulk projection used by cohort monitoring. It exposes invoice
   receivables and prepaid service funding as separate values; it does not net
   them into a generic balance or absorb payment lifecycle or service-access
   state. Bulk callers do not loop the single-customer ledger reader.
8. `financial.access_resolution` owns financial suspension/restoration
   eligibility. For prepaid service, both directions compare the customer
   financial position with the single `financial.prepaid_threshold`; the
   existence or size of one payment is never itself permission to restore.
9. `financial.prepaid_enforcement` owns the prepaid candidate cohort and the
   warn/suspend/restore plan consumed by both dry-run and execution. It consumes
   the funding decision from `financial.access_resolution`; it does not create
   another balance or threshold rule. Audit reconstruction may supply a named,
   timestamped funding snapshot (for example, Splynx cutover position plus
   native post-cutover events), but the enforcement owner still applies billing
   profile validity, grace, activation floor, shields, health, and lifecycle
   policy, including selection of the candidate cohort. Supplied snapshots are
   complete-or-error for that cohort and never fall back to a different local
   balance for missing accounts.
10. `financial.prepaid_plan_change` owns the immediate prepaid plan-change quote,
   affordability decision, confirmation fingerprint, and idempotent financial
   adjustment. It binds the human preview to a durable change request, locks the
   account and recomputes at write time, then records the exact adjustment or
   credit-note and ledger transaction on that request. Portal, admin, API, and
   change-request application paths do not post their own plan-change debit.
   Debits delegate to `financial.account_adjustments`; credits delegate to
   `financial.credit_notes`. Immediate admin bulk changes are gated until a
   batch contract can preview and confirm every subscription separately;
   next-cycle bulk scheduling produces no immediate financial transaction.
11. `financial.account_adjustments` owns debit eligibility, preview, locked
   confirmation, idempotency, actor audit, exact ledger evidence, and previewed
   append-only reversal. It never issues customer credits and never decides
   service-access state.
12. `financial.addon_purchases` owns customer add-on price, subscription-state,
   and entitlement confirmation. A paid add-on delegates one exact debit to
   `financial.account_adjustments` and stores the structural entitlement-to-
   adjustment link; a free add-on explicitly produces no ledger transaction.
13. Dunning owns postpaid enforcement; prepaid enforcement owns prepaid access.
   Both submit owner-produced previews to `financial.dunning`'s shared
   financial-access consequence confirmation. It locks and rechecks billing
   profile validity, payment-arrangement/proof/extension shields, canonical
   receivables or prepaid funding, and billing enforcement health immediately
   before acting. `access.subscription_lifecycle` is the sole writer of
   enforcement locks and subscription/account access status.
14. `financial.payment_arrangements` owns arrangement eligibility, lifecycle,
   installment schedule, payment application, and active-arrangement shield
   state. Dunning consumes the shield; it does not reimplement arrangement
   eligibility, and an arrangement does not rewrite receivables or access.
15. `financial.billing_health` owns monitoring snapshots and anomaly
    classification. Health signals are observations, not balances or direct
    suspension/restoration permission.
16. Scheduled billing, collections, and payment-reconciliation services own DB
   sessions, transaction outcomes, and operational logging for Celery runners.
17. `financial.payment_webhooks` owns signature-verified provider-payload
   projection and inbound dead-letter lifecycle. Replay rebuilds the same
   settlement command as live delivery; `financial.payment_provider_events`
   owns idempotent event processing, delegates the monetary write to the
   payment owner, and must resume an incomplete event rather than treating
   receipt identity as proof that money was posted.
18. Referral rewards are account credits owned by `financial.credit_notes`;
   neither CRM nor referral services post a parallel wallet balance. Automated
   referral issuance uses the same owner-generated preview, locked confirmation,
   idempotency, audit, and exact funding-ledger evidence as other credit issuance.
19. Every money-moving financial command is previewed by the same owner that executes it.
   Execution locks and recomputes the preview, rejects stale confirmation,
   records idempotency and actor audit evidence, and structurally links the
   command result to its exact ledger transaction(s). Financial settlement may
   request access reconciliation, but it never promises restoration itself.

Account adjustments and add-on purchase debits use one evidenced contract:

- Old paths: the generic ledger API could post or reverse arbitrary account
  entries, plan changes posted their own ledger debit, and customer add-on
  purchases derived a wallet balance before constructing a bare adjustment row.
  None recorded a durable decision-to-transaction link.
- New debit owner: `financial.account_adjustments` exposes prepaid funding,
  postpaid receivables, collection-blocking balance, and service-access
  consequence as distinct preview fields. Confirmation locks the account,
  recomputes the preview, rejects stale or unfunded requests, records
  idempotency and actor audit evidence, and links one decision to one exact debit.
- Credit boundary: the adjustment contract is debit-only. Customer credits,
  including the credit side of a prepaid plan change, remain documents owned by
  `financial.credit_notes`; callers cannot use a generic adjustment as a second
  credit authority.
- Add-on boundary: `financial.addon_purchases` combines the current catalog
  price and subscription state with the adjustment owner's funding preview.
  Mobile/API confirmation sends the fingerprint and an idempotency key, then the
  entitlement and exact adjustment link commit atomically. Clients do not
  derive affordability from a displayed balance.
- Reversal boundary: generic ledger reversal is gated. An adjustment reversal
  is separately previewed and confirmed, preserves the original category,
  records audit/idempotency evidence, and structurally points its exact credit to
  the debit it reverses. It does not promise restoration or mutate access state.
- Cutover gate: generic ledger writes/reversals remain disabled; plan-change and
  add-on paths contain no direct debit writer; stale preview, insufficient
  funding, idempotent replay, exact debit/reversal links, audit, architecture,
  API, and mobile contract tests must remain green.

Immediate plan changes use the same evidenced wrapper contract:

- Old wrapper: customer web/mobile/API and admin could show a proration quote,
  then submit only the target offer. The nested debit owner recomputed safely,
  but nothing proved which wrapper quote the person confirmed, and the change
  request did not name the resulting adjustment, credit note, or ledger row.
- New owner contract: the quote exposes one fingerprint plus distinct prepaid
  funding, postpaid receivables, collection-blocking balance, exact ledger type,
  source and amount, and the explicitly non-restorative access consequence.
  Confirmation supplies that fingerprint and an idempotency key. The owner
  locks and recomputes before changing money.
- Exact evidence: revision `302_plan_change_confirmation_evidence` links the
  applied request to at most one account adjustment or credit note and directly
  to its exact ledger entry. Zero-money immediate changes record the confirmed
  snapshot and no ledger link. Actor audit, request state, subscription state,
  and nested financial evidence commit together.
- Historical boundary: pre-cutover and scheduled next-cycle requests retain
  NULL confirmation/evidence fields; no amount, memo, or timestamp matching is
  used to invent financial provenance.
- Batch boundary: bulk admin changes schedule at each service's next cycle.
  Immediate batch execution is rejected until it can carry per-subscription
  owner previews, fingerprints, idempotency, and results.

Credit-note application is the first migrated financial-action contract:

- Old path: the invoice template derived credit availability and settlement
  totals, then posted directly to an unpreviewed application command.
- New owner: `financial.credit_notes` resolves choices, preview, eligibility,
  locked execution, idempotency, and application-to-ledger evidence;
  `financial.invoices` owns the receivable summary and settlement handoff.
- Cutover gate: preview fingerprint, exact ledger link, audit metadata,
  idempotent replay, invoice-summary, access-reconciliation, and template
  boundary tests must remain green.
- Historical application rows are not heuristically linked to ledger entries;
  reconciliation must use reviewed evidence rather than amount/memo guesses.

Credit-note issuance and voiding are the next migrated financial-action contract:

- Old owners: admin, refund, cancellation-proration, prepaid plan-change, CRM,
  and remediation paths could construct issued documents directly; some posted
  a separate credit ledger row and some posted no ledger evidence at all.
- New owner: `financial.credit_notes` produces the issue/void preview, locks and
  rechecks confirmation, creates the document and descriptive line, requests
  the exact append-only funding or reversal transaction, records idempotency and
  audit evidence, and structurally links every result.
- Projection boundary: the issued credit-note document owns the customer
  financial-position effect. Credit-note funding, application-transfer, and
  void-reversal ledger rows are operational evidence and are excluded from that
  projection so the same credit is not counted twice.
- Application boundary: applying a structurally funded note also links the exact
  unallocated debit that consumes the operational credit pool. Historical notes
  without reviewed funding evidence retain their legacy application behavior.
- Verification phase: direct writers have migrated to the owner and architecture
  tests reject new document, line, or status writers outside the owner package.
- Cutover gate: issue/void preview fingerprints, idempotent replay, actor audit,
  exact funding/application/reversal links, customer-position non-duplication,
  access separation, and adapter-boundary tests must remain green.
- Historical reconciliation is explicit and dry-run-first. It never guesses a
  ledger link from amount or memo; an operator must select the exact entry or
  explicitly approve creation of missing funding for the remaining amount.

Payment refunds are the next migrated financial-action contract:

- Old paths: the admin button and provider-event adapter could flip payment
  status without a confirmed amount, preview, idempotency key, or structural
  link to the refund transaction. The compatibility command could also grant a
  cash refund and credit note for the same amount.
- New owner: `financial.payments` exclusively resolves refund capability,
  previews customer funding, unallocated account credit, invoice receivables,
  exact ledger results, and the access-reconciliation handoff; then locks and
  recomputes those facts before confirmation.
- Provider boundary: manual recording is limited to non-provider payments.
  Provider-backed refunds require a signature-verified, provider-matched event
  carrying a normalized amount and currency; the provider-event adapter submits
  that observation and never sets refund status itself.
- Projection boundary: the payment document owns the refund's customer-position
  effect. Its payment-linked refund ledger row is exact accounting evidence and
  is not debited again from unallocated account credit. A separate structurally
  linked internal debit consumes only the refund portion attributable to
  spendable account credit and is excluded from the customer ledger projection.
- Access boundary: refund confirmation requests the canonical account-status
  recheck. Neither the preview nor the UI promises suspension, restoration, or
  any other service-access outcome.
- Cutover gate: stale-preview rejection, idempotent replay, audit evidence,
  exact total and account-credit ledger links, proportional invoice effects,
  normalized provider-event evidence, UI boundary, and owner-writer tests must
  remain green. Refund-plus-credit-note double benefit remains rejected.
- Historical reconciliation is dry-run-first and identifies every unlinked
  refund ledger row. Execution requires an operator-selected exact row and an
  explicitly reviewed account-credit consumption amount; it does not infer
  either from UI balances, memo text, or today's eligibility.

Payment reversals and chargebacks are a separate migrated financial-action
contract; they are not failed captures or customer refunds:

- Old owner/path: the compatibility command combined status mutation and a
  refund-shaped ledger row. It had no preview, confirmation fingerprint,
  idempotency reservation, audit record, or structural reversal evidence; a
  partially refunded payment could be marked failed without reversing its
  remaining settled value, and unallocated credit could remain spendable.
- New owner: `financial.payments` exclusively resolves reversal capability,
  previews the remaining settled value after completed refunds, separates
  customer funding, unallocated account credit, and invoice receivables, then
  locks and recomputes those facts before writing one `PaymentReversal` and its
  exact ledger links. The terminal payment state is `reversed`, distinct from a
  failed capture and from `refunded`/`partially_refunded`.
- Provider boundary: manual recording is limited to non-provider payments and
  represents a chargeback or bank reversal already confirmed outside Sub.
  Provider-backed reversal requires a verified, provider-matched event with the
  explicit normalized `reversal_confirmed` financial effect, exact remaining
  amount, and matching currency. Raw event names or UI-selected statuses are not
  financial evidence.
- Projection boundary: the reversed payment document removes its remaining
  settled value once from customer financial position. Its payment-linked total
  reversal debit is exact accounting evidence and is excluded from both the
  customer-position projection and the unallocated-credit pool. A second,
  structurally linked internal debit consumes only reversal value that was still
  spendable as account credit.
- Access boundary: confirmation requests the canonical account-status recheck;
  payment reversal does not decide, promise, or render a suspension or
  restoration amount.
- Verification/cutover gate: distinct status presentation, stale-preview
  rejection, idempotent replay, actor audit, exact total and account-credit
  links, proportional receivable reopening, normalized provider evidence,
  adapter boundaries, and sole-writer tests must remain green. Generic status
  edits and provider adapters cannot write `reversed` directly.
- Historical reconciliation is explicit and repairable. Inspection reports
  unlinked candidate debits, while execution requires the exact selected row and
  a reviewed account-credit consumption amount. It does not guess from an old
  failed status, a memo, or a current UI balance.

Payment creation, settlement, and allocation are one coherent owner contract:

- Old path: constructing a payment immediately posted allocations, unallocated
  credit, events, and access consequences even when the document said
  `pending`, `failed`, or `canceled`. Generic status edits later treated
  `succeeded` as a field value, provider adapters constructed allocations, and
  the admin form used a browser confirmation instead of an owner preview.
- New owner: `financial.payments` separates payment intent/observation from
  confirmed settlement. Pending, failed, and canceled documents post no money,
  change no receivable, emit no payment-received event, and request no access
  consequence. Only settlement writes `PaymentSettlement`, allocation ledger
  links, an unallocated-credit link, optional prepaid-renewal debit evidence,
  actor audit, and the access-reconciliation handoff.
- Position boundary: the preview keeps confirmed funding, unallocated account
  credit, postpaid invoice receivables, prepaid service renewal, payment state,
  and service-access consequence visibly distinct. A prepaid renewal is an
  explicit previewed debit and billing-period consequence, never a UI-derived
  balance or billing date.
- Allocation boundary: applying already-settled unallocated credit to an
  invoice is a transfer, not new funding. Confirmation writes and structurally
  links the exact invoice credit and a separate internal account-credit debit;
  customer financial position excludes that internal debit so the transfer
  does not double-change total funding. Provider adapters and APIs call the
  same owner.
- Immutability boundary: evidence-backed payment amounts, currencies,
  settlements, and allocations are not edited, deleted, or re-pointed in
  place. Pending allocation intent has no money evidence and may be withdrawn.
  Generic import rollback cannot delete financial rows; imported-payment
  reversal uses the separate batch owner below.
- Provider boundary: verified provider success is a settlement origin, while a
  non-success webhook remains an observation. A verified invoice hint becomes
  pending intent before settlement or uses the confirmed allocation-transfer
  owner after settlement; the provider adapter never constructs financial rows.
- Historical boundary: old succeeded payments are not automatically trusted or
  linked by amount/memo similarity. Inspection lists candidates; reconciliation
  requires an operator-selected exact ledger row for every active allocation,
  remainder, and prepaid debit, verifies the complete payment partition, links
  evidence, records audit, and posts no new money.
- Cutover gate: pending/no-money tests, stale-preview rejection, idempotent
  creation/settlement/allocation replay, exact settlement/allocation/prepaid
  links, provider replay, explicit historical reconciliation, owner-writer
  architecture tests, and admin/API preview-confirm boundaries must remain
  green. Generic succeeded status edits and direct settled-allocation commands
  remain gated.

Consolidated payment settlement has a separate scoped owner contract:

- Old path: a billing-account payment entered the generic payment creator as
  already succeeded. That path allocated member invoices immediately, mutated
  `BillingAccount.balance` for any surplus without a ledger row, and accepted a
  browser confirmation instead of an owner preview. Provider verification,
  proof approval, reconciliation, reseller checkout, admin, and API callers
  could each enter that parallel path.
- Owner: `financial.consolidated_payments` exclusively owns the exact FIFO or
  explicit member-invoice allocation preview, locked fingerprint confirmation,
  idempotency, actor audit, and settlement evidence. Verified provider facts
  and approved proofs use the same preview-bound owner; generic
  `financial.payments` may record a pending consolidated observation but gates
  a succeeded consolidated write.
- Position boundary: the preview and confirmation keep payment state, each
  subscriber invoice receivable, reseller-held consolidated credit, prepaid
  funding, and service-access consequence distinct. Paying a reseller account
  does not itself decide subscriber access; paid member invoices request the
  existing access-reconciliation owner.
- Ledger boundary: each member-invoice allocation links its exact subscriber
  `LedgerEntry`. Any surplus links one exact
  `BillingAccountLedgerEntry`; `BillingAccount.balance` is only the current
  projection of those consolidated-account transactions and never substitutes
  for ledger evidence or a fake subscriber account.
- Adapter boundary: admin uses a server-rendered preview and a second
  fingerprint-bound confirmation. The API exposes matching preview and confirm
  commands. Provider webhooks, top-up reconciliation, reseller checkout, and
  proof approval treat their verified fact or human approval as confirmation
  but still bind it to the owner-produced fingerprint and stable idempotency
  key.
- Cutover gate: read-only preview, exact dual-ledger evidence, stale-preview
  rejection, idempotent replay, pending/no-money behavior, generic-writer gate,
  provider replay, admin/API boundary, and owner-registry tests remain green.
  Historical succeeded consolidated payments are not guessed into evidence;
  they require a later explicit reconciliation slice.

Imported-payment batch reversal is a separate migrated wrapper owner:

- Owner: `financial.import_payment_batch_reversals` owns durable creation
  provenance, batch eligibility, the human preview fingerprint, locked
  confirmation, batch idempotency, actor audit, and exact links from import row
  to source settlement to resulting `PaymentReversal` and ledger transactions.
  `financial.payments` remains the sole writer of every nested payment reversal.
- Provenance boundary: a new applied payment row records both its exact
  `payment_id` and whether that run created or merely reused the payment. The
  created Payment also links back to that run. A later idempotent import cannot
  claim or reverse a payment created by an earlier run.
- Historical boundary: nullable provenance is deliberate. Existing import rows
  without both structural links fail closed and are not backfilled from row
  JSON, external ID, amount, memo, file name, or current UI state.
- Preview boundary: the batch owner resolves every exact source settlement,
  allocation ledger link, unallocated-credit link, prepaid-funding link,
  remaining reversible amount, receivable reopening, and resulting reversal
  ledger debit. Prepaid funding, unallocated account credit, postpaid
  receivables, collection-blocking balance, payment state, and service-access
  consequence remain visibly distinct.
- Confirmation boundary: the owner locks the import run and every affected
  account, rebuilds the whole batch preview, rejects drift before posting, then
  composes idempotent per-payment reversal commands in one transaction. A
  changed payment, refund, allocation, funding position, receivable, or source
  evidence aborts the entire batch. No imported row is deleted or deactivated.
- Reused-row boundary: rows that structurally say `record_created = false` are
  shown as skipped and remain unchanged. A batch with no newly created payments
  is ineligible rather than reversing somebody else's payment.
- Result/access boundary: `PaymentImportBatchReversalItem` links each import row,
  source settlement, payment reversal, exact result ledger debit, and optional
  account-credit-consumption debit. Nested payment reversal reopens receivables
  and requests canonical account/access reconciliation; the batch UI never
  promises restoration, suspension, or an eligibility amount.
- Cutover gate: revision `303_payment_import_batch_reversal.py`, stale-preview,
  replay, atomic multi-payment, mixed created/reused, invoice reopening,
  historical fail-closed, exact-evidence, adapter, and sole-writer tests must
  remain green. Legacy settings-history rollback stays nonfinancial only.

Nonterminal invoice lifecycle transitions are owned alongside terminal closure:

- Old paths: scheduled billing constructed draft/issued invoices directly and
  temporarily flipped prepaid drafts to issued; prepaid credit reconciliation
  and cleanup moved invoices back to draft; overdue automation, dunning, and
  admin bulk issue assigned status and timestamps themselves. The architecture
  allowlist normalized these parallel writers instead of enforcing one owner.
- Owner: `financial.invoices` now stages automation-created invoice documents,
  owns draft issuance, rechecks whether an untouched prepaid receivable may
  return to draft, and owns overdue eligibility, transition, one-time
  observation event, and audit. Automation, reconciliation, cleanup, dunning,
  and UI services select candidates and call the owner.
- Derived-state boundary: payment and credit settlement still derive
  `paid`/`partially_paid`/reopened status inside the invoice owner package from
  canonical settlement facts. No adapter may assign those states. Draft,
  issued, and overdue transitions record that no ledger transaction resulted;
  terminal monetary closure continues to require exact evidence below.
- Access boundary: `invoice.overdue` is an observation. It does not create a
  dunning consequence or decide service access. Returning an unfunded prepaid
  invoice to draft likewise changes no funding and grants no access.
- Verification boundary: the invoice lifecycle writer allowlist contains only
  `app.services.billing.invoices` and its derived-total helper. Direct status
  assignments in automation, reconciliation, cleanup, collections, and web
  adapters are rejected by architecture tests.

Invoice void and write-off are distinct terminal owner contracts:

- Old path: generic invoice status edits, single/bulk routes, and prepaid
  remediation jobs could set `void` or `written_off` directly. Void constructed
  ad hoc credits and deactivated original debits, violating append-only ledger
  semantics; partially settled invoices could retain stranded payment or credit
  allocations. Write-off trusted the stored balance and had no structural link
  from the terminal decision to its adjustment entry.
- New owner: `financial.invoices` exclusively resolves void/write-off
  eligibility, derives the receivable from invoice total plus canonical payment
  and credit-note settlement facts, previews exact consequences, locks and
  rechecks confirmation, records idempotency/audit evidence, writes one terminal
  `InvoiceClosure`, and links every exact ledger result through
  `InvoiceClosureLedgerEvidence`.
- Meaning boundary: void means the invoice should never have existed and is
  permitted only after effective payment/credit value is removed through its
  own owner. It reverses each original invoice debit append-only and leaves the
  original active. Current `invoice`-source debits and historical
  `adjustment`-source debits qualify only when the ledger row carries the exact
  `invoice_id`; unlinked account adjustments remain outside this owner.
  Write-off means collectible postpaid debt will not be
  collected; it writes one exact adjustment credit for the remaining
  receivable. It is not payment, prepaid funding, customer credit, or invoice
  void.
- Position boundary: a native written-off invoice remains the original customer
  debit and its `InvoiceClosure` contributes only the confirmed remaining-debt
  credit, preserving any already-applied payment/credit value. The linked
  operational ledger entry is evidence and is not counted a second time. Void
  removes the invoice document from customer position; its reversal rows are
  likewise evidence-only. Historical evidence reconciliation changes neither
  projection.
- State boundary: generic create/update/delete paths cannot manufacture paid,
  partially-paid, void, or written-off state, edit `balance_due`, or delete an
  issued receivable. Admin and API adapters use owner preview/confirmation;
  bulk void displays and confirms each per-invoice owner preview. Prepaid repair
  and cleanup workflows call deterministic system confirmation rather than
  mutating terminal state.
- Access boundary: the preview names only an access-reconciliation handoff.
  Confirmation clears the receivable and asks the access/collections owners to
  re-evaluate eligibility; neither void nor write-off promises restoration.
- Historical boundary: legacy terminal invoices remain immutable. Inspection
  lists exact invoice-linked ledger candidates; reconciliation requires explicit
  operator-selected evidence (one exact write-off credit or one exact reversal
  for every original invoice debit), records links/audit, and posts no money.
- Cutover gate: append-only reversal, exact write-off link, no-settlement void,
  stale-preview rejection, idempotent replay, draft/no-money closure, explicit
  historical reconciliation, remediation-adapter, admin/API confirmation, and
  owner-writer architecture tests must remain green.

Dunning decisions and their service-access consequences are now a distinct,
evidenced owner contract:

- Old paths: scheduled dunning selected policy steps, `_execute_dunning_action`
  independently rechecked some gates, `_suspend_account` rechecked a different
  set, payment events decided whether restoration was allowed from invoice
  snapshots, invoice-overdue events maintained a second warning/shield path,
  and case resolution restored throttle state without evidence linking the
  decision to what changed.
- Decision owner: `financial.dunning` owns postpaid policy selection and the
  shared financial access consequence preview/confirmation used by dunning,
  prepaid enforcement, payment settlement, and billing reconciliation.
  `financial.access_resolution`, payment arrangements/proofs/extensions, and
  billing health supply independent decision inputs; none writes access state.
- Grace owner: `app.services.collections.grace_policy` resolves the effective
  duration and provenance once: explicit account override, then active policy
  set, then billing-mode default. Postpaid dunning steps count from the end of
  that grace decision; prepaid planning, enforcement, and customer status use
  the same low-balance deadline. The former collections settings
  `prepaid_grace_days` and `prepaid_deactivation_days` are retired.
- Consequence writer: `access.subscription_lifecycle` exclusively creates or
  resolves `EnforcementLock` rows, persists their `access_mode`, and derives
  subscription/account status.
  RADIUS and session-enforcement services project that lifecycle result; they
  do not decide whether debt, funding, a shield, or a case permits access.
- Evidence boundary: every confirmed financial suspend, reject, throttle, or
  restore writes one `FinancialAccessConsequence` containing the exact locked
  preview fingerprint, idempotency key, separated receivable/prepaid/profile/
  shield/health inputs, outcome, and system audit. Structural evidence links
  the decision to every exact enforcement lock, access credential, and dunning
  case it created, resolved, throttled, or restored. A `DunningActionLog` links
  to the consequence that implemented its access action.
- Access-tier boundary: hard reject is the default. Captive is a requested
  exception only for an explicitly opted-in, direct-house account with an
  explicit residential classification and a valid enabled portal network
  contract. Business, government, NGO, reseller-owned, reseller-principal,
  system, disabled, canceled, and uncategorized accounts fail closed even when
  a stale opt-in flag exists. `app.services.walled_garden_policy` revalidates
  persisted captive intent and applies most-restrictive-active-lock-wins before
  RADIUS, connectivity, and UI read projections consume it.
- Captive cutover gate: the global captive setting remains disabled until
  staging proves RADIUS projection readback, portal reachability from the
  restricted tier, a real test payment, and canonical post-payment access
  restoration. Any failed or stale readiness input downgrades the effective
  tier to hard reject.
- Restoration boundary: payment and invoice settlement submit observations;
  they never promise restoration. Confirmation resolves overdue locks/cases/
  throttle only after canonical overdue receivables are empty, and resolves
  prepaid locks/timers only after the canonical funding threshold is met.
  Other active lock reasons remain untouched.
- Transport boundary: `invoice.overdue` is observation only. Notifications,
  throttle, suspension, and rejection come from the configured dunning step.
  `payment.received` always asks the owner to reconcile and contains no local
  invoice-balance eligibility branch.
- Retired controls: billing automation no longer reads or seeds
  `auto_suspend_on_overdue`, `suspension_grace_hours`,
  `dunning_escalation_days`, `blocking_period_days`, or
  `deactivation_period_days`. Existing database rows are inert legacy data.
  The hourly billing-notification job sends pre-due invoice reminders only;
  it does not re-emit overdue events or maintain invoice metadata as access
  evidence. Policy dunning steps are the only overdue timing/action controls.
  `PolicySet.suspension_action` is retained as compatibility data but is not an
  execution input and is no longer exposed by the admin policy form.
- UI boundary: subscriber pages do not derive a "next block" date or access
  eligibility from balance, grace, or legacy metadata. They render only
  owner-produced financial/access projections and confirmed consequences.
- Historical boundary: a throttled credential without a structurally captured
  `pre_throttle_radius_profile_id` is reported in preview/audit and is not
  restored by guessing from an offer, UI state, or current profile. It requires
  explicit reviewed historical reconciliation.
- Cutover gate: stale-preview rejection, idempotent replay, exact consequence
  links, canonical receivable evidence, shield/health enforcement, reason-
  scoped restoration, event-adapter thinness, and owner-writer architecture
  tests must remain green.

Rule: no module should infer access from draft invoices, ad hoc balances, or
legacy import fields when a billing/access resolver exists. Celery tasks only
apply scheduling, routing, idempotency, and feature-gate concerns before calling
the owning financial service. Retired VAS archive tables have no application
writer and are excluded from schema autogeneration so history cannot be dropped
accidentally. Templates and mobile clients do not calculate invoice
receivables, credit availability, restoration amounts, billing dates, or
financial-action eligibility.

Tax-accounting migration record:

- Old owner: `app.services.web_reports_extended` queried invoice models and the
  Jinja report interpreted them as `tax_amount`/`total_amount`, ignored its date
  controls, mixed currencies, and labelled issued tax as collected.
- New source-fact owner: `app.services.tax_accounting` projects bounded invoice,
  credit-note, and WHT rows plus full filtered aggregates per currency. It owns
  legal WHT transitions and the WHT official timeline. Web services and routes
  remain thin adapters.
- Accounting owner: Dotmac ERP owns TaxCode configuration and account mappings,
  balanced invoice/credit-note/payment/WHT journals, tax transactions, tax
  returns, and financial statements. Its existing pull integration consumes
  Sub's bounded sync feeds; no parallel push or local Sub subledger is added.
- Read boundary: the tax report is the canonical tax-register projection from
  authoritative invoice, credit-note, and WHT source documents. ERP journals do
  not replace source-document ownership, and Sub report rows do not replace ERP
  accounting.
- Credit-note tax point: `financial.credit_notes` persists the first `issued_at`
  when a credit enters an adjusting state; `financial.tax_accounting` uses that
  timestamp for report periods and the ERP sync contract. Migration 291
  backfills existing issued/applied rows from `created_at`. All direct automated
  writers use the shared lifecycle adapter, and cancellation credits preserve the
  source invoice, rate, and inclusive/exclusive/exempt line treatment.
- Fallback retirement: the false `total_tax`/`invoices` model contract and
  `tax_amount`/`total_amount` template fields are removed in this slice.
- Feed contract: invoice and credit-note sync lines expose `tax_rate_id` and
  `tax_application`; the tax-rate feed exposes code/rate; payment sync exposes
  gross cash settlement, net bank cash, WHT amount/rate/status/record/certificate,
  and the source resolution timestamp for terminal decisions. WHT transitions
  advance the owning payment watermark so ERP re-pulls changes.
- ERP resolution: ERP resolves each source rate/treatment to exactly one active,
  effective, ERP-owned TaxCode and fails closed on missing or ambiguous account
  configuration. Corrections reverse and re-post in one transaction rather than
  mutating posted lines.
- Operator control: `/admin/billing/tax-accounting` is the permission-protected
  source-fact and WHT evidence console with server-side search, status filters,
  counts, and pagination. It does not offer account mapping or journal controls.
- WHT lifecycle: payment-proof verification creates the pending source record.
  The tax owner alone permits pending -> certified -> reclaimed, pending/certified
  -> written_off, requires certificate evidence or a write-off reason, and appends
  `withholding_tax_transitions`. Each transition advances the payment sync
  watermark; ERP applies the accounting consequence from its own mapped accounts.

## Customer Context

1. Customer context owns identity, account, billing, service, support, and
network summary composition.
2. Customer network context owns the raw customer-to-network footprint.
3. Network access path owns the customer service path.
4. `customer.service_status` owns customer-visible service health and action
   hints, including whether payment can restore every active service hold and
   the authoritative amount required by financial policy.
5. `customer.usage_summary` owns customer usage windows, headline totals, and
   total provenance. An authoritative zero is a valid value, not a missing-data
   sentinel.

Rule: admin, portal, support, and reporting views should consume context
services instead of rebuilding customer joins. Customer clients must not infer
that `blocked` or `suspended` means payment-restorable, or calculate restoration
amounts from locally loaded invoice rows; they consume `/me/service-status`.
Customer clients consume `/me/usage-summary` totals and provenance; they do not
replace a server total with a loaded-session page, chart-series sum, or a
different time window.

## Support Operations

1. `support.ticket_lifecycle` owns the ticket status vocabulary, guarded status
   transitions, lifecycle timestamps, and transition consequences.
2. `support.ticket_configuration` owns the operator-visible status subset,
   priority/type choices, routing, and SLA policy. A configured status must be
   part of the lifecycle vocabulary.
3. Status configuration does not own labels, tones, icons, or platform colors;
   those are read-side presentation concerns.

Rule: API, admin, customer, reseller, automation, and import adapters request
ticket mutation through the ticket lifecycle service. Settings may narrow the
choices presented to operators but cannot create a state the lifecycle owner
will reject.

## UI List Projections

1. `ui.list_contracts` owns normalized list query state, list capability
   declarations, page metadata, and canonical URL serialization.
2. Each resource declares one projection owner for its searchable fields,
   filters, stable sort, row projection, and filtered count.
3. `ui.customer_list_projection` is the first migrated resource. The live admin
   customer route and Jinja table consume `ListQuery` and `PageMeta` from
   `app.services.web_customer_lists`.
4. The configurable-table customer data endpoint is now a compatibility
   projection over `app.services.web_customer_lists`. `app.services.table_config`
   still owns saved column visibility/order and serialization, but it does not
   select, filter, count, sort, or paginate customer rows. The live customer
   template does not load or mount the legacy client.
5. Customer configurable-table migration record:
   - Old owner: the generic
     `TableConfigurationService.apply_query_config` customer branch.
   - New owner: `app.services.web_customer_lists`, using `ui.list_contracts`.
   - Verification phase: contract tests exercise canonical scope, compatibility
     aliases, filters, stable sorting, and clamped pagination. A runtime dual-read
     shadow was not retained because the live customer screen had already been
     gated off the legacy client.
   - Cutover gate: customer list, compatibility API, SOT-registry, and route
     architecture tests must remain green.
   - Fallback retirement: the generic customer scalar-filter and location-filter
     branches were removed; unsupported inputs fail closed with HTTP 400.
6. Legacy `q`, `activation_state`, `customer_type`, NAS/location,
   `customer_name` sort, `limit`, and aligned `offset` inputs are normalized into
   `ListQuery`.
7. `ui.subscriber_list_projection` owns the remaining subscriber
   configurable-table query. There is no separate live subscriber list: the
   production admin list and legacy Playwright facade both use `/admin/customers`,
   while `app.web.admin.subscribers` is an import alias to the customer router.
8. Subscriber configurable-table migration record:
   - Old owner: the generic `TableConfigurationService.apply_query_config`
     Subscriber branch.
   - New owner: `app.services.web_subscriber_lists`, using `ui.list_contracts`
     and delegating subscriber scope/full-text search to
     `app.services.subscriber.Subscribers.query`.
   - Verification phase: contract tests exercise scope, search aliases, filters,
     stable sorting, filter-before-pagination, and clamped offsets. No runtime
     shadow was retained because no production template mounts the subscriber
     dynamic-table client.
   - Cutover gate: subscriber service, compatibility projection, SOT registry,
     and architecture tests must remain green.
   - Fallback retirement: the generic table query engine and Subscriber-specific
     fallback were removed. New table data resources require a named projection
     owner before registration.
9. Subscriber list reads are read-only. The retired table path used to generate
   missing subscriber numbers and commit them during serialization. Identifier
   assignment remains with subscriber creation/update workflows; projections
   return the stored value, including `null`, and never repair it implicitly.
10. Legacy subscriber `q`, `status`/`activation_state`, `subscriber_type`,
    declared sorts, `limit`, and aligned `offset` inputs normalize into
    `ListQuery`; undeclared scalar filters and sorts fail closed with HTTP 400.
11. `ui.invoice_list_projection` extends the existing
    `app.services.web_billing_overview` invoice owner with declared searchable,
    filterable, and sortable fields; stable ID tie-breaking; page clamping; and
    an uncapped export scope. Full-page and HTMX reads render the same
    `_invoices_list.html` and `_invoices_table.html` projections, so status
    totals, filters, canonical URLs, pagination, and rows cannot diverge.

Rule: filters and search are applied before pagination; every paginated sort has
a unique tie-breaker. Web list state is encoded in URL query parameters so deep
links, refresh, and browser history reproduce the same projection. A changed
search, filter, sort, or page size starts at page one. Templates render the
owner-provided query and page metadata and do not hand-build competing query
strings, totals, page counts, or sort rules. Under the global Dotmac UI
standard, the interaction model follows the Carbon data-table, filtering, and
pagination patterns, with WCAG 2.2 AA as the accessibility floor. This is a
behavior standard, not a Carbon visual-theme migration. Column-configuration
responses derive their `sortable` flags from the corresponding resource owner
rather than the legacy table-field registry.

## UI Bulk Actions

1. `ui.bulk_action_contracts` owns code-native selection modes and the
   authorized presentation of bulk action label, description, semantic tone,
   preview/confirmation requirements, execution mode, and result-reference
   vocabulary. It does not own business eligibility or mutation.
2. A bulk resource declares page select-all semantics and whether the list owner
   supports an explicit all-filtered selection. Empty selected IDs never imply
   a filtered cohort.
3. `ui.customer_bulk_action_projection` is the first adopted resource. It
   projects only customer actions authorized for the current principal and
   depends on `ui.customer_list_projection` for filtered scope semantics.
4. The customer table header checkbox selects the visible page. A separate
   affordance promotes that selection to all rows matching the canonical search
   and filters. Search, filter, or page-size changes clear the selection.
5. `app.services.web_customer_actions` resolves selected IDs or the explicit
   filtered query again at preview and execution. Mutations require the preview
   count and exact-membership token in the confirmation request and fail with
   HTTP 409 when the cohort has changed. Commands continue to re-check domain
   state and return partial
   outcomes or notification identifiers.
6. `ui.invoice_bulk_action_projection` adopts the same interaction contract for
   invoice issue, send, void, mark-paid, PDF-generation, and export actions.
   `app.services.web_billing_invoice_bulk` remains the single eligibility and
   command owner; the projection calls that policy rather than copying status
   rules into Jinja or JavaScript.
7. Invoice selection is page-only. Mutation and PDF-generation commands require
   a server preview, exact resolved count, and impact token. The token covers
   selected membership plus each row's eligibility outcome, so a status change
   that expands or shrinks impact after preview fails with HTTP 409. Execution
   re-checks eligibility and audits only processed invoice IDs.

Migration record:

- Old owners: customer Jinja/Alpine independently exposed the actions menu,
  stored selected IDs, and interpreted an empty array as every row matching
  submitted filters; the reusable data-grid selectable mode was a second local
  ID collector without action capabilities. Invoice Jinja/Alpine independently
  hardcoded actions and confirmation text, while its full-page and HTMX tables
  rebuilt different filters, rows, and pagination.
- New owners: `app.services.bulk_actions` owns the generic interaction contract,
  `app.services.web_customer_bulk_actions` owns the customer projection,
  `app.services.web_customer_lists` owns filtered customer cohort semantics,
  `app.services.web_billing_overview` owns the invoice list/export scope,
  `app.services.web_billing_invoice_bulk_actions` owns invoice action
  presentation, and existing customer/invoice command services retain mutation
  and consequence ownership.
- Verification: contract, service, route/template architecture, selection,
  explicit filtered-scope, list-query, preview, membership/eligibility drift,
  and partial-outcome tests protect the boundary.
- Cutover gate: no-selection requests fail closed; unauthorized actions and
  selection controls are omitted; page selection and filtered promotion are
  distinguishable; preview membership or eligibility drift prevents execution.
- Fallback retirement: the customer page no longer exposes bulk actions before
  selection, and `resolve_bulk_customer_scope` no longer falls through from an
  empty ID list to filtered execution. The invoice page no longer hardcodes
  action buttons, eligibility assumptions, manual query strings, or a second
  HTMX-only table. Other resources remain unchanged until they adopt named list
  and bulk projections.

Rule: bulk controls appear only when a selection exists and a canonical command
supports it. Filtered, customer-visible, financial, destructive, or fleet-wide
operations require explicit impact preview and confirmation. WCAG 2.2 AA labels,
indeterminate state, selected-count announcements, and focus/keyboard behavior
are part of the contract; hidden controls are never authorization enforcement.

## UI Semantic Presentation

1. Account, subscription, invoice, payment, outage-incident, support-ticket, and
   work-order lifecycle owners remain authoritative for raw values and
   transitions. `network.device_state` remains authoritative for the derived
   device operational vocabulary, retry-pending state, and alarm classification;
   `network.connection_health` owns the separate customer-safe
   `connected/trouble/outage` verdict and diagnostic wording.
2. `ui.status_presentation` owns the human label, semantic tone (`positive`,
   `info`, `warning`, `negative`, or `neutral`), and non-color icon key for each
   account, subscription, invoice, payment, outage-incident, device operational,
   customer connection health, support-ticket, and field work-order status.
3. Admin customer, billing, and support screens; customer billing/support;
   reseller invoice/ticket and customer-connection screens; network outage and
   device NOC consoles;
   catalog, billing, service-status, support, CRM outage, and network-device API
   projections; customer mobile;
   field job/manager APIs; and field mobile consume the same
   `StatusPresentation` contract.
4. Server responses carry semantic meanings, not Tailwind classes, Flutter
   colors, or other platform-specific tokens. `customer.branding` owns the
   concrete primary, secondary, and five-role semantic palette. Web renders it
   through `/branding/theme.css`; both Flutter clients resolve the same
   `BRAND_SEMANTIC_*_COLOR` build inputs from `brand.json`. Renderers select a
   role and icon; they do not keep local role-to-color dictionaries.
   The runtime stylesheet also owns compatibility aliases for legacy non-neutral
   Tailwind palette names and the ordered `data-1` through `data-7` categorical
   palette used by charts and maps. Structural neutral surfaces, text, borders,
   shadows, white, and black remain owned by the design-system foundation.
5. Unknown or old-backend values fail neutral. Clients may humanize the raw
   value for compatibility, but must not recreate state-specific tone policy.

Migration record:

- Old owners: account label/color dictionaries in customer Jinja and portal
  context, subscription/invoice/ticket state-to-tone switches in customer
  mobile, invoice and ticket label/color dictionaries in portal/admin/reseller
  Jinja, configurable ticket status colors, and work-order label/color
  dictionaries in field mobile, plus outage lifecycle badges in the manual,
  classifier, and notification-review consoles, plus device operational label/
  color maps in NOC inventory, detail, monitoring, worklist, and map surfaces,
  plus customer-connection state/color switches in portal, reseller, and mobile
  diagnostic surfaces.
- Old color owners: literal Tailwind/hex tone maps in the shared badge,
  connection diagnostics, NOC map/summary renderers, and Flutter status widgets.
- New meaning owner: `app.services.status_presentation`, transported through
  `app.schemas.status_presentation.StatusPresentation`. New concrete-color
  owner: `app.services.brand_profiles` and the generated brand theme tokens.
- Compatibility phase: legacy Tailwind palette names resolve to branding-owned
  scales at runtime; new or touched code uses primary, accent, semantic, or
  categorical data tokens directly. Literal chart, map, and mobile palettes are
  retired from migrated slices.
- Verification phase: exhaustive enum coverage, API serialization, projection,
  template architecture, and Flutter parsing/rendering tests.
- Cutover gate: no customer account/subscription, invoice, payment, outage-incident,
  device operational, customer connection-health, support-ticket, or field
  work-order status dictionary or local semantic role-to-color map remains in
  migrated templates or mobile presentation paths. Configured semantic seeds
  must retain WCAG 2.2 AA text contrast in light and dark themes.
- Fallback retirement: client compatibility fallbacks are neutral-only and may
  be removed after all supported servers emit `status_presentation`.

Rule: UI consumers render semantic tones and icon keys through branding-owned
theme tokens. They do not decide that a domain state is positive, warning,
negative, informational, or neutral, and they do not assign a literal color to
one of those roles locally.

## Secrets and Credentials

1. Bootstrap secrets required before the application starts use environment or
   mounted secret files.
2. Low-cardinality application and integration secrets use OpenBao references.
3. High-cardinality customer, device, and connector credentials use the
   declared encrypted database-field inventory.
4. Scheduled rotation stages current and previous keys, converges stored
   ciphertext, and retires the previous key only after the grace period.

Rule: callers request a secret or credential outcome from the owning service.
They do not choose fallback precedence, store plaintext, reveal existing values
in forms, or rotate key material directly.

## Notifications and Communications

1. Notification channel policy owns channel eligibility and preferences.
2. Event notification policy owns event enablement and balance-notification
   suppression.
3. Notification service owns notification rows and delivery lifecycle.
4. Staff notification service owns internal/admin notification creation.
5. `communications.customer_read_state` owns customer notification read/unread
   state and unread counts across the web portal and mobile app. Subscriber
   metadata is its bounded persistence mechanism; `/me/notifications` projects
   that state, and `/me/notifications/read` is the self-scoped mutation
   boundary. Device storage is only a one-way legacy migration input. The
   identity-cleared GET response cache may render last-known state offline but
   never accepts read decisions.
6. `communications.team_inbox` owns conversation notes, assignment, replies,
   contact-linking, widget writes, inbound-channel ingestion, collaboration,
   and admin mutation transactions. `app.services.team_inbox_commands` is the
   committed admin command boundary; `app.web.admin.inbox` only translates HTTP
   inputs and outcomes.
7. Campaign services own marketing audience, sequence, and content decisions.
   They request a canonical sender key; email delivery alone resolves that key
   to SMTP identity and credentials.

Rule: domain services request a notification outcome; they should not construct
notification rows, choose email/SMS/WhatsApp directly, or maintain recipient
read state outside the owning service. Admin inbox routes must not load or
mutate inbox ORM rows, control commits, or select alternate mutation helpers.

## Events and Webhooks

1. Event dispatcher owns event routing.
2. Event-store service owns event rows, handler attempts, retry lookup, cleanup,
   and stale processing.
3. Webhook delivery service owns webhook delivery rows and queueing.
4. Subscription lifecycle event service owns lifecycle audit rows.

Rule: handlers orchestrate. Persistence and retry bookkeeping live in services.

## Observability

1. Observability service owns task/job run recording.
2. Task reliability owns task metadata, heartbeat interpretation, and alerting.
3. Metrics collectors expose read-only gauges/counters for runtime pressure.
4. Scheduled single-flight producers own expensive business-health snapshots;
   metrics collectors only read those bounded snapshots.
5. The cross-Dotmac scrape contract is defined in
   `docs/METRICS_SCRAPE_SAFETY.md`: `/metrics` reads process-local instruments,
   bounded snapshots, and static metadata only. It never opens a database
   session or invokes a business resolver.

Rule: Celery tasks report lifecycle through shared observability helpers; they
should not write heartbeat/run rows directly unless they are the helper.
Scrape-time collectors must never perform unbounded business-table scans or
per-customer financial reconstruction. Database and infrastructure queries are
also produced out of band so pool exhaustion cannot make the scrape path block.

## Network Domain

Dependency order:

1. `network.identity`: resolves cross-model network/customer links.
2. `network.monitoring_inventory`: owns monitoring inventory, metric records,
   alert rules, and alert state mutations.
3. `network.access_path`: resolves `subscriber/subscription -> access path`.
4. `network.radius_sessions`: resolves online-now state from active sessions.
5. `network.device_state`: derives NOC operational state, retry state, and alarm
   classification from administrative intent and monitoring observations, and
   owns the `up/degraded/down/maintenance` vocabulary. Retry-pending gaps stay
   binary but are non-alarming; presentation renders retry-pending `down` as
   warning/clock rather than a confirmed negative failure.
6. `network.outage_impact`: resolves affected customers from topology.
7. `network.device_groups`: owns device-group mutations, membership, and bulk
   action queueing.
8. `network.outage_lifecycle`: owns the persisted incident status vocabulary,
   incident transitions, escalation planning, and outage event emission.
9. `network.connection_health`: combines authoritative path, live-session,
   last-mile, impact, and active-incident inputs into the customer-safe
   `connected/trouble/outage` verdict plus headline/message/advice. It does not
   own device operational state or raw online-session observations.
10. `network.control_plane_intent`: owns the shared desired-state delivery
   lifecycle, control-plane target/revision identity, and vendor status
   projections. Vendor adapters project through this one
   desired-to-readback lifecycle.
11. `network.routeros_sot`: owns typed MikroTik desired state, the managed
   resource/field registry, Dotmac ownership markers, verified reconciliation,
   and periodic drift evidence. Router routes and tasks only orchestrate it,
   and it projects through `network.control_plane_intent`.
12. `network.operation_ledger`: owns the tracked device operation lifecycle and
   status vocabulary, the terminal-transition guard, correlation-key duplicate
   suppression, stale-active reclamation, parent/child rollup, and whether an
   operation may run, resume, or be re-executed. Celery is transport: tasks
   report progress through the ledger and do not decide retry eligibility.
   `app.services.task_reliability` declares each task's retry/idempotency/
   visibility contract and is a *projection* of this owner, not a second
   authority. A contract may only claim operator redrive
   (`MANUAL_REDRIVE`/`ADMIN_REDRIVE`) once a redrive path exists in the ledger;
   declaring an affordance that does not exist is drift, not policy.

Rule: pollers write observations; resolver services decide state; event services
decide consequences. Customer-facing outage, SLA, expiry suppression, support
context, and escalation should consume these network SOT layers.
Outage list/detail projections add `StatusPresentation` from the raw lifecycle
state; templates and CRM consumers do not maintain their own state-to-severity
dictionaries. Device operational state and customer connection-health verdicts
remain separate vocabularies owned by their corresponding network services.
Customer portal, reseller, support context, API, and mobile verdict surfaces
consume the same connection-health payload and semantic presentation; raw
session dots on subscription views remain observation surfaces outside that
verdict.

## Subscriber Sessions

Dependency order:

1. `sessions.radius_reconciliation`: is the canonical writer of the
   `radius_active_sessions` projection; it reconciles external `radacct` open
   sessions and prunes dead rows.
2. `sessions.radius_resolution`: answers online-now and primary-session
   questions for customers/subscribers.
3. `sessions.enforcement`: owns CoA, disconnect, and session refresh outcomes
   after billing/access/FUP state changes.

Rule: accounting imports write session facts; resolvers answer online state;
enforcement applies network-side consequences. Billing/access code should not
query `RadiusAccountingSession` or `radius_active_sessions` directly to decide
access.

## Application Sessions

Dependency order:

1. `app_sessions.store`: owns Redis-backed storage, principal indexes, fallback
   store, and revocation epochs.
2. `app_sessions.customer_portal`: owns customer portal session lifecycle,
   refresh, revoke-all, impersonation, and read-only policy.
3. `app_sessions.auth`: owns database auth-session listing and revocation.

Rule: routes authenticate and authorize, but session lifecycle and revocation
policy belongs in session services. Do not duplicate cookie/session mutation
logic in route handlers.

## Runtime Infrastructure

Dependency order:

1. `runtime.db_sessions`: owns background DB session lifecycle and advisory lock
   safety.
2. `runtime.task_idempotency`: owns duplicate suppression and stale task
   execution rows.
3. `runtime.task_heartbeat`: owns task success/skip heartbeat signals.
4. `runtime.infrastructure_polling`: owns native poll observations and the
   pollable-device predicate.
5. `runtime.infrastructure_health`: owns dependency health checks for
   Postgres, Redis, VictoriaMetrics, Celery, and related infrastructure.

Rule: tasks should use shared DB-session, lock, idempotency, and heartbeat
helpers. Infrastructure pollers write observations only; network/device SOT
services interpret state for customer impact, alerts, and SLA.

## Provisioning Operations

Dependency order:

1. `operations.provisioning_context`: composes subscriber, subscription, ONT,
   CPE, TR-069, ACS, service address, and NAS context.
2. `operations.provisioning_workflow`: executes service-order workflows and
   provisioning steps from the resolved context.
3. `operations.work_order_status`: declares persisted work-order values and the
   canonical open, assignable, and terminal sets.
4. `operations.work_orders`: exposes work-order read models and customer links.
5. `operations.field_completion`: owns field-job completion eligibility, evidence
   requirements, and completion transitions.
6. `operations.project_lifecycle`: owns native project field/status mutations,
   project SLA synchronization, and lifecycle event/notification requests.

Rule: provisioning callers should resolve customer/network context once through
the operations context service before running workflow steps. Step executors may
consume context, but should not rediscover subscriber/ONT/CPE links themselves.
`Projects.update` is the canonical writer for native project mutations;
Kanban, Gantt, normal edit, API, and web adapters delegate to it rather than
maintaining parallel SLA/event/notification paths. Customer and reseller read
authority is owned by `projects.native_read`. Where CRM project data is shown, it
is served from a local mirror populated over the CRM API and treated as a cache,
never as the authority. Field job detail projects `completion_requirements`
from the same transition service that validates completion. Field clients consume
that contract and may offer advisory quality checks, but must not invent a separate
completion gate from local checklist state or cached settings.
Work-order API projections carry server-owned status labels, tones, and icons;
field clients retain the raw value for transitions and filtering, but do not
reinterpret its presentation.

## Control Planes

Feature controls:

1. `control.module_manager`: owns product module enablement.
2. `control.domain_settings`: owns stored setting mutation.
3. `control.settings_spec`: owns setting schema, coercion, and env fallback.
4. `control.feature_registry`: composes module, feature, safety, canonical, and
   legacy flag resolution.

Rule: task and feature gates should call the feature registry. Callers should
not separately read env vars, domain settings, module state, and legacy flags.
Registered capability gates include billing capture/collections/payment
options, prepaid monthly invoicing, RADIUS/session enforcement,
usage/FUP emission gates, CRM/native transition flags, and GIS/network worker
toggles. Numeric intervals, thresholds, profile IDs, account lists, and other
tuning values remain in `settings_spec`.

Authorization:

1. `auth.rbac`: owns roles, permissions, and assignments.
2. `auth.permission_gate`: owns request/route permission dependencies.
3. `auth.staff_provisioning`: owns staff account bootstrap.

Rule: routes declare permissions and business services receive an authorized
principal. RBAC mutation stays inside RBAC services.

Scheduler:

1. `scheduler.registry`: owns effective task registration, cadence, and toggle
   synchronization.
2. `scheduler.operations`: owns `ScheduledTask` CRUD and manual enqueue.
3. `scheduler.worker_control`: owns worker restart targets/actions.

Rule: task cadence and enablement flow through scheduler config and the feature
control plane. Task bodies execute work and report status.

Network access:

1. `access.control_resolution`: owns desired service access outcomes.
2. `access.event_policy`: owns event-driven enforcement settings, FUP action
   policy, and overdue suspension policy reads.
3. `access.walled_garden_policy`: resolves persisted restriction intent to the
   effective hard-reject/captive tier. Hard reject is default; captive requires
   explicit eligible residential opt-in and network readiness.
4. `access.radius_state`: maps the effective tier to RADIUS groups/profiles.
5. `access.radius_reject`: owns reject IP lifecycle.
6. `access.session_enforcement`: applies CoA/disconnect outcomes.

Rule: billing, FUP, and admin actions resolve the desired access outcome once,
map it to RADIUS state once, and let enforcement apply the network-side change.

Service intent:

1. `service_intent.catalog_policy`: owns catalog policy lookup.
2. `service_intent.catalog_validation`: owns catalog consistency checks.
3. `service_intent.catalog_billing_governance`: owns billing-critical catalog
   mutation safety, audit, and operator alerts. Live pricing/cadence is versioned
   rather than edited in place, and routes require `catalog:billing_write`.
4. `service_intent.subscription_lifecycle`: owns the current/proposed lifecycle
   projection, command eligibility, reviewed-head contract, and billing/access
   impact preview.
5. `service_intent.subscription_lifecycle_execution`: owns serialized,
   idempotent execution and structured single/batch outcomes. It delegates the
   resulting mutations to account lifecycle, catalog, billing, scheduler, and
   RADIUS owners. Admin routes and bulk adapters submit commands to this owner;
   they do not update subscription status or offers directly.
6. `service_intent.subscription_nas_assignment`: owns commercial-service NAS
   assignment.
7. `service_intent.ont`: projects provisioning intent to ONT operations.

Rule: catalog policy and subscription owners define commercial intent. Every
lifecycle execution carries a reviewed head and idempotency key. Network owners
project configured intent without a parallel catalog-to-network adapter.

Integrations:

1. `integration.registry`: owns connectors and capabilities.
2. `integration.jobs`: owns targets, jobs, and runs.
3. `integration.sync`: owns sync orchestration.
4. `integration.hooks`: owns hook dispatch and subscriptions.

Rule: integration routes/webhooks validate and enqueue. Connector behavior,
sync lifecycle, and hook delivery stay inside integration services.
