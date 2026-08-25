"""Static ownership guards for the `receivable-shadow-01` projection.

The slice's whole claim is "authority does not move". These are the checks that
make the claim inspectable rather than asserted in a docstring:

* the projection never reaches a decision owner — no collections case, no
  settlement-mirror row, no invoice or payment mutation;
* the cohort declaration stays importable by a static guard, which is what
  keeps the rule and the checks from drifting apart;
* dry run is the structural default, not an argparse habit;
* the projection table carries no tenant column, so a later editor cannot
  half-add one and leave a decorative RLS policy behind.

Every rejection guard is paired with a sensitivity proof. A detector that only
ever sees conforming input passes just as happily when it has stopped matching
anything at all.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COHORT = ROOT / "app/services/billing/receivable_cohort.py"
OWNER = ROOT / "app/services/billing/receivable_projection.py"
PARITY = ROOT / "app/services/billing/receivable_parity.py"
MODELS = ROOT / "app/models/billing_receivable_projection.py"
MIGRATION = ROOT / "alembic/versions/558_receivable_observation_projection.py"
CLI = ROOT / "scripts/billing/receivable_projection.py"

SLICE_MODULES = (COHORT, OWNER, PARITY, MODELS, CLI)

#: Names whose appearance anywhere in the slice would mean it had reached past
#: observation into a decision owner. `CollectionsCase` and `DunningCase` are
#: the two case constructors; `PaymentProviderEvent` and `IntegrationInbox` are
#: the settlement mirror's only two construction sites.
FORBIDDEN_NAMES = (
    "CollectionsCase",
    "CollectionsLifecycle",
    "DunningCase",
    "DunningWorkflow",
    "PaymentProviderEvent",
    "IntegrationInbox",
    "BillingEnforcementReconciler",
)

#: Incumbent columns whose assignment would make the projection a second writer
#: of state another owner already owns.
FORBIDDEN_ASSIGNMENT_TARGETS = (
    "balance_due",
    "paid_at",
    "resolved_amount",
    "observation_digest",
)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_source(path), filename=str(path))


def _imported_modules(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _assigned_attributes(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def _called_attributes(path: Path) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _code_identifiers(path: Path) -> set[str]:
    """Every name the module actually REFERENCES in code.

    Deliberately not a text scan. These guards are about what the slice
    *reaches*, and a docstring that explains which owner keeps the only
    `CollectionsCase(...)` construction site is precisely the documentation we
    want — it must not be mistaken for the slice constructing one. Prose lands
    in `ast.Constant`; this collects imports, `Name` loads and attribute
    accesses, so only real references count.
    """
    names: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
            names.update(alias.asname for alias in node.names if alias.asname)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            names.update(alias.name for alias in node.names)
            names.update(alias.asname for alias in node.names if alias.asname)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _module_string_constants(path: Path) -> dict[str, str]:
    """Module-level `NAME = "literal"` assignments."""
    constants: dict[str, str] = {}
    for node in _tree(path).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, str):
            constants[target.id] = value
    return constants


def _resolved_source(path: Path) -> str:
    """Source with `{_CONSTANT}` f-string placeholders substituted.

    The migration composes its DDL from module constants, so the literal SQL a
    reviewer cares about never appears in the file. Matching the template form
    (`trg_{_TRIGGER_FN}`) would pass while proving nothing about what the
    trigger is actually NAMED, which is most of the guard's value. Resolving
    first keeps the assertions about meaning, and keeps them correct when a
    constant is renamed.
    """
    source = _source(path)
    for name, value in _module_string_constants(path).items():
        source = source.replace("{" + name + "}", value)
    return source


def _argparse_option_strings(path: Path) -> set[str]:
    """Every option string declared by an `add_argument(...)` call."""
    options: set[str] = set()
    for node in ast.walk(_tree(path)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            options.update(
                argument.value
                for argument in node.args
                if isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
            )
    return options


def _constructed(path: Path, name: str) -> int:
    return sum(
        1
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    )


# ── The projection never reaches a decision owner ───────────────────────────


def test_the_slice_never_references_a_case_or_settlement_mirror_writer() -> None:
    """The slice must not REACH these owners — regardless of what it discusses.

    Scanned as identifiers rather than as text, on purpose. These modules
    document at length which owner keeps the only `CollectionsCase(...)` and
    `PaymentProviderEvent(...)` construction sites, and that documentation is
    the point of the boundary. A text scan would forbid explaining the rule it
    exists to enforce, which pushes the explanation out of the code.
    """
    for path in SLICE_MODULES:
        referenced = _code_identifiers(path) & set(FORBIDDEN_NAMES)
        assert not referenced, (
            f"{path.relative_to(ROOT)} references {sorted(referenced)} in code. "
            "The receivable projection observes; it never creates a collections "
            "case and never writes the settlement mirror."
        )


def test_the_forbidden_name_guard_still_bites() -> None:
    """Sensitivity proof for the check above.

    Two ways it could pass for the wrong reason — an empty vocabulary, or an
    extractor that has stopped finding identifiers. The incumbent owners really
    do reference these names, so they must trip the same detector.
    """
    assert FORBIDDEN_NAMES, "the forbidden-name set must not be empty"
    for module, expected in (
        ("app/services/collections/lifecycle.py", "CollectionsCase"),
        ("app/services/payment_provider_events.py", "PaymentProviderEvent"),
    ):
        identifiers = _code_identifiers(ROOT / module)
        assert expected in identifiers, (
            f"{module} must trip this detector on {expected!r}; if it no longer "
            "does, the detector has stopped matching rather than the slice "
            "having become clean"
        )


def test_the_identifier_scan_ignores_prose_not_code() -> None:
    """Guard the guard's discrimination, not just its reach.

    An extractor that quietly fell back to a text scan would still satisfy the
    sensitivity proof above while re-breaking every documented module. This
    pins the actual distinction: the same name is invisible in a docstring and
    visible in a call.
    """
    import textwrap

    prose = ast.parse('"""Mentions CollectionsCase in prose only."""\n')
    code = ast.parse(
        textwrap.dedent("""
        from app.models.collections_case import CollectionsCase

        case = CollectionsCase()
        """)
    )

    def identifiers(tree: ast.Module) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        return names

    assert "CollectionsCase" not in identifiers(prose)
    assert "CollectionsCase" in identifiers(code)


def test_the_slice_never_assigns_an_incumbent_money_column() -> None:
    for path in SLICE_MODULES:
        assigned = _assigned_attributes(path)
        overlap = assigned & set(FORBIDDEN_ASSIGNMENT_TARGETS)
        assert not overlap, (
            f"{path.relative_to(ROOT)} assigns {sorted(overlap)}. "
            "financial.invoices, financial.payments and billing.obligations "
            "keep those columns; the projection only reads them."
        )


def test_the_assignment_guard_still_bites() -> None:
    """Sensitivity proof: the incumbent writer must trip the same detector."""
    common = ROOT / "app/services/billing/_common.py"
    assigned = _assigned_attributes(common)
    assert "balance_due" in assigned, (
        "_recalculate_invoice_totals is the single writer of invoices.balance_due; "
        "if this detector no longer sees it, the detector is broken"
    )


def test_the_parity_module_writes_nothing() -> None:
    calls = _called_attributes(PARITY)
    for forbidden in ("add", "add_all", "commit", "flush", "rollback", "delete"):
        assert forbidden not in calls, (
            f"receivable_parity calls db.{forbidden}(). The parity report is "
            "read-only; recording it is the projection owner's write."
        )


def test_only_the_owner_constructs_the_projection_rows() -> None:
    for name in ("BillingReceivableProjection", "ReceivableProjectionRun"):
        assert _constructed(OWNER, name) == 1, (
            f"{name} must be constructed exactly once, in the owner module"
        )
        for path in (COHORT, PARITY, CLI):
            assert _constructed(path, name) == 0, (
                f"{path.relative_to(ROOT)} constructs {name}; "
                "billing.receivable_projection is its only writer"
            )


# ── The cohort declaration stays statically importable ──────────────────────


def test_the_cohort_declaration_imports_no_orm_or_model() -> None:
    """The rule must be readable without a database or a mapped class.

    A guard that has to import models to read the cohort rule ends up
    re-encoding the rule in a regex instead, and the two then drift.
    """
    modules = _imported_modules(COHORT)
    for forbidden in ("sqlalchemy", "app.db", "app.models"):
        assert not any(
            module == forbidden or module.startswith(f"{forbidden}.")
            for module in modules
        ), f"receivable_cohort imports {forbidden}; it must stay dependency-light"


def test_the_cohort_vocabularies_are_closed_enums() -> None:
    """A vocabulary that is the contract must be an enum, not a convention."""
    from app.services.billing import receivable_cohort as cohort

    for enum_name in (
        "ReceivableLane",
        "CohortClassification",
        "ParityDimension",
        "ParityOutcome",
        "NotExpressibleReason",
    ):
        member = getattr(cohort, enum_name)
        assert issubclass(member, __import__("enum").StrEnum), (
            f"{enum_name} must be a closed StrEnum"
        )
        assert list(member), f"{enum_name} must declare at least one member"


def test_every_required_parity_dimension_is_declared() -> None:
    from app.services.billing.receivable_cohort import ParityDimension

    required = {
        "cadence",
        "proration",
        "obligations",
        "settlements",
        "receivable_amount",
        "due_date_provenance",
        "service_scope",
    }
    assert {item.value for item in ParityDimension} == required


def test_the_standing_blocker_carries_real_pin_coordinates() -> None:
    """A blocker without a pin is indistinguishable from "we did not get to it"."""
    from app.services.billing.receivable_cohort import STANDING_BLOCKERS
    from app.shadow.cohort import SHADOW_COHORT

    assert STANDING_BLOCKERS, "the treatment blocker must be declared"
    pins = {
        (entry.package, entry.contract_version, entry.source_revision)
        for entry in SHADOW_COHORT.modules
    }
    for blocker in STANDING_BLOCKERS:
        assert blocker.pinned_version, f"{blocker.code} names no pinned version"
        assert (
            blocker.pinned_package,
            blocker.pinned_version,
            blocker.pinned_revision,
        ) in pins, (
            f"{blocker.code} names a pin the module-adoption manifest does not "
            "record; the two must not drift"
        )


def test_the_data_cohort_does_not_claim_module_adoption() -> None:
    """Recording a data cohort must not advance a module's adoption state."""
    from app.shadow.cohort import SHADOW_COHORT
    from app.shadow.vocabulary import AdoptionState, AuthorityMode

    for module in ("subscriptions", "billing", "collections"):
        entry = next(item for item in SHADOW_COHORT.modules if item.module == module)
        assert entry.adoption_state is AdoptionState.SOURCE_ONLY
        assert entry.authority_mode is AuthorityMode.NONE


# ── Dry run is structural ───────────────────────────────────────────────────


def test_the_command_defaults_to_dry_run() -> None:
    from app.services.billing.receivable_projection import (
        ProjectionMode,
        ReconcileReceivableProjectionCommand,
    )

    default = (
        inspect.signature(ReconcileReceivableProjectionCommand)
        .parameters["mode"]
        .default
    )
    assert default is ProjectionMode.DRY_RUN, (
        "an adapter that forgets to pass a mode must get the safe one"
    )


def test_the_cli_declares_no_dry_run_flag_only_an_apply_flag() -> None:
    """Omission can only mean dry run.

    A `--dry-run` flag would make safety depend on a flag being PRESENT, which
    is one typo or one edited runbook line away from an unintended write.

    Asserted over DECLARED options rather than file text: the module docstring
    and the `--apply` help string both state that no `--dry-run` flag exists,
    and saying so is exactly what a reader needs. A text scan would forbid the
    explanation while permitting the flag to be declared under an alias.
    """
    options = _argparse_option_strings(CLI)
    assert "--dry-run" not in options, (
        "the CLI declares a --dry-run flag; safety must come from omission, "
        "not from remembering to pass an argument"
    )
    assert "--apply" in options


def test_the_option_extractor_sees_the_real_flags() -> None:
    """Sensitivity proof: an extractor returning nothing would pass the above."""
    options = _argparse_option_strings(CLI)
    for expected in ("--apply", "--strict", "--window-start", "--cutoff"):
        assert expected in options, (
            f"{expected} is declared by the CLI but the extractor did not find "
            "it; the no-dry-run assertion above is therefore vacuous"
        )


def test_every_writing_subcommand_accepts_apply() -> None:
    from scripts.billing.receivable_projection import build_parser

    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, __import__("argparse")._SubParsersAction)
    )
    for name in ("backfill", "reconcile", "repair-drift", "parity"):
        options = {
            option
            for action in subparsers.choices[name]._actions
            for option in action.option_strings
        }
        assert "--apply" in options, f"{name} cannot be applied"
        assert "--dry-run" not in options, f"{name} exposes a --dry-run flag"


def test_the_dry_run_path_never_enters_the_owner_boundary() -> None:
    """Dry run must not open the transaction at all, not merely not commit."""
    from app.services.billing import receivable_projection as owner

    source = inspect.getsource(owner.reconcile_receivable_projection)
    tree = ast.parse(source.lstrip())
    dry_branch = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and "DRY_RUN" in ast.unparse(node.test)
    )
    assert "execute_owner_command" not in ast.unparse(dry_branch)


# ── Persistence shape ───────────────────────────────────────────────────────


def test_the_projection_carries_no_tenant_column() -> None:
    """A deliberate, structural absence.

    Every authoritative input — invoices, invoice_lines, payment_allocations,
    subscriptions, billing_obligations — is tenant-free; Sub's tenancy is the
    ADR-0009 operator bridge, not a column on financial tables. A `tenant_id`
    here would have no authoritative source to fill it, and the RLS policy over
    it would be decorative rather than isolating. Asserted so a later editor
    cannot half-add one.
    """
    from app.models.billing_receivable_projection import (
        BillingReceivableProjection,
        ReceivableProjectionRun,
    )

    for model in (BillingReceivableProjection, ReceivableProjectionRun):
        columns = {column.name for column in model.__table__.columns}
        assert "tenant_id" not in columns, (
            f"{model.__tablename__} grew a tenant column; either its inputs "
            "became tenant-scoped (then add NOT NULL, composite uniques and "
            "RLS in one migration) or the column is decorative and must go"
        )
    migration = _source(MIGRATION)
    assert "ROW LEVEL SECURITY" not in migration


#: The DDL the migration must emit, spelled as it will actually reach the
#: database rather than as it is written in the file.
_REQUIRED_MIGRATION_DDL = (
    "CREATE TRIGGER trg_billing_receivable_projections_monotonic",
    "BEFORE UPDATE ON billing_receivable_projections",
    "projection_version <= OLD.projection_version",
    "source_observed_at < OLD.source_observed_at",
    "CREATE SEQUENCE billing_receivable_projection_version_seq",
)


def test_the_migration_installs_the_structural_monotonic_guard() -> None:
    """The predicate in the writer is a convention; the trigger is the guard."""
    source = _resolved_source(MIGRATION)
    for fragment in _REQUIRED_MIGRATION_DDL:
        assert fragment in source, (
            f"migration 558 must emit {fragment!r}. Without the trigger the "
            "monotonic rule is only a convention the writer happens to follow."
        )


def test_the_migration_ddl_guard_reads_composed_sql() -> None:
    """Sensitivity proof for `_resolved_source`.

    The migration builds its DDL from module constants, so the literal SQL is
    absent from the raw file. If the resolver ever degraded to returning the
    source unchanged, the guard above would fail loudly — but if it degraded
    the other way, substituting so aggressively that everything matched, it
    would pass while proving nothing. This pins both ends: the constants are
    real, the substitution is load-bearing, and it changes the specific names
    the guard asserts on.
    """
    constants = _module_string_constants(MIGRATION)
    assert constants.get("_TRIGGER_FN") == "billing_receivable_projections_monotonic"
    assert constants.get("_OBSERVATIONS") == "billing_receivable_projections"
    assert constants.get("_SEQUENCE") == "billing_receivable_projection_version_seq"

    raw = _source(MIGRATION)
    resolved = _resolved_source(MIGRATION)
    composed = [
        fragment
        for fragment in _REQUIRED_MIGRATION_DDL
        if fragment not in raw and fragment in resolved
    ]
    assert composed, (
        "no asserted fragment is actually composed, so resolution is doing "
        "nothing and the guard has silently become a plain text scan"
    )
    assert "trg_{_TRIGGER_FN}" in raw, "the raw file must still hold the template"
    assert "trg_{_TRIGGER_FN}" not in resolved, "resolution must consume it"


def test_the_writer_carries_the_staleness_predicate() -> None:
    """The upsert must compare watermarks, not merely mention doing so.

    The previous form of this test scanned the file for
    `excluded.source_observed_at`, which the module docstring also contains —
    so it would have passed with the predicate deleted. This reads the actual
    `where=` argument of the actual `on_conflict_do_update` call.
    """
    upserts = [
        node
        for node in ast.walk(_tree(OWNER))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "on_conflict_do_update"
    ]
    assert len(upserts) == 1, "exactly one upsert writes the projection"

    where = next(
        (keyword.value for keyword in upserts[0].keywords if keyword.arg == "where"),
        None,
    )
    assert where is not None, (
        "the upsert carries no where= predicate, so a stale observation would "
        "overwrite a newer one whenever the planner's read raced the write"
    )
    predicate = ast.unparse(where)
    assert ".excluded.source_observed_at" in predicate, predicate
    assert isinstance(where, ast.Compare), predicate
    assert isinstance(where.ops[0], ast.Gt), (
        f"the predicate must be strictly greater-than; got {predicate!r}. "
        ">= would let an equal watermark overwrite a differing fingerprint "
        "instead of failing closed."
    )
    assert "source_observed_at" in ast.unparse(where.comparators[0]), predicate


def test_the_natural_key_is_unique_and_structurally_spelled() -> None:
    from app.models.billing_receivable_projection import BillingReceivableProjection

    constraints = {
        constraint.name
        for constraint in BillingReceivableProjection.__table__.constraints
    }
    assert "uq_billing_receivable_projection_key" in constraints
    assert "uq_billing_receivable_projection_invoice_lane" in constraints


def test_the_owner_is_registered_with_a_complete_contract() -> None:
    from app.services.sot_manifest import contract_validation_errors
    from app.services.sot_registry import registry

    services = registry.all_services()
    names = {service.name for service in services}
    owner = next(
        service
        for service in services
        if service.name == "billing.receivable_projection"
    )
    assert owner.module == "app.services.billing.receivable_projection"
    assert owner.contract is not None
    assert contract_validation_errors(owner, service_names=names) == ()


def test_the_projection_does_not_import_the_collections_module_contract() -> None:
    """The product row and module input may coexist but remain independent."""
    model_source = _source(MODELS)
    assert "class BillingReceivableProjection" in model_source
    assert "class ReceivableObservationV1" not in model_source
    for path in SLICE_MODULES:
        assert "dotmac_collections" not in _source(path), (
            f"{path.relative_to(ROOT)} imports the collections module contract; "
            "the projection must not become a second writer of it"
        )


def test_shadow_contract_terms_cannot_drive_native_invoice_due_dates() -> None:
    """Fail closed until contract authority and the invoice resolver move together.

    `BillingContractVersion.payment_terms_days` is still shadow evidence. Using
    it in an invoice writer would move financial authority without the registry
    cutover. This gate must be replaced, in the same change, by resolver tests
    when `billing.contracts` becomes authoritative and `financial.invoices`
    owns the typed due-date resolution contract.
    """
    from app.services.sot_manifest import AuthorityMigrationState
    from app.services.sot_registry import registry

    contract_owner = next(
        service
        for service in registry.all_services()
        if service.name == "billing.contracts"
    )
    assert contract_owner.contract is not None
    assert contract_owner.contract.migration.state is AuthorityMigrationState.SHADOWING

    for path in (
        ROOT / "app/services/billing/invoices.py",
        ROOT / "app/services/billing_automation.py",
        ROOT / "app/services/crm_api.py",
    ):
        source = _source(path)
        assert "BillingContracts.effective_version_at" not in source
        assert "payment_terms_days" not in source

    design = _source(ROOT / "docs/designs/RECEIVABLE_PROJECTION_SHADOW.md")
    assert "Due-date authority gate" in design
