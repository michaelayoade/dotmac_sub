# Prepaid purchase and outage compensation implementation plan

Close the purchase and compensation gaps in PR #3393 while retaining its protections for collected cash, immutable invoices and paid service. Outage events will calculate proposed compensation; Finance must approve each outage grant before service time is posted. Michael selected this policy on 7 October 2026, preserving the approved posting gate in `OUTAGE_SLA_SPINE.md`.

The implementation should proceed in the slices below. Keep both feature flags off until the billing, provider and browser acceptance gates pass. This plan does not enable a feature, change existing customer balances or authorize deployment.

## Local integration baseline

Working repository: `C:\Users\Dotmac\Desktop\dotmac\dotmac_sub`.

- Main updated from `e0bf5683d` to `f850e893a8e3b73d1b8f60cfd868571e7a02ee33`.
- Feature branch: `feat/prepaid-periods-outage-compensation`, tracking the PR branch.
- Reviewed PR head: `171210b8daa5906cbdcd411e5238d53f592d786f`.
- Main is integrated into the feature branch locally. Existing untracked work is preserved.
- Resolve the migration-head test by retaining both `646_prepaid_purchase_safety` and `646_test_connection_finance_review`, together with the composed module heads.
- Retain both branches' concurrency fixture declarations. The merged AST count is 172; update the ratchet and its documentation to that measured count.
- No new merge migration is required merely because these two application heads coexist. Deployment and the test database adapter already use `alembic upgrade heads`. Rehearse that actual graph rather than stamping or choosing one head.

## System responsibilities

| Responsibility | Existing owner and files | Required boundary |
| --- | --- | --- |
| Quotes, purchase state and exact settlement | `financial.prepaid_period_purchases`; `prepaid_period_purchases.py` | Own purchase transitions and admission; never substitute generally spendable credit for a selected receipt. |
| Provider observations and payment intent lifecycle | `topup_intents.py`, `payment_reconciliation.py`, `payment_webhook_commands.py` | Persist verified provider facts and durable outputs; a timeout or missing reference is not proof that no charge occurred. |
| Invoices, allocations, cash and refunds | `billing/invoices.py`, `billing/payments.py`, `billing/account_credit.py` | Retain existing canonical financial commands and their transaction semantics. |
| Funded coverage and renewal boundaries | `prepaid_service_coverage.py`, `prepaid_service_renewals.py` | Derive coverage from entitlements and applied extension grants; a mutable billing anchor alone is not funding evidence. |
| Outage observations and finalization | `network/customer_outage_accrual.py`, `network/service_impact.py`, `topology/outage_lifecycle.py` | Distinguish provisional recovery from sustained final recovery. |
| Pause preservation and existing bulk extensions | `account_lifecycle.py`, `service_entitlements.py`, `service_extensions.py` | Retain each owner's grant authority while sharing evidence of which original clock ranges have already received time credit. |
| Customer and Finance presentation | Customer routes/templates, recovery CLI and Finance read projections | Render owner-provided dates, states, reasons and permitted actions; no payment or eligibility decisions in JavaScript. |

## Financial invariants

1. A confirmed capture is recorded once and remains durable if optional invoice or service settlement is rejected.
2. Reserved purchase receipts cannot fund unrelated debt, ordinary renewals or another purchase. Wrong amounts, additional captures and partial refunds require review.
3. A settled purchase creates exactly one paid invoice and entitlement per frozen monthly period. Dates, VAT provenance and amounts are preserved on replay.
4. A verified unsuccessful checkout with no collected money can be retired safely. Unknown provider outcomes remain unresolved.
5. Each eligible clock second receives outage compensation at most once across pause preservation, legacy outage extensions and new outage grants. Compare original lost-time ranges, not the future dates of the compensating entitlement.
6. Every outage posting requires an active, authorized staff principal, an exact reviewed fingerprint, a reason and an idempotency key. No event handler, scheduled job or feature flag substitutes for that approval.
7. Approval, time-credit evidence, entitlement, billing anchor and eligible cancellation schedule changes commit atomically. The approved calculation and resulting history remain auditable.
8. Refund and reversal consequences retract dependent grants through the existing financial owners. Historical decisions and claims are not deleted, and reversed history must not silently authorize a new award.

## Slice 1 Restore the customer purchase journey

Change `templates/customer/billing/service_periods.html` and the customer payment transport tests.

- Include `X-CSRF-Token` on both preview and intent requests, using the shared live-cookie token helper. Keep portal CSRF enforcement intact.
- Handle HTML errors, invalid JSON, provider failures and transport errors visibly. Reset loading state in a `finally` block for preview as well as payment.
- Render each monthly start and end, the overall coverage boundary, subtotal, VAT, total, currency and quote expiry before the customer confirms payment. Use the application timezone consistently.
- Explain that this payment buys dated periods, differs from flexible account credit, and restricts early cancellation and plan changes under the current policy.
- Bind the displayed quote to the selected subscription and month count. Ignore stale preview responses after input changes or a newer preview; disable confirmation when the displayed quote is no longer current.
- Preserve the same browser idempotency key when a charge outcome is uncertain. A customer retry must verify or resume the original reference, not initialize another charge.

Acceptance: a real browser with normal CSRF middleware can review and start payment; missing/invalid tokens remain rejected; exact dates are visible before payment; a network interruption and double click cannot create two charges. Exercise both hosted checkout and saved-card transport.

## Slice 2 Resolve unsuccessful checkout state safely

Change `prepaid_period_purchases.py`, the intent observation/consequence boundaries, `purchased_service_coverage.py`, the recovery CLI and the corresponding ownership declarations.

Introduce typed terminal-resolution commands and outcomes owned by the purchase service. The intent owner persists provider observations and emits a durable output; a receipted purchase consequence converges the purchase state. Checkout admission and reviewed recovery use the same transition policy so a delayed consumer does not permanently strand the service. Establish the exact dependency declarations with the ownership graph tests before adding a participant call across owners.

Under the established account/subscription/purchase/intent locking order, reload the provider evidence and receipt set. A provider-confirmed unsuccessful intent with no captured, reserved or completed payment can transition its purchase to `failed` or `canceled`, record the evidence and release the purchase restriction. Do not authorize release solely because `expires_at` passed, the customer closed a browser, a provider initialization timed out or verification returned not-found.

Preserve the original purchase and payment reference. A genuine late capture on a retired purchase must still produce a durable reserved receipt and review outcome. It must not replace or automatically settle a newer checkout. Recovery previews must distinguish safe unpaid closure, await-provider, receipt settlement retry and refund/provider review; the CLI must expose the matching canonical command.

Acceptance cases:

- Verified failure and abandonment with no capture release new purchase and lifecycle admission after the canonical transition.
- Unknown and expired-but-unverified outcomes remain blocked.
- A late capture after retirement is held exactly once, including when another checkout exists.
- Capture racing with unpaid closure or another checkout is serialized on migrated PostgreSQL.
- Replayed terminal evidence converges without extra charges, state reversals or duplicate audits.
- A captured, allocated, refunded or disputed receipt cannot be cleared by an unpaid-resolution command.

## Slice 3 Require Finance approval for outage grants

Reconcile `PREPAID_PERIOD_PURCHASE_AND_OUTAGE_COMPENSATION.md` with the approved manual posting gate in `OUTAGE_SLA_SPINE.md`. Replace the event path's automatic grant behavior with proposal persistence and Finance review.

- Keep `network.customer_outage_accrual` as the downtime evidence owner and `financial.outage_compensation` as the remedy owner.
- Add a distinct proposed/awaiting-approval state. Below-threshold, excluded, ambiguous and unsupported outcomes remain explicit recorded decisions.
- Use `consume_outage_compensation_event` to calculate and persist a receipted proposal only. It must create no entitlement, move no billing anchor and rebase no cancellation schedule.
- Introduce a typed approval command that rechecks current evidence, credited ranges, funding, lifecycle conflicts and policy under the account lock. A changed fingerprint returns a fresh preview for approval; it must not silently approve a different award.
- Add a dedicated approval permission, proposed as `billing:outage_compensation:approve`, through an additive RBAC migration and role policy. Verify the active staff principal through the canonical permission gate. Do not treat an actor label or repair scope alone as approval.
- Keep repair authority separate from the authority to approve a new financial remedy. Record the staff principal, reason, policy version and frozen calculation in durable audit evidence.
- Preserve conditional funded-tail cancellation rebasing and explicit-date protections within the approved posting transaction.
- Replayed approvals return the same grant. Concurrent approvals cannot post twice.

Policy: six hours is a configurable eligibility threshold for proposals, not posting authorization. Retain effective-policy provenance and the approved no-contract behavior; do not infer an SLA from a global setting. Policy adoption, review and posting are separate facts.

Acceptance: resolved/discarded events and their replays create only proposals; unauthorized or stale approval cannot post; one approved proposal produces one exact time grant; rollback leaves no partial entitlement, anchor or cancellation change.

## Slice 4 Reconcile time already credited across all mechanisms

Add a shared, typed range-claim boundary below the pause, service-extension and outage consequence owners. Proposed owner: `financial.compensated_service_time`. It owns evidence that original clock ranges have received a time remedy; it does not decide outage policy, create invoices or coordinate upstream lifecycle commands.

Claims should identify the subscription, source kind and identity, original start/end, exact credited ranges, grant reference, policy version and provenance. Persist claims and append-only correction/retraction evidence in additive PostgreSQL tables. Grant writers stage claims in their own atomic transaction through a flush-only participant. The lower boundary accepts validated typed inputs and does not call its producer coordinators; prove the registered dependency graph remains acyclic.

Wire all applicable writers: pause resume, current bulk outage extension apply, approved outage compensation, and reviewed recovery. Calculate uncovered lost-time ranges under the account lock, using interval union and subtraction. Retain the distinction between preserving paused service and approving a new outage remedy; the manual outage gate must not introduce an approval requirement for ordinary customer vacation resume.

For existing pause history, use the canonical episode's `[effective_at, resumed_at)` and exact granted outcome. For legacy extensions, an outage window and a rounded `days` grant do not automatically prove a precise per-subscription mapping. Match the actual affected entry and source facts; use a fingerprinted staff attestation where the credited ranges cannot be established exactly. Unresolved overlapping legacy compensation must block another award and surface a review reason.

Cutover sequence: expand the schema; write/read new claims with grants; inventory and backfill exact historical mappings; route ambiguity to Finance; verify no drift; then enable the new posting path. Backfill must preserve existing invoices, entitlements and billing dates. Do not claw back historic service simply to make a ledger agree with a new formula. Discretionary additional goodwill requires an explicit policy/approval and separate provenance.

Acceptance: an eight-hour pause plus an overlapping outage does not produce sixteen hours; a legacy extension followed by a new renewal does not hide the prior credit; partial overlap awards only uncovered seconds; all writer orders and competing approvals converge; uncertain history requires review. Refund/reversal and reapproval tests must prove that history cannot create an unreviewed repeat award.

## Slice 5 Preserve the outage recovery hold in purchase admission

Add or extend a typed query owned by the network outage/impact domain for purchase admission. Consume it from quote creation, checkout initiation and settlement revalidation. Replace financial code's interpretation of `ended_at` with that owner decision.

An initial recovery observation with a provisional `ended_at` and no finalization still requires a recovery hold. Reopening restores the same interruption, and sustained finalization releases the restriction according to the outage contract. Reuse the decision for customer status presentation so the portal and billing guard cannot disagree. Multiple incidents and ambiguous service impact must fail closed.

Acceptance: quote/initiation remain blocked during confirmed failure and provisional recovery, including clearing-to-reopened transitions; finalized recovery permits checkout; a new outage after checkout causes a durable held receipt if captured funds can no longer settle safely.

## Slice 6 Provide a usable Finance review workflow

Add typed Finance projections for held receipts, unpaid checkout resolution, compensation proposals and uncertain historical credits. Each item must show the account/service, provider reference, collected amount, held amount, status/reason, age and the permitted owner command. Keep future compensation eligibility and SLA evidence within authorized staff surfaces, consistent with the approved outage presentation boundary.

Extend the existing reviewed recovery CLI and add an authorized Finance UI adapter where appropriate. Show held purchase money separately from available account credit in the customer financial projection. No review action may charge a card, release an unconfirmed receipt, guess capture time or manufacture a refund. Refund actions continue through the existing provider-confirmed refund owner.

Use existing managed Finance work-item/observability mechanisms for outstanding counts, age and SLA breaches. Update customer confirmation text to distinguish completed periods from a recorded payment awaiting review. Test permission revocation, stale previews, actor provenance and idempotency.

## Validation and release order

For each slice, add behavior tests through the same typed boundaries used in production; update the executable registry, generated relationship map, architecture guards and operator documents together. Turn the review's probes into regression assertions for the corrected behavior, not tests that accept the known defect. Do not hide legitimate new test fixture sites by weakening the session ratchet.

Use focused unit tests for interval math and transport behavior. PostgreSQL/PostGIS created through the actual Alembic chain owns migration, constraint and concurrency acceptance. Rehearse fresh install and upgrade from current main's real predecessor, preserving both application migration heads and every composed module head. Prove grant/claim/approval rollback and receipt persistence from a fresh connection.

Run the repository's prescribed Ruff, formatting, mypy, import-linter, Bandit, architecture, non-integration and integration suites before publication. Test shared account credit, normal renewal, selected-payment allocation, VAT rounding, delayed capture, additional captures, partial/full refunds, reversal, restoration, pause resume, Test Connections, service extensions, plan changes and cancellation schedules. Run purchase-specific Playwright/mobile checks with the flag enabled; existing default-off E2E success is not purchase acceptance.

Provider acceptance must cover Paystack saved cards and hosted checkout, Flutterwave hosted checkout, uncertain initialization, capture timestamps, delayed webhooks, reconciliation and confirmed refunds. A provider without adequate evidence routes to review rather than inferred settlement.

Release schema first with both flags off. Verify every application and worker uses the accepted compatible digest before enabling a writer. Reconcile existing drift in the pilot cohort: the supplied logs already contain missing funding baselines, unresolved/quarantined coverage and credit-allocation invariant violations. Set Finance ownership and review SLA before accepting customer purchase cash. Enable purchases and compensation proposal collection separately; outage posting remains manual. Follow the existing staging acceptance and immutable-digest production authorization process.

Disabling a feature must stop new admission/proposal collection while preserving safe verification, recorded-cash recovery, approval history and refund handling for existing records. Rollback must never make reserved receipts generally spendable or erase grants and approval evidence.

## Completion criteria

- The browser purchase path works under real CSRF enforcement and presents the exact dates before payment.
- Confirmed unpaid failures can be resolved; uncertain and collected payments remain protected.
- No outage event or scheduled task can post a remedy without Finance approval.
- All applicable time-credit writers account for the same original ranges, with reviewed legacy ambiguity and safe reversals.
- Provisional recovery cannot prematurely release purchase admission.
- Finance can locate and resolve held money and pending proposals through authorized, fingerprinted commands.
- Current-main integration, migration rehearsals, PostgreSQL races, shared billing regressions and provider/browser acceptance are green for the exact candidate.

The feature slices now have local implementations on the existing PR branch. Local financial, browser and ownership checks passed. PostgreSQL rehearsals and full Linux/provider/staging acceptance remain release gates; Windows cannot provide those proofs. The branch has not been pushed, pending Michael's approval.
