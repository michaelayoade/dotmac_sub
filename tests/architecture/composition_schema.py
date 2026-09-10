"""The dimensional composition schema (frozen contract for cross-product reuse).

Three product repositories each kept their own answer to "is distribution X
composed into product Y?", and the three answers disagreed about what the
question even meant:

* ERP derived it from module manifests plus migration lineage.
* Sub derived it from a module-scope vocabulary-registry call plus lineage.
* Academy derived it from "every ``dotmac-*`` dependency and import minus a
  hand-named baseline" — i.e. from installation and import alone.

Same field name (``composed_distributions``), three incompatible definitions.
This module is the one schema all three rebuild their composition records
against. It is a NEW schema — ``CURRENT_SCHEMA_VERSION`` below — and it never
reads, upgrades, or partially accepts a record in any of the three old
shapes, including the shared legacy tag ``kernel-runtime-composition.v1``
that Academy's exporter used for its ``composed_distributions`` payload. A
payload declaring that tag, or any tag other than the current one, is
refused outright by :func:`composition_record_from_payload` — see that
function's docstring for why defaulting a missing dimension (even to
``unknown``) is exactly the bug this refusal exists to prevent.

Why v2 exists — the measured defect in v1's registration boundary
-------------------------------------------------------------------

v1's :func:`classify_registration_call_site` required
``argument_kind == "ModuleManifest_tuple"`` **and** ``consumed_by_assembly``.
The second conjunct discriminated nothing on real trees — measured directly
against both product repositories:

* ``dotmac_erp/app/product_assembly.py`` (``ERP_PRODUCT_ASSEMBLY =
  ProductAssemblySpec(..., modules=COMPOSED_MODULE_MANIFESTS, ...)``) is
  imported by no file under ERP's own ``app/`` — only four architecture
  tests and ``scripts/product_manifest.py``. ERP's ``app/main.py`` builds
  its FastAPI application with direct ``include_router`` calls and never
  touches this spec. Its own docstring says so: "composition metadata, not
  a second application factory."
* ``dotmac_sub/app/services/inbox_channels.py`` says the same of itself:
  "Nothing under `app/` imports this module at runtime yet, and that is
  deliberate."

Both are inert release/declaration metadata a v1-shaped ``consumed_by_assembly:
bool`` would have recorded identically to a genuinely booted assembly, since
v1 only asked whether a ``ProductAssemblySpec`` construction happened
ANYWHERE, never whether the product's own process actually reaches it. A
conjunct that cannot refuse is the dominant defect class this fleet
measures: a check that answers without being able to refuse.

v2 replaces it with :class:`AssemblyConsumptionTrace` — two independently
observable facts about the product's real boot/runtime path (see "The
registration boundary" below) — and adds the product-independent catalogue
universe derived from every ``packages/*/EXTRACTION.toml`` (see "The
catalogue universe" below). There is no adapter and no migration path from
``dimensional-composition.v1``: a v1-tagged payload is refused exactly like
every other unrecognized version, loudly naming the version it carries — see
:func:`composition_record_from_payload`.

Four dimensions, per product x distribution
--------------------------------------------

============================  ===============================================
dimension                     meaning
============================  ===============================================
``installation``               the distribution is resolved and installed
``module_registration``        its actual ``ModuleManifest`` is registered
                                through the consumed assembly — nothing else
                                counts (see "The registration boundary" below)
``migration_lineage``          its lineage is present in effective migration
                                configuration
``runtime_consumption``        production code can actually execute/import
                                its relevant surface
============================  ===============================================

Each dimension holds one :class:`DimensionValue`: ``TRUE``, ``FALSE``,
``UNKNOWN``, or ``NOT_APPLICABLE``. These four are pairwise distinct and are
never used interchangeably:

* ``FALSE`` ("absent") means the dimension APPLIES to this package kind and
  was measured to NOT hold. This is the correct value for, e.g., Academy
  having no module-registration mechanism at all for an ``optional-module``
  distribution: that distribution's classification says registration
  applies, and Academy's assembly does not do it, so the honest value is
  ``FALSE`` — never ``NOT_APPLICABLE``. A product's own lack of a mechanism
  is not evidence about what the distribution's package kind requires, and
  :class:`CompositionRecord` structurally refuses the shortcut (see
  ``__post_init__``): a required dimension can never be recorded as
  ``NOT_APPLICABLE``, no matter what the consuming product failed to do.
* ``NOT_APPLICABLE`` means the dimension does not apply to this package kind
  AT ALL, derived solely from :class:`PackageClassification` (never from a
  hand-maintained subtraction list, never from what one product happens to
  do). The platform baseline (``dotmac-kernel``, classification
  ``universal-facility``; ``dotmac-ui``, classification
  ``presentation-foundation``) is installed but has no ``ModuleManifest`` and
  no migration lineage by definition of those two classifications — so both
  dimensions are ``NOT_APPLICABLE`` for every product that installs them.
* ``UNKNOWN`` means nobody measured it yet. Any required (i.e. applicable)
  dimension left ``UNKNOWN`` blocks certification of a composed state —
  :func:`derive_composition_state` returns ``EVIDENCE_INCOMPLETE`` rather
  than guessing.

Derived states follow MECHANICALLY from those dimensions (see
:func:`derive_composition_state`) — never a judgement a record author
supplies. A payload or a direct :class:`CompositionRecord` construction that
tries to author a state instead of dimensions is structurally refused: the
dataclass has no ``state`` field, so supplying one raises ``TypeError``, and
:func:`composition_record_from_payload` explicitly rejects a payload carrying
a ``state``/``fully_composed`` key even alongside otherwise-valid dimensions.

The derivation pipeline, in order
----------------------------------

:func:`derive_composition_state` is a fixed, ordered pipeline of small steps
(``_DERIVATION_PIPELINE``, a tuple of functions each returning either a
decided :class:`CompositionState` or ``None`` to defer to the next step).
The order is load-bearing, not incidental — a reordered pipeline reaches a
DIFFERENT, wrong answer for at least one real input (see
``test_pipeline_ordering_is_pinned_not_incidental`` in the test module,
which builds the reordered pipeline and shows the divergence directly):

1. **Validate coherence** — the classification/`NOT_APPLICABLE` invariants
   :class:`CompositionRecord`'s constructor already enforces, re-asserted
   here as the pipeline's own first line of defence
   (``DimensionalIncoherence``, not silently tolerated even if a record
   somehow bypassed construction).
2. **Refuse any required dimension left `UNKNOWN`** — `installation`
   always; `module_registration`/`migration_lineage` only when the
   classification says they apply. Returns `EVIDENCE_INCOMPLETE`.
3. **Refuse contradictions** — `installation = FALSE` (confirmed absent)
   together with `module_registration`, `migration_lineage`, OR
   `runtime_consumption` reporting `TRUE` is not a state, it is a
   contradiction: a distribution that is not installed cannot be
   registered, have lineage, or show runtime consumption. This raises
   ``DimensionalIncoherence`` — a not-installed distribution with runtime
   evidence is the sharpest form of it, and is refused rather than filed as
   `NOT_COMPOSED`.
4. **`installation = FALSE` with no contradicting `TRUE`** (step 3 has
   already cleared every dimension reporting `TRUE`) derives `NOT_COMPOSED`.
   This is not the same as complete negative evidence: `runtime_consumption`
   may still be `UNKNOWN` here (it is never a required dimension), and that
   absence of evidence is accepted rather than treated as a fourth
   measured negative — see `_step_installation_absent`'s docstring and
   `test_installation_absent_accepts_unknown_runtime_consumption`.
5. **Classification-derived `NOT_APPLICABLE`** — reached only once
   `installation` is confirmed `TRUE` and no contradiction or unknown
   blocked the pipeline. If neither `module_registration` nor
   `migration_lineage` applies to this classification, the state is
   `NOT_APPLICABLE`. Because step 4 runs first, a platform-baseline
   distribution that is genuinely not installed still reports
   `NOT_COMPOSED`, never `NOT_APPLICABLE` — "not applicable" is a claim
   about the QUESTION, not a substitute for "not observed."
6. Otherwise the ruled table over `module_registration` /
   `migration_lineage`: both present -> `FULLY_COMPOSED`; lineage only ->
   `LINEAGE_ONLY`; registration without lineage -> `INVALID`; neither
   (both measured `FALSE`) -> `NOT_COMPOSED`.

Three consequences the pipeline order exists to guarantee, each with its
own test: an `optional-module` with no registration mechanism records
`module_registration = FALSE` and the pipeline can never reach
`NOT_APPLICABLE` for it (step 5's classification check would refuse that
combination via step 1 long before step 5 is reached); a
`universal-facility` with inapplicable module dimensions reaches
`NOT_APPLICABLE` exactly when installed and otherwise uncontradicted; and a
distribution reporting `installation = FALSE` alongside `runtime_consumption
= TRUE` is refused outright (step 3) rather than silently filed as
`NOT_COMPOSED` (which is what step 4 alone, without step 3 ahead of it,
would have done — the exact defect this ordering fixes).

``NOT_COMPOSED`` is a derivation, not a second copy of the evidence
---------------------------------------------------------------------

Ruled by Michael: ``NOT_COMPOSED`` deliberately covers several different
situations, not one —

* confirmed not installed, with no contradictory positive evidence;
* installed, and both applicable dimensions (``module_registration`` and
  ``migration_lineage``) are proven absent; and
* installed and registration APPLIES but is proven absent, while migration
  lineage does NOT apply at all (Ruling 1: a genuinely stateless
  ``optional-module`` manifest, e.g. ``dotmac-document-rendering``, that
  was never registered — see ``_step_registration_lineage_table``'s
  ``not lineage_applies`` branch).

There is no ``INSTALLED_ONLY`` (or any other) state to split them apart,
and there will not be one: adding a member for every distinction the
dimensions already carry would duplicate dimensional evidence inside the
derived enum, and the enum is a derivation, not a second copy of the
evidence. The ``installation`` dimension already preserves exactly this
distinction on the record itself — see
``test_not_composed_collapses_installation_absent_and_installed_but_unregistered``,
which derives the identical ``NOT_COMPOSED`` state from two records whose
``installation`` values remain distinguishable throughout.

The sharpest consequence of this: an INSTALLED distribution with
``module_registration = FALSE``, ``migration_lineage = FALSE``, and
``runtime_consumption = TRUE`` derives ``NOT_COMPOSED`` — the state says
"not composed" while the record it came from is visibly, truthfully
carrying ``runtime_consumption = TRUE``. This is INTENTIONAL, not a
defect to fix. It exists because ``runtime_consumption`` never folds into
the state at all (see "The derivation pipeline, in order" and
`test_runtime_consumption_is_not_inferred_by_derive_composition_state`),
and because ``NOT_COMPOSED`` is exactly what step 6 derives once
`installation`, `module_registration` and `migration_lineage` are
resolved — it was never meant to also answer "and is anything running."
A caller that needs to know whether a ``NOT_COMPOSED`` distribution is
nonetheless running must read ``runtime_consumption`` off the record (or
consult :class:`RuntimeExposureReport`) directly; it can never get that
answer from ``CompositionState`` alone, by design — see
``test_state_only_reports_cannot_answer_a_cross_dimensional_composition_question``
for a real, sensitivity-proven demonstration that a consumer restricted to
the derived state (or to the categorized reports built from it) provably
cannot answer that question, and must go back to the records.

The registration boundary
--------------------------

``module_registration`` means a real ``ModuleManifest`` registered through an
assembly the PRODUCT'S OWN BOOT PATH actually consumes — not that a
``ProductAssemblySpec`` object was constructed somewhere. The distinction is
built into :func:`classify_registration_call_site` as a pure function of
``argument_kind`` and an :class:`AssemblyConsumptionTrace` (two named facts —
see that class's docstring) rather than a single free-text kind a record
author picks directly.

Be plain about what this buys and what it does not. :class:`AssemblyConsumptionTrace`
is a REPORTING schema for a measurement performed elsewhere — it is not a
verifier, and nothing in this module walks an AST or an import graph to
populate its two boolean fields on its own. An author can set either field
to whatever value produces the answer they want, exactly as easily as the
flat ``consumed_by_assembly: bool`` v1 shipped could be set. What v2 changes
is the SHAPE of the claim, not who is able to lie about it: v1's single bool
could not even express "reached the boot path but never actually consumed,"
so no honest record of that real case (ERP's) was possible under it; v2's
two facts can express it, and — for THIS repository's own tree only — a
reader can independently RE-DERIVE them from source rather than trust an
assertion, via :func:`measure_starter_boot_assembly_consumption` below.
Correctness for the two cross-repository controls still rests on whoever
populated the trace having actually read the named files, the same
discipline any hand-authored architecture test requires; Starter's own CI
cannot open another repository's files, and this module says so rather than
claiming to have verified them.

Three real, paired call sites anchor the boundary (Ruling 1: the running
product assembly must consume the real ``ModuleManifest``; release-only
metadata does not count):

* **Positive control — Starter's own assembly**, ``app/assembly.py`` +
  ``app/main.py`` (this repository). This is the one control this module
  RE-DERIVES from disk rather than asserts:
  :func:`measure_starter_boot_assembly_consumption` reads THIS repository's
  actual ``app/main.py``, ``app/assembly.py``, and
  ``packages/dotmac-kernel/src/dotmac_kernel/app_factory.py`` and returns an
  ``AssemblyConsumptionTrace`` built from what those files actually say —
  ``imported_by_boot_entry_point=True`` because ``app/main.py`` contains
  ``from app.assembly import assembly``, and ``consumed_by_a_real_effect=True``
  because it also contains ``create_app(assembly)`` and the kernel's
  ``app_factory.py`` contains ``ModuleRegistry(spec.modules)`` (module
  validation "happens FIRST and fails closed", per that module's own
  docstring, before mounting anything). This traces
  ``AssemblyConsumptionKind.BOOT_PATH_CONSUMED`` and classifies as
  ``MODULE_MANIFEST_REGISTERED``.
  ``test_deleting_the_boot_entry_point_makes_the_trace_indeterminate`` proves
  this is a real read, not a fixed answer: pointed at a tree with no
  ``app/main.py``, the same function returns an ``INDETERMINATE`` trace
  instead. The positive control is not optional garnish: two passing
  refusals below are equally consistent with a checker that refuses
  everything, and this is the one input that proves it can also say yes.
* **Negative control — ERP**, ``app/product_assembly.py`` (dotmac_erp repo,
  a SEPARATE repository Starter's own CI cannot open). ``COMPOSED_MODULE_MANIFESTS``
  is a tuple of real ``ModuleManifest`` objects (``accounting_module``,
  ``files_module``, ...) passed as ``modules=COMPOSED_MODULE_MANIFESTS`` into
  ``ProductAssemblySpec(...)`` — a real ``ModuleManifest_tuple``, exactly
  like the positive control's argument kind. But ERP's ``app/main.py``
  builds its FastAPI application with direct ``include_router`` calls and
  never imports ``app.product_assembly`` at all — only four architecture
  tests and ``scripts/product_manifest.py`` do. This is a ONE-TIME, DATED
  manual reading of ERP's tree (recorded in ``test_composition_schema.py``
  as literal field values, not re-derived by execution), which traces
  ``imported_by_boot_entry_point=False`` —
  ``AssemblyConsumptionKind.RELEASE_METADATA_ONLY`` — and classifies as
  ``VOCABULARY_REGISTRATION`` even though the argument kind alone looks
  identical to the positive control. This is exactly the
  discrimination v1's flat ``consumed_by_assembly: bool`` could not make.
* **Negative control — Sub**, ``app/services/inbox_channels.py:230``
  (dotmac_sub repo, likewise a separate repository not read by this module's
  own execution): the module-scope statement ``register_channels(SUB_CHANNELS)``.
  ``register_channels``'s signature is
  ``register_channels(specs: list[ChannelSpec] | tuple[ChannelSpec, ...])`` —
  it registers ``ChannelSpec`` VOCABULARY into a channel registry, never a
  ``ModuleManifest`` at all, so ``classify_registration_call_site`` refuses
  it on ``argument_kind`` alone before the assembly-consumption trace is even
  consulted. The same module's own docstring states plainly: "Nothing under
  `app/` imports this module at runtime yet, and that is deliberate" — so a
  (likewise one-time, dated, manually recorded) trace of it independently
  reaches ``RELEASE_METADATA_ONLY`` as well. This classifies as
  ``VOCABULARY_REGISTRATION`` and separately demonstrates why
  ``runtime_consumption`` must be its own measured dimension rather than
  something inferred from installation/registration/lineage: Sub's channel
  declaration can be installed, registered-as-vocabulary and have lineage,
  while its own author states runtime consumption is exactly, deliberately,
  absent.

An :class:`AssemblyConsumptionTrace` that cannot establish one or both facts
(e.g. a dynamic import, or an indirection static analysis cannot resolve)
classifies as ``AssemblyConsumptionKind.INDETERMINATE`` — a refusal, mapped
to ``DimensionValue.UNKNOWN`` by ``RegistrationEvidence.as_dimension_value``
even when the call site itself was ``measured=True``. This is distinct from
``measured=False`` (nobody looked at the call site at all) — two independent
routes to the same honest "don't know," never collapsed into a guessed
``FALSE``.

Two negative controls alone would only prove the checker refuses things; the
positive control proves it also recognizes the real shape when it is
present. The positive control is re-derived by executing
:func:`measure_starter_boot_assembly_consumption` against this repository's
own tree; the two negative controls are a one-time, dated manual reading of
ERP's and Sub's files, recorded as literals — Starter's CI has no access to
those repositories, so they are not, and cannot be, re-verified by execution
from here. All three are exercised in ``test_composition_schema.py``. Be
precise about what the ERP and Sub literals are for: they are non-
authoritative regression fixtures that pin THIS module's own classifier
logic against a snapshot someone once read by hand — never product evidence
about ERP's or Sub's current registration state, and never a re-derivation
of it. Nothing in this module or its tests may cite them as such.

The catalogue universe
------------------------

Ruling 2: every product record uses the same product-independent catalogue —
every ``packages/*/EXTRACTION.toml`` in this repository, never a
product-scoped subset. :func:`derive_distribution_universe` derives it by
globbing ``packages_root`` (a plain path parameter; the function signature
takes no product name and no product-specific behaviour branches on one) and
reading each ``EXTRACTION.toml``'s ``package`` (distribution name) and
``classification`` field. It refuses rather than silently tolerates:

* a ``packages/<x>/`` directory with no ``EXTRACTION.toml`` — skipping it
  would silently drop a real package from the universe;
* a dossier with no declared ``package`` name;
* two dossiers declaring the same distribution name;
* a dossier whose ``classification`` is absent or not a member of
  :class:`PackageClassification` — ``PackageClassification(...)`` raising
  ``ValueError`` is caught and re-raised as :class:`CatalogueDerivationError`
  naming the offending file, so it surfaces as a named refusal rather than an
  unhandled traceback deep in a comprehension.

``PackageClassification.STATELESS_CONTRACT_CATALOGUE`` has no dossier in this
repository's real ``packages/`` tree as of this commit — see
``test_stateless_contract_catalogue_is_exercised_by_a_synthetic_dossier`` for
the synthetic case that exercises it, since an unexercised enum branch
proves nothing about its own correctness. This is a snapshot fact, not a
standing one: ``.github/release-contracts.json`` documents this as an
ACTIVE, deliberately-empty lane (seven candidate catalogues awaiting a
kernel grammar), so nothing in this module or its tests asserts the
classification's absence from the real tree — only that the synthetic
branch is exercised. The universe's SIZE is never a governing constant
anywhere in this module or its tests: every assertion about "how many"
re-derives the count from the glob at test time, because a hard-coded
number is exactly the kind of check that answers without being able to
refuse a real change to the tree.

No aggregate
------------

Nothing in this module can produce a single "N distributions composed"
number — not the record, not :func:`derive_composition_state`, not the
coverage report, not the runtime-exposure report. Both report dataclasses
hold only named per-state / per-value counters, define no ``__add__``,
``__int__`` or ``total``, and there is no helper anywhere in this module
that sums them. See ``test_no_scalar_total_exists_anywhere`` for the
structural proof (it inspects every top-level name in this module, not just
the two report types, since a stray helper function would be just as much
of a violation as a field).
"""

from __future__ import annotations

import ast
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Schema identity and the refusal of every prior shape
# ---------------------------------------------------------------------------

#: The one schema version this module reads. Bumped only by a deliberate,
#: reviewed migration — never silently, and never with a translation layer
#: bolted on for an older tag (see module docstring).
CURRENT_SCHEMA_VERSION: Final[str] = "dimensional-composition.v2"

#: Every dimension a `dimensional-composition.v1` payload MUST declare
#: explicitly. There is no default for a missing one — a missing dimension
#: is refused, never silently treated as `unknown` (that would let an old,
#: dimension-free record through the gate wearing new clothes).
REQUIRED_PAYLOAD_FIELDS: Final[tuple[str, ...]] = (
    "product",
    "distribution",
    "classification",
    "installation",
    "module_registration",
    "migration_lineage",
    "runtime_consumption",
)

#: Field names that only ever appear on a DERIVED result, never on an input
#: payload. A payload carrying one of these is authoring a state instead of
#: supplying dimensions, and is refused for that reason alone.
_DERIVED_ONLY_FIELDS: Final[tuple[str, ...]] = (
    "state",
    "fully_composed",
    "migration_lineage_manifest_applies",
)

#: The literal legacy tag Academy's ``composed_distributions`` exporter used.
#: Named explicitly (rather than folded into "anything not current") so the
#: refusal test can prove this exact historical shape is rejected, not just
#: some arbitrary unrecognized string.
LEGACY_SCHEMA_VERSION_V0: Final[str] = "kernel-runtime-composition.v1"

#: v1 of THIS schema — superseded by v2's runtime-path registration evidence
#: and the catalogue-universe derivation (see module docstring, "Why v2
#: exists"). Named explicitly, exactly like ``LEGACY_SCHEMA_VERSION_V0`` above,
#: so the refusal test proves this specific, once-current tag is rejected —
#: not merely "some string that isn't current." There is no adapter and no
#: migration path from v1: a v1 record must fail loudly, naming the version
#: it carries, never be silently reinterpreted under v2's rules.
LEGACY_SCHEMA_VERSION_V1: Final[str] = "dimensional-composition.v1"


class IncompatibleSchemaVersion(ValueError):
    """A payload does not declare `CURRENT_SCHEMA_VERSION` and is refused
    outright — no translation, no partial read, no defaulting of any absent
    dimension."""


class DimensionalIncoherence(ValueError):
    """Raised by `derive_composition_state`'s pipeline when a record's own
    dimensions cannot jointly be true of one real distribution — refused
    outright, never silently resolved to a state. Covers both the
    classification/`NOT_APPLICABLE` invariant (pipeline step 1) and the
    installation-absent-with-present-evidence contradiction (step 3)."""


# ---------------------------------------------------------------------------
# Dimension values
# ---------------------------------------------------------------------------


class DimensionValue(str, Enum):
    """The four, pairwise-distinct values a dimension may hold. See the
    module docstring for why `FALSE` ("absent") and `NOT_APPLICABLE` are
    never interchangeable, and why `UNKNOWN` blocks rather than defaults."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


# ---------------------------------------------------------------------------
# Package classification — the ONLY source `not_applicable` may be derived
# from (packages/*/EXTRACTION.toml `classification`, joined by
# scripts/module_catalog.py's CLASSIFICATION_LABELS in this repository).
# ---------------------------------------------------------------------------


class PackageClassification(str, Enum):
    """Mirrors the `classification` values `scripts/module_catalog.py`
    accepts from `packages/*/EXTRACTION.toml` (`CLASSIFICATION_LABELS`).
    This is the fleet's one existing package-kind taxonomy; nothing here
    invents a parallel one."""

    #: e.g. dotmac-kernel. Installed everywhere; no ModuleManifest, no
    #: migration lineage — it IS the thing manifests and lineages run on.
    UNIVERSAL_FACILITY = "universal-facility"
    #: e.g. dotmac-ui. Same shape as UNIVERSAL_FACILITY for these purposes:
    #: installed, no manifest, no lineage.
    PRESENTATION_FOUNDATION = "presentation-foundation"
    #: A real reusable module: has a `ModuleManifest` (`manifest.py`) and its
    #: own migration lineage. The only classification where
    #: `module_registration`/`migration_lineage` are REQUIRED (applicable)
    #: dimensions rather than `NOT_APPLICABLE`.
    OPTIONAL_MODULE = "optional-module"
    #: An external protocol edge a product CALLS, not installs as rows — no
    #: manifest, no lineage (module_catalog.py: "no rows to persist").
    STATELESS_PROTOCOL_ADAPTER = "stateless-protocol-adapter"
    #: Canonical capability-schema bytes and digests; calls nothing, owns no
    #: rows — no manifest, no lineage.
    STATELESS_CONTRACT_CATALOGUE = "stateless-contract-catalogue"

    @property
    def module_registration_applies(self) -> bool:
        """Whether `module_registration` is a real (non-NOT_APPLICABLE)
        dimension for this package kind. Derived from classification alone —
        never from what any one product happens to do. Unchanged by Ruling 1:
        registration applicability stays classification-only."""
        return self is PackageClassification.OPTIONAL_MODULE

    def migration_lineage_applies(self, manifest_applicability: bool) -> bool:
        """Whether `migration_lineage` is a real (non-NOT_APPLICABLE)
        dimension for this package kind.

        Ruling 1: classification alone still decides for every platform-
        baseline kind (`universal-facility`, `presentation-foundation`,
        `stateless-protocol-adapter`, `stateless-contract-catalogue`) —
        those always return `False` here regardless of
        `manifest_applicability`, exactly as before.

        `optional-module` applicability is supplied by the one authoritative
        ingestion boundary after it reads the manifest. It is never a
        compatibility fallback chosen by a record author."""
        if self is not PackageClassification.OPTIONAL_MODULE:
            return False
        return manifest_applicability


# ---------------------------------------------------------------------------
# Manifest-derived migration-lineage applicability (Ruling 1).
#
# Read by AST — never by text scan, grep, or substring search. A substring
# search for "short_code" matches
# `packages/dotmac-document-rendering/src/dotmac_document_rendering/
# manifest.py`'s own comment "Deliberately no short_code, migration prefix,
# tables or plane declaration" and returns the opposite of the truth; this
# module parses the `ModuleManifest(...)` call's keyword arguments instead.
#
# Exactly three outcomes, decided in `derive_migration_lineage_applicability_
# from_manifest`:
#
# 1. `short_code` AND `migration_prefix` both declared (present, non-empty)
#    -> lineage applies.
# 2. Both absent, AND no `tables`, no `platform_tables`, and no
#    `migration_branch` declared -> lineage does not apply.
# 3. Everything else (one present without the other; a stateless pair
#    contradicted by a declared `tables`/`platform_tables`/`migration_branch`;
#    or a manifest that is malformed, unreadable, absent, or has no
#    `ModuleManifest(...)` call at all) -> refused, by name
#    (`ManifestDeclarationError`), never defaulted to either applicability.
# ---------------------------------------------------------------------------


class ManifestDeclarationError(ValueError):
    """A distribution's `packages/<dist>/src/<import_pkg>/manifest.py`
    cannot be read, or its `ModuleManifest(...)` keyword arguments are
    internally contradictory about whether the distribution owns migration
    lineage. Raised by name, identifying the distribution and exactly what
    was contradictory — never silently resolved to `True` or `False`
    (Ruling 1, outcome 3)."""


#: The two keywords whose joint, non-empty presence is the ONLY thing that
#: makes migration lineage apply (outcome 1).
_LINEAGE_IDENTITY_KEYWORDS: Final[tuple[str, str]] = ("short_code", "migration_prefix")

#: Keywords whose declared (non-empty) presence, alongside an absent
#: `_LINEAGE_IDENTITY_KEYWORDS` pair, is a contradiction (outcome 3) rather
#: than a genuinely stateless module (outcome 2) — a module cannot own
#: tables, platform tables, or a migration branch without the identity that
#: names its migration lineage.
_LINEAGE_SIGNAL_KEYWORDS: Final[tuple[str, ...]] = (
    "tables",
    "platform_tables",
    "migration_branch",
)


def _manifest_source_path(packages_root: Path, distribution: str) -> Path:
    """`packages/<distribution>/src/<import_pkg>/manifest.py`, where
    `<import_pkg>` is `distribution` with `-` replaced by `_` — the naming
    convention every real manifest in this tree follows (verified directly
    against all 80 `packages/*/src/*/manifest.py` files at the time of this
    ruling: zero mismatches)."""
    root = packages_root.resolve()
    if packages_root.is_symlink() or not root.is_dir():
        raise ManifestDeclarationError(f"{packages_root} is not a directory")
    if (
        not distribution
        or Path(distribution).name != distribution
        or distribution in {".", ".."}
    ):
        raise ManifestDeclarationError(
            f"{distribution!r} is not a safe single distribution component"
        )
    import_package = distribution.replace("-", "_")
    relative_path = Path(distribution) / "src" / import_package / "manifest.py"
    path = root
    for component in relative_path.parts:
        path /= component
        if path.is_symlink():
            raise ManifestDeclarationError(
                f"{distribution}: manifest path component {path} is a symlink"
            )
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ManifestDeclarationError(
            f"{distribution}: manifest path escapes packages root {root}"
        )
    return path


def _find_module_manifest_call(tree: ast.Module) -> ast.Call | None:
    """Return exactly one proven module-level ``module = ModuleManifest`` call."""
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ModuleManifest"
    ]
    if len(calls) != 1:
        return None
    candidate: ast.Call | None = None
    for statement in tree.body:
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name) and target.id == "module":
                value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            if (
                isinstance(statement.target, ast.Name)
                and statement.target.id == "module"
            ):
                value = statement.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "ModuleManifest"
        ):
            candidate = value
    return candidate if candidate is calls[0] else None


def _is_declared_empty(value: ast.expr) -> bool:
    """True only for a literal that is unambiguously empty — an empty
    string/`None` constant, or an empty tuple/list/set literal. A `Name`,
    `Call`, or any other expression (e.g. `tables=TENANT_TABLES`, the shape
    every real stateful manifest in this tree actually uses) cannot be
    statically evaluated by AST alone and is treated as declared/non-empty.

    This single conservative default — "cannot prove empty, so treat as
    present" — has a DIFFERENT justification at each of its two call sites
    in `derive_migration_lineage_applicability_from_manifest`, and both
    justifications point the same direction (toward refusal over a silent
    "does not apply"), which is why one shared function is still correct
    even though the two callers' risks are not the same:

    * For `_LINEAGE_IDENTITY_KEYWORDS` (`short_code`/`migration_prefix`):
      guessing a `Name`-valued keyword empty could wrongly make a real,
      coherent stateful manifest look like it declares no identity — either
      manufacturing a spurious identity-mismatch contradiction (outcome 3),
      or, if both looked empty, silently misclassifying a real stateful
      module as stateless (outcome 2).
    * For `_LINEAGE_SIGNAL_KEYWORDS` (`tables`/`platform_tables`/
      `migration_branch`): the risk runs the OTHER way. The outcome-3
      contradiction only fires when both identity keywords are ALREADY
      absent, so treating a `Name`-valued `tables=TENANT_TABLES` as
      non-empty is what CORRECTLY surfaces that contradiction rather than
      suppressing it — guessing it empty here would silently let a module
      that truly owns tables through as outcome 2 ("does not apply"),
      hiding a real declaration behind an unevaluated reference. Treating
      it as present is not a source of false contradictions at this call
      site; it is what makes the contradiction check see a real
      declaration at all.
    """
    if isinstance(value, ast.Constant):
        return value.value in ("", None)
    if isinstance(value, ast.Tuple | ast.List | ast.Set):
        return len(value.elts) == 0
    return False


def _keyword_declared_and_nonempty(call: ast.Call, name: str) -> bool:
    for kw in call.keywords:
        if kw.arg == name and not _is_declared_empty(kw.value):
            return True
    return False


def derive_migration_lineage_applicability_from_manifest(
    packages_root: Path, distribution: str
) -> bool:
    """The one function that decides Ruling 1's three outcomes for one
    `optional-module` distribution. See the section banner above for the
    exact rule; this docstring covers only what each refusal names.

    Raises `ManifestDeclarationError`, naming `distribution` and the exact
    contradiction, when: the manifest file does not exist; it cannot be
    parsed as Python; it declares no `ModuleManifest(...)` call at all;
    exactly one of `short_code`/`migration_prefix` is declared without the
    other; or neither is declared but `tables`, `platform_tables`, or
    `migration_branch` is."""
    manifest_path = _manifest_source_path(packages_root, distribution)
    if not manifest_path.is_file():
        raise ManifestDeclarationError(
            f"{distribution}: no manifest.py at {manifest_path} — cannot "
            "derive migration-lineage applicability from an absent file"
        )

    try:
        tree = ast.parse(manifest_path.read_text())
    except SyntaxError as exc:
        raise ManifestDeclarationError(
            f"{distribution}: {manifest_path} could not be parsed as Python " f"— {exc}"
        ) from exc

    call = _find_module_manifest_call(tree)
    if call is None:
        raise ManifestDeclarationError(
            f"{distribution}: {manifest_path} does not declare exactly one "
            "module-level module = ModuleManifest(...)"
        )

    has_short_code, has_migration_prefix = (
        _keyword_declared_and_nonempty(call, name)
        for name in _LINEAGE_IDENTITY_KEYWORDS
    )
    lineage_signal_declared = any(
        _keyword_declared_and_nonempty(call, name) for name in _LINEAGE_SIGNAL_KEYWORDS
    )

    if has_short_code and has_migration_prefix:
        return True
    if not has_short_code and not has_migration_prefix and not lineage_signal_declared:
        return False

    contradictions: list[str] = []
    if has_short_code != has_migration_prefix:
        contradictions.append(
            f"short_code declared={has_short_code!r} but migration_prefix "
            f"declared={has_migration_prefix!r} — both or neither is coherent"
        )
    if not has_short_code and not has_migration_prefix and lineage_signal_declared:
        contradictions.append(
            "declares neither short_code nor migration_prefix (a stateless "
            "pair) but also declares tables, platform_tables, or "
            "migration_branch — a module cannot own lineage-bearing state "
            "with no identity to name that lineage"
        )
    raise ManifestDeclarationError(
        f"{distribution}: {manifest_path} is contradictory about migration "
        f"lineage — {'; '.join(contradictions)}"
    )


# ---------------------------------------------------------------------------
# The module-registration boundary: paired classifier, not a free-text kind
#
# v2 replaces the flat `consumed_by_assembly: bool` v1 shipped. That bool
# discriminated nothing on real trees: it was identical (True, in the loose
# "an assembly object exists" sense v1 measured it) for ERP's inert release
# spec and for whatever v1 recorded for a genuinely booted product, because
# v1 never asked whether the OBJECT itself reaches a running process — only
# whether it reaches a `ProductAssemblySpec` construction anywhere, and a
# construction that only tests and a script ever call satisfies that just as
# well as one `app/main.py` calls. `AssemblyConsumptionTrace` below replaces
# it with two independently observable facts about the PRODUCT'S OWN
# boot/runtime path, not about whether an assembly-shaped object was merely
# built somewhere.
# ---------------------------------------------------------------------------


class AssemblyConsumptionKind(str, Enum):
    """What a static trace established about whether an assembly-shaped
    object (e.g. a `ProductAssemblySpec`) is consumed by the product's real
    boot/runtime path, as opposed to constructed only for tests, scripts, or
    release tooling."""

    #: The object is reached by the product's boot entry point AND, once
    #: reached, fed into a call proven to use it for something real (e.g.
    #: `ModuleRegistry(spec.modules)` inside `create_app`). The Starter
    #: positive control.
    BOOT_PATH_CONSUMED = "boot_path_consumed"
    #: Everything else a trace can positively establish: the object exists,
    #: perhaps even imported somewhere, but the boot entry point does not
    #: transitively reach it, or reaches it without feeding it to a real
    #: effect. The ERP positive-object/negative-consumption control.
    RELEASE_METADATA_ONLY = "release_metadata_only"
    #: The trace itself could not establish one or both facts (e.g. dynamic
    #: import, re-export through an unresolvable indirection). This is a
    #: REFUSAL to answer, never collapsed into `RELEASE_METADATA_ONLY` — see
    #: `RegistrationEvidence.as_dimension_value`, which maps it to `UNKNOWN`
    #: even when the call site itself was `measured=True`.
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class AssemblyConsumptionTrace:
    """A structural description of whether an assembly-shaped object is
    reached by the product's real boot/runtime path. This is a REPORTING
    shape for a measurement performed elsewhere — see the module docstring's
    "The registration boundary" for what that does and does not buy. Model a
    real trace by reading the product's actual entry point and the callee it
    hands the object to, as the three paired controls in the module
    docstring do, or by calling `measure_starter_boot_assembly_consumption`
    for THIS repository's own tree; do not invent a shape that happens to
    produce the answer wanted.

    `None` on either boolean field means the trace could not establish that
    fact (e.g. the import is dynamic, or the consuming call is behind an
    indirection static analysis cannot resolve) — this is a refusal, and
    `classify()` reports it as `INDETERMINATE` rather than guessing `False`.
    """

    #: The product's actual process entry point the trace claims to be
    #: about, e.g. "app/main.py". PROVENANCE ONLY — this field records which
    #: file grounded the measurement for a human reader; `classify()` below
    #: reads only the two boolean fields and never inspects this one, so a
    #: trace naming the wrong file is not caught by construction. Anyone
    #: populating a trace is responsible for making this name match the file
    #: the two booleans were actually read from.
    boot_entry_point: str
    #: Whether `boot_entry_point`, by static import-graph analysis,
    #: transitively imports the module that defines the object under test —
    #: as opposed to only test, script, or release-tooling modules importing
    #: it. `None` if this could not be established.
    imported_by_boot_entry_point: bool | None
    #: Whether, once reached from the boot path, the object is passed into a
    #: call whose own body is proven to use its contents for a real runtime
    #: effect (e.g. `ModuleRegistry(spec.modules)` inside `create_app`) — as
    #: opposed to being merely re-exported or held as an inert reference.
    #: `None` if this could not be established.
    consumed_by_a_real_effect: bool | None

    def classify(self) -> AssemblyConsumptionKind:
        """Reads only `imported_by_boot_entry_point` and
        `consumed_by_a_real_effect` — `boot_entry_point` is provenance and is
        deliberately not consulted here; see that field's docstring."""
        if (
            self.imported_by_boot_entry_point is None
            or self.consumed_by_a_real_effect is None
        ):
            return AssemblyConsumptionKind.INDETERMINATE
        if self.imported_by_boot_entry_point and self.consumed_by_a_real_effect:
            return AssemblyConsumptionKind.BOOT_PATH_CONSUMED
        return AssemblyConsumptionKind.RELEASE_METADATA_ONLY


def measure_starter_boot_assembly_consumption(
    repo_root: Path,
) -> AssemblyConsumptionTrace:
    """The one control this module RE-DERIVES from disk rather than asserts.
    Reads `repo_root`'s own `app/main.py`, `app/assembly.py`, and
    `packages/dotmac-kernel/src/dotmac_kernel/app_factory.py` and builds an
    `AssemblyConsumptionTrace` from what their source text actually
    contains — plain substring matching over real files, NOT a full AST or
    import-graph walker (see the module docstring's honesty note on what
    `AssemblyConsumptionTrace` does and does not guarantee).

    Scoped to `repo_root` — this repository's own tree — because this
    repository's own CI can open its own files; it cannot open another
    repository's files, which is why the ERP and Sub controls in the module
    docstring are NOT produced by a function like this one. Returns an
    `INDETERMINATE`-classifying trace (both booleans `None`) if either
    `app/main.py` or `app/assembly.py` is missing, rather than guessing —
    see `test_deleting_the_boot_entry_point_makes_the_trace_indeterminate`.
    """
    boot_entry_point = "app/main.py"
    main_path = repo_root / "app" / "main.py"
    assembly_path = repo_root / "app" / "assembly.py"
    factory_path = (
        repo_root
        / "packages"
        / "dotmac-kernel"
        / "src"
        / "dotmac_kernel"
        / "app_factory.py"
    )

    if not main_path.is_file() or not assembly_path.is_file():
        return AssemblyConsumptionTrace(
            boot_entry_point=boot_entry_point,
            imported_by_boot_entry_point=None,
            consumed_by_a_real_effect=None,
        )

    main_source = main_path.read_text()
    assembly_source = assembly_path.read_text()
    imported = (
        "from app.assembly import assembly" in main_source
        and "assembly = ProductAssemblySpec(" in assembly_source
    )

    consumed: bool | None = None
    if imported and "create_app(assembly)" in main_source and factory_path.is_file():
        consumed = "ModuleRegistry(spec.modules)" in factory_path.read_text()

    return AssemblyConsumptionTrace(
        boot_entry_point=boot_entry_point,
        imported_by_boot_entry_point=imported,
        consumed_by_a_real_effect=consumed,
    )


class RegistrationEvidenceKind(str, Enum):
    """What kind of thing a call site actually did. Only one member ever
    produces `module_registration = TRUE` — see
    `RegistrationEvidence.as_dimension_value`."""

    #: A real `ModuleManifest` bound into an assembly that is itself
    #: consumed by the product's boot path (`AssemblyConsumptionKind.
    #: BOOT_PATH_CONSUMED`). The Starter positive control.
    MODULE_MANIFEST_REGISTERED = "module_manifest_registered"
    #: Either the argument is not `ModuleManifest` values at all (the Sub
    #: negative control), or it is, but the assembly it is bound into is
    #: only release metadata (`AssemblyConsumptionKind.RELEASE_METADATA_ONLY`
    #: — the ERP negative control).
    VOCABULARY_REGISTRATION = "vocabulary_registration"
    #: The argument is `ModuleManifest` values, but the trace of whether the
    #: assembly they are bound into reaches the boot path was itself
    #: `AssemblyConsumptionKind.INDETERMINATE` — a refusal to answer, never
    #: collapsed into either of the above.
    INDETERMINATE_ASSEMBLY_CONSUMPTION = "indeterminate_assembly_consumption"


@dataclass(frozen=True)
class RegistrationCallSite:
    """A structural description of one call site that registers something.
    Every field is an independently-observable fact about the call site —
    never a conclusion the test author asserts directly. Model a real call
    site by reading it, as the three paired controls in the module docstring
    do; do not invent a shape that happens to produce the answer wanted."""

    #: The name of the thing being called, e.g. "ProductAssemblySpec" or
    #: "register_channels".
    callee: str
    #: What kind of object is actually passed, e.g. "ModuleManifest_tuple"
    #: or "ChannelSpec_tuple". Not a boolean — the actual declared kind.
    argument_kind: str
    #: Whether the assembly-shaped object the values are bound into is
    #: itself consumed by the product's boot/runtime path — see
    #: `AssemblyConsumptionTrace`. This is what v2 replaces the flat
    #: `consumed_by_assembly: bool` with (see the section banner above).
    assembly_consumption: AssemblyConsumptionTrace


def classify_registration_call_site(
    site: RegistrationCallSite,
) -> RegistrationEvidenceKind:
    """The one function allowed to decide `MODULE_MANIFEST_REGISTERED` vs
    `VOCABULARY_REGISTRATION` vs `INDETERMINATE_ASSEMBLY_CONSUMPTION`. Two
    facts must hold for a positive result: the argument must actually be
    `ModuleManifest` values, AND the assembly they are bound into must trace
    as `BOOT_PATH_CONSUMED` — a module-scope call that merely LOOKS like
    registration (any callee name, any side effect, or an assembly object
    that only tests/scripts ever construct) is never enough on its own."""
    if site.argument_kind != "ModuleManifest_tuple":
        return RegistrationEvidenceKind.VOCABULARY_REGISTRATION
    kind = site.assembly_consumption.classify()
    if kind is AssemblyConsumptionKind.BOOT_PATH_CONSUMED:
        return RegistrationEvidenceKind.MODULE_MANIFEST_REGISTERED
    if kind is AssemblyConsumptionKind.INDETERMINATE:
        return RegistrationEvidenceKind.INDETERMINATE_ASSEMBLY_CONSUMPTION
    return RegistrationEvidenceKind.VOCABULARY_REGISTRATION


@dataclass(frozen=True)
class RegistrationEvidence:
    """One measurement of `module_registration` for one product x
    distribution. `measured=False` means nobody looked at the call site at
    all — the honest answer is `UNKNOWN`, never a guess in either direction.
    A call site that WAS looked at (`measured=True`) but whose assembly-
    consumption trace was itself `INDETERMINATE` is a second, independent
    route to `UNKNOWN` — see `as_dimension_value`."""

    kind: RegistrationEvidenceKind
    measured: bool

    def as_dimension_value(self) -> DimensionValue:
        if not self.measured:
            return DimensionValue.UNKNOWN
        if self.kind is RegistrationEvidenceKind.MODULE_MANIFEST_REGISTERED:
            return DimensionValue.TRUE
        if self.kind is RegistrationEvidenceKind.INDETERMINATE_ASSEMBLY_CONSUMPTION:
            return DimensionValue.UNKNOWN
        return DimensionValue.FALSE


# ---------------------------------------------------------------------------
# The record — dimensions only, state never authored
# ---------------------------------------------------------------------------


@dataclass(frozen=True, init=False)
class CompositionRecord:
    """One product x distribution's measured dimensions. Deliberately has NO
    `state` field: `CompositionRecord(..., state=...)` raises `TypeError`
    because the dataclass has no such parameter — the state is always
    computed by `derive_composition_state`, never supplied.

    `__post_init__` ties `NOT_APPLICABLE` strictly to `classification`: an
    `optional-module` distribution can never record `module_registration` or
    `migration_lineage` as `NOT_APPLICABLE` (that would let a product's own
    missing mechanism masquerade as "doesn't apply" — see the module
    docstring's `FALSE` vs `NOT_APPLICABLE` distinction), and a
    platform-baseline distribution can never record either dimension as
    anything OTHER than `NOT_APPLICABLE`. `installation` may never be
    `NOT_APPLICABLE` — every distribution is either installed, confirmed not
    installed, or unmeasured.

    `migration_lineage_manifest_applies` (Ruling 1) is deliberately absent
    from any public constructor. ``__new__`` always refuses, so records are
    allocated only inside :func:`composition_record_from_payload` after it
    has read the real dossier and manifest. Pure derivation tests that need
    synthetic records use an explicit ``object.__new__`` bypass in the test
    module; that bypass is intentionally not production evidence. There is
    no adjacent boolean-taking builder and no legacy ``None -> True``
    fallback.
    """

    product: str
    distribution: str
    classification: PackageClassification
    installation: DimensionValue
    module_registration: DimensionValue
    migration_lineage: DimensionValue
    runtime_consumption: DimensionValue
    __migration_lineage_manifest_applies: bool = field(init=False, repr=False)

    def __new__(cls, *args: object, **kwargs: object) -> CompositionRecord:
        del args, kwargs
        raise TypeError(
            "CompositionRecord is derived by composition_record_from_payload; "
            "it has no public constructor"
        )

    @property
    def migration_lineage_manifest_applies(self) -> bool:
        """Starter-derived output; never a constructor or payload input."""
        return self.__migration_lineage_manifest_applies

    def __post_init__(self) -> None:
        if not isinstance(self.__migration_lineage_manifest_applies, bool):
            raise TypeError("manifest applicability must be a derived bool")
        if self.installation is DimensionValue.NOT_APPLICABLE:
            raise ValueError(
                f"{self.product}/{self.distribution}: installation is never "
                "not_applicable — every distribution is installed, "
                "confirmed absent, or unmeasured"
            )

        self._check_applicability(
            "module_registration",
            self.module_registration,
            self.classification.module_registration_applies,
        )
        self._check_applicability(
            "migration_lineage",
            self.migration_lineage,
            self.classification.migration_lineage_applies(
                self.migration_lineage_manifest_applies
            ),
        )

    def _check_applicability(
        self, field_name: str, value: DimensionValue, applies: bool
    ) -> None:
        is_not_applicable = value is DimensionValue.NOT_APPLICABLE
        if applies and is_not_applicable:
            raise ValueError(
                f"{self.product}/{self.distribution}: {field_name} applies to "
                f"classification {self.classification.value!r} and cannot be "
                "recorded not_applicable — if it was never satisfied, the "
                "honest value is 'false' (absent), not 'not_applicable'"
            )
        if not applies and not is_not_applicable:
            raise ValueError(
                f"{self.product}/{self.distribution}: {field_name} does not "
                f"apply to classification {self.classification.value!r} "
                f"(must be not_applicable, got {value.value!r})"
            )


# ---------------------------------------------------------------------------
# Payload ingestion — the schema-version refusal boundary
# ---------------------------------------------------------------------------


def composition_record_from_payload(
    payload: Mapping[str, object], packages_root: Path
) -> CompositionRecord:
    """Parse one raw record. Refuses outright — never upgrades, never
    partially reads, never defaults a missing dimension to anything
    (including `unknown`) — any payload that is not exactly
    `CURRENT_SCHEMA_VERSION`.

    This is the boundary that keeps a `kernel-runtime-composition.v1` record
    (Academy's old `composed_distributions` shape, and by extension ERP's
    and Sub's differently-derived old records of the same field name) from
    ever reaching `CompositionRecord`. A defaulting translator — treating a
    missing dimension as `unknown` and proceeding — would let exactly that
    old record through the gate wearing this schema's clothes, which is the
    one thing this function must never do.

    `packages_root` is REQUIRED, not optional with a `None`/classification-
    only fallback: this is the one real ingestion path every product record
    travels, and Ruling 1's migration-lineage applicability must be DERIVED
    here, from the distribution's real `ModuleManifest` (via
    `derive_migration_lineage_applicability_from_manifest`), for every
    `optional-module` payload — never left to the classification-only
    default. An optional parameter would silently reinstate exactly the bug
    Ruling 1 exists to remove the first time a caller omitted it, and the
    omission would look like working code (every payload would simply fall
    back to "lineage always applies," and a genuinely stateless module's
    `not_applicable` payload would go back to being refused, or its `false`
    payload would go back to deriving `invalid`). A required parameter means
    a caller that has not decided what packages root to derive against
    cannot compile a record at all.

    A payload can never supply this applicability itself:
    `migration_lineage_manifest_applies` is a derived-only field and is
    refused alongside `state`/`fully_composed`. Silently ignoring it would
    accept an authority-shaped input while leaving a producer believing its
    value mattered. A product asserting its own applicability would be
    authoring a derivation this schema exists to prevent.
    """
    declared = payload.get("schema_version")
    if declared != CURRENT_SCHEMA_VERSION:
        raise IncompatibleSchemaVersion(
            f"refusing payload with schema_version={declared!r}; this reads "
            f"only {CURRENT_SCHEMA_VERSION!r} records — no translation, no "
            "partial read, no defaulting of an old record's missing "
            "dimensions"
        )

    derived_only_present = [f for f in _DERIVED_ONLY_FIELDS if f in payload]
    if derived_only_present or "composed_distributions" in payload:
        offending = derived_only_present or ["composed_distributions"]
        raise IncompatibleSchemaVersion(
            f"payload carries derived/legacy field(s) {offending!r}; a "
            "composition state (or an old composed_distributions rollup) is "
            "computed, never supplied on the input record"
        )

    missing = [f for f in REQUIRED_PAYLOAD_FIELDS if f not in payload]
    if missing:
        raise IncompatibleSchemaVersion(
            f"payload declares {CURRENT_SCHEMA_VERSION!r} but is missing "
            f"required field(s) {missing!r}; a missing dimension is refused, "
            "never defaulted to unknown"
        )

    distribution = str(payload["distribution"])
    dossiers = {
        dossier.distribution: dossier
        for dossier in derive_distribution_universe(packages_root)
    }
    dossier = dossiers.get(distribution)
    if dossier is None:
        raise CatalogueDerivationError(
            f"{distribution!r} is not declared beneath {packages_root}"
        )
    declared_classification = PackageClassification(payload["classification"])
    if declared_classification is not dossier.classification:
        raise CatalogueDerivationError(
            f"{distribution!r} declares classification "
            f"{declared_classification.value!r} in payload but "
            f"{dossier.classification.value!r} in its dossier"
        )
    classification = dossier.classification

    # Ruling 1: derived HERE, from the real manifest, never taken from the
    # payload. Only meaningful for optional-module — every other
    # classification's applicability is classification-only and this value
    # is ignored for it (see `PackageClassification.migration_lineage_
    # applies`), and no manifest.py exists for a platform-baseline
    # distribution to read in the first place.
    manifest_lineage_applicability = False
    if classification is PackageClassification.OPTIONAL_MODULE:
        manifest_lineage_applicability = (
            derive_migration_lineage_applicability_from_manifest(
                packages_root, distribution
            )
        )

    record = object.__new__(CompositionRecord)
    for name, value in (
        ("product", str(payload["product"])),
        ("distribution", distribution),
        ("classification", classification),
        ("installation", DimensionValue(payload["installation"])),
        ("module_registration", DimensionValue(payload["module_registration"])),
        ("migration_lineage", DimensionValue(payload["migration_lineage"])),
        ("runtime_consumption", DimensionValue(payload["runtime_consumption"])),
        (
            "_CompositionRecord__migration_lineage_manifest_applies",
            manifest_lineage_applicability,
        ),
    ):
        object.__setattr__(record, name, value)
    record.__post_init__()
    return record


# ---------------------------------------------------------------------------
# Derived state — a pure function of the record, never authored
# ---------------------------------------------------------------------------


class CompositionState(str, Enum):
    FULLY_COMPOSED = "fully_composed"
    LINEAGE_ONLY = "lineage_only"
    INVALID = "invalid"
    NOT_COMPOSED = "not_composed"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"
    #: Neither `module_registration` nor `migration_lineage` applies to this
    #: classification (platform baseline). Distinct from every other state:
    #: it is not a claim about whether composition happened, only that the
    #: question does not apply to this package kind.
    NOT_APPLICABLE = "not_applicable"


def _step_validate_coherence(record: CompositionRecord) -> CompositionState | None:
    """Pipeline step 1. Re-asserts the same classification/`NOT_APPLICABLE`
    invariant `CompositionRecord.__post_init__` already enforces at
    construction. In normal operation this can never fire — construction
    already refused an incoherent record — but it is restated here,
    deliberately first, so the ordered pipeline is complete on its own and
    so a record that reaches this function by any other path (e.g. a test
    that bypasses `__post_init__` to simulate a construction-layer defect)
    is still refused before any state is derived from it."""
    for field_name, value, applies in (
        (
            "module_registration",
            record.module_registration,
            record.classification.module_registration_applies,
        ),
        (
            "migration_lineage",
            record.migration_lineage,
            record.classification.migration_lineage_applies(
                record.migration_lineage_manifest_applies
            ),
        ),
    ):
        is_not_applicable = value is DimensionValue.NOT_APPLICABLE
        if applies and is_not_applicable:
            raise DimensionalIncoherence(
                f"{record.product}/{record.distribution}: {field_name} "
                f"applies to classification {record.classification.value!r} "
                "and cannot be not_applicable"
            )
        if not applies and not is_not_applicable:
            raise DimensionalIncoherence(
                f"{record.product}/{record.distribution}: {field_name} does "
                f"not apply to classification {record.classification.value!r} "
                "and must be not_applicable"
            )
    if record.installation is DimensionValue.NOT_APPLICABLE:
        raise DimensionalIncoherence(
            f"{record.product}/{record.distribution}: installation is never "
            "not_applicable"
        )
    return None


def _step_refuse_required_unknown(
    record: CompositionRecord,
) -> CompositionState | None:
    """Pipeline step 2. `installation` is always required. `registration`/
    `lineage` are required exactly when their classification says they
    apply. `runtime_consumption` is deliberately NOT checked here — it is
    never a required dimension for certifying a composition state (see the
    module docstring); it only participates, separately, in step 3's
    contradiction check."""
    if record.installation is DimensionValue.UNKNOWN:
        return CompositionState.EVIDENCE_INCOMPLETE
    if (
        record.classification.module_registration_applies
        and record.module_registration is DimensionValue.UNKNOWN
    ):
        return CompositionState.EVIDENCE_INCOMPLETE
    if (
        record.classification.migration_lineage_applies(
            record.migration_lineage_manifest_applies
        )
        and record.migration_lineage is DimensionValue.UNKNOWN
    ):
        return CompositionState.EVIDENCE_INCOMPLETE
    return None


def _step_refuse_contradictions(
    record: CompositionRecord,
) -> CompositionState | None:
    """Pipeline step 3. `installation = FALSE` (confirmed absent) together
    with ANY of registration, lineage, or runtime consumption reporting
    `TRUE` is refused outright — a not-installed distribution cannot be
    registered, have lineage, or show runtime consumption, so seeing one is
    either a measurement error or a real hazard, never a state to file."""
    if record.installation is not DimensionValue.FALSE:
        return None
    contradicting = [
        name
        for name, value in (
            ("module_registration", record.module_registration),
            ("migration_lineage", record.migration_lineage),
            ("runtime_consumption", record.runtime_consumption),
        )
        if value is DimensionValue.TRUE
    ]
    if contradicting:
        raise DimensionalIncoherence(
            f"{record.product}/{record.distribution}: installation is false "
            f"(absent) but {contradicting!r} report true — refused, not "
            "filed as not_composed"
        )
    return None


def _step_installation_absent(record: CompositionRecord) -> CompositionState | None:
    """Pipeline step 4. Reached only once step 3 has cleared: none of
    `module_registration`, `migration_lineage`, or `runtime_consumption`
    reports `TRUE`. That is NOT the same as complete negative evidence —
    `runtime_consumption` is never a required dimension (step 2 does not
    refuse it when `UNKNOWN`, and step 3 only refuses a `TRUE` value), so
    it may still be `UNKNOWN` here. A record with `installation = FALSE`,
    `module_registration = FALSE`, `migration_lineage = FALSE`, and
    `runtime_consumption = UNKNOWN` reaches this step with three measured
    negatives and one unmeasured dimension, and still derives
    `NOT_COMPOSED`: an unmeasured runtime signal on an already-confirmed
    absent installation is not itself grounds to block the verdict, because
    `runtime_consumption` never participates in composition-state
    derivation except as a contradiction canary (step 3) — its absence of
    evidence here is accepted, not silently treated as a fourth negative.
    See `test_installation_absent_accepts_unknown_runtime_consumption` for
    this exact case, named and reasoned about on its own."""
    if record.installation is DimensionValue.FALSE:
        return CompositionState.NOT_COMPOSED
    return None


def _step_not_applicable(record: CompositionRecord) -> CompositionState | None:
    """Pipeline step 5. Reached only once `installation` is confirmed
    `TRUE` (steps 2 and 4 already handled `UNKNOWN`/`FALSE`). If neither
    `module_registration` nor `migration_lineage` applies to this
    classification, the composition question itself does not apply."""
    if (
        not record.classification.module_registration_applies
        and not record.classification.migration_lineage_applies(
            record.migration_lineage_manifest_applies
        )
    ):
        return CompositionState.NOT_APPLICABLE
    return None


def _step_registration_lineage_table(
    record: CompositionRecord,
) -> CompositionState | None:
    """Pipeline step 6, the ruled table. Reached only once installation is
    `TRUE`, at least one of registration/lineage applies (step 5 already
    handled "neither applies"), and neither applicable dimension is
    `UNKNOWN`.

    Ruling 1 consequence: once `migration_lineage` applicability can differ
    from `module_registration` applicability WITHIN `optional-module` (a
    genuinely stateless manifest — see
    `derive_migration_lineage_applicability_from_manifest` — makes lineage
    not apply while registration still does), this table must READ
    `lineage_applies` rather than infer "lineage absent" from a `False`
    dimension value. The former shape (`has_lineage = lineage_applies and
    record.migration_lineage is TRUE`) collapsed "lineage does not apply"
    and "lineage was required and measured absent" into the same `False`,
    so a registered stateless module (`registered=True, has_lineage=False`)
    wrongly reached `INVALID` — the state reserved for a genuine defect. A
    distribution registered against the running assembly whose lineage does
    not apply at all is fully composed, not invalid."""
    registration_applies = record.classification.module_registration_applies
    lineage_applies = record.classification.migration_lineage_applies(
        record.migration_lineage_manifest_applies
    )
    registered = (
        registration_applies and record.module_registration is DimensionValue.TRUE
    )

    if not lineage_applies:
        # Lineage is not a real requirement for this distribution at all
        # (enforced by construction: migration_lineage is NOT_APPLICABLE
        # here, never TRUE/FALSE). Whether it is composed depends solely on
        # registration — there is no "lineage only" or "invalid" reading
        # possible when lineage was never a question to begin with.
        return (
            CompositionState.FULLY_COMPOSED
            if registered
            else CompositionState.NOT_COMPOSED
        )

    has_lineage = record.migration_lineage is DimensionValue.TRUE

    if registered and has_lineage:
        return CompositionState.FULLY_COMPOSED
    if not registered and has_lineage:
        return CompositionState.LINEAGE_ONLY
    if registered and not has_lineage:
        return CompositionState.INVALID
    return CompositionState.NOT_COMPOSED


#: The pinned, ordered derivation pipeline. `derive_composition_state` is a
#: thin loop over exactly this tuple, in exactly this order — the order is
#: the ruled contract, not an implementation detail; see the module
#: docstring's "The derivation pipeline, in order" section and
#: `test_pipeline_ordering_is_pinned_not_incidental`.
_DERIVATION_PIPELINE: Final[
    tuple[Callable[[CompositionRecord], CompositionState | None], ...]
] = (
    _step_validate_coherence,
    _step_refuse_required_unknown,
    _step_refuse_contradictions,
    _step_installation_absent,
    _step_not_applicable,
    _step_registration_lineage_table,
)


def derive_composition_state(record: CompositionRecord) -> CompositionState:
    """The one, pure, mechanical derivation. Runs `_DERIVATION_PIPELINE` in
    order and returns the first step's decided state; a step that finds a
    genuine contradiction raises `DimensionalIncoherence` instead of
    returning. The final step (the ruled registration/lineage table) always
    decides, so this function always either returns a `CompositionState` or
    raises."""
    for step in _DERIVATION_PIPELINE:
        result = step(record)
        if result is not None:
            return result
    raise AssertionError("unreachable: _step_registration_lineage_table always decides")


# ---------------------------------------------------------------------------
# Reports — categorized sets only, never a scalar total
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompositionCoverageReport:
    """Per-state counts across a set of records. Deliberately has no
    `total`/`sum`/`__add__`/`__int__` — see the module docstring's
    "No aggregate" section and `test_no_scalar_total_exists_anywhere`."""

    fully_composed: int
    lineage_only: int
    invalid: int
    not_composed: int
    evidence_incomplete: int
    not_applicable: int


def build_coverage_report(
    records: Iterable[CompositionRecord],
) -> CompositionCoverageReport:
    counts: dict[CompositionState, int] = {state: 0 for state in CompositionState}
    for record in records:
        counts[derive_composition_state(record)] += 1
    return CompositionCoverageReport(
        fully_composed=counts[CompositionState.FULLY_COMPOSED],
        lineage_only=counts[CompositionState.LINEAGE_ONLY],
        invalid=counts[CompositionState.INVALID],
        not_composed=counts[CompositionState.NOT_COMPOSED],
        evidence_incomplete=counts[CompositionState.EVIDENCE_INCOMPLETE],
        not_applicable=counts[CompositionState.NOT_APPLICABLE],
    )


@dataclass(frozen=True)
class RuntimeExposureReport:
    """Reported entirely separately from `CompositionCoverageReport`:
    `runtime_consumption` is its own dimension and must never be folded into
    — or inferred from — the composition state. See
    `test_runtime_consumption_is_not_inferred`."""

    exposed: int
    not_exposed: int
    unknown: int


def build_runtime_exposure_report(
    records: Iterable[CompositionRecord],
) -> RuntimeExposureReport:
    records = list(records)
    return RuntimeExposureReport(
        exposed=sum(1 for r in records if r.runtime_consumption is DimensionValue.TRUE),
        not_exposed=sum(
            1 for r in records if r.runtime_consumption is DimensionValue.FALSE
        ),
        unknown=sum(
            1 for r in records if r.runtime_consumption is DimensionValue.UNKNOWN
        ),
    )


# ---------------------------------------------------------------------------
# The catalogue universe — product-independent, derived from every
# packages/*/EXTRACTION.toml (Ruling 2). See the module docstring's "The
# catalogue universe" section for the refusal cases and why the size is
# never a governing constant.
# ---------------------------------------------------------------------------


class CatalogueDerivationError(ValueError):
    """The `packages/` tree cannot produce one unambiguous, product-independent
    distribution universe. Raised instead of silently skipping the offending
    directory or dossier — a missing `EXTRACTION.toml`, a package/directory
    identity mismatch, or an unrecognized `classification` are refusals, not
    warnings."""


@dataclass(frozen=True)
class PackageDossier:
    """One `packages/<distribution>/EXTRACTION.toml`, reduced to exactly what
    this module needs: the distribution's name and its declared package
    classification. Nothing else in that TOML file is this module's
    concern — ownership, contract text, and adoption evidence belong to
    `adoption_evidence.py`, a different owner."""

    distribution: str
    classification: PackageClassification


def derive_distribution_universe(
    packages_root: Path,
) -> tuple[PackageDossier, ...]:
    """Derive the fleet's product-independent distribution catalogue from
    every `packages/*/EXTRACTION.toml` under `packages_root`.

    Takes only a filesystem path — no product name, and no branch anywhere
    in this function's body inspects or depends on which product is asking.
    The same universe is what every product's composition records are
    checked against (Ruling 2).

    Glob-derived, so the count this function returns is never a constant
    that governs behaviour here or in any test — a test that wants to know
    "how many" re-derives it from `packages_root.iterdir()` itself, at test
    time, rather than asserting a literal count that a real change to the
    tree would silently leave stale.

    Refuses, in each case naming the offending path in the raised
    `CatalogueDerivationError`:

    * `packages_root` itself does not exist or is not a directory;
    * a `packages/<x>/` directory with no `EXTRACTION.toml` (skipped
      silently, this would understate the universe without anyone noticing);
    * a dossier with no `package` field, or an empty one;
    * a dossier whose ``package`` differs from its owning directory name —
      this one-to-one binding also makes duplicate distribution names
      structurally impossible beneath a single packages root;
    * a dossier whose `classification` is absent or is not a value
      `PackageClassification` recognizes — caught and re-raised naming the
      file, so this surfaces as a named refusal rather than an unhandled
      `ValueError` deep in a loop.
    """
    root = packages_root.resolve()
    if packages_root.is_symlink() or not root.is_dir():
        raise CatalogueDerivationError(
            f"{packages_root} is not a directory — cannot derive a "
            "distribution universe from it"
        )

    dossiers: list[PackageDossier] = []
    for package_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if package_dir.is_symlink():
            raise CatalogueDerivationError(
                f"{package_dir} is a symlink — refused in packages root"
            )
        resolved_package_dir = package_dir.resolve()
        if not resolved_package_dir.is_relative_to(root):
            raise CatalogueDerivationError(
                f"{package_dir} resolves outside packages root {root}"
            )
        toml_path = package_dir / "EXTRACTION.toml"
        if toml_path.is_symlink():
            raise CatalogueDerivationError(
                f"{toml_path} is a symlink — refused in packages root"
            )
        if not toml_path.is_file():
            raise CatalogueDerivationError(
                f"{package_dir} has no EXTRACTION.toml — refused, not "
                "silently skipped"
            )

        data = tomllib.loads(toml_path.read_text())

        distribution = data.get("package")
        if not isinstance(distribution, str) or not distribution:
            raise CatalogueDerivationError(
                f"{toml_path} declares no non-empty 'package' name"
            )
        if distribution != package_dir.name:
            raise CatalogueDerivationError(
                f"{toml_path} declares package {distribution!r}, but its "
                f"directory is {package_dir.name!r}"
            )

        raw_classification = data.get("classification")
        try:
            classification = PackageClassification(raw_classification)
        except ValueError as exc:
            raise CatalogueDerivationError(
                f"{toml_path} declares unrecognized classification "
                f"{raw_classification!r}"
            ) from exc

        dossiers.append(
            PackageDossier(distribution=distribution, classification=classification),
        )

    return tuple(dossiers)
