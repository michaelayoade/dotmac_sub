# Billing alignment — read-only reconciliation run

Provenance record for the alignment pass described in
`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`. **No persistent source or
business table was written.** The harness (`scripts/one_off/billing_alignment_audit.py`) has no
`--apply` path and rolls back its session in a `finally` block. The later
2026-07-14 adjudication restored only ephemeral copies on an isolated Docker
network, reconstructed the final legacy position and service schedule, ran the
counterfactual comparison, and destroyed every temporary container, volume,
network and file. A later D12 rehearsal used only session-local PostgreSQL
temporary tables against seabone's staging-local database; it was rejected as
current-production evidence. The accepted D12 pass then ran against the
explicitly named Sub production host `selfcare.dotmac.io`; both passes destroyed
their session-local tables and temporary files (§7).

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
| D2 unbacked dead credit | F1 | 0 unresolved | 0.00 | staging showed 1,103 candidates; source cutoff deposits adjudicated all as legacy projections (§7) |
| D3 paid-with-balance | F24 | 23 | 411,821.25 | Real; matches the 23 from the 2026-06-24 audit |
| D4 native orphan payment | F19 | **1** | 18,812.50 | Real, single instance |
| D5 misallocated payment | F3 | **2** | 60,000.00 | **Real — F3 fired in production, twice** |
| D6 succeeded, no `paid_at` | F15 | 1 | 17,500.00 | Real; the `paid_at` fix largely held |
| D8 unapplied credit note | F4 | 339 | 2,290,830.01 | Real; the F4 drift mechanism is populated |
| D9 pending money | F18 | 0 | 0.00 | **Zero** |
| D10 void with live debits | F6 | 0 | 0.00 | **Zero** |
| D11 opening debits | — | 0 | 0.00 | **Zero — cohort already remediated** |
| D7 expected position vs persisted outputs | F4 | 4,698 with at least one output gap | 73,041,254.69 persisted-deposit absolute drift | **Retained-backup result**; 589 deposit gaps, separately from document/ledger projections (§7) |
| D12 enforcement mismatch | F6/F7 | 2,539 | 68,139,241.85 threshold gap | **Current production**; 0 funded with a money lock, 2,533 unfunded and marked served (§7) |

### D7 / D12: the original staging numbers were artifacts

Both classes derive the **document-derived** balance, which on a migrated account is
served partly from `splynx_billing_transactions`. That table is **empty on staging**, so every
migrated account's pre-cutover payments are missing from the derivation and the document
position is driven wrongly negative. The samples show it plainly:

```
D7   ledger_credit: 50000.00   document_position: -70000.00   risk: wrongly_suspended
D12  available: -402500.00     threshold: 17500.00            verdict: unfunded_but_active
```

**The 3,242 and 2,445 staging row counts are the missing mirror, not damage.**
They remain recorded only as detector/performance history. The source-based
retained-backup result that supersedes them is in §7.

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

### D12's remaining cost was removed without creating a second rule

The 16,361-statement result is historical. PR #1220 introduced
`prepaid_threshold.resolve_prepaid_thresholds`, the canonical batch owner; the
scalar resolver delegates to it. D12 now calls that owner once for the complete
prepaid cohort instead of calling `_prepaid_threshold` per account. It also uses
the current shared `COLLECTIBLE_SERVICE_STATUSES` contract rather than importing
a private sweep constant that no longer exists.

The audit regression suite proves scalar/batch equivalence and applies a D12
query budget to a 20-account cohort. The focused alignment suite is green (19
tests after the independent-replay additions); ruff and mypy are clean. The
optimized D12 subsequently completed against the isolated source-bearing
restore (§7).

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

**But this is not yet a finding.** On a migrated account, pre-cutover money is
owned by the authoritative Splynx cutoff ledger. A deactivated local row dated
before native payment activity is only a projection; when the account maps to
the cutoff ledger, the counterfactual opening position includes the source money
without consuming that local row at all. `subscribers.deposit` is deliberately
not used to validate the cutoff: it is mutable post-cutover state under audit.

Corrected D2 therefore excludes cutoff-covered legacy projections, retains
unmapped accounts, and retains deactivated credits dated after native payment
activity for settlement/adjustment provenance review. It does not invent an
individual receipt link.

Staging structurally cannot distinguish *"lost"* from *"never imported into this
environment"*. §7 corrects the earlier cross-backup parity gate and defines the
counterfactual reconstruction required to settle the question.
**Do not repair on the basis of this number.**

**2026-07-14 adjudication:** once every source `customer_billing.deposit` was
loaded as the cutoff baseline, all 1,103 rows / 470 accounts /
**₦219,685,784.58** were classified as pre-cutover local projections covered by
source cutoff state. **D2 = 0. No historical repair exists for this cohort.**

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

The consolidated D2/D7/D12 adjudication package must run once against a
source-proven cutoff plus an independently replayed post-cutover event set,
after Michael names any production SSH target involved. The harness now:

- starts a PostgreSQL read-only transaction before the first audit query;
- applies a 10-second per-statement timeout by default;
- refuses a PostgreSQL primary unless `--allow-primary` is explicitly passed;
- batches D7/D12 customer-position derivation and D12's canonical threshold;
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
| Repair the D2 ₦219.7M cohort | **No** — D2 adjudicated to zero; these are cutoff-covered projections, not lost money |
| Fix F19/F15 forward | **Yes** — cheap; D4/D6 show the blast radius is 1 payment each |
| Strike F22 | **Yes** — already fixed by `b6d1accd` |

---

## 7. 2026-07-14 retained-backup adjudication — methodology corrected

Michael explicitly named `seabone` for the restore. All row-level reconstruction
ran against retained backups in isolated containers. Before the restore, a
small set of read-only aggregate inventory queries was also run against the
named host's current Sub database to determine which post-cutover fact tables
were populated; no row-level customer data was exported and no database was
written.

### Backup provenance and isolation

| Artifact | Verified facts |
|---|---|
| `/home/dotmac/backups/dotmac_sub/dotmac_sub_2026-07-12_185549.sql.gz` | gzip-valid; 1,024,343,067 bytes; restored Alembic head `254_waiting_bulk_item_status`; 15,286 subscribers, 224,573 ledger entries, 126,585 invoices, 98,284 payments; **0 mirror rows** |
| `/home/dotmac/backups/splynx/splynx_2026-06-15_132449.sql.gz` | gzip-valid; 962,844,692 bytes; 232,298 billing transactions through 2026-06-15; 5,418 customer IDs across all transaction rows, 4,854 active transaction ledgers |
| `/home/dotmac/backups/splynx/splynx_2026-06-29_023001.sql.gz` | retained final legacy snapshot; 232,377 billing transactions, maximum transaction date 2026-06-17; 25,085 final customer deposits; 11,475 internet-service rows |

Both restores ran in fresh containers on a Docker `--internal` network with no
published ports and fresh named volumes. No existing Sub, staging, or other
service container was joined to the network. The source backup files were never
modified.

### Evidence and correction

1. The Sub backup was not mirror-bearing: `splynx_billing_transactions = 0`.
2. The June 15 Splynx transaction ledger reconciled exactly to its own
   `customer_billing.deposit`: **4,854 checked, 4,854 matched, 0 mismatches**.
3. A purpose-built loader applied the checked-in historical import
   mapping, required the exact `232,298 / 2026-06-15` source fingerprint, and
   streamed only mirror fields plus the non-identifying cutoff-deposit table into
   one PostgreSQL transaction.
4. The first version wrongly required the reconstructed cutoff net to equal
   current `subscribers.deposit`. It found **598 differences** and rolled back.
   That was safe operationally but invalid methodologically: the audit hypothesis
   is that post-cutover scripts may have changed local billing state, so local
   deposit cannot be the gate that accepts or rejects source truth.
5. The June 29 retained snapshot establishes the end of the overlap window:
   Splynx contains 232,377 transactions and no financial transaction after
   2026-06-17. Its 4,854 transaction-bearing customer ledgers still reconcile
   exactly to final source deposits.
6. Between the June 15 opening and the final legacy state, 38 deposits changed:
   signed change **−₦907,913.48**, absolute change **₦2,830,940.48**. The replay
   therefore starts from the final June 17 source position, not from June 15
   plus a guessed overlap exclusion.
7. The final source service schedule contributes only non-identifying decision
   fields: service/customer IDs, source status, charged amount, and the last
   paid-through period. Of 11,475 source services, 11,468 map to a Sub customer,
   11,458 map to a Sub subscription, and 3,999 are active/non-deleted at handoff.

The corrected source-wide load proves:

- 25,085 Splynx cutoff deposits loaded; 15,257 map to Sub accounts and 9,828 do
  not (the source population is larger than Sub's imported population);
- 4,854 transaction-bearing source ledgers reconcile exactly to their own
  deposits; the cutoff deposit remains authoritative for every source customer,
  including those without retained active transaction rows;
- 598 mapped June 15 deposits differ from current Sub deposit, absolute difference
  **₦73,106,957.61** — an intermediate replay question, not a repair total;
- the final June 17 source position differs from current Sub deposit on the same
  broad 598-account comparison, absolute difference **₦74,549,324.95**, before
  post-legacy service renewal is replayed; and
- corrected D2 excludes 1,103 cutoff-covered local projection rows and returns
  **0 rows / ₦0.00**.

The D2 anti-join exceeded the harness's 10-second default on the cold restored
volume and completed under a 60-second timeout on the isolated database. The
10-second default remains unchanged for primary safety; no primary run was used
for D2.

A second read-only aggregate check on the explicitly named `seabone` target
found that current Sub billing documents stop near cutover: `payments` end on
2026-06-16, `invoices` and `ledger_entries` on 2026-06-22, while `event_store`
continues through 2026-07-14. There are no `payment_provider_events`,
`payment_proofs`, `service_extensions`, or `service_extension_entries` in that
snapshot. This does **not** prove missing money by itself. It proves that current
invoice/payment rows cannot be treated as a complete post-cutover event journal.

After the completed pass, both database containers, both volumes, the internal
network, and all temporary TSV/log/tar artifacts were destroyed and verified
absent. The original backup files remain intact.

### Counterfactual reconstruction contract

The required adjudication is now:

```text
expected current position =
  authoritative Splynx cutoff net
+ independently proven post-cutover cash / refunds / credit decisions
- independently derived post-cutover service consumption
+ provenance-backed manual adjustments
```

The financial-position replay and service-schedule replay are separate:

- **Money facts:** provider settlement observations, verified bank proofs,
  refunds/chargebacks, issued credit notes, and explicitly approved adjustments.
  Allocations only distribute value; they do not create it.
- **Service facts:** cutoff services/tariffs and billing dates, then authoritative
  subscription/catalog changes and applied service-extension entries. An
  extension shifts the expected billing schedule; it is not itself a cash credit.
- **Comparison outputs:** current deposit, ledger balance, invoices and statuses,
  allocations, enforcement locks, and access state. None may validate its own
  reconstruction input.

For every source class the run must record `authoritative`, `corroborating`, or
`incomplete`. A gap remains inconclusive; it is never silently replaced with a
current derived row.

### Counterfactual replay result at the backup timestamp

Snapshot: **2026-07-12 18:55:49 UTC**. Replay begins at 2026-06-18, after the
last source financial transaction. The retained Sub snapshot contains no
post-legacy payment, credit-note, provider-settlement, verified-proof, or
service-extension fact. It does contain one post-legacy manual adjustment,
which is quarantined pending provenance. Funded renewals are derived from the
source paid-through schedule; an extension would shift the next due date rather
than credit cash.

| Replay component | Result |
|---|---:|
| Active Sub accounts with complete replay | 5,164 |
| Proven post-legacy credits | 0 accounts / ₦0.00 |
| Derived funded service renewals | 176 accounts / ₦7,093,381.77 |
| Incomplete replay | 93 accounts: 92 missing a source paid-through period; 1 has an unproven post-legacy adjustment |
| Active Sub accounts without a mapped source baseline | 12 |

For those 5,164 complete accounts, D7 reports the quantities separately:

| Persisted output compared with expected | Accounts | Absolute gap |
|---|---:|---:|
| `subscribers.deposit` | **589** | **₦73,041,254.69** |
| local document-derived position | 4,636 | ₦3,307,519,834.67 |
| unallocated active ledger credit | 2,580 | ₦315,390,121.40 |

The document and ledger figures are diagnostic projections and are **not added
to the deposit figure**. Doing so would manufacture a multi-billion-naira
"repair" from non-equivalent quantities. Deposit direction is 321 currently
overcredited accounts / ₦52,291,360.21 and 268 currently understated accounts /
₦20,749,894.48. Those are review populations, not an approved correction
packet: customer-debit cases require finance approval, and the 93 incomplete / 12
unmapped accounts remain outside any automated repair.

D12 applies the canonical prepaid threshold at that same immutable timestamp:

| Enforcement comparison | Accounts | Threshold gap |
|---|---:|---:|
| Funded but carrying an active money lock | **116** | **₦23,328,010.66** |
| Unfunded with no active money lock | 2,380 | ₦65,043,196.97 |
| Above cohort also marked actively served | **2,378** | **₦65,008,196.97** |

The first cohort is a wrongful-cutoff risk; the actively served subset is a
free-service signal. Both are snapshot findings, not instructions to mutate
production blindly: enforcement must be rechecked against current state and the
same reconstructed inputs immediately before any containment action.

### Rejected current-state attempt — staging is not production

At **2026-07-14 11:18:03 UTC**, a source-replay D12 rehearsal ran against
seabone's current `dotmac_sub_db`. It was initially described as a production
snapshot, but the provenance manifest above already identifies that database as
**staging-local Postgres, not the production central PG**. The claim and numbers
are retracted as current-production evidence. The rehearsal used one bounded
database session:

- final Splynx balances and service paid-through facts were exported without
  names, contact details, credentials or free text;
- the source fingerprints remained 232,377 transactions through June 17,
  4,854 transaction-bearing customer ledgers with zero deposit mismatches, and
  11,475 services / 3,999 active;
- those source facts were loaded only into session-local PostgreSQL temporary
  tables; the staging comparison then began with
  `SET TRANSACTION READ ONLY` and a 60-second statement timeout;
- the run printed only aggregates. No account CSV or identifier list was
  retained, and all container/host temporary files were deleted afterward.

The staging-only result was 114 funded+locked and 2,378 unfunded+marked-served.
It is retained only as rejected execution evidence, not as a containment
worklist or proof that the July 12 production-shaped backup cohorts changed.
“Marked served” was also only Sub's persisted
`subscriptions.access_state`/status projection, not a direct FreeRADIUS
observation.

Two earlier execution attempts were also rejected. PostgreSQL
first refused temporary-table creation after read-only mode was set. A later
attempt exposed SQLAlchemy's inability to discover session-local temporary
tables and announced the document-balance fallback; its `31 / 2,413` output was
discarded. The staging rehearsal did explicitly prove and announce
`funding is reconstructed from the final Splynx position and proven
post-legacy facts`, but correct replay logic cannot repair wrong-environment
provenance.

No fresh production Sub backup existed on seabone (only the two July 12 files),
so no production dump was initiated implicitly. Michael subsequently named
`selfcare.dotmac.io` as the Sub production target, allowing the accepted pass
below.

### Accepted current-production D12 pass — 2026-07-14

The accepted source replay ran against `selfcare.dotmac.io` at the exact
PostgreSQL transaction timestamp **2026-07-14 12:08:25 UTC**. The target was
`dotmac_sub` on the local PostgreSQL primary. The deployed image is
`dotmac_sub:support-suspend-activate-20260714`; it has no revision label, so the
three enforcement files were compared by content hash. They match the merged
F6/F7/F8 implementations, including threshold owner `e50d72ab`.

The final Splynx source fingerprints remained unchanged: 232,377 financial
transactions through June 17, 4,854 transaction-bearing customer ledgers with
zero deposit mismatches, and 11,475 services / 3,999 active. The source facts
were streamed without identity, credentials or free text into session-local
temporary tables. After that staging step, the complete production comparison
ran with `SET TRANSACTION READ ONLY` and a 60-second statement timeout. The
runner asserted the independent replay path and printed aggregates only.

| Enforcement comparison | Accounts | Threshold gap |
|---|---:|---:|
| Funded but carrying an active money lock | **0** | **₦0.00** |
| Unfunded with no active money lock | **2,539** | **₦68,139,241.85** |
| Above cohort also marked actively served | **2,533** | **₦67,989,048.85** |

The wrongful-cutoff cohort has therefore converged to zero after the owner fix.
The free-service direction has not. The replay excludes 769 provenance-incomplete
accounts and reports seven current prepaid accounts without a mapped source
baseline; neither population enters these action counts.

“Marked served” was independently checked against live FreeRADIUS:

| Live projection check within the 2,533-account served cohort | Accounts | Threshold gap |
|---|---:|---:|
| Has an active access credential | 2,529 | — |
| Password authentication with no reject or walled-garden marker | **2,166** | **₦56,609,463.94** |
| Walled/captive marker | **0** | — |
| Recent open session (updated within two hours) | **492** | **₦13,719,474.67** |

This is a confirmed current free-service population, not merely a local status
projection. `dotmac-active` group membership is zero; unrestricted access here
comes from usable password rows with neither a reject nor captive marker.

The remaining drift is controlled by configuration, not an unfixed caller:

- the billing module is enabled and enforcement health is green;
- `collections.prepaid_balance_enforcement` is disabled by the active legacy
  database row `collections.prepaid_balance_enforcement_enabled=false` (updated
  2026-07-04); there is no environment override and no canonical control row;
- `prepaid_deactivation_days=0`;
- 2,161 unfunded accounts have no low-balance timer, 378 are already due, four
  due accounts are shielded, and 374 clear the bulk shield/dedicated/health/
  window gates but still require the full per-account billing-profile/status
  planner;
- among recent live sessions, 104 are due/eligible and 385 have no timer.

Do **not** flip the control blindly. With a zero-day policy, the first sweep
could act on up to 374 already-due accounts after the remaining per-account
planner gates and would arm the 2,161 untimed accounts; the next sweep could act
on that newly armed population. The next action is an explicit policy decision
on warning/grace, followed by an independent-funding dry-run through the full
owner planner before enabling the control.

That safety slice is now implemented in draft PR #1284. It proposes a three-day
minimum warning window, requires an explicit activation timestamp, floors old
timers at activation, and lets the owner planner consume a named, timestamped
funding snapshot without a local-money fallback. The audit-side bridge is
`scripts/one_off/export_prepaid_funding_snapshot.py`: it reuses this harness's
source replay and the canonical threshold owner, while the prepaid enforcement
owner still selects the cohort and applies every non-money gate. The export is
complete-or-error. Missing source baselines or incomplete replay provenance
produce a UUID-only blocker manifest and no planner-consumable funding file.
PR #1284 is CI-green but remains draft and is not deployed; this paragraph is
implementation status, not authorization to enable enforcement.

No persistent production table, RADIUS table, lock, timer or setting was
written. Every temporary file was deleted from the production container and
verified absent.

### Source-independent containment signal: known prepaid-runner invoices

The read-only current-state check also found a cohort that does not depend on the
missing cutoff mirror for its classification:

| Population | Rows | Total |
|---|---:|---:|
| Active `issued` invoices created after the service handoff | 397 | ₦13,318,375.00 |
| Above rows satisfying the repository's known prepaid-phantom criteria | **396** | **₦13,283,375.00** |
| Remaining issued row (no billing period) | 1 | ₦35,000.00 |

All 397 accounts are currently marked prepaid. The 396-row cohort is locally
generated, automated, has a billing period and invoice number, remains active
and issued, and has no credit-exception or void-reason marker. Those are the
exact criteria documented by
`scripts/one_off/cleanup_prepaid_phantom_invoices.py`, whose checked-in history
states that the pre-#301 runner incorrectly generated postpaid-style invoices
for prepaid accounts. The event store independently confirms that the 404
post-handoff invoices were actually created; an event proves occurrence, not
legitimacy.

This is a containment signal, not a debit/credit repair amount. In the corrected
replay these invoices are comparison outputs, never service-consumption inputs.
Per-account correction still depends on the reconstructed position and excludes
the incomplete/unmapped populations. These rows must not license dunning or
suspension merely because they remain open.
