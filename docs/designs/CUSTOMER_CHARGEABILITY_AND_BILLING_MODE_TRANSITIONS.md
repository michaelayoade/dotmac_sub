# Customer chargeability and billing-mode transitions

Status: canonical

Owners:

- `financial.customer_chargeability` owns the operational classification.
- `financial.billing_mode_transition` owns reviewed prepaid/postpaid changes.
- `customer.billing_approval` owns restoration of a billing-owned disable.
- `ui.customer_list_projection` owns the customer-list filter projection.

## Chargeability rules

Classification is based on every current service in `pending`, `active`,
`blocked`, `suspended`, `stopped`, or `disabled` state. Account lifecycle state
does not decide whether service is chargeable: an active or delinquent account
can be non-billable, and a disabled account can still require pricing review.

For each service, the active offer-version recurring price takes precedence;
the active offer recurring price is the fallback. The outcomes are:

- **Confirmed non-billable:** every current service has an effective billing
  treatment or exactly one active recurring catalog price of zero, with no
  contradictory positive subscription price.
- **Review required:** any current service has no active recurring catalog
  price, multiple active prices in the selected price scope, or a catalog and
  subscription price contradiction. Review is deliberately not treated as
  free service.
- **Billable:** at least one current service has positive, coherent recurring
  price evidence and no service requires pricing review.
- **No current service:** there is no service in the current-service scope.

The **Non-billable / review** customer filter is an operational work queue. It
contains confirmed non-billable accounts and review-required accounts, with a
separate badge and reason so staff can tell them apart. Prepaid and postpaid
filters exclude both groups and remain based on the canonical billing profile.
This includes delinquent confirmed-free accounts and billing-owned disabled
accounts; customer status is not a hidden filter.

Confirmed-free accounts may use Self-Care when lifecycle policy otherwise
permits it. Chargeability does not change existing Self-Care payment, invoice
payment, or account-credit deposit controls. It is used for filter visibility,
access repair, and billing-mode conversion eligibility only.

The billing-approval reconciler may restore an account only when all current
services are conclusively non-billable and the disable was created by the
billing-approval owner. It never restores missing-price cases, security/admin
disables, fraud/FUP locks, or canceled accounts.

## Bidirectional billing-mode transition

Staff with `billing:mode:write` can preview and confirm an account-wide change
from prepaid to postpaid or postpaid to prepaid on the billing account detail
page. The permission is registered without a default role grant.

The preview fails closed unless:

1. billing approval and account lifecycle are eligible;
2. the account and all current services share one canonical source mode;
3. pricing is positive, coherent, and expressed in one currency;
4. every current service has a billing anchor;
5. every offer supports the target mode;
6. there is no open billing treatment, pending plan change, active enforcement
   lock, or other listed blocker; and
7. a postpaid-to-prepaid change due at the boundary has sufficient native
   credit. Any draft invoice must first be issued, voided, or corrected.

`OfferBillingModeAvailability` active rows are the supported variants. When no
active rows exist, `CatalogOffer.billing_mode` is the legacy/default supported
mode. The offer field is therefore a default, not a reason to reject a target
variant explicitly allowed by availability.

Confirmation is fingerprint-bound and idempotent. It locks the account,
current subscriptions, offer availability, treatments, pending changes,
enforcement locks, and invoices; re-evaluates the preview; then updates the
account and current subscriptions in one owner transaction. Prepaid enforcement
timers are cleared only when leaving prepaid. The command does not rewrite
billing anchors, granted/paid service periods, account credit, finalized
invoices, allocations, or unrelated access locks. This prevents period overlap,
credit loss, and duplicate billing while retaining existing receivables.

## Remaining restrictions

- Missing, duplicate, zero/positive contradictory, or multi-currency pricing
  requires manual repair.
- Non-billable/treatment-protected service requires a separate commercial
  decision before it can become ordinarily billable.
- Pending plan changes, open treatments, active locks, and draft invoices must
  be resolved by their named owners.
- Accounts without a current service cannot be converted.
- Conversion is account-wide; a mixed-mode subset is not supported.
- Canceled and administratively disabled accounts are not conversion targets.
- Existing finalized debt survives either direction and remains payable.

## Verification matrix

Focused tests cover active, delinquent, blocked, and billing-owned disabled
accounts; explicit zero, positive, missing, duplicate, and contradictory price
evidence; both conversion directions; insufficient prepaid funding; unsupported
offer variants; stale previews; idempotent replay; permission-gated UI; and
preservation of finalized financial evidence.
