# Retired backdated subscription-service invoicing

The creation and reconstruction workflow introduced in PR #3605 is removed.
Its routes, customer actions, preview/create owner, form, and permission seeds
are no longer present. Existing invoice data retains its stored meaning.

The invoice owner still reads and validates stored `controlled_draft` seals,
refuses ordinary edits of sealed drafts, and permits issuance only after the
same fingerprint validation. New backdated evidence cannot be authored through
generic invoice/line writers. The payment owner retains historical-only
settlement and refund/reversal semantics for `backdated_service_invoice` and
`backdated-service-invoicing:` evidence, without moving current service anchors
or granting present access.

Previously applied migrations 689/690, permission/grant rows, stored event
vocabulary, invoice provenance, allocations and entitlements remain intact.
See `docs/designs/REVERT_3605_BILLING.md` for retirement boundaries and rollout.
