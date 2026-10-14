# Scoped retirement of PR #3605 billing workflows

This change withdraws the reviewed next-period subscription invoice workflow
from #3602 and the backdated invoice creation/reconstruction workflow from
#3599, including their consolidation corrections in #3605. The source owners,
web projections, routes, forms, action links and permission seeds are removed.
The existing scheduled advance-renewal owner returns to its pre-#3605 contract.
Generic invoice authoring stays available; the previously retired direct admin
generate-from-subscription writer stays retired rather than adding a parallel
service-period writer.

## Historical compatibility

Invoices already created or reconstructed by the withdrawn workflow remain
authoritative records. `financial.invoices` retains sealed-draft fingerprint
validation and edit refusal. New reserved backdated evidence is refused by
generic invoice/line writers; the feature-only creation, sealing and idempotency
participants are removed. `financial.payments` retains interpretation of the
historical line marker/reference, historical entitlement settlement and reversal,
and the absence of current-access or billing-anchor effects.

The two published permission migrations, 689 and 690, remain byte-for-byte
unchanged. No database downgrade, permission/grant deletion, invoice rewrite,
entitlement rebuild or historical event deletion is performed. Existing event
codes and document provenance remain readable. Dormant permission rows do not
provide an executable creation/reconstruction route.

## Preserved scope

PR #3604's payment CSV streaming/session fix is unchanged. The other #3605
features (expense description limit, dashboard, catalog search, support pause
chains, reseller standing and Android release verification) are unchanged.
Later customer authentication, opening-evidence exports and process health
probes are unchanged. No version metadata is manually rolled back.

## Verification and rollout

Retirement tests require the withdrawn web routes and source owners to be
absent, generic invoice authoring to remain, reserved evidence writes to fail,
legacy controlled drafts to retain validation, and historical settlement/refund
to preserve existing service semantics. Ownership documentation is regenerated
from the reduced registry. CI must run the full backend, migrated PostgreSQL,
architecture and mobile gates before merge.

Deploy through the normal immutable-image staging acceptance and production
authorization process. Do not remove the retained migrations or rewrite records
to simulate a database rollback. Scheduled renewal configuration remains owned
by its existing service/settings contract.
