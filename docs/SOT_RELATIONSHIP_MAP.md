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
2. `financial.payments`, `financial.invoices`, and `financial.credit_notes` own
   their document lifecycles and the ledger postings currently implemented for
   those transitions. Native invoice issuance is an authoritative billing fact
   but does not yet write a customer `ledger_entries` debit; invoice write-off,
   void, and credit-note paths own their adjustment/reversal postings.
   A credit note becomes spendable only when its owner issues it after final
   totals are known. Issuance posts one structurally linked, unallocated ledger
   credit; application atomically consumes that credit with an account debit and
   settles the invoice with a paired invoice credit, bounded by both the note's
   unapplied amount and the account's locked spendable balance; void reverses the
   issuance.
   Issued notes and their lines are immutable. Customer financial position
   counts the source CreditNote document and excludes these linked operational
   postings, so allocation changes location, not net customer value. Migration
   294 deliberately does not infer or backfill historical ledger links.
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
6. `financial.vas_wallet` owns its separate append-only wallet, spendable
   balance, and atomic bridge into `financial.payments` for bill settlement.
7. Customer financial position owns read-side financial summaries, including
   the bounded bulk projection used by cohort monitoring. Bulk callers do not
   loop the single-customer ledger reader.
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
   affordability decision, and idempotent financial adjustment. It locks the
   account and recomputes at write time; portal, admin, API, and change-request
   application paths do not post their own plan-change debit.
11. Dunning owns postpaid enforcement; prepaid enforcement owns prepaid access.
   Both converge on the account lifecycle writer, which re-checks billing
   profile validity, payment-arrangement/proof/extension shields, and billing
   enforcement health immediately before a financial suspension.
12. Scheduled billing, collections, and payment-reconciliation services own DB
   sessions, transaction outcomes, and operational logging for Celery runners.
13. `financial.payment_webhooks` owns signature-verified provider-payload
   projection and inbound dead-letter lifecycle. Replay rebuilds the same
   settlement command as live delivery; `financial.payment_provider_events`
   owns idempotent event processing, delegates the monetary write to the
   payment owner, and must resume an incomplete event rather than treating
   receipt identity as proof that money was posted.
14. `financial.vas_operations` owns admin VAS mutation transactions and manual
   purchase resolution. `financial.vas_refunds` exclusively owns
   refund-to-source eligibility, the durable request lifecycle, and wallet
   reservation/reversal projection. It commits the request and wallet debit
   before contacting the gateway; provider responses are observations that an
   idempotent reconciler can replay. Gateway adapters only submit or observe a
   refund against the original funding transaction and never decide eligibility
   or lifecycle state.

Rule: no module should infer access from draft invoices, ad hoc balances, or
legacy import fields when a billing/access resolver exists. Celery tasks only
apply scheduling, routing, idempotency, and feature-gate concerns before calling
the owning financial service. Admin VAS routes do not mutate catalog or
transaction rows, control wallet transactions, or decide whether funds may
leave through a gateway.

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
4. `support.ticket_bulk_commands` owns exact selected membership, normalized
   shared changes, side-effect-free eligibility preview, confirmation drift
   detection, and structured outcomes for admin ticket bulk update. Eligible
   execution delegates through `app.services.support.Tickets.update`; it does
   not maintain a second status, priority, assignment, SLA, automation,
   work-order, notification, event, audit, or workqueue path.

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
12. `ui.support_ticket_list_projection` extends the existing
    `app.services.web_support_tickets` web owner and delegates its filtered
    domain query to `app.services.support.Tickets`. It owns the declared admin
    search/filter/sort capabilities, exact count, page clamping, status-summary
    links, and uncapped CSV scope. Full-page and HTMX reads render the same
    `_list.html` and `_table.html` projections.
13. Support-ticket list migration record:
    - Old owners: the admin route and Jinja fragments independently interpreted
      sort/page inputs, inferred a next page from one extra row, hand-built URLs,
      and applied a silent 10,000-row export cap. Advanced filters submitted by
      the page were not accepted by the export route.
    - New owner: `app.services.web_support_tickets`, using `ui.list_contracts`
      and the canonical filtered query in `app.services.support.Tickets`.
    - Verification phase: contract, query, route/template architecture,
      filter-before-pagination, stable-order, exact-count, clamped-page,
      canonical-URL, accessibility, and complete-export tests protect the
      boundary. A runtime dual-read was not retained because both paths used the
      same database query and the old implementation had no independent owner.
    - Cutover gate: support service, web projection, route/template, SOT
      registry, and focused list tests must remain green.
    - Fallback retirement: the route no longer owns pagination semantics; the
      templates no longer assemble sort/filter/page URLs; the one-extra-row page
      estimate and silent export cap are removed. Legacy `order_by`/`order_dir`
      inputs remain only as canonicalizing compatibility aliases.

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
8. `ui.support_ticket_bulk_action_projection` projects authorized support-ticket
   update controls and page-row eligibility. Selection is page-only and never
   implies all filtered tickets.
9. `support.ticket_bulk_commands` requires an in-modal, side-effect-free preview
   of exact selected membership, the shared proposed change set, eligible rows,
   and skipped reasons. Confirmation binds matched count, proposed changes, and
   every row eligibility outcome; drift returns HTTP 409.

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

Support-ticket bulk migration record:

- Old owners: the public bulk API delegated to `Tickets.bulk_update`, but that
  method directly changed status, priority, and assignment while bypassing the
  canonical single-ticket lifecycle consequences. The admin list had no
  selection, authorization projection, impact preview, or drift contract.
- New owners: `support.ticket_bulk_commands` owns selected membership, change
  normalization, preview, confirmation, and outcomes;
  `ui.support_ticket_bulk_action_projection` owns authorized page-selection
  presentation; `support.ticket_lifecycle` remains the mutation/consequence
  owner through `Tickets.update`.
- Verification: service, projection, route-permission, architecture, template,
  no-selection, preview/no-side-effect, proposal drift, eligibility drift,
  lifecycle-audit, and structured-outcome tests protect the boundary.
- Cutover gate: unauthorized users receive no selection controls; empty or
  filtered scope fails closed; no update executes without the exact server
  preview; changed membership, eligibility, or proposal returns HTTP 409.
- Fallback retirement: `Tickets.bulk_update` no longer writes lifecycle fields
  directly and the admin page exposes no unpreviewed or all-filtered ticket
  mutation path.

Rule: bulk controls appear only when a selection exists and a canonical command
supports it. Filtered, customer-visible, financial, destructive, or fleet-wide
operations require explicit impact preview and confirmation. WCAG 2.2 AA labels,
indeterminate state, selected-count announcements, and focus/keyboard behavior
are part of the contract; hidden controls are never authorization enforcement.
## UI Action Forms

## UI Display Formatting

1. `ui.display_formatting` / `app.services.display_format` owns the code-native
   display rules for normalized currency codes, currency symbols, single-value
   money, ordered multi-currency summaries, configured display timezone, and
   timestamp strings. Missing scalar facts use one explicit em-dash marker;
   only a caller-declared aggregate absence becomes zero.
2. Financial, network, usage, and other domain owners retain the typed facts:
   amount, ISO currency, unit, timestamp, and whether a value is zero, unknown,
   stale, or unavailable. Formatting never changes or derives those facts.
3. Single-currency values may use the declared symbol form. Mixed-currency
   totals use explicit ISO-style codes, group normalized codes independently,
   sort them deterministically, and never add unlike currencies together.
4. `control.settings_spec` owns the configured billing default currency and
   scheduler timezone. `ui.display_formatting` resolves those settings for
   display; templates and mobile clients do not independently default to NGN or
   Africa/Lagos when a projection declares another value.
5. `mobile/lib/src/core/formatters.dart` is the existing platform renderer for
   mobile layout and locale mechanics. It is not a second owner of currency,
   timezone, missing-value, or unit facts.
6. First adoption: billing overview/invoice/aging, payments/import history,
   ledger, and reconciliation delegate their multi-currency summary strings to
   `app.services.display_format`. Their former private currency-code, amount,
   and grouped-total formatter copies are retired.

Migration record:

- Old owners: four billing web projection modules each carried equivalent
  `_currency_code`, `_format_currency_amount`, and `_format_currency_groups`
  implementations. Their behavior could drift independently from the existing
  global money filter and configured display settings.
- New owner: `app.services.display_format`; billing services still assemble
  domain-owned totals and request a display projection from that owner.
- Missing-state correction: the prior scalar `format_money` helper rendered
  missing or invalid values as currency zero. It now renders the shared em-dash
  marker; aggregate callers request zero explicitly through the grouped/amount
  functions.
- Verification phase: formatter behavior tests cover normalization, explicit
  ISO labels, deterministic grouping, duplicate normalized codes, empty totals,
  and setting resolution. Existing billing overview, payment import, ledger,
  and reconciliation tests prove byte-compatible output.
- Cutover gate: the four pilot modules import `display_format` and contain no
  private currency normalization or formatter definitions.
- Fallback retirement: the private formatter copies are removed. Other screens
  migrate incrementally; no second shared formatter or template-local default
  may be introduced.

Rule: formatting projects authoritative facts; it does not repair missing data,
convert currency, select business precision, or collapse unknown into zero.
Callers must make aggregate-zero behavior explicit and keep unlike currencies
separate.

1. `ui.action_form_contracts` owns the code-native interaction projection for
   an action: visibility, disabled reason, semantic tone, impact preview,
   confirmation requirement, declared fields/options, submitted values, and
   structured field/general errors.
2. Domain command and transition services still own authorization, business
   eligibility, validation, locking, mutation, audit, and consequences. A form
   contract is a read projection, not an execution bypass. The command owner
   rechecks every decision when the form is submitted.
3. Unauthorized actions are omitted. State-ineligible actions are shown
   disabled only when the owner-provided reason helps the operator understand
   what must change.
4. `ui.payment_proof_review_projection` is the first adopted resource.
   `financial.payment_proofs` owns submitted/verified/rejected eligibility,
   duplicate-reference policy, payment creation/allocation, WHT consequences,
   and typed command errors. The web projection adapts those facts into the
   shared verify/reject forms.
5. Failed payment-proof submissions render the same detail page with declared
   values preserved and typed field or general errors. Successful mutations
   keep POST-Redirect-GET. Templates do not map domain error strings back to
   fields or infer review availability from raw status.
6. High-impact actions expose their consequence before submit and require an
   explicit confirmation supplied by the action contract. Web rendering uses
   branding-owned semantic roles and WCAG 2.2 AA labels, descriptions, focus,
   invalid-state, and live-error semantics.

Migration record:

- Old owner: payment-proof detail Jinja selected review actions from raw status,
  declared fields/defaults, hardcoded impact/confirmation copy, and redirected
  failed submissions through one unstructured query-string error.
- New owners: `app.services.payment_proofs` supplies typed eligibility and
  command errors; `app.services.web_billing_payment_proofs` builds the resource
  projection through `app.services.action_forms`; the shared Jinja macro only
  renders that contract.
- Verification phase: contract, domain eligibility, route/RBAC, submitted-value,
  structured-error, template architecture, accessibility, payment, duplicate,
  and WHT tests.
- Cutover gate: the payment-proof template contains no raw verify/reject form,
  status-based action branch, local confirmation copy, or domain-error mapping.
- Fallback retirement: the successful redirect remains; the old failed-action
  redirect is removed. Other forms migrate incrementally only after their
  command owner exposes equivalent eligibility and error contracts.

Rule: UI action projections explain and collect a command; they do not decide or
execute it. Routes pass submissions to the named owner, templates render only
declared controls, and the owner rechecks permission and eligibility under the
same lock or transaction that protects the mutation.
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
11. `network.huawei_cli_response`: owns Huawei CLI response classification,
   stable error codes, expected-absence predicates, unsupported-command
   detection, and idempotent response semantics. Huawei SSH sessions, protocol
   adapters, readback verification, and web workflows consume these projections
   and do not maintain firmware response string tables. A response classified
   as accepted is transport evidence, not proof of convergence; write workflows
   still require the control-plane intent readback contract.
12. `network.routeros_sot`: owns typed MikroTik desired state, the managed
   resource/field registry, Dotmac ownership markers, verified reconciliation,
   and periodic drift evidence. Router routes and tasks only orchestrate it,
   and it projects through `network.control_plane_intent`.
13. `network.operation_ledger`: owns the tracked device operation lifecycle and
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
options, prepaid monthly invoicing, RADIUS/session enforcement, VAS wallet,
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
3. `access.radius_state`: maps desired access to RADIUS groups/profiles.
4. `access.radius_reject`: owns reject IP lifecycle.
5. `access.session_enforcement`: applies CoA/disconnect outcomes.

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
