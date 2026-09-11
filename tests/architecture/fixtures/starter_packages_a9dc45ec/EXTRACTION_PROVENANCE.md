# Provenance of this fixture tree

This directory is an offline `packages_root` for
`composition_schema.derive_distribution_universe` and
`composition_schema.derive_migration_lineage_applicability_from_manifest`
(both mirrored in `tests/architecture/composition_schema.py`). It exists so
`tests/architecture/test_kernel_runtime_composition.py` can round-trip every
record in `docs/kernel-runtime-composition.json` through the real v2 schema
functions offline, in this repository's own CI, which has no access to
`dotmac_starter_mt`'s tree — exactly as that module's own docstring says
`dotmac_starter_mt`'s CI has no access to Sub's or ERP's.

## What is in it, and how it was produced

For every one of the 95 `packages/*/EXTRACTION.toml` directories present in
`dotmac_starter_mt` at protected-main revision
`08a2dae1b1f6510e9d1076ac9dbd6eca0db06137`:

- `<distribution>/EXTRACTION.toml` carries exactly the real `package` name
  and `classification` value read from that revision — nothing invented.
- For every `optional-module` distribution, `<distribution>/src/<import
  package>/manifest.py` is a **byte-for-byte verbatim copy** of the real
  `packages/<distribution>/src/<import package>/manifest.py` at that same
  revision — full source, imports, comments and all. Nothing is normalized,
  substituted, or stripped. This is deliberate, not incidental: an earlier
  version of this fixture used a minimal synthetic `ModuleManifest(...)`
  call whose keyword values were chosen to reproduce whatever lineage
  applicability had already been measured against the real manifest — which
  made the fixture agree with the record it was supposed to check by
  construction, regardless of whether the underlying measurement was
  correct. That is exactly the defect class this v2 contract exists to
  retire (a check built from the answer it verifies, which cannot refuse a
  wrong measurement). Verbatim copies close that hole: fidelity was verified
  mechanically by diffing each of the 80 fixture manifests against its
  Starter original at `08a2dae1` — **zero differ** — and this directory is
  regenerated the same way, never hand-edited or parameterized from a
  conclusion.
  `dotmac-document-rendering`'s manifest is worth naming explicitly: its
  real comment — "Deliberately no short_code, migration prefix, tables or
  plane declaration" — is exactly the text `composition_schema.py`'s own
  module docstring warns would defeat a substring/text scan for
  `"short_code"`. Only a verbatim copy, read by the real AST-based
  `derive_migration_lineage_applicability_from_manifest`, actually exercises
  that distinction; a synthetic stand-in could never have proven it.

`<distribution>/EXTRACTION.toml` remains a minimal, field-only copy (just
`package` and `classification`) rather than the full real TOML file, since
those are the only two fields `derive_distribution_universe` reads and
nothing about EXTRACTION.toml's prose is load-bearing for a text-scan
distinction the way manifest.py's comments are.

## Re-generating this fixture

1. Check out `dotmac_starter_mt` at `08a2dae1b1f6510e9d1076ac9dbd6eca0db06137`
   (or a later revision, if the pin is deliberately moved forward — that is
   a reviewed change, not a silent drift).
2. For each `packages/<dist>/EXTRACTION.toml`, copy its `package` and
   `classification` fields verbatim into
   `tests/architecture/fixtures/starter_packages_08a2dae1/<dist>/EXTRACTION.toml`.
3. For each `optional-module` distribution, copy
   `packages/<dist>/src/<import package>/manifest.py` **byte-for-byte,
   unmodified** into
   `tests/architecture/fixtures/starter_packages_08a2dae1/<dist>/src/<import package>/manifest.py`.
   Verify with `diff` against the source that zero files differ before
   committing the regenerated fixture.
4. Update `PINNED_STARTER_REVISION` in
   `tests/architecture/test_kernel_runtime_composition.py` and this file's
   cited revision together, in the same change.

This directory's *size* (95 today, 80 with a manifest) is never asserted as
a literal anywhere — `test_catalogue_is_the_full_product_independent_universe`
derives it from `derive_distribution_universe(FIXTURE_ROOT)` at test time.
