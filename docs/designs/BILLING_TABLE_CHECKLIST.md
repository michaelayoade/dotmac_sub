# Billing table implementation checklist

Branch: `billing-table-cleanup`; implementation base `7c52f50e3`.

Worktree: `C:/Users/Dotmac/Documents/Codex/2026-11-09/ar/work/billing-table-cleanup`.

## Compatibility and scope

- [x] Separate worktree; existing settings branch and edits untouched.
- [x] Read Sub AGENTS, coding/UI standards, SOT registry and owning financial contracts.
- [x] Verify Starter at `702e8fe84a033857b23137ea8586b98108d77539`.
- [x] Keep exact dependency pins, shared assets/loader/theme hooks and commercial authority unchanged.
- [x] No Starter table component exists to duplicate; Sub owns these product projections.
- [x] New credit detail is a typed read-only adapter over the incumbent credit owner.
- [x] Entry navigation replaces redundant View/Open/Review actions.
- [x] Separate customer/account navigation retains its distinct destination.
- [x] Financial values retain explicit currency and precision; no new template monetary arithmetic.
- [x] Removed list facts remain on details or expandable run evidence.
- [x] Selection, filtering, sort URLs, pagination, totals, exports and confirmation forms preserved.

## Table inventory

Checked means source reviewed and classified, not that a live server was exercised.

| Reviewed | Billing template | Disposition |
| --- | --- | --- |
| [x] | `_account_statement.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `_invoice_batch_history_table.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `_invoices_table.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `_subledger_opening_panel.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `account_detail.html` | Embedded recent-invoice table compacted; duplicate View-only column removed |
| [x] | `accounts.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `ar_aging.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `cache.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `collection_accounts.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `consolidated/detail.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `consolidated/index.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `consolidated/payment_confirm.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `credit_detail.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `credits.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `documents.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `dunning.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `dunning_bulk_confirm.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `dunning_detail.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `health.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `index.html` | Overview invoice identifiers now use permission-gated native links instead of click-only rows |
| [x] | `invoice_batch.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `invoice_bulk_review.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `invoice_detail.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `ledger.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `payment_arrangement_detail.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `payment_arrangements.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `payment_channel_accounts.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `payment_channels.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `payment_detail.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `payment_import.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `payment_proofs.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `payment_reconciliation.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `payment_refund_confirm.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `payment_reversal_confirm.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `payments.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `prepaid_billing_calendar_reconciliation.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `service_extension_detail.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `service_extensions.html` | Compact list reviewed; product-scoped layout applied |
| [x] | `tax_accounting.html` | Investigation, overview, import, reconciliation or confirmation table; retain evidence and command context |
| [x] | `tax_rates.html` | Compact list reviewed; product-scoped layout applied |

## List projection contract

Audience: billing/support staff identifying records, comparing state and amounts,
and opening a record to investigate or act. Record identity leads; customer/account
links retain their separate meaning. Identity opens details rather than duplicating
View/Open controls. Existing read/context owners remain authoritative.

| Surface | Default business columns | Read/context owner |
| --- | --- | --- |
| Invoices | Invoice, customer, total, balance due, status, due | `web_billing_invoices` |
| Payments | Transaction, account, amount, method, status, date | `web_billing_payments` |
| Ledger | Entry, account, type, source, amount, effective date | `web_billing_ledger` |
| Accounts | Subscriber number/name, status, balance, created | `web_billing_accounts` |
| Credits | Credit number, account, status, amount, applied, created | `web_billing_credits` over `financial.credit_notes` |
| Proofs | Subscriber/record identity, claimed amount, reference, transfer date, status, submitted | incumbent payment-proof owner |
| Arrangements | Subscriber/arrangement identity, amount, installments, frequency, status, next due | `web_billing_arrangements` |
| Dunning | Account/case identity, status, step, started | `web_billing_dunning` |
| Extensions | Reason, outage window, scope, days, affected, status | `web_billing_service_extensions` |
| Consolidated | Reseller, account name, status, currency-prefixed balance | incumbent consolidated account owner |
| Batch history | Run/date, cycle, billed, invoices, duration, status | `web_billing_invoice_batch` |

Actions use one native disclosure where multiple operations exist. Batch run IDs
expand full investigation evidence because there is no dedicated run-detail route.
No mutable command, eligibility policy or commercial package owner changed.

Filters/default sort/page size, totals and export scope retain their current owners.
Secondary dates, narration and evidence remain on the relevant details. Empty and
failed data use existing states; no new fallback value or freshness policy is added.
At widths up to 1100px, labeled record rows replace compressed desktop columns.
Audit/confirmation tables retain their evidence because those are investigation
surfaces rather than everyday record lists. No audit-writing behavior changes.

## Verification

- [x] All 76 billing templates parse; `git diff --check` passes.
- [x] 54 focused template/navigation/ownership/financial-boundary checks pass on Python 3.12 with `--noconftest`.
- [ ] Application-backed credit detail route/unit tests: blocked by missing installed `dotmac_kernel`.
- [x] Five static dotmac-ui adoption checks pass (pins, no vendoring, asset mount/order). Billing target, thin-client, UI policy and static navigation guards pass.
- [ ] Installed-package UI checks and full route permission guard: blocked by missing `dotmac_ui` / `dotmac_kernel`.
- [x] Locked frontend build succeeds. Existing Tailwind scan warning for `[-:\s]` recorded; it does not fail the build.
- [x] Eleven actual table templates rendered with long names/unbroken references, USD/NGN and 123,456,789.99 amounts; no live/customer data used.
- [x] 55 browser geometry checks across 1440, 1280, 1024, 768 and 390px: no page/table overflow in fixtures. Invoice light/dark/mobile captures inspected; readable identity and full monetary values confirmed.
- [x] Rendered tests cover exact ledger destination/amount/escaping and batch retry evidence/CSRF. Keyboard Enter opens the native action menu. Document-facet tests cover destination permissions.
- [x] Ledger no-results state renders the actual local empty-state macro with six columns.
- [ ] Full assembled-page rendering and authenticated server navigation: require installed shared dependencies.
- [x] Limitations recorded below.

Full repository/ PostgreSQL acceptance remains a publication gate; this local task does not publish.

## Billing inventory baseline

The explicitly requested implementation includes the previously reviewed missing
credit detail page. Its typed read-only adapter plus document link mappings add
94 measured lines to the path-based billing inventory: 98,226 -> 98,320.
The baseline was regenerated using the existing inventory renderer for that
authorized scope. No new monetary rules, writes, package activation, dependencies
or hidden inventory paths were introduced. `ui.credit_detail_projection` now has
a complete registry contract, generated relationship-map row and negative-control
architecture tests. Financial authority remains `financial.credit_notes`.

## Follow-up: all four review findings corrected

- [x] Invoice contract: retain six business columns; Created (UTC), Issued and
  the existing sort fields are available in a persistent Sort control above the
  table. Owner-built URLs preserve search, customer/status/date filters and page
  size while resetting to page one. Updated frontend specification, registry
  notes and the existing invoice contract test together.
- [x] Responsive controls: invoice and dunning Select page controls stay above
  their tables. Narrow headers are removed from display instead of leaving
  clipped keyboard targets. Invoice partial-selection state uses the native
  indeterminate property and stays correct after HTMX replacement.
- [x] Cached CSS: both stylesheet URLs now use `v=20261109-billing2`; the existing
  action-form asset contract is updated. A browser with the previous immutable
  cache keys requests both fresh stylesheet URLs.
- [x] Ledger empty state: span six columns, matching the header and footer.
  Regression test renders the actual macro with no results and active filters.

### Follow-up verification results

- [x] 54 focused tests pass on Python 3.12 with `--noconftest`; five additional
  static shared-UI adoption checks pass (59 focused pytest checks total).
- [x] Existing invoice full/HTMX contract and shared action-form asset assertion
  bodies both pass when executed independently of unavailable app imports.
  This does not claim that their entire application-backed test modules ran.
- [x] Browser checks using the project's actual Alpine and HTMX scripts pass at
  1280, 1024, 768 and 390px: visible Sort/Select page, keyboard page selection,
  mixed selection, selection reset/reinitialization after an HTMX swap, query
  preservation through sorting, and dunning page selection.
- [x] Warm-cache browser check requests both newly versioned stylesheets.
- [x] 55 geometry checks across eleven table fixtures and five widths pass with
  no page/table overflow. Updated desktop, mobile and dark previews inspected.
- [x] All 76 billing templates parse; application Python compilation passes.
- [x] Full repository Ruff lint and format checks pass (6,358 formatted files).
  Whitespace check passes and the locked frontend build succeeds. The existing
  Tailwind `[-:\s]` scan warning remains non-failing.
- [x] Layout detector returns no findings. The billing inventory remains 98,320;
  no additional financial code, package versions or dependency lockfiles changed.

### Still blocked

- [ ] Full normal pytest collection for credit details, invoice list contracts,
  action-form ownership and assembled permission routes: rerun stops in the
  repository conftest with `ModuleNotFoundError: dotmac_kernel`.
- [ ] Installed `dotmac-ui` integration and authenticated application browser
  checks: private packages remain unavailable. The previous exact-lock install
  attempt received authorization errors from `registry.dotmac.io`; configured
  registry access is required before installation can complete.
- [ ] Complete SOT relationship-map generation: rerun reaches the unrelated
  provisioning import graph and stops on the same missing kernel package.
  The modified list-projection notes do not alter generated manifest columns.
- [ ] Full PostgreSQL/integration acceptance: not run; requires the complete
  package environment and a disposable migrated test database.

At this verification checkpoint, no merge, commit or GitHub push had been
performed. Publication was subsequently authorized; see the publication section.

## Follow-up: four route findings corrected

- [x] Bulk dunning confirmation: constrain the individual case route to UUIDs,
  so pause/resume/close bulk confirmations reach their own handler. Regression
  coverage checks declaration order and demonstrates the previous collision.
- [x] Consolidated account members: link to the registered Customer 360 person
  route, gated by `customer:read`; unauthorized viewers see plain text.
- [x] Billing overview: remove the placeholder Export link. There is no overview
  export handler; existing document-specific exports remain available.
- [x] Invoice send/void returns: accept only the invoice list or the acted-on
  invoice detail. Preserve valid queries/fragments, encode feedback correctly,
  and fall back safely for external, malformed or nonexistent destinations.
- [x] Both existing send-and-return form producers use these allowed targets.
- [x] Add 37 locally runnable regression cases, including a negative control
  reproducing the old dunning collision, rendered link/permission checks,
  redirect validation and handler call-site checks. Add three actual-router
  dispatch cases to the application-backed dunning test module.
- [x] Repeat the complete billing destination audit: 76 templates, 1,775 admin
  registrations, 346 concrete URL variants; zero missing destinations, zero
  shadowed billing routes, zero extraction errors or unresolved includes.
- [x] All 76 billing templates parse, application compilation and repository
  lint/format checks pass. No dependency versions or lockfiles changed.
- [x] Updated focused suite: 91 tests pass, plus five shared-UI adoption checks
  (96 pytest checks total). Two existing source-only contract assertions also
  pass independently of their blocked application-backed module imports.
- [ ] Full normal application collection, including the three actual-router
  tests, still stops in conftest on missing `dotmac_kernel`. Authenticated HTTP,
  installed shared-package integration and PostgreSQL acceptance remain blocked
  by the private-package/registry access requirements recorded above. Source
  route matching does not establish live page rendering or permissions.

## Practical limits

These are rendered table fixtures, not a running authenticated application.
Shared-package integration, application-backed credit tests, complete SOT-document
regeneration and full route guards need the installed private packages. No
dependency stubs were used. PostgreSQL/migration/full CI acceptance was not run.
All application-facing checks must pass in the complete development/CI environment
before merge.

## Publication preparation

- [x] User authorized committing/pushing this branch and opening its PR.
- [x] Rename branch to `billing-table-cleanup`; retain the isolated worktree.
- [x] Re-read `AGENTS.md`, the PR template and the version-impact workflow.
- [x] Target `main` and select exactly one `version:patch` label for these fixes.
  Leave `VERSION` and dependency pins/lockfiles unchanged; the separate rolling
  version automation owns release metadata after merge.
- [x] Fetch `origin/main`: `793d9194f832d070341cfc4ac5fc9bbfd2f81553`.
- [x] Publication checks: import-linter keeps all three contracts; full Bandit
  scan passes (existing suppression/comment warnings remain non-failing).
- [ ] Full mypy acceptance: the local Windows run reports unsupported Unix
  platform attributes in `app/syslog/health.py`; complete Linux CI is required.
- [ ] Full CI and authenticated application/database acceptance must pass before
  merge. Local source/fixture checks do not replace these gates.
- [ ] No merge or deployment authorized by this publication request.

## PR #3864 CI follow-up

- [x] Inspect the first complete CI run and all PR conversation/review/inline
  comments. No review comments were present at the time of inspection.
- [x] Update the linked billing/invoice/credit workflow guidance for identifier
  navigation, compact menus, responsive records, sort/selection and credit detail.
- [x] Include the new credit-projection domain in the documented and tested
  registry order; preserve the existing generated manifest and its contract.
- [x] Remove the resolved `web_billing_credits.py` adapter baseline entry (2 to
  0) as required by the shrink-only architecture guard; do not relax the guard.
- [x] Replace obsolete sticky-column assertions with responsive table/field-label
  checks while retaining the actual payment-proof detail-link assertion.
- [x] Patch the actual credit-note module in the missing-document test, rather
  than the package's exported service instance; retain the owner 404 assertion.
- [x] Local follow-up: 40 source-level tests pass; full repository lint/format
  and whitespace checks pass. Application-backed test collection still requires
  the private packages and is delegated to the complete GitHub CI environment.
- [x] Initial CI already passed Linux type checking, import boundaries, security,
  engineering standards, Docker build/health, E2E, migrations and all four
  PostgreSQL test shards. The remaining failures above require a fresh CI run.
- [ ] Confirm all required checks pass on the follow-up commit before merge.

## November 10: refresh after other PRs merged

- [x] Preserve remote billing update `d2906a082` and integrate `origin/main`
  at `e4d7c18c3` into the feature branch without merging PR #3864.
- [x] Inspect run `42657004022`: aggregate test gates failed because unit,
  architecture and PostgreSQL jobs were `abandoned`, not because a failing
  test assertion was reported. All jobs that completed before those gates
  passed. No inline review comments were present.
- [x] Regenerate the settings-hub register after integration. The billing-credit
  setting reference moves from line 131 to 223; no setting identity, value,
  destination or authority changes.
- [x] Lint/format and composition checks pass; billing route audit checks 346
  destinations against 1,779 admin registrations with no missing or shadowed
  billing routes.
- [x] Re-run 63 regression checks after refreshing the register: all pass,
  including the complete source census, credit projection, billing navigation,
  responsive controls, adapter baseline and composition checks. Application
  compilation also passes. Local application-backed collection still requires
  the private packages; the full GitHub run remains the acceptance gate.
- [ ] Fresh full CI must complete on the pushed, updated branch. Abandoned jobs
  do not establish test acceptance and must not be treated as passing.
