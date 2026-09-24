# Self-Care material partial-issue rollout

Change pair: [Self-Care #3243](https://github.com/michaelayoade/dotmac_sub/pull/3243)
and [ERP #645](https://github.com/michaelayoade/dotmac_erp/pull/645).

This is an acceptance/runbook checklist, not authorization to merge, deploy,
change production data, or replay an existing stock transaction.
The domain contract remains `docs/designs/MATERIALS_VENDOR_ERP_CHAIN.md` and
the material owner declared in the executable source-of-truth registry.

## Deployment order

1. Pass the repository-required checks and review for each exact source revision.
2. Deploy the updated Self-Care receiver first, using the normal immutable-digest
   staging/acceptance/release process. The old receiver rejects additional fields
   in ERP webhook payloads. Keep the updated receiver when rolling ERP back.
3. Before deploying ERP, confirm its existing `20260922_mr_partial_issue`
   migration is applied. This paired change introduces no new database migration.
4. Deploy the ERP producer through its normal staging and authorized production
   process. Do not allow partial issuing before both sides are compatible.
5. Release the field-mobile build to display issued and outstanding quantities;
   mobile release does not grant authority to issue warehouse stock.

Do not roll Self-Care back to the old strict receiver while the new ERP producer
or queued version-1 webhook deliveries remain active. Reconcile delivery backlog
and select a compatible rollback pair first.

## Staging acceptance

Use isolated staging records and a named reviewer. Record request IDs, source
revision/digest, before/after ledger quantities and delivery receipts. A mocked
unit test is not database or browser acceptance.

- Open an existing Self-Care-origin ERP ISSUE request older than 30 days, in
  SUBMITTED or PENDING_STOCK state, with verified unissued stock history. Confirm
  that **Issue now** fields are visible and an approver can use **Issue remaining
  line items**. No creation-date filter or request recreation is required.
- Issue less than the original quantity and mark a genuine zero-stock line out
  of stock. Confirm ERP posts only the selected quantities, preserves requested
  quantities, and leaves the balance PARTIALLY_ISSUED.
- Confirm both webhook delivery and later status polling project the same
  cumulative issued/outstanding quantities in Self-Care. Its operational state
  remains pending_stock with a Partially issued fulfillment label; no fulfilled
  event or full work-order allocation occurs yet.
- Repeat the delivery and submit an old/stale form. Neither may duplicate stock
  posting, reduce cumulative quantities, or falsely fulfill the request.
- Issue the remainder. Confirm only the remaining quantity posts, the request
  becomes issued and the terminal material consequence is emitted once.
- Validate saved serialized units across two issues. No serial from the first
  issue may be reused; missing or invalid selections must remain blocked.
- Confirm an old request retaining a manual channel label accepts observations
  only when its existing dotmac_erp reference matches the verified ERP request
  ID or number. Unlinked manual requests remain blocked and are not enrolled.
- Verify non-approvers cannot issue stock and unrelated-organization request IDs
  are rejected. Check web layout and decimal quantities in field-mobile views.

## Historical records and exceptions

The original requested quantities and source request identity are immutable.
Do not copy/recreate old requests to obtain the controls. Terminal requests are
not reopened by this change. ERP compares existing issued counters with recorded
stock issues; a mismatch or an unmatched historical line stops further issuance
and requires separately authorized reconciliation. Never treat that stop as a
reason to zero a counter or replay all requested quantities.

Legacy observations without a complete version-1 quantity snapshot may lack
known line quantities. Display unknown explicitly rather than fabricating zero.
After partial issuance, cancellation is not an inventory reversal; the remaining
balance stays open for the supported issue workflow.

## Validation evidence

ERP focused suite: run 35868673915, 172 tests passed, covering the material issue,
web-service, approval-integrity, operations-adapter, Sub sync and OpenAPI suites.
Self-Care initial focused run 35866725085: 76 server checks and 18 field-mobile
checks passed. Follow-up legacy-bound observation run 35869176010 also passed.
These development-run results do not replace the final source commit's full
required CI, real PostgreSQL validation or end-to-end staging acceptance.
