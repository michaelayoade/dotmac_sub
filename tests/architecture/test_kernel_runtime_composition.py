"""Validates docs/kernel-runtime-composition.json against
``dimensional-composition.v2`` (composition_schema.py, mirrored byte-for-byte
in this directory from dotmac_starter_mt's protected main at
``08a2dae1b1f6510e9d1076ac9dbd6eca0db06137`` — see that module's own
docstring for the schema this file enforces).

This module never imports dotmac_starter_mt; it is a same-repository mirror,
per the outcome brief that produced it ("Mirror it; do not import across
repositories").

Cross-repository catalogue completeness cannot be re-verified live in this
repository's CI — Sub's CI has no access to dotmac_starter_mt's tree, exactly
as composition_schema.py's own docstring says Starter's CI has no access to
Sub's or ERP's. ``tests/architecture/fixtures/starter_packages_08a2dae1/``
is therefore a frozen offline snapshot of every ``packages/*/EXTRACTION.toml``
(a minimal, field-only copy: just ``package`` and ``classification``) and,
for every ``optional-module`` distribution, a byte-for-byte VERBATIM copy of
the real ``manifest.py`` at that pinned revision — not a synthetic
stand-in, and not normalized (see ``EXTRACTION_PROVENANCE.md`` in that
fixture directory for exactly why a synthetic manifest was rejected and how
this one is regenerated). Re-generating it requires re-reading Starter's
tree at the same pinned revision; nothing here re-derives it from a live
cross-repository fetch.
"""

from __future__ import annotations

import json
from pathlib import Path

import composition_schema as cs

RECORD_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "kernel-runtime-composition.json"
)
FIXTURE_ROOT = (
    Path(__file__).resolve().parent / "fixtures" / "starter_packages_08a2dae1"
)

#: The exact protected-main revision every record must cite. Not the local
#: branch head this record was rebuilt from (845e0a62...) — that was the
#: superseded record's mistake, repointed here.
PINNED_STARTER_REVISION = "08a2dae1b1f6510e9d1076ac9dbd6eca0db06137"

#: The six distributions Sub's own commercial composition slice concerns
#: itself with, and their fully-measured dimensions. Re-asserted explicitly
#: (not just round-tripped generically) so a silent regression on any one of
#: these load-bearing rows is named rather than merged into "95 records
#: passed."
COMMERCIAL_DISTRIBUTIONS = (
    "dotmac-billing",
    "dotmac-collections",
    "dotmac-inbox",
    "dotmac-payments",
    "dotmac-service-orders",
    "dotmac-subscriptions",
)


def _load_records() -> dict:
    payload = json.loads(RECORD_PATH.read_text())
    return payload


def test_record_file_declares_v2_only() -> None:
    payload = _load_records()
    assert payload["schema_version"] == cs.CURRENT_SCHEMA_VERSION
    for record in payload["records"]:
        assert record["schema_version"] == cs.CURRENT_SCHEMA_VERSION


def test_envelope_shape_matches_the_one_cross_product_gate_reads() -> None:
    """Academy, ERP and Sub's records are read by ONE gate: the envelope
    shape (not just the row shape) must be identical across all three, or a
    gate that can read one product's file cannot read another's — the exact
    defect this contract exists to retire, reappearing at the envelope
    level. The agreed shape: `schema_version`, `product` (this repository's
    directory name, mandatory), `starter_catalogue_revision` (the full
    40-character pinned Starter SHA, mandatory, never abbreviated), and
    `records` — nothing else. No stored count (`catalogue_size` or
    equivalent) anywhere: the count is `len(records)`, and a stored copy of
    it can drift from the thing it describes."""
    payload = _load_records()
    assert set(payload.keys()) == {
        "schema_version",
        "product",
        "starter_catalogue_revision",
        "records",
    }
    assert payload["product"] == "dotmac_sub"
    assert isinstance(payload["starter_catalogue_revision"], str)
    assert len(payload["starter_catalogue_revision"]) == 40, (
        "starter_catalogue_revision must be the full, unabbreviated SHA"
    )
    assert payload["starter_catalogue_revision"] == PINNED_STARTER_REVISION
    # The superseded record's mistake, named so it can never silently
    # reappear: a local branch head cited as if it were protected main.
    assert (
        payload["starter_catalogue_revision"]
        != "845e0a6265075fbbc58489527c0ad34eac239287"
    )
    for forbidden_count_key in ("catalogue_size", "count", "total", "record_count"):
        assert forbidden_count_key not in payload, (
            f"{forbidden_count_key!r} is a stored count that can drift from "
            "len(records) — never store one"
        )


def test_no_derived_or_legacy_field_on_any_record() -> None:
    payload = _load_records()
    for record in payload["records"]:
        for forbidden in (*cs._DERIVED_ONLY_FIELDS, "composed_distributions"):
            assert forbidden not in record, (
                f"{record['distribution']} carries derived/legacy field "
                f"{forbidden!r} — a state is computed, never stored"
            )


def test_every_record_round_trips_through_the_real_v2_schema() -> None:
    """Every record must construct a coherent CompositionRecord and derive
    a CompositionState with no exception — this exercises the exact same
    classification/NOT_APPLICABLE invariants, contradiction refusals, and
    derivation pipeline the schema's own author built, not a re-implemented
    approximation of them."""
    payload = _load_records()
    for record in payload["records"]:
        built = cs.composition_record_from_payload(record, FIXTURE_ROOT)
        cs.derive_composition_state(built)  # must not raise


def test_catalogue_is_the_full_product_independent_universe() -> None:
    """Every packages/*/EXTRACTION.toml distribution at the pinned revision
    must have exactly one record — derived from the fixture snapshot at test
    time, never a hard-coded count or name list."""
    payload = _load_records()
    recorded = {r["distribution"] for r in payload["records"]}
    assert len(recorded) == len(payload["records"]), (
        "a distribution is recorded more than once"
    )

    universe = {d.distribution for d in cs.derive_distribution_universe(FIXTURE_ROOT)}
    assert recorded == universe


def test_installation_false_never_carries_positive_evidence() -> None:
    """Contradiction canary (pipeline step 3): a distribution recorded as
    not installed can never also report a positive registration, lineage,
    or runtime-consumption fact."""
    payload = _load_records()
    for record in payload["records"]:
        if record["installation"] != "false":
            continue
        for dim in ("module_registration", "migration_lineage", "runtime_consumption"):
            assert record[dim] != "true", (
                f"{record['distribution']}: installation is false but {dim} "
                "reports true"
            )


def test_platform_baseline_distributions_are_not_applicable_for_module_dimensions() -> (
    None
):
    payload = _load_records()
    by_dist = {r["distribution"]: r for r in payload["records"]}
    for distribution in ("dotmac-kernel", "dotmac-ui"):
        record = by_dist[distribution]
        assert record["module_registration"] == "not_applicable"
        assert record["migration_lineage"] == "not_applicable"


def test_commercial_distributions_are_measured_lineage_only_not_registered() -> None:
    """The six distributions this record is load-bearing for: installed,
    lineage present in alembic.ini's version_locations, but NEVER registered
    through Sub's boot-consumed assembly — Sub's sole ProductAssemblySpec
    call site (app/composition.py) passes four Sub-built FeatureManifest
    values, not a package ModuleManifest."""
    payload = _load_records()
    by_dist = {r["distribution"]: r for r in payload["records"]}
    for distribution in COMMERCIAL_DISTRIBUTIONS:
        record = by_dist[distribution]
        assert record["installation"] == "true", distribution
        assert record["module_registration"] == "false", distribution
        assert record["migration_lineage"] == "true", distribution
        built = cs.composition_record_from_payload(record, FIXTURE_ROOT)
        assert cs.derive_composition_state(built) is cs.CompositionState.LINEAGE_ONLY, (
            distribution
        )


def test_collections_is_the_one_true_runtime_consumption_among_the_six() -> None:
    """dotmac-collections is the sole commercial distribution whose
    runtime_consumption is true, and only because a supported operator
    entry point (scripts/migration/collections_module_shadow_parity.py)
    imports app/services/collections_module_shadow.py, which imports
    ReceivableObservationV1 from dotmac_collections directly. This is a
    measured production import chain, not an inference from installation,
    registration, or lineage."""
    payload = _load_records()
    by_dist = {r["distribution"]: r for r in payload["records"]}
    assert by_dist["dotmac-collections"]["runtime_consumption"] == "true"
    for distribution in COMMERCIAL_DISTRIBUTIONS:
        if distribution == "dotmac-collections":
            continue
        assert by_dist[distribution]["runtime_consumption"] == "false", distribution


def test_inbox_runtime_consumption_is_measured_by_reachability_not_docstring() -> None:
    """app/services/inbox_channels.py's own docstring claims nothing under
    app/ imports it — that claim must not be taken on trust. Only tests
    import it; nothing under app/ does, so runtime_consumption is false,
    established independently of the docstring."""
    payload = _load_records()
    by_dist = {r["distribution"]: r for r in payload["records"]}
    assert by_dist["dotmac-inbox"]["runtime_consumption"] == "false"


def test_a_v1_tagged_or_legacy_payload_is_refused() -> None:
    """Sensitivity proof (planted defect 1): a payload declaring either the
    legacy v0 tag or the superseded v1 tag must be refused outright, never
    translated or partially read."""
    payload = _load_records()
    sample = dict(payload["records"][0])
    for legacy_tag in (cs.LEGACY_SCHEMA_VERSION_V0, cs.LEGACY_SCHEMA_VERSION_V1):
        mutated = {**sample, "schema_version": legacy_tag}
        try:
            cs.composition_record_from_payload(mutated, FIXTURE_ROOT)
        except cs.IncompatibleSchemaVersion:
            continue
        raise AssertionError(f"payload tagged {legacy_tag!r} was wrongly accepted")


def test_an_optional_module_cannot_fake_not_applicable_registration() -> None:
    """Sensitivity proof (planted defect 2): an optional-module distribution
    recording module_registration as not_applicable must be refused — a
    product's own missing registration mechanism is FALSE, never
    NOT_APPLICABLE."""
    payload = _load_records()
    optional_module_record = next(
        r for r in payload["records"] if r["classification"] == "optional-module"
    )
    mutated = {**optional_module_record, "module_registration": "not_applicable"}
    try:
        cs.composition_record_from_payload(mutated, FIXTURE_ROOT)
    except ValueError:
        return
    raise AssertionError(
        "an optional-module distribution wrongly accepted "
        "module_registration=not_applicable"
    )


def test_a_genuinely_stateless_optional_module_is_accepted_not_refused() -> None:
    """Near-miss: dotmac-document-rendering is a real, legitimate
    optional-module whose manifest declares neither short_code nor
    migration_prefix nor any lineage-bearing signal — migration_lineage is
    correctly not_applicable for it, and this must be ACCEPTED (not treated
    as though it were the planted defect above)."""
    payload = _load_records()
    by_dist = {r["distribution"]: r for r in payload["records"]}
    record = by_dist["dotmac-document-rendering"]
    assert record["classification"] == "optional-module"
    assert record["migration_lineage"] == "not_applicable"
    built = cs.composition_record_from_payload(record, FIXTURE_ROOT)
    assert cs.derive_composition_state(built) is cs.CompositionState.NOT_COMPOSED


def test_no_scalar_total_or_state_field_anywhere_in_the_record_file() -> None:
    payload = _load_records()
    for record in payload["records"]:
        assert "state" not in record
        assert "fully_composed" not in record
