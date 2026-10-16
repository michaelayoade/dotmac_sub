# Prospective gateway invoice intent binding owner

Owner: `financial.gateway_topup_intent_commands` coordinates one existing
`execute_owner_command` root. Released `dotmac-payments==0.1.0a2` alone owns new
invoice checkout reference, payer, invoice target, purpose, exact requested
money/scale, provider/channel and opening/expiry instants. Sub owns initial
invoice eligibility/amount selection, routing/actor/display provenance, provider
transport, compatibility lifecycle and incumbent receivable settlement.
Direct transfer, reseller/prepaid binding creation and historical local-only
verification/settlement retain their existing owners.

After account, invoice and local-reference locks, the root reads the module on
its caller Session and `TenantScope(OPERATOR_TENANT_ID)`. Both absent means
validate current payable invoice and enabled routing, refuse amount loss before
Money quantization at the observed legacy scale of two, capture one UTC opening
instant and its TTL, call public `open_payment_intent`, and read back the public
frozen observation. The module ORM result never crosses the boundary. Sub's
projection uses the returned module UUID as local UUID, carries the paired
module UUID/operator-tenant structural link, and projects module facts without
reinterpreting them. Module binding, projection and creation event share one
atomic root; integrity errors abort the whole root, with no savepoint recovery.

Both present means authenticate account/invoice and every original binding and
routing fact, replay the module command with original money/opening/expiry,
validate the frozen observation again and return a stable typed outcome. It
never uses current invoice balance or current TTL on replay, stages a second
creation event, resets terminal lifecycle, or overwrites later reconciliation
metadata. Structural invoice ID and original metadata invoice target must agree.
Provider/channel map module uppercase to legacy lowercase/customer_selfcare.
Local-only and module-only references refuse creation or reconstruction: the
module binding does not contain legacy routing/lifecycle evidence. Mismatches
are stable domain conflicts; there is no fallback creator or repair admission.
The retired `stage_gateway_topup_intent` rejects invoice flow before DB access.

Saved-card transport uses returned requested amount, currency, reference and
serialized typed routing evidence after the root reacquires locks. A successful
existing charge reservation retains its original one-charge replay response.
A module binding replay without that successful reservation evidence refuses a
new charge and requires verification of its original reference; it does not
assert provider success. Hosted checkout retains its normal rendering from the
returned binding. Explicit currency is forwarded through the existing payment
capability and connector, without installation/default substitution. Paystack's
[Charge Authorization contract](https://paystack.com/docs/api/transaction/#charge-authorization)
accepts currency and amount in that currency's subunits.

## Deployment hold and recovery

This is SOURCE ONLY and MUST NOT be deployed while the barriers remain pending.
After the first module-owned invoice creation, rolling back to a legacy creator
can reopen parallel authority. Kernel's Session.info transaction latch is not a
persistent image/schema rollback floor. Before activation, integrate and prove:

- paired module identity/tenant columns, composite FK and correlation checks;
- a durable legacy invoice INSERT fence effective against an old creator image;
- persistent installed-schema/image rollback floor and an actual deployment
  rollback guard that refuses the legacy creator, or disables checkout during
  recovery before any legacy writer can run;
- actual writer quiescence/drain before activation, with immutable source/digest
  and recovery evidence;
- reviewed runtime principal privileges and full app_user root-command canaries.

The structural/barrier source and its reviewed narrow runtime grants are a
separate required dependency. No test-only GRANT can substitute for runtime
grants. Authored migrated PostgreSQL root atomicity/concurrency and getter
canaries use app_user with the actual application Session tenant hook; elevated
fixture access seeds data and simulates later invoice-owner updates only. Full
runtime acceptance requires those actual-role canaries to pass in hosted CI
after the privilege dependency is integrated. PR3640's read-observation exception does not
approve this writer transfer. This slice needs its own concrete source review,
feature-freeze/growth approval, hosted gates and explicit activation decision.
No cap, baseline, exception, schema, deployment control or authority flag is
changed by this owner slice. Confirmation, allocation, credit, provider payment,
renewal and receivable authority are not transferred.


## Typed charge transport boundary

`payment_charge_contracts.py` contains immutable values only. Existing financial
owners retain their facts and decisions; the original provider-status enum and
invoice routing value are moved once and re-exported through their prior names.
`ChargeAuthorizationCommand` carries exact integral currency subunits, explicit
currency/reference, UUID binding identity and one closed metadata variant:
invoice routing, intent identity, reseller identity, or autopay invoice identity.
Authorization code and email are excluded from repr. All five callers construct
this command; the public boundary has no free-form metadata, string/UUID binding
union or JSON result bag.

External request serialization and response parsing stay in the connector.
`ChargeAuthorizationObservation` distinguishes strict top-level boolean provider
acceptance from normalized transaction status and observed reference. Unknown,
missing or unrelated reference/status evidence refuses admission; nested success
or a truthy string cannot imply top-level acceptance. Provided malformed money
or time facts refuse parsing. Missing observed money remains explicitly optional;
provided differing amount/currency/fee facts are retained without substituting
the expected request. Allowlisted output excludes provider authorization/customer
objects, and diagnostics never include request credential material deliberately.

The typed reference query uses the same existing Paystack verification endpoint.
It introduces no second settlement policy. Autopay retains its existing owner
success decision for this bounded intake/typing transfer; this does not qualify
cash or complete payment authority migration. Complete verified-money evidence
and correlation remain an explicit gate for the upcoming confirmation/ledger
caller transfer. The new shared contract file also requires its own reviewed
feature-freeze/growth approval; the read-observation exception does not cover it.


The executable SOT entry `integration.payment_charge_transport` owns the typed
submission/reference transport only. `integration.installations` owns enabled
routing, and `integration.runtime` owns version-pinned execution and bounded
secret resolution. The caller Session is read-only at this boundary; irreversible
provider submission is not a database transaction. Financial callers retain
charge admission, reservation, idempotency and settlement. Currency validation
requires three uppercase ASCII letters at the command, serialized request and
observed response boundaries. Existing provider HTTP 400/404 recovery
classification is preserved by the integration error adapter and exercised by
a focused canary; HTTP 500 does not become a not-found observation.
