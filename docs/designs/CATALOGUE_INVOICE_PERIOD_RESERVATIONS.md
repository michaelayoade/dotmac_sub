# Catalogue invoice period reservations

The `financial.invoices` owner treats an active catalogue-authored line on an
active, non-void invoice as the reservation. Draft save, replacement, deletion
and issuance remain in existing owners. No entitlement is created to reserve a
period and no independent expiry clock releases a still-existing draft.

## Evidence and identity

`financial.invoices` decodes Sub line transport into shared `dotmac_billing.contracts.ServiceChargePeriodV1`; existing Starter Billing service owns admission and overlap policy. The Sub invoice owner reads authoritative financial/coverage facts and parses typed
`ServicePeriodCharge` values from the line's `future_catalogue_invoice` marker,
subscription ID, exact line metadata dates and charge component. Line dates
precede header dates, matching existing entitlement interpretation. New dated
claims must carry their own complete timezone-aware interval. A base charge and
a recurring add-on are different economic components; add-ons retain their
subscription-add-on identity. Adjacent half-open intervals do not overlap.

Unsettled capped renewal requests reserve one pending renewal identity without
promising dates. Already paid prior capped cycles do not prevent buying fresh
quota while their old validity window remains; the existing quota reset owner
binds actual dates at full settlement. Pending base requests and dated monthly
claims are checked separately from paid coverage semantics.

## One lock and all ordinary constructors

All three ordinary `InvoiceLine` constructors are in `billing/invoices.py`:
complete admin replacement, system line staging and generic line creation.
They acquire the existing account lock before reading other reservations.
Complete replacement checks both other invoices and duplicates in the proposed
replacement set before mutating any lines. Ordinary billing lines also check
catalogue reservations, so using another source key is not a duplicate bypass.

The legacy issuance boundary rechecks reservations. Starter issuance must call
`Invoices.assert_service_period_reservations_for_issue` under its existing
owner transaction before creating any financial document. A failed check rolls
back the entire composing command.

Updates through the generic line API cannot strip a catalogue marker or alter
its price: the complete form re-resolves catalogue evidence and owns replacement.
Deleting a draft line or replacing its active set releases the removed claim
under the same account lock. A void/inactive invoice releases ordinary draft
claims; a written-off invoice still represents a billed charge, not permission
to bill it again. Funded/granted coverage remains independently checked even
when a document no longer reserves a period.

## Compatibility and limits

The change does not globally prohibit existing legacy overlap/correction
semantics: ordinary legacy-versus-legacy line writes are unchanged. A new
catalogue claim checks existing documentary intervals, including ambiguous
periodless linked documents, and applicable active coverage. Ordinary writers
check the new claims. Prepaid extension intervals are coverage facts; postpaid
extension enforcement shields are not assumed to forgive financial charges.

Query previews are advisory and do not lock or reserve. Only the authoritative
save/issue commands decide current availability. Generic SQL or a new writer
outside the central invoice owner would bypass application locking and must not
be introduced. The AST constructor-inventory guard pins all current ordinary
line constructors; migration-backed PostgreSQL races are required before this
is described as qualified concurrency protection.

Validation sources: `tests/test_invoice_period_reservations.py` and
`tests/architecture/test_catalogue_invoice_period_reservation_boundary.py`.

The dedicated local reservation-policy module is retired. Shared Billing's
`resolve_service_charge_conflict_v1` consumes immutable document and coverage
facts, with the same half-open interval primitive used by multi-period document
admission. Capped historical closure requires canonical settlement evidence;
`Invoice.status = paid` alone is not funding evidence. Native documents use the
public current-receivable observation, while legacy documents use the incumbent
settlement resolver. A missing historical settlement remains unresolved.

Read previews call `Invoices.validate_service_period_reservations` without
flushing or acquiring the command's account fence; dirty sessions are refused.
Issuance acquires the account fence, flushes its own staged changes, refreshes,
and then calls the same validation. The raw public invoice-header creation
endpoint is retired; new manual invoices enter catalogue draft authoring.
