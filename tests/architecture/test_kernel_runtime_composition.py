"""Validates docs/kernel-runtime-composition.json against
``dimensional-composition.v2`` (composition_schema.py, mirrored byte-for-byte
in this directory from dotmac_starter_mt's protected main at
``a9dc45ecd00d5a0163b6544278888220082e2e75`` — see that module's own
docstring for the schema this file enforces).

This module never imports dotmac_starter_mt; it is a same-repository mirror,
per the outcome brief that produced it ("Mirror it; do not import across
repositories").

Cross-repository catalogue completeness cannot be re-verified live in this
repository's CI — Sub's CI has no access to dotmac_starter_mt's tree, exactly
as composition_schema.py's own docstring says Starter's CI has no access to
Sub's or ERP's. ``tests/architecture/fixtures/starter_packages_a9dc45ec/`` is
therefore a frozen offline snapshot of every ``packages/*/EXTRACTION.toml``
(a minimal, field-only copy: just ``package`` and ``classification``) and,
for every ``optional-module`` distribution, a byte-for-byte VERBATIM copy of
the real ``manifest.py`` at that pinned revision — not a synthetic
stand-in, and not normalized (see ``EXTRACTION_PROVENANCE.md`` in that
fixture directory for exactly why a synthetic manifest was rejected, how
this one is regenerated, and why a re-pin from 08a2dae1 to a9dc45ec only
relabelled the directory rather than recopying it — ``packages/`` is
byte-identical between those two revisions, verified by ``git diff``).

This module ALSO derives two dimensions independently, against the real
contract functions, rather than merely round-tripping asserted values:

* ``installation`` via :func:`composition_schema.derive_lock_group_membership`
  on this repository's real ``poetry.lock``, :func:`composition_schema.
  derive_group_optionality` on its real ``pyproject.toml``, and
  :func:`composition_schema.parse_install_command` on every checked-in,
  deployed install recipe this repository actually has (see
  ``test_installation_is_derived_from_every_real_deployed_recipe`` for the
  recipe inventory and how it was searched for).
* ``runtime_consumption`` via a real AST ``Import``/``ImportFrom`` reachability
  graph from this repository's declared production entry points (see
  ``_build_production_import_graph`` and
  ``test_runtime_consumption_is_derived_from_the_real_ast_import_graph``) —
  never a substring match, and never taken on the word of a module's own
  docstring (``test_inbox_runtime_consumption_is_measured_by_reachability_
  not_docstring`` exists precisely because a docstring's claim about its own
  reachability is not evidence of it).

If either derivation disagrees with the stored record for any distribution,
the derivation wins and the test fails naming the row — there is no
fallback to the stored value.
"""

from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

import composition_schema as cs

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORD_PATH = REPO_ROOT / "docs" / "kernel-runtime-composition.json"
FIXTURE_ROOT = (
    Path(__file__).resolve().parent / "fixtures" / "starter_packages_a9dc45ec"
)

#: The exact protected-main revision every record must cite. Not the local
#: branch head this record was rebuilt from — that was the superseded
#: record's mistake, repointed here, twice now (845e0a62... then 08a2dae1...).
PINNED_STARTER_REVISION = "a9dc45ecd00d5a0163b6544278888220082e2e75"

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

#: Sub's own production entry points this test walks an AST import graph
#: from, as dotted module names resolvable under REPO_ROOT. `app.main` is
#: the web/API process; `app.celery_app` is the worker/beat process (all of
#: `celery-worker*`/`celery-beat` in docker-compose.yml run the identical
#: `${APP_IMAGE}` and the identical `celery -A app.celery_app.celery_app`
#: command — one image, one entry point, not one per queue);
#: `scripts.migration.collections_module_shadow_parity` is the one checked-in,
#: supported operator/migration entry point this repository has that reaches
#: outside `app/` — found by a real search (see
#: ``test_installation_is_derived_from_every_real_deployed_recipe``'s sibling
#: search for recipes; the same repository-wide grep for a second operator
#: entry point beyond this one script found none). All three run from the
#: SAME deployed image/profile (the one recipe below), so there is no
#: entry-point/profile misalignment to reconcile here — a product with more
#: than one deployed profile would need to pair each root with the profile
#: that actually supplies it; Sub does not have that shape today.
_PRODUCTION_ENTRY_POINT_ROOTS = ("app.main", "app.celery_app")
_OPERATOR_ENTRY_POINT_ROOTS = ("scripts.migration.collections_module_shadow_parity",)

#: Local top-level packages this repository's own import graph may traverse
#: into. Anything outside this set (a third-party or `dotmac-*` distribution
#: import) is an external leaf recorded by `_build_production_import_graph`,
#: never followed further — this repository does not vendor dependency
#: source, so there is nothing under these two directories to walk past.
_LOCAL_IMPORT_ROOTS = frozenset({"app", "scripts"})


def _load_records() -> dict:
    payload = json.loads(RECORD_PATH.read_text())
    return payload


def _load_envelope_records() -> tuple:
    """The one reader every record in this file is parsed through —
    ``composition_schema.composition_records_from_envelope`` — rather than a
    locally hand-rolled envelope check. Academy, ERP, and Sub all read
    through this same function so a gate that can read one product's
    envelope can read any of theirs; see that function's own docstring."""
    payload = _load_records()
    return cs.composition_records_from_envelope(payload, FIXTURE_ROOT)


# ---------------------------------------------------------------------------
# Independent re-derivation: installation, from the real lock + recipe(s)
# ---------------------------------------------------------------------------


def _module_name_for_path(base: Path, file_path: Path) -> str:
    rel = file_path.relative_to(base)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _build_local_module_index(base: Path, tops: frozenset[str]) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for top in tops:
        top_path = base / top
        if not top_path.is_dir():
            continue
        for py_file in top_path.rglob("*.py"):
            index[_module_name_for_path(base, py_file)] = py_file
    return index


def _resolve_relative_import(
    current_module: str, node: ast.ImportFrom, current_is_package: bool
) -> str | None:
    parts = current_module.split(".")
    base_len = len(parts) if current_is_package else len(parts) - 1
    up = node.level - 1
    truncated_len = base_len - up
    if truncated_len < 0:
        return None
    anchor = parts[:truncated_len]
    if node.module:
        anchor = anchor + node.module.split(".")
    if not anchor:
        return None
    return ".".join(anchor)


def _discover_celery_autodiscover_roots(celery_app_path: Path) -> tuple[str, ...]:
    """AST-read `app/celery_app.py`'s real `...autodiscover_tasks([...])`
    call and return its string-literal arguments as additional BFS roots —
    the one dynamic-import shape Celery's own autodiscovery requires, read
    from the real `Call` node rather than assumed."""
    tree = ast.parse(celery_app_path.read_text())
    roots: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "autodiscover_tasks"
        ):
            for arg in node.args:
                if isinstance(arg, ast.List):
                    for elt in arg.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            roots.append(elt.value)
    return tuple(roots)


def _build_production_import_graph(
    repo_root: Path, root_modules: tuple[str, ...]
) -> tuple[set[str], dict[str, str]]:
    """BFS from `root_modules` over this repository's own `app/` and
    `scripts/` trees, following real `Import`/`ImportFrom` AST nodes
    (including relative imports) — never a substring/text match. Returns
    `(external_top_level_import_names_reached, {name: an_example_reaching_module})`.
    A local (`app.*`/`scripts.*`) import is followed further; anything else
    is recorded as an external leaf, since this repository does not vendor
    third-party or `dotmac-*` source under `app/`/`scripts/`."""
    index = _build_local_module_index(repo_root, _LOCAL_IMPORT_ROOTS)
    visited: set[str] = set()
    queue = list(dict.fromkeys(m for m in root_modules if m))
    external: set[str] = set()
    external_source: dict[str, str] = {}

    while queue:
        module = queue.pop()
        if module in visited or module not in index:
            continue
        visited.add(module)
        file_path = index[module]
        is_package = file_path.name == "__init__.py"
        tree = ast.parse(file_path.read_text())

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in _LOCAL_IMPORT_ROOTS:
                        if alias.name not in visited:
                            queue.append(alias.name)
                    else:
                        external.add(top)
                        external_source.setdefault(top, module)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    target = _resolve_relative_import(module, node, is_package)
                    if (
                        target
                        and target.split(".")[0] in _LOCAL_IMPORT_ROOTS
                        and target not in visited
                    ):
                        queue.append(target)
                    continue
                if node.module is None:
                    continue
                top = node.module.split(".")[0]
                if top in _LOCAL_IMPORT_ROOTS:
                    if node.module not in visited:
                        queue.append(node.module)
                else:
                    external.add(top)
                    external_source.setdefault(top, module)

    return external, external_source


def _find_dockerfile_poetry_run_instructions(dockerfile_text: str) -> tuple[str, ...]:
    """Locate, BY CONTENT, every `RUN` instruction in `dockerfile_text`
    whose (possibly multi-line, backslash-continued) body contains `poetry
    install` or `poetry sync` — never by a hand-copied line number. Returns
    each instruction's full, joined-by-newline raw text, exactly as
    `parse_install_command` expects to receive it."""
    lines = dockerfile_text.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("RUN"):
            block_lines = [line]
            j = i
            while block_lines[-1].rstrip().endswith("\\"):
                j += 1
                block_lines.append(lines[j])
            block = "\n".join(block_lines)
            if "poetry install" in block or "poetry sync" in block:
                blocks.append(block)
            i = j + 1
        else:
            i += 1
    return tuple(blocks)


def _this_repository_deployed_recipes() -> tuple[cs.InstallRecipe, ...]:
    """Every checked-in, DEPLOYED install recipe this repository actually
    has, found by a real search rather than assumed. The search covered:
    every `Dockerfile`/`Containerfile` under this repository (`Dockerfile`,
    `docker/genieacs/Dockerfile`, `deploy/egress-proxy/Containerfile`,
    `examples/connectors/echo/Containerfile`); every docker-compose service
    definition (`docker-compose.yml`, `docker-compose.dev.yml`) for a
    `build:`/`dockerfile:` key naming a recipe other than the root
    `Dockerfile`; and every `.github/workflows/*.yml` step invoking `poetry
    install`/`poetry sync`.

    The result: exactly ONE deployed recipe exists — the root `Dockerfile`'s
    `poetry install --only main --no-interaction --no-ansi` line. Every
    compose service that runs application code (`app`, every
    `celery-worker*`, `celery-beat`) names the identical
    `${APP_IMAGE}` built from this one `Dockerfile`; `docker-compose.dev.yml`
    restores `build: .` against that SAME `Dockerfile` for local dev, not a
    second recipe. The two other `Dockerfile`/`Containerfile`s found
    (`docker/genieacs/Dockerfile`: Node.js, no Poetry at all;
    `deploy/egress-proxy/Containerfile`: Alpine + tinyproxy, no Poetry) and
    the example connector (`examples/connectors/echo/Containerfile`: no
    Poetry) are not Python/Poetry recipes and carry nothing to parse. The
    `.github/workflows/{ci,e2e,e2e-gate}.yml` `poetry install --no-interaction`
    steps are CI/test tooling installs, not the product's deployed artifact —
    they are not read here.

    This is an honest inventory of ONE recipe, not a fabricated count: an
    earlier draft of this schema's own module docstring asserted a specific
    multi-recipe count for this repository without a real search backing it,
    and that assertion had to be retracted (see composition_schema.py's own
    "The union across every deployed profile" section, F2). A future
    reviewer who adds a second genuinely deployed recipe (a worker image
    built from its own Dockerfile, say) must add it to this tuple in the
    same change that adds the recipe itself — not assume this function
    already covers it.
    """
    dockerfile_text = (REPO_ROOT / "Dockerfile").read_text()
    blocks = _find_dockerfile_poetry_run_instructions(dockerfile_text)
    assert len(blocks) == 1, (
        f"expected exactly one poetry install/sync RUN instruction in "
        f"Dockerfile, found {len(blocks)} — update this function's "
        "docstring and the recipe tuple below if that count has genuinely "
        "changed"
    )
    return tuple(
        cs.parse_install_command(block, source="Dockerfile") for block in blocks
    )


def _derive_installation_for_every_distribution() -> dict[str, cs.DimensionValue]:
    with open(REPO_ROOT / "poetry.lock", "rb") as handle:
        lock_document = tomllib.load(handle)
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        pyproject_document = tomllib.load(handle)

    lock_membership = cs.derive_lock_group_membership(lock_document)
    group_optionality = cs.derive_group_optionality(pyproject_document)
    recipes = _this_repository_deployed_recipes()

    payload = _load_records()
    return {
        record["distribution"]: cs.derive_installation_dimension(
            distribution=record["distribution"],
            lock_membership=lock_membership,
            recipes=recipes,
            group_optionality=group_optionality,
        )
        for record in payload["records"]
    }


def test_record_file_declares_v2_only() -> None:
    payload = _load_records()
    assert payload["schema_version"] == cs.CURRENT_SCHEMA_VERSION
    for record in payload["records"]:
        assert record["schema_version"] == cs.CURRENT_SCHEMA_VERSION


def test_envelope_parses_through_the_one_cross_product_reader() -> None:
    """Academy, ERP and Sub's records are read by ONE gate —
    ``composition_records_from_envelope`` — not three independently
    hand-rolled envelope checks. This repository's own local envelope-shape
    assertions are retired in favour of calling that function directly: if
    it raises, the envelope is wrong; if it returns, the envelope (including
    ``starter_catalogue_revision``'s shape, ``product``'s non-emptiness, the
    absence of a stored count, and every row's own closed shape) was already
    proven coherent by the shared contract, not re-proven here."""
    records = _load_envelope_records()
    assert len(records) == len(_load_records()["records"])
    for record in records:
        assert record.product == "dotmac_sub"

    payload = _load_records()
    assert payload["starter_catalogue_revision"] == PINNED_STARTER_REVISION
    assert len(payload["starter_catalogue_revision"]) == 40
    # The superseded record's mistakes, named so neither can silently
    # reappear: a local branch head, then an earlier Starter pin, cited as
    # if either were the current protected-main revision.
    assert payload["starter_catalogue_revision"] not in (
        "845e0a6265075fbbc58489527c0ad34eac239287",
        "08a2dae1b1f6510e9d1076ac9dbd6eca0db06137",
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
    """Every record, read through the shared envelope function, must derive
    a CompositionState with no exception — this exercises the exact same
    classification/NOT_APPLICABLE invariants, contradiction refusals, and
    derivation pipeline the schema's own author built, not a re-implemented
    approximation of them."""
    for record in _load_envelope_records():
        cs.derive_composition_state(record)  # must not raise


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


# ---------------------------------------------------------------------------
# Independent re-derivation tests (not a round-trip of the stored value)
# ---------------------------------------------------------------------------


def test_installation_is_derived_from_every_real_deployed_recipe() -> None:
    """Re-derives `installation` for every recorded distribution from this
    repository's own real `poetry.lock`, `pyproject.toml`, and every
    checked-in, deployed install recipe found by
    `_this_repository_deployed_recipes` (exactly one today — see that
    function's docstring for the search that established this and why it is
    not assumed). If the derivation disagrees with any stored value, the
    derivation wins and this test names the row; it never falls back to
    "the record says so"."""
    derived = _derive_installation_for_every_distribution()
    payload = _load_records()
    mismatches = [
        (r["distribution"], r["installation"], derived[r["distribution"]].value)
        for r in payload["records"]
        if derived[r["distribution"]].value != r["installation"]
    ]
    assert not mismatches, (
        f"installation re-derived from the real lock/recipe disagrees with "
        f"the stored record for: {mismatches!r} — the derivation wins; "
        "update docs/kernel-runtime-composition.json"
    )
    # None of the 95 catalogued distributions can be left UNKNOWN by a
    # working derivation — every one resolves to TRUE or FALSE here.
    assert all(value is not cs.DimensionValue.UNKNOWN for value in derived.values())


def test_runtime_consumption_is_derived_from_the_real_ast_import_graph() -> None:
    """Re-derives `runtime_consumption` for every recorded distribution from
    a real AST `Import`/`ImportFrom` reachability graph rooted at this
    repository's declared production entry points — `app.main`,
    `app.celery_app` (+ its real `autodiscover_tasks([...])` argument), and
    the one supported operator entry point,
    `scripts.migration.collections_module_shadow_parity`. If the graph
    disagrees with any stored value, the graph wins and this test names the
    row; it never falls back to the stored value or to a module's own
    docstring claim about its own reachability."""
    autodiscover_roots = _discover_celery_autodiscover_roots(
        REPO_ROOT / "app" / "celery_app.py"
    )
    root_modules = (
        _PRODUCTION_ENTRY_POINT_ROOTS + autodiscover_roots + _OPERATOR_ENTRY_POINT_ROOTS
    )
    external, external_source = _build_production_import_graph(REPO_ROOT, root_modules)

    payload = _load_records()
    mismatches = []
    for record in payload["records"]:
        import_package = record["distribution"].replace("-", "_")
        derived_true = import_package in external
        derived = "true" if derived_true else "false"
        if derived != record["runtime_consumption"]:
            mismatches.append(
                (
                    record["distribution"],
                    record["runtime_consumption"],
                    derived,
                    external_source.get(import_package),
                )
            )
    assert not mismatches, (
        f"runtime_consumption re-derived from the real AST import graph "
        f"disagrees with the stored record for: {mismatches!r} — the graph "
        "wins; update docs/kernel-runtime-composition.json"
    )


def test_collections_import_chain_is_a_real_ast_edge_from_an_operator_entry_point() -> (
    None
):
    """dotmac-collections is the sole commercial distribution whose
    runtime_consumption is true, and only because the supported operator
    entry point `scripts/migration/collections_module_shadow_parity.py`
    imports `app/services/collections_module_shadow.py` (a real
    `ImportFrom` node: `from app.services.collections_module_shadow import
    postpaid_eligibility_parity_report`), which itself imports
    `ReceivableObservationV1` from `dotmac_collections` directly (`from
    dotmac_collections import ReceivableObservationV1`). This is a measured
    production import chain read by AST from both files, not an inference
    from installation, registration, or lineage, and not a round-trip of a
    previously asserted value."""
    payload = _load_records()
    by_dist = {r["distribution"]: r for r in payload["records"]}
    assert by_dist["dotmac-collections"]["runtime_consumption"] == "true"
    for distribution in COMMERCIAL_DISTRIBUTIONS:
        if distribution == "dotmac-collections":
            continue
        assert by_dist[distribution]["runtime_consumption"] == "false", distribution

    parity_script = ast.parse(
        (
            REPO_ROOT / "scripts" / "migration" / "collections_module_shadow_parity.py"
        ).read_text()
    )
    imports_shadow_module = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.services.collections_module_shadow"
        for node in ast.walk(parity_script)
    )
    assert imports_shadow_module, (
        "scripts/migration/collections_module_shadow_parity.py no longer "
        "imports app.services.collections_module_shadow — the collections "
        "runtime_consumption chain is broken at its first hop"
    )

    shadow_module = ast.parse(
        (REPO_ROOT / "app" / "services" / "collections_module_shadow.py").read_text()
    )
    imports_receivable_observation = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "dotmac_collections"
        and any(alias.name == "ReceivableObservationV1" for alias in node.names)
        for node in ast.walk(shadow_module)
    )
    assert imports_receivable_observation, (
        "app/services/collections_module_shadow.py no longer imports "
        "ReceivableObservationV1 from dotmac_collections — the collections "
        "runtime_consumption chain is broken at its second hop"
    )


def test_inbox_runtime_consumption_is_measured_by_reachability_not_docstring() -> None:
    """app/services/inbox_channels.py's own docstring claims nothing under
    app/ imports it — that claim must not be taken on trust. The real AST
    graph in test_runtime_consumption_is_derived_from_the_real_ast_import_
    graph confirms dotmac_inbox is unreached from any production entry
    point; this test re-asserts the stored value agrees, independently of
    that docstring."""
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


# ---------------------------------------------------------------------------
# Provenance plant: a mutated recipe is detected against the unchanged record
# ---------------------------------------------------------------------------


def test_a_stale_record_against_a_mutated_recipe_is_reported_as_a_mismatch(
    tmp_path: Path,
) -> None:
    """Sensitivity proof for the installation derivation itself (planted
    defect 3, the motivating case this contract's `installation` ruling
    exists for): copy this repository's REAL `Dockerfile` to a temp path,
    rewrite its one `poetry install` line to select a different profile
    (`--only dev` instead of `--only main`), and re-derive `installation`
    for the eight distributions that are `main`-only in the real lock and
    currently recorded `installation: true`
    (`dotmac-auth-oidc`/`dotmac-billing`/`dotmac-collections`/`dotmac-inbox`/
    `dotmac-payments`/`dotmac-service-orders`/`dotmac-subscriptions`/
    `dotmac-ui`; `dotmac-kernel` is deliberately excluded — it resolves into
    BOTH `main` and `dev`, so it would stay `true` under either profile and
    could not demonstrate a mismatch). Against the MUTATED recipe, every one
    of those eight derives `false` (none of them resolve into the `dev`
    group) while the STORED record still says `true` for all eight — a real,
    detected disagreement between a stale record and the recipe that now
    governs it.

    The near-miss half lives in
    `test_installation_is_derived_from_every_real_deployed_recipe` above:
    the SAME derivation, against the UNMUTATED real `Dockerfile`, agrees
    with every one of these eight records (and all 87 others) — so this
    proof demonstrates both that the check refuses a real disagreement and
    that it does not refuse the honest, unmutated case it exists to accept.
    """
    real_dockerfile_text = (REPO_ROOT / "Dockerfile").read_text()
    (blocks := _find_dockerfile_poetry_run_instructions(real_dockerfile_text))
    assert len(blocks) == 1
    real_block = blocks[0]
    assert "--only main" in real_block, (
        "the real Dockerfile's recipe no longer reads '--only main' — "
        "update this planted mutation to match the real shape it is "
        "supposed to differ from"
    )
    mutated_block = real_block.replace("--only main", "--only dev")

    mutated_dockerfile = tmp_path / "Dockerfile"
    mutated_dockerfile.write_text(
        real_dockerfile_text.replace(real_block, mutated_block)
    )

    mutated_blocks = _find_dockerfile_poetry_run_instructions(
        mutated_dockerfile.read_text()
    )
    assert len(mutated_blocks) == 1
    mutated_recipe = cs.parse_install_command(
        mutated_blocks[0], source=str(mutated_dockerfile)
    )

    with open(REPO_ROOT / "poetry.lock", "rb") as handle:
        lock_document = tomllib.load(handle)
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        pyproject_document = tomllib.load(handle)
    lock_membership = cs.derive_lock_group_membership(lock_document)
    group_optionality = cs.derive_group_optionality(pyproject_document)

    main_only_installed_distributions = (
        "dotmac-auth-oidc",
        "dotmac-billing",
        "dotmac-collections",
        "dotmac-inbox",
        "dotmac-payments",
        "dotmac-service-orders",
        "dotmac-subscriptions",
        "dotmac-ui",
    )
    payload = _load_records()
    by_dist = {r["distribution"]: r for r in payload["records"]}

    reported_mismatches = []
    for distribution in main_only_installed_distributions:
        stored = by_dist[distribution]["installation"]
        assert stored == "true", (distribution, stored)
        under_mutation = cs.derive_installation_dimension(
            distribution=distribution,
            lock_membership=lock_membership,
            recipes=(mutated_recipe,),
            group_optionality=group_optionality,
        )
        if under_mutation.value != stored:
            reported_mismatches.append((distribution, stored, under_mutation.value))

    assert len(reported_mismatches) == len(main_only_installed_distributions), (
        "the mutated recipe (--only dev instead of --only main) was "
        f"expected to disagree with the stale, unchanged record for all "
        f"{len(main_only_installed_distributions)} main-only distributions, "
        f"but only {len(reported_mismatches)} mismatches were reported: "
        f"{reported_mismatches!r} — the mutation did not bite"
    )
