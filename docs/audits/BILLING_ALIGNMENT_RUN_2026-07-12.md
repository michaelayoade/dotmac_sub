# Billing alignment — read-only reconciliation run

Provenance record for the alignment pass described in
`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`. **No writes were made.** The
harness (`scripts/one_off/billing_alignment_audit.py`) has no `--apply` path and
rolls back its session in a `finally` block.

---

## 1. Run provenance

| | |
|---|---|
| **Run date** | 2026-07-12 |
| **Environment** | staging — `seabone` (`hp-server`, 160.119.127.195, user `dotmac`) |
| **Container** | `dotmac_sub_app` |
| **Database** | `db:5432/dotmac_sub` (staging-local Postgres, *not* the prod central PG) |
| **App image** | `ghcr.io/michaelayoade/dotmac_sub:sha-81b7c35` |
| **Code SHA audited** | `81b7c35c` (= `origin/main` at run time) |
| **Alembic head** | `268_sot_safety_closure` |
| **Interpreter** | `/opt/venv/bin/python`, Python 3.12.13 |
| **Harness SHA** | authored this session; not yet committed |
| **Static audit SHA** | `481a0a67` — findings re-verified against `81b7c35c` before this run |

### Code drift handled during the run

The staging container initially ran `sha-f2aa252`, **19 commits behind** `main`.
It was redeployed to `sha-81b7c35` mid-session. Verified before trusting results:

- The 10 intervening migrations touch asset inventory, vendor invoices, warehouse
  serials, ERP sync cursors, campaigns, ONT and sales orders — **none touch the
  billing money tables**, so the schema the detectors read was unchanged.
- **None of the resolvers D7/D12 depend on** (`get_account_credit_balance`,
  `calculate_customer_balance`, `_prepaid_threshold`, `get_available_balance`)
  changed between the two SHAs.

All figures below are from `sha-81b7c35`.

### Finding invalidated by the redeploy

**F22 (CRM billing push resets subscriber status) is RESOLVED.** Commit
`b6d1accd` ("Close the CRM write-back door", #1204) deleted
`app/services/crm_billing_push.py` outright. The finding is struck; no repair
needed. F1 and F6 were re-verified as still live (line numbers shifted only).

---

## 2. Dataset

| Table | Rows |
|---|---|
| `subscribers` (active) | 5,269 |
| `ledger_entries` | 224,573 |
| `invoices` | 126,585 |
| `payments` | 98,284 |
| `splynx_billing_transactions` | **0** |

### ⚠️ Staging is not representative for pre-cutover money

`splynx_billing_transactions` is **empty** here. `_has_legacy_mirror()`
(`customer_financial_ledger.py:112`) therefore returns `False` for **every**
account, so the balance derivation takes the no-mirror branch and skips the
`PAYMENT_ACTIVITY_AT` / `SERVICE_ACTIVITY_AT` / `LEGACY_LEDGER_CUTOVER` gating
that is live on prod.

Consequences, and they are load-bearing:

- **D2 cannot be adjudicated here** (see below).
- **D7 and D12 results do not carry to prod for migrated customers** — on prod
  their balances are partly served from the mirror, which does not exist here.

Any class that depends on pre-cutover money must be re-run read-only against an
environment that has the mirror before it drives a repair.

---

## 3. Results

| Class | Ref | Rows | NGN | Verdict |
|---|---|---:|---:|---|
| D1 ledger double-swing | F1 | **0** | 0.00 | **Latent — fix forward, no historical repair** |
| D2 unbacked dead credit | F1 | 1,103 | 219,685,784.58 | **INCONCLUSIVE on staging** — needs mirror |
| D3 paid-with-balance | F24 | 23 | 411,821.25 | Real; matches the 23 from the 2026-06-24 audit |
| D4 native orphan payment | F19 | **1** | 18,812.50 | Real, single instance |
| D5 misallocated payment | F3 | **2** | 60,000.00 | **Real — F3 fired in production, twice** |
| D6 succeeded, no `paid_at` | F15 | 1 | 17,500.00 | Real; the `paid_at` fix largely held |
| D8 unapplied credit note | F4 | 339 | 2,290,830.01 | Real; the F4 drift mechanism is populated |
| D9 pending money | F18 | 0 | 0.00 | **Zero** |
| D10 void with live debits | F6 | 0 | 0.00 | **Zero** |
| D11 opening debits | — | 0 | 0.00 | **Zero — cohort already remediated** |
| D7 balance sign split | F4 | 3,242 | 2,674,049,793.33 | **STAGING ARTIFACT — not a finding** (see below) |
| D12 enforcement mismatch | F6/F7 | 2,445 | 1,412,205,883.88 | **STAGING ARTIFACT — not a finding** (see below) |

### D7 / D12: staging can measure their COST, not their RESULT

Both classes derive the **document-derived** balance, which on a migrated account is
served partly from `splynx_billing_transactions`. That table is **empty on staging**, so every
migrated account's pre-cutover payments are missing from the derivation and the document
position is driven wrongly negative. The samples show it plainly:

```
D7   ledger_credit: 50000.00   document_position: -70000.00   risk: wrongly_suspended
D12  available: -402500.00     threshold: 17500.00            verdict: unfunded_but_active
```

**The 3,242 and 2,445 row counts are the missing mirror, not damage.** They are recorded here
only to prove the detectors run to completion; they must not be quoted as findings and no
repair may be designed from them. The real result requires the production pass.

### What staging DID establish: performance, and two harness defects

Instrumented run, 2026-07-12, staging (`sha-81b7c35`), `statement_timeout = 10000 ms`,
`SET TRANSACTION READ ONLY`, batch size 250:

| Class | Runtime | SQL statements | Timeouts |
|---|---:|---:|---|
| D7 | 36.9 s | **10** | none |
| D12 | 172.5 s | **16,361** | none |

The first instrumented attempt **failed on both classes**, which is the whole reason to rehearse
on staging:

1. **D7 timed out.** The "batched" derivation issued a chunked `IN (…)` list of 250 UUIDs per
   batch, which makes the planner re-scan `ledger_entries` once *per chunk*; the first chunk
   blew the 10 s timeout. `EXPLAIN (ANALYZE)` showed a single whole-table aggregate over the
   same rows — `GROUP BY account_id, entry_type, currency`, 212k rows — costs one sequential
   scan at **3.8 s**. Rewritten to one whole-table aggregate per source: **10 queries total**,
   for every account at once.
2. **D12 crashed outright.** `SELECT DISTINCT subscribers.*` cannot execute: `subscribers.metadata`
   is a `json` column and Postgres has no equality operator for it
   (`could not identify an equality operator for type json`). Fixed by selecting DISTINCT ids and
   loading those rows.

### D12's remaining cost is an open decision — NOT yet fit for production

D12 still issues **16,361 statements**. The balance derivation is now batched; what remains is
`service_status._prepaid_threshold(db, account)`, called **once per account** (~6 queries each:
the account's prepaid subscriptions, paid-coverage end per subscription, and the resolved price).

Two options, and the choice is not obvious:

- **A — keep the canonical resolver.** ~16k small indexed read-only queries, ~3 min, on a
  replica. Correct *by construction*: it is the same code enforcement uses.
- **B — batch the threshold in the harness.** Far fewer queries, but it re-implements an
  enforcement-critical rule inside the audit tool. That is a second implementation of the
  suspension threshold, and a divergence there would make the audit lie about the exact thing
  it exists to check. Would need equivalence tests before it could be trusted.

**Pending Michael's decision. D12 does not go to production until this is settled.**

Execution errors among completed classes: **none**. D7 and D12 were still
running when this manifest revision was recorded.

### Explicit zero-result classes

**D1, D9, D10, D11 returned zero rows.** These are real results, not skipped checks:

- **D1 = 0** is the single most valuable result of the run. F1 is proven defective
  in code (`tests/test_ledger_reversal_integrity.py` failed against audited SHA
  `81b7c35c`: reversing a ₦10,000 credit moves the balance to −₦10,000) but **has
  never fired on this data**. The
  `POST /ledger-entries/{id}/reverse` endpoint appears unused. Fix forward and land
  the regression tests; **do not build a historical repair.**
- **D11 = 0** — only one adjustment-debit exists in the entire ledger. The
  opening-balance seed cohort (₦106M/₦63.5M from the June audit) is **gone**,
  consistent with the remediation recorded then.

---

## 4. Two detectors were wrong, and how

Both first-pass detectors failed the same way: **they counted a deliberate design
decision as damage.** Recorded here because the mistake will recur.

**D2 — gross was 8,347 rows / ₦2,204,546,908.** That is larger than the whole
book, which is what prompted the check. Decomposition against real data:

| | Accounts | Meaning |
|---|---|---|
| Deactivated credits backed by a succeeded `Payment` doc | 1,759 | **Benign** — soft-delete removed a double-count against the documents the balance is actually derived from |
| Deactivated credits with **no** `Payment` doc | 470 | The only cohort worth adjudicating |

The residual 470 accounts / 1,103 rows / **₦219,685,784.58** carry real bank-receipt
memos ("Bank transfer", "Zenith 461 Bank", "advance"). 162 are **active**
subscribers (₦113,258,901.11), and **65 carry ₦84,133,447.02 of open AR** — if those
payments were genuine, those customers are being dunned for money they already paid.

**But this is not yet a finding.** On prod that pre-cutover money legitimately lives
in `splynx_billing_transactions`, which is empty here. Staging structurally cannot
distinguish *"lost"* from *"never imported into this environment"*. **Requires a
read-only prod query to adjudicate. Do not repair on the basis of this number.**

**D4 — gross was 3,117 payments / ₦146,688,352.** Of those, **3,116 were
splynx-imported**, which have no local allocation or ledger entry *by design* —
they were mirrored, not posted. The real native signal is **1 payment (₦18,812.50)**.

---

## 5. CSV handling

- Written **only** to `/tmp/align` **inside the staging container** — never to the
  repo working tree.
- `.gitignore` has no blanket `*.csv` rule, so any local copy is kept in the
  gitignored `scratchpad/` (`.gitignore:75`) and is **not** committed.
- Contents: account/entry/payment/invoice UUIDs, amounts, currency, status and
  effective dates. **No free-text memos, names, emails, phone numbers,
  addresses, card data, or credentials.** Detectors may classify memos in
  memory, but the raw text is never written to CSV.
- Retain until the historical repair is verified, then delete.

### Production-query safety contract

The consolidated D2/D7/D12 adjudication package must run once, after Michael
names the target host. The harness now:

- starts a PostgreSQL read-only transaction before the first audit query;
- applies a 10-second per-statement timeout by default;
- refuses a PostgreSQL primary unless `--allow-primary` is explicitly passed;
- batches D7/D12 customer-position derivation (250 accounts by default); and
- restricts D12 to accounts with a current prepaid service.

Prefer a production read replica. Before any approved primary run, execute the
same package against staging, review `EXPLAIN` for the grouped source queries,
and retain the explicit host approval with the run record.

---

## 6. What this run licenses

| Action | Licensed? |
|---|---|
| Land the F1 forward fix + its regression suite | **Yes** — defect proven in code |
| Build an F1 historical repair | **No** — D1 = 0, there is nothing to repair |
| Fix F3 (admin re-allocation) forward | **Yes** — D5 proves it fired twice |
| Repair the 2 D5 payments | **Yes**, once the forward fix is in |
| Repair the D2 ₦219.7M cohort | **No** — inconclusive; needs a prod read-only pass first |
| Fix F19/F15 forward | **Yes** — cheap; D4/D6 show the blast radius is 1 payment each |
| Strike F22 | **Yes** — already fixed by `b6d1accd` |
