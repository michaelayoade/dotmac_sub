# Cross-repository engineering conformance profiles

Two files, one of which is live.

## `standards-profile.json` — LIVE

Schema 5. Evaluated on every pull request by the `Dotmac engineering standards`
required check (`.github/workflows/engineering-standards.yml`), which runs the
Governance conformance engine at the exact accepted revision this file pins.
Nothing about connector surfaces is in it, because schema 5 has no such object.

## `standards-profile.next.json` — STAGED, NOT LIVE

Schema 6: the same profile plus the mandatory `external_connector_surface`
object introduced by dotmac_governance **ADR 0010**, which measures and ratchets
the direct provider clients, provider credentials, provider callbacks, connector
schedules, feed checkpoints and delivery-retry machinery still living in Sub's
own application runtime.

**ADR 0010 is `Proposed`. It is neither approved by Michael Ayoade nor merged to
dotmac_governance canonical `main`, so there is no revision to pin.**
`governance_model.revision` therefore holds the literal `PENDING-APPROVAL`,
which the Governance profile schema refuses (`^[0-9a-f]{40}$`). That refusal is
the point: this file must not be loadable, and no job may report green against
it. `.github/workflows/external-connector-ratchet.yml` carries the same
placeholder and fails closed for the same reason.

### Why the schema-6 object is not simply merged into the live profile

The live profile is read by the *accepted* engine, which is a schema-5 parser
and rejects `schema_version: 6` outright. Migrating the live file today would
take the required check down across the whole repository. ADR 0010 says the same
thing from the other side: a repository migrates to schema 6 *in the same change
that repins the accepted Governance commit*.

### What enforces the baseline in the meantime

`tests/architecture/test_external_connector_ratchet.py`, which runs in Sub's own
`architecture` CI job on every pull request and reads its thresholds from
`external_connector_surface.baselines` in this staged file. There is exactly one
set of numbers in the repository; the test fails if both profiles ever declare
the object, so a second threshold cannot appear.

This local gate exists because the fleet-wide sweep in
`dotmac_starter_mt/scripts/external_connector_sweep.py` measures sibling
repositories from one checkout and reports `UNMEASURED` for `dotmac_sub` in
starter CI, where no `dotmac_sub` checkout exists. A central sweep must never be
the only gate.

## Landing the staged profile — one change, five steps

1. ADR 0010 is accepted and its carrying revision merges to dotmac_governance
   canonical `main`. Record the 40-hex commit.
2. Replace `PENDING-APPROVAL` in `standards-profile.next.json` and in
   `.github/workflows/external-connector-ratchet.yml` with that commit.
3. Repin `.github/workflows/engineering-standards.yml` to the same commit.
4. Move `standards-profile.next.json` over `standards-profile.json` and delete
   the staged file, so exactly one profile declares the connector surface.
5. Give `external-connector-ratchet.yml` its `pull_request` and `merge_group`
   triggers and make the check required. Until step 2 it is
   `workflow_dispatch`-only on purpose: a permanently red required check
   deadlocks the merge queue rather than protecting anything, and
   `tests/architecture/test_external_connector_ratchet.py` enforces that
   coupling in both directions.
