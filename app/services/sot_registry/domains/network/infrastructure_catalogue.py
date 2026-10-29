"""Shared native infrastructure selection contract."""

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

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="network.infrastructure_catalogue",
        module="app.services.infrastructure_catalogue",
        owns=(
            "native infrastructure selection and exact-reference resolution",
            "ticket location suggestions",
        ),
        depends_on=("network.identity",),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="native infrastructure selection and exact-reference resolution",
                    role=OwnerRole.RESOLVER,
                    input_names=("native inventory identities",),
                ),
                ConcernContract(
                    name="ticket location suggestions",
                    role=OwnerRole.RESOLVER,
                    input_names=("native inventory identities",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="native inventory identities",
                    owner="network.identity",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Native NAS, POP, wireless AP, fiber AP, OLT, PON and FDH records; role and active flags qualify new selections. PON parent OLT is a native identity-bearing eligibility dependency returned in typed LockedInfrastructureScope, never inferred from display context.",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary="Bounded typed search, exact-reference query and lock_reference participant in the caller's read or owner transaction; no inventory writes or transaction completion",
                locking="lock_reference acquires native target and reviewed related POP location locks in stable table/UUID order; PON also locks its parent OLT and rechecks parent identity after acquisition. Ticket creation holds a shared lock on its exact selected native location before rechecking role and active state. Caller holds locks through its owner command.",
                idempotency="Identical queries over identical inventory return identical ordered results",
                retries="Caller retries its whole owner command on transient database concurrency failure or changed parent during lock acquisition; no internal transaction retry.",
            ),
            errors=ErrorContract(
                domain_codes=(
                    "network.infrastructure_catalogue.stale_scope",
                    "network.infrastructure_catalogue.unavailable",
                ),
                mapping_owner="customer/project adapters and calling engineering owner",
                fail_closed_on=(
                    "invalid query or reference",
                    "inactive or role-mismatched locked target",
                    "PON parent changed during lock acquisition",
                ),
            ),
            projections=(
                ProjectionContract(
                    name="infrastructure picker options",
                    input_names=("native inventory identities",),
                    writer="network.infrastructure_catalogue",
                    freshness="Direct database read, no cached inventory",
                    stale_behavior="New selections reject inactive or role-mismatched records; historical labels may include inactive records",
                    drift_signal="Exact reference cannot resolve to its declared inventory kind",
                    rebuild_operation="Repeat the typed catalogue query",
                    repair_owner="network.infrastructure_catalogue",
                ),
                ProjectionContract(
                    name="ticket location suggestions",
                    input_names=("native inventory identities",),
                    writer="network.infrastructure_catalogue",
                    freshness="Direct database read, no cached inventory",
                    stale_behavior="Only active BTS-linked POP sites and active wireless or fiber access points are suggested; the Ticket owner rejects an exact reference that is no longer eligible at creation",
                    drift_signal="A suggestion no longer matches its native source role or active status",
                    rebuild_operation="Repeat the typed ticket location suggestion query",
                    repair_owner="network.infrastructure_catalogue",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner="customer-page inventory search SQL",
                new_owner="network.infrastructure_catalogue",
                verification="Customer and project selector parity and unavailable-reference tests",
                cutover_gate="Both search adapters delegate to the shared query",
                fallback_retirement="Customer-specific search SQL removed",
            ),
            steward="network inventory and service delivery",
            design_refs=(
                "docs/designs/PROJECT_INFRASTRUCTURE_SCOPE.md",
                "docs/designs/PROJECT_ENGINEERING_PLANS.md",
                "docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md",
            ),
            test_refs=(
                "tests/test_project_infrastructure.py",
                "tests/test_project_engineering_plans.py",
                "tests/test_support_ticket_typeahead.py",
                "tests/architecture/test_project_infrastructure_boundary.py",
            ),
        ),
    ),
)
