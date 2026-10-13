"""Typed supervisor completion presentation declaration."""

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    MigrationContract,
    OwnerRole,
    ProjectionContract,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
)

SERVICES = (
    SOTService(
        name="ui.field_completion_review_projection",
        module="app.services.field_completion_review_projection",
        owns=("staff completion review action and history presentation",),
        depends_on=(
            "operations.field_completion",
            "ui.action_form_contracts",
            "ui.form_contracts",
        ),
        notes="Typed owner-produced native/engineering enrollment, explicit project/work-order policy, versioned criteria, current/history evidence, native failed-scope replacement candidates/criterion coverage/lineage and ActionReadiness only; no ORM, permission policy or business transition.",
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="staff completion review action and history presentation",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "protected completion action snapshot",
                        "immutable completion command receipt",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="immutable completion command receipt",
                    owner="operations.field_completion",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Optional typed canonical CompletionReviewOutcome command outcome, including exact historical replay after current authorization; no synthesized browser receipt.",
                ),
                AuthorityInput(
                    name="protected completion action snapshot",
                    owner="operations.field_completion",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Exact-purpose protected native/engineering CompletionWorkspace or explicitly scoped CompletionPolicySnapshot with owner-derived readiness.",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary="Pure typed composition; adapters invoke protected owner reads in read transactions and release them before atomic commands.",
                locking="Owner reads revalidate exact-purpose staff admission and exact enrollment provenance, immutable criteria revision and completed execution lineage; current acceptance uses engineering freshness. Projection acquires no locks.",
                idempotency="Submitted intent UUID and exact preview values preserved on errors; initial form creates a new unpersisted intent.",
                retries="Refresh explicitly for new intent after stale evidence; exact command retry governed by canonical receipts.",
            ),
            errors=ErrorContract(
                domain_codes=(),
                mapping_owner="API and admin completion review adapters",
            ),
            projections=(
                ProjectionContract(
                    name="staff completion review page",
                    input_names=(
                        "protected completion action snapshot",
                        "immutable completion command receipt",
                    ),
                    writer="ui.field_completion_review_projection",
                    freshness="Owner snapshot evaluated per request; mutation revalidates all authoritative inputs.",
                    stale_behavior="Preserve raw choices, stable intent and original tokens; disable stale submit pending explicit reload. Unavailable protected reads render only the submitted draft, without any prior protected snapshot or receipt.",
                    drift_signal="Owner domain stale/refusal and generation/digest mismatch.",
                    rebuild_operation="Re-read protected owner snapshot and compose typed page.",
                    repair_owner="operations.field_completion",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                new_owner="ui.field_completion_review_projection",
                old_owner="No native staff completion review adapter",
                verification="Typed capability, CSRF, form-preservation and template tests.",
                cutover_gate="Root validates composed adapters and authorized browser journey before publication.",
                fallback_retirement="Field execution completion remains separate; no status or operational closure fallback.",
            ),
            steward="field operations and project supervision",
            design_refs=("docs/designs/FIELD_COMPLETION_REVIEW_ADAPTERS.md",),
            test_refs=(
                "tests/test_field_completion_review_adapters.py",
                "tests/test_failed_scope_replacement_adapters.py",
                "tests/architecture/test_field_completion_review_adapter_boundary.py",
            ),
        ),
    ),
)
