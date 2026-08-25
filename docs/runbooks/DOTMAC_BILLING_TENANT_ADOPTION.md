# `dotmac-billing` tenant adoption rehearsal and cutover contract

This runbook prepares Sub's tenant-plane adoption. It does **not** authorize a
production deployment, authority switch, legacy-data deletion, or retirement.
Run the rehearsal commands only on a named disposable development/test database.
A future production execution needs Michael to name the target and authorize
the coupled switch separately.

## Pinned evidence

| Evidence | Exact revision/version | Use |
| --- | --- | --- |
| Sub adoption base | `a9da920926a9d9212a8cf03a4744b48a1d4e14f2` | current `origin/dev` used for the preparation |
| Vendor CP source | `f8f8c3fd636e663e4a17275c19e82fc1667aa52a` | platform-first adopter evidence |
| ERP source | `2749ec5396cbbd7a1132b394e85855a1d133a7cd` | read-only tax/FX/accounting boundary evidence |
| Integrator source | `35167813c83ab0ec29c683259ad31479503d812f` | read-only payment-transport boundary evidence |
| Durable Timers, Starter | `7e0543004864845f0035c9ec325e3f5064c281cc` | read-only selectable-module and relay design |
| Durable Timers, Sub | `4489ca1712f3c263d914f2af0ebfcf044aa70605` | read-only recurring scheduling/adoption evidence |
| Kernel release evidence | `0.1.0a67` | released `outbox_relay.v1` provider and verifier |
| Billing candidate | `dotmac-kernel==0.1.0a69`, `dotmac-billing==0.1.0a1` | exact isolated-harness pins; publication pending |

The final review must also record the exact Starter Billing and Sub adoption
commit SHAs in the cross-repository extraction dossier. A branch name or
working-tree state is not an immutable pin.

## Invariants

- The legacy Sub invoice, settlement, allocation, balance, and tax/FX writers
  are the sole production authority throughout rehearsal.
- The shadow database identity differs from the Sub source database identity.
- Product routes are unmounted and outbound delivery is disabled.
- Only the tenant plane is selected. Every Billing row carries a real Sub
  tenant UUID; no platform table, fake tenant, nullable tenant, or plane mode
  boolean exists.
- Commands use exact `Decimal` money, uppercase ISO currency, and persisted
  minor-unit precision. A float is rejected.
- Provider acknowledgement, pending checkout, uploaded proof, or UI approval is
  not confirmed settlement evidence and moves no money.
- Unknown/unverified due-date basis is reportable and non-collectible. It is not
  replaced with a plausible current term.
- No historical backfill emits a new ERP accounting fact. ERP projection is a
  later, separately watermarked transport migration.

## S0 — classify the complete source cohort

Run read-only measurements on an approved clone/snapshot. Capture query text,
database identity, transaction snapshot, code/schema revision, result count,
per-currency totals, and a SHA-256 digest of the ordered result.

Baseline population:

```sql
SELECT 'invoice' AS kind, currency, status::text, count(*) AS rows,
       sum(total) AS gross, sum(balance_due) AS recorded_balance
FROM invoices
WHERE is_active
GROUP BY currency, status
UNION ALL
SELECT 'confirmed_settlement', currency, origin::text, count(*),
       sum(amount), sum(unallocated_amount)
FROM payment_settlements
GROUP BY currency, origin
ORDER BY 1, 2, 3;
```

Due-date provenance blockers:

```sql
SELECT due_date_basis::text, currency, count(*) AS rows,
       sum(balance_due) AS recorded_balance
FROM invoices
WHERE is_active
  AND status::text <> 'draft'
  AND (
    due_date_basis IS NULL
    OR due_date_basis::text = 'unknown_unverified'
    OR due_date_basis_ref IS NULL
    OR due_date_policy_version IS NULL
  )
GROUP BY due_date_basis, currency
ORDER BY 1, 2;
```

Allocation evidence and currency integrity:

```sql
SELECT pa.payment_id, pa.invoice_id,
       p.currency AS payment_currency,
       i.currency AS invoice_currency,
       sum(pa.amount) AS allocated,
       ps.amount AS confirmed
FROM payment_allocations AS pa
JOIN payments AS p ON p.id = pa.payment_id
JOIN payment_settlements AS ps ON ps.payment_id = p.id
JOIN invoices AS i ON i.id = pa.invoice_id
WHERE pa.is_active
GROUP BY pa.payment_id, pa.invoice_id, p.currency, i.currency, ps.amount
HAVING p.currency <> i.currency OR sum(pa.amount) > ps.amount
ORDER BY pa.payment_id, pa.invoice_id;
```

Every source row is converted to `LegacyFinancialFactV1` and passed to
`classify_legacy_fact`. The result must be exactly one of:

- `TARGET_BACKFILL`: active authoritative fact with complete provenance;
- `PROVIDER_PROJECTION`: provider-owned observation, never relabelled native;
- `CLOSED_LEGACY_ARCHIVE`: closed evidence gap retained read-only;
- `CUTOVER_BLOCKER`: active/open fact with missing evidence;
- `KNOWN_INCORRECT_NATIVE_FACT`: requires an owner correction before cutover.

The complete ordered classification artifact and its per-row fingerprints are
the backfill manifest. A filtered subset is not cutover evidence.

## S1 — run the isolated typed shadow

The harness resides at `adoption/dotmac_billing`. Before publication, candidate
testing may inject the exact Starter source trees through `PYTHONPATH`; the
committed dependency contract remains the two exact registry versions and no
path dependency or fabricated lock is allowed.

Create a fresh disposable database, then run the real composed migrations to
both heads with `make_shadow_alembic_config(DATABASE_URL)`. Confirm:

```sql
SELECT version_num FROM alembic_version ORDER BY version_num;

SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'mod_billing' AND c.relkind = 'r'
ORDER BY c.relname;
```

The only heads are the exact kernel head and `bi_0001_billing`; every installed
Billing table is tenant-declared with ENABLE+FORCE RLS. Platform tables are
absent, `app_user` has only the declared tenant privileges, and `platform_api`
has no `mod_billing` schema usage.

Mapping is explicit:

| Sub evidence | Billing contract |
| --- | --- |
| rated contract line/period/tax/FX snapshot | `AcceptRatedObligationV1` |
| independently confirmed cash evidence | `AcceptSettlementV1` |
| invoice due-date provenance | `DueDateBasisV1` |
| exact money/currency/precision | `MoneyV1` |
| service coverage interval/provenance | `ServicePeriodEvidenceV1` |

Allocation and coverage are internal Billing behavior. The harness publishes
no invented allocation or coverage contract.

`run_shadow` refuses a mixed-tenant bundle, an unknown account/currency, the
same source and target database identity, mounted product routes, or enabled
outbound delivery. It writes only the isolated Billing target; it never calls a
legacy financial command.

## Exact reconciliation

For every `(tenant, account, currency, surface, source_identity)`, create one
`FinancialObservationV1` from each of:

1. the frozen legacy source;
2. the isolated Billing target;
3. an independently implemented control calculation.

Digests compare exact canonical Decimal/provenance payloads. A missing source or
different digest is unclassified until a typed `DriftAcceptanceV1` records a
concrete class. Customer debit, over-credit, tax, or access impact also requires
a non-secret Finance/product review reference. There is no rounding tolerance.

Rebuild target positions directly from immutable effects:

```sql
WITH rebuilt AS (
  SELECT tenant_id, billing_account_id, currency, minor_units,
         sum(amount_delta) FILTER (WHERE lane = 'receivable') AS receivable,
         sum(amount_delta) FILTER (WHERE lane = 'available_credit') AS credit,
         sum(amount_delta) FILTER (WHERE lane = 'prepaid_funding') AS funding
  FROM mod_billing.posting_effects
  GROUP BY tenant_id, billing_account_id, currency, minor_units
), latest AS (
  SELECT DISTINCT ON (tenant_id, billing_account_id, currency)
         tenant_id, billing_account_id, currency, minor_units,
         collectible_receivable, available_credit, prepaid_funding,
         state_fingerprint
  FROM mod_billing.receivable_position_facts
  ORDER BY tenant_id, billing_account_id, currency, source_version DESC
)
SELECT r.*, l.collectible_receivable, l.available_credit, l.prepaid_funding,
       l.state_fingerprint
FROM rebuilt AS r
FULL OUTER JOIN latest AS l
  USING (tenant_id, billing_account_id, currency, minor_units)
WHERE coalesce(r.receivable, 0) <> coalesce(l.collectible_receivable, 0)
   OR coalesce(r.credit, 0) <> coalesce(l.available_credit, 0)
   OR coalesce(r.funding, 0) <> coalesce(l.prepaid_funding, 0)
   OR r.billing_account_id IS NULL
   OR l.billing_account_id IS NULL
ORDER BY tenant_id, billing_account_id, currency;
```

The mismatch query must return zero rows. The canonical rebuild service must
also reproduce every persisted `state_fingerprint`. Acceptance requires three
distinct consecutive complete reconciliation reports with zero unclassified
results.

## Coupled authority watermark

The maintenance record contains exact high-water marks for invoices,
settlements, allocations, and the Integrator checkpoint. The only allowed
sequence is:

1. pause all three legacy writers together;
2. drain and record the inbound transport checkpoint;
3. capture the source high-water marks in one typed
   `CoupledAuthorityWatermarkV1`;
4. rerun classification and exact reconciliation at that watermark;
5. enable Billing invoice, settlement, and allocation authority together;
6. prove the first post-watermark fact and immediately reconcile again.

Do not partition the switch by customer, date, document type, or money path.
Before the first Billing fact, a technical rollback re-enables all three legacy
writers together. After it, recovery is roll-forward only.

## Retirement gates

Run the two-directional scanner:

```bash
poetry run pytest -q \
  tests/architecture/test_dotmac_billing_adoption_boundary.py
```

Its exact count and stable-site digest cover:

- invoice/credit authority;
- payment/settlement authority;
- allocation authority;
- direct balance assignment;
- tax/FX decisions;
- provider/job money mutations.

A count increase, decrease without a baseline reduction, or one-for-one
substitution fails. The baseline reaches zero only in the separately authorized
retirement change. Historical rows remain read-only until a retention policy
authorizes deletion.

## Stop and rollback conditions

Stop before a switch for any classification blocker, incomplete or duplicate
reconciliation, position/hash mismatch, missing RLS/wrong-plane proof, retirement
ratchet drift, missing transport checkpoint, unreviewed customer-impacting
difference, or non-exact dependency/revision pin.

The rehearsal database is disposable. Dropping it changes no authority. Never
repair source money by editing target rows, never replay historical accounting
facts into ERP, and never interpret a clean shadow as production adoption.
