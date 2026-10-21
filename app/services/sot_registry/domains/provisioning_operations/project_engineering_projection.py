"""Typed staff engineering projection declaration."""

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
        name="ui.project_engineering_projection",
        module="app.services.project_engineering_projection",
        owns=("staff project engineering action and history presentation",),
        depends_on=(
            "operations.project_engineering_plans",
            "operations.project_work_order_handoff",
            "ui.action_form_contracts",
            "ui.form_contracts",
        ),
        notes="Typed owner-produced current/history evidence and ActionReadiness only; no ORM, permission policy or business transition.",
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="staff project engineering action and history presentation",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "protected engineering action snapshot",
                        "immutable issuance receipt",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="immutable issuance receipt",
                    owner="operations.project_work_order_handoff",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Optional typed canonical ProjectWorkOrderIssued command outcome, including exact historical replay after current authorization; no synthesized browser receipt.",
                ),
                AuthorityInput(
                    name="protected engineering action snapshot",
                    owner="operations.project_engineering_plans",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Exact-purpose protected EngineeringWorkspace or current-approved-only ProjectDispatchPreview with owner-derived readiness.",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary="Pure typed composition; adapters invoke protected owner reads in read transactions and release them before atomic commands.",
                locking="Owner reads lock Project then task then plan then native targets; projection acquires no locks.",
                idempotency="Submitted intent UUID and exact preview values preserved on errors; initial form creates a new unpersisted intent.",
                retries="Refresh explicitly for new intent after stale evidence; exact command retry governed by canonical receipts.",
            ),
            errors=ErrorContract(
                domain_codes=(), mapping_owner="API and admin engineering adapters"
            ),
            projections=(
                ProjectionContract(
                    name="staff engineering page",
                    input_names=(
                        "protected engineering action snapshot",
                        "immutable issuance receipt",
                    ),
                    writer="ui.project_engineering_projection",
                    freshness="Owner snapshot evaluated per request; mutation revalidates all authoritative inputs.",
                    stale_behavior="Preserve raw choices, stable intent and original tokens; disable stale submit pending explicit reload.",
                    drift_signal="Owner domain stale/refusal and generation/digest mismatch.",
                    rebuild_operation="Re-read protected owner snapshot and compose typed page.",
                    repair_owner="operations.project_engineering_plans",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                new_owner="ui.project_engineering_projection",
                old_owner="No native staff engineering adapter",
                verification="Typed capability, CSRF, form-preservation and template tests.",
                cutover_gate="Root validates composed adapters and authorized browser journey before publication.",
                fallback_retirement="Customer work-order creation remains separate; no Subscriber/Ticket inference.",
            ),
            steward="project operations and network engineering",
            design_refs=("docs/designs/PROJECT_ENGINEERING_STAFF_ADAPTERS.md",),
            test_refs=(
                "tests/test_project_engineering_adapters.py",
                "tests/architecture/test_project_engineering_adapter_boundary.py",
            ),
        ),
    ),
)
