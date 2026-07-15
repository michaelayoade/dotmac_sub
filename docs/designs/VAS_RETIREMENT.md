# VAS retirement decision and cutover

Status: approved retirement, 2026-07-15.

## Authority change

The VAS wallet, VTPass purchase catalog, reseller float, purchase delivery,
refund-to-source, and wallet-to-billing bridge have no active owner after this
cutover. Their server and mobile routes, services, repositories, tasks,
settings, permissions, provider adapter, templates, and ORM write models are
removed.

Referral rewards are not retired. Their new owner is
`financial.credit_notes`; `crm_api.create_account_credit` invokes that owner's
preview/confirmation contract and records the exact funding ledger entry,
idempotency key, and audit event.

## Preserved evidence

The following tables remain in the database as immutable financial history:

- `vas_wallets`
- `vas_wallet_entries`
- `vas_refund_requests`
- `vas_services`
- `vas_service_variations`
- `vas_transactions`
- `vas_rate_cards`
- `vas_topup_intents`

They are explicitly excluded from Alembic autogeneration. Removing live ORM
models is not authorization to drop or rewrite those tables. Obsolete encrypted
delivery tokens are cleared at cutover; amounts, references, statuses, and
provider observations remain.

## Cutover gate

Revision `300_retire_vas_runtime` fails before changing configuration when any
of these is true:

- a wallet has a non-zero credit-minus-debit liability;
- a purchase is pending, debited, submitted, or under review;
- a refund is prepared, submitting, accepted, or needs attention;
- a VAS gateway top-up intent is pending.

Do not bypass the gate by editing a balance or terminal status. Resolve each
customer liability through a reviewed refund or evidence-backed account-credit
transfer, with an explicit compensating wallet entry, before retrying cutover.

Deployment order is: disable new VAS activity, drain old web/worker/beat
processes, resolve the gate to zero, apply the migration, then deploy the code
without the VAS runtime. This prevents an old process from recreating a write
after the check.

## Fallback

Before revision 291, rollback is the prior application release. After revision
291, a rollback requires explicitly reviewed configuration re-seeding; deleted
secret settings and delivery tokens are not reconstructed by downgrade. The
archive is retained in either direction.

## Enforcement

Architecture tests keep removed runtime paths, imports, `/vas` routes, and VAS
task names absent across the server and mobile client. Migration tests prove
that unresolved money blocks cutover and that a safe cutover removes runtime
controls and obsolete permissions while preserving archive rows.
