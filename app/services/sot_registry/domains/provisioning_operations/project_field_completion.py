"""Extend existing owners with explicit accepted project completion authority."""

from dataclasses import replace

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    ConcernContract,
    OwnerRole,
    SOTService,
)

FIELD_OWNER = "operations.field_completion"
PROJECT_OWNER = "operations.project_lifecycle"


def with_project_field_completion(service: SOTService) -> SOTService:
    if service.name not in (FIELD_OWNER, PROJECT_OWNER):
        return service
    assert service.contract is not None
    contract = service.contract
    assert (
        contract.transaction is not None
        and contract.errors is not None
        and contract.migration is not None
    )
    field = service.name == FIELD_OWNER
    inputs: tuple[AuthorityInput, ...] = (
        AuthorityInput(
            name="explicit project field requirements",
            owner=PROJECT_OWNER,
            kind=AuthorityKind.AUTHORITATIVE_RECORD,
            source="Immutable exact enrollment/WorkOrder/project/task/customer registrations; all registered descendants including archived historical nodes remain required. No inferred physical scope.",
        ),
        AuthorityInput(
            name="fresh project completion admission",
            owner="auth.staff_permission_admission",
            kind=AuthorityKind.CONTROL_INPUT,
            source="Fresh active staff Person and leave admission: project:update or project:task:write, before and after lock waits. Does not grant completion review authority.",
        ),
    )
    if field:
        inputs += (
            AuthorityInput(
                name="current scoped accepted field basis",
                owner=FIELD_OWNER,
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source="Shared stage_scoped_completion_basis kernel validates every exact required enrollment, effective acceptance, ordered reviewed replacement lineage, assignment, criteria, policy and engineering freshness. Immutable application evidence records this transaction's exact result; replay revalidates full current basis.",
            ),
        )
    names = (
        (
            ("accepted project field scope completion", False),
            ("accepted project field scope completion preview", True),
        )
        if field
        else (
            ("explicit project field requirement registration", False),
            ("protected accepted project field completion", False),
            ("project field requirement scope", True),
            ("project field completion dependency readiness", True),
        )
    )
    return replace(
        service,
        owns=(*service.owns, *(name for name, _ in names)),
        implementation_modules=(
            *service.implementation_modules,
            "app.services.field.completion_project_scope",
        )
        if field
        else service.implementation_modules,
        contract=replace(
            contract,
            concerns=(
                *contract.concerns,
                *(
                    ConcernContract(
                        name=name,
                        role=OwnerRole.RESOLVER if read else OwnerRole.COMMAND_WRITER,
                        input_names=tuple(i.name for i in inputs),
                        canonical_writer=None if read else service.name,
                    )
                    for name, read in names
                ),
            ),
            authoritative_inputs=(*contract.authoritative_inputs, *inputs),
            transaction=replace(
                contract.transaction,
                boundary=contract.transaction.boundary
                + (
                    " Explicit accepted-project action uses the single field-completion executor: immutable application/bases/ordered lineage/receipt with server-stamped full outer xid8 identity across savepoints (no caller-chosen marker, historical backfill or late children after physical commit), then protected flush-only project participant, canonical transition output and audit atomically."
                    if field
                    else " Enrollment registration and accepted completion are protected flush-only participants requiring active operations.field_completion; projects never interprets completion approval tables. Generic and mapped completion reject marked scopes; legacy unenrolled behavior stays."
                ),
                locking=contract.transaction.locking
                + " Observe exact requirement set; sorted Ticket/WO linkage fences before Project, all task nodes then canonical shared evidence locks. Reread markers under Project and refuse scope drift rather than acquire new linkage fences late. SQL project lock serializes enrollment versus generic completion; marked task subtree archival/reparenting refuses.",
                idempotency=contract.transaction.idempotency
                + " UUID accepted-project intent binds actor/scope/reason/expected tokens and full basis. Replay requires fresh permission and exact current basis, emits no repeated output.",
            ),
            errors=replace(
                contract.errors,
                domain_codes=(
                    *contract.errors.domain_codes,
                    *(
                        f"{service.name}.{c}"
                        for c in (
                            (
                                "participant_required",
                                "scope_mismatch",
                                "stale_state",
                                "invalid_request",
                                "idempotency_conflict",
                                "completion_required",
                            )
                            if field
                            else ("accepted_field_completion_required",)
                        )
                    ),
                ),
            ),
            migration=replace(
                contract.migration,
                verification=(contract.migration.verification or "")
                + " Migration682 freezes exact five-table adoption including nullable xid8 creation marker without default/identity/generated expression, backfills only explicit project enrollments, and adds immutable lineage/full-coverage/receipt/status SQL guards; PostgreSQL fresh/predecessor/race tests required.",
            ),
            design_refs=(
                *contract.design_refs,
                "docs/designs/ACCEPTED_PROJECT_FIELD_COMPLETION.md",
            ),
            test_refs=(
                *contract.test_refs,
                "tests/test_project_field_completion.py",
                "tests/integration/test_project_field_completion_acceptance.py",
            ),
        ),
    )


from app.services.sot_manifest import (
    AuthorityMigrationState,
    ErrorContract,
    MigrationContract,
    ProjectionContract,
    ServiceContract,
    TransactionContract,
    TransactionMode,
)

SERVICES = (
    SOTService(
        name="ui.project_field_completion",
        module="app.services.project_field_completion_projection",
        owns=("accepted project field completion staff form",),
        depends_on=(FIELD_OWNER, "ui.action_form_contracts", "ui.form_contracts"),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="accepted project field completion staff form",
                    role=OwnerRole.RESOLVER,
                    input_names=("protected accepted project completion preview",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="protected accepted project completion preview",
                    owner=FIELD_OWNER,
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Fresh exact-purpose staff preview with current owner-produced ActionReadiness, project/task revisions and immutable requirement token.",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary="Pure typed form composition; adapters release their read transaction before the one canonical command.",
                locking="No projection locks; protected owner preview owns evidence reads.",
                idempotency="Original submitted intent/tokens/reason remain unchanged on error; explicit reload creates a new preview.",
                retries="Stale draft disabled; refresh explicitly for a new intent.",
            ),
            errors=ErrorContract(
                domain_codes=(), mapping_owner="Project API and admin adapters"
            ),
            projections=(
                ProjectionContract(
                    name="accepted project completion form",
                    input_names=("protected accepted project completion preview",),
                    writer="ui.project_field_completion",
                    freshness="Fresh protected preview per request; command revalidates all evidence.",
                    stale_behavior="Preserve original raw values and stable intent, disable submit after refusal.",
                    drift_signal="Owner readiness or command refusal.",
                    rebuild_operation="Reload protected preview.",
                    repair_owner=FIELD_OWNER,
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="ui.project_field_completion",
                verification="Typed form and browser adapter tests; no acceptance proof inferred in presentation.",
            ),
            steward="project operations",
            design_refs=("docs/designs/ACCEPTED_PROJECT_FIELD_COMPLETION.md",),
            test_refs=("tests/test_project_field_completion.py",),
        ),
    ),
)
