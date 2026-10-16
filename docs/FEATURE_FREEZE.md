# Sub feature freeze — corrections only

Sub is migrating to a thin client of the build-once Starter platform
(ADR 0020; billing destination is Starter `dotmac-billing`, decided
2026-10-13). New capability is built once in Starter and consumed here as a
pinned, provenance-published package. To keep collaborators from shipping
code that re-thickens Sub during the migration, this repository accepts
**corrections only**. The rule is enforced mechanically, not by reviewer
vigilance.

## Allowed without an exception (corrections)

- Bug and security fixes to existing code, with a regression test.
- Freeze and retirement changes: disabling writers, refusing `--apply`,
  deleting dead or repair code, lowering ratchet baselines.
- Tests, test infrastructure, and documentation.
- Pinning a published Starter package version and the composition wiring
  needed to consume its contracts.
- Ratchet/baseline decreases that lock in shrinkage.
- Read-only migration evidence already registered as transitional tooling
  (see below).

## Feature-shaped (blocked unless Michael approves an exception)

Enforced by `scripts/ci/check_feature_freeze.py` in the **Feature Freeze
Gate** workflow on every pull request:

- a PR title declaring `feat:` / `feat(...)`;
- any newly added Python module under `app/`;
- any newly added Alembic migration;
- any newly added transitional billing tool (`opening_*`,
  `billing_numbering_*`, `scripts/billing/**`);
- any change to the pinned OpenAPI contract manifest
  (`tests/architecture/openapi_contract_surface.json`).

Billing is additionally pinned by
`tests/architecture/test_billing_thin_client_ratchet.py`: the billing file
inventory is exact, the total line count is two-directional (shrinkage must
lower `billing_thin_client_baseline.txt` in the same change), and
transitional tools must be registered in
`tests/architecture/billing_transitional_tools.json` with a deletion
trigger and must not grow.

### The gate runs from the base branch

The workflow extracts `scripts/ci/check_feature_freeze.py` and
`docs/feature_freeze_exceptions.json` from `origin/<base>` and runs that
script against the PR checkout with `--exceptions` pointing at the base copy.
Consequences: an exception must merge to `main` in its own (code-owned) PR
before the PR it exempts can pass, and changes to the gate itself take
effect only after they merge. A PR cannot weaken the gate or approve its own
exception.

## Legacy one-off repair CLI freeze

`scripts/one_off/soft_delete_failed_payment_reversal_pairs.py`,
`scripts/one_off/repair_billing_date_anchors.py`,
`scripts/one_off/repair_stale_prepaid_billing_anchors.py`, and
`scripts/one_off/align_catalog_vat_flags.py` refuse `--apply` immediately after
argument parsing, before opening a database session or writing reports.
This closes the ledger-pair/memo, billing-anchor, stale prepaid anchor, and
catalog VAT-flag CLI write paths. Existing preview modes remain available.
Static and actual-main session-trap canaries
cover refusal order, preview routing, and removed or late guards.

This is CLI retirement only: historical writer code remains, direct service
calls and other billing writers are not disabled by these guards, and this
change does not establish complete writer retirement or cutover readiness.

## Exceptions

An exception is an entry in `docs/feature_freeze_exceptions.json`,
approved by Michael (`approved_by: "michaelayoade"`) and reviewed through
CODEOWNERS on that file:

| kind | when | extra requirement |
|---|---|---|
| `feature-exception` | new capability truly must land in Sub first | `starter_contract`: the Starter contract it consumes or will extract to |
| `schema` | corrective migration (e.g. a constraining index) | `reason` naming the defect it corrects |
| `transitional-tool` | time-boxed cutover evidence/proof tooling | `deletion_trigger`: the event that deletes it, plus registration in `billing_transitional_tools.json` |
| `approved-growth` | owner-approved net growth of the pinned billing surface with no new capability (e.g. an approved batch landed before this guard) | `reason` citing the approval; the baseline raise is reviewed in the same change |

An exception covers only the `paths` globs it lists. "We'll move it to
Starter later" is not an exception reason; extraction to Starter is the
default path for anything new.

## Owner enforcement (repository settings, post-merge)

CODEOWNERS in this change is advisory until branch protection requires
code-owner reviews. After this change merges, Michael enables, in one
settings update on `main`:

1. `require_code_owner_reviews = true` (guard files, billing surface and
   the OpenAPI manifest then need Michael's approval);
2. `Feature Freeze Gate` added to the required status checks.

Until step 2 lands, the gate still runs and reports on every PR; it just
does not yet block the merge button.
