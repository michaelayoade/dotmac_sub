"""Explicit approved project infrastructure issuance coordination."""

from app.services.project_work_order_handoff_contracts import CONCERN, OWNER
from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    EventContract,
    MigrationContract,
    OwnerRole,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
)

SERVICES = (
    SOTService(
        name=OWNER,
        module="app.services.project_work_order_handoff",
        owns=(CONCERN, "protected completion issuance evidence"),
        depends_on=(
            "operations.project_lifecycle",
            "operations.project_engineering_plans",
            "operations.work_order_commands",
            "auth.staff_permission_admission",
            "events.dispatcher",
            "observability.audit_log",
        ),
        notes="Explicit infrastructure-only issuance from a current engineering approval; no synthesized Subscriber/Ticket, assignment, notification or completion effects.",
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=CONCERN,
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "authenticated staff Person",
                        "native project engineering origin",
                        "immutable engineering plan evidence",
                        "canonical project WorkOrder creation",
                    ),
                    canonical_writer=OWNER,
                ),
                ConcernContract(
                    name="protected completion issuance evidence",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "authenticated staff Person",
                        "immutable issuance receipt",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="immutable issuance receipt",
                    owner=OWNER,
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Exact native issuance UUID, WorkOrder UUID and public ID; immutable approved project/task/plan/revision/decision/digest/target/purpose/technology evidence. Fresh completion READ or WRITE-purpose staff admission; this query grants no issuance eligibility.",
                ),
                AuthorityInput(
                    name="canonical project WorkOrder creation",
                    owner="operations.work_order_commands",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Tagged typed project-native create participant and immutable outcome; canonical template validation with no Subscriber/Ticket or implicit assignment/notification.",
                ),
                AuthorityInput(
                    name="authenticated staff Person",
                    owner="auth.staff_permission_admission",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="Canonical PermissionAdmission freshly revalidated against current active staff RBAC aliases/wildcards and write leave restriction; canonical active Person binding required.",
                ),
                AuthorityInput(
                    name="native project engineering origin",
                    owner="operations.project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Typed Project-before-task origin snapshot, updated-at preview tokens, exact template bindings and current structural ProjectInfrastructure target; active/current eligibility is never inferred from external labels.",
                ),
                AuthorityInput(
                    name="immutable engineering plan evidence",
                    owner="operations.project_engineering_plans",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Exact current approved revision/decision/digest and structural location/scope resolved under owner locks by the typed dispatch participant.",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary="One issuance execute_owner_command; canonical approval read participant and WorkOrder create participant; immutable issuance receipt, audit and versioned outbox are atomic.",
                locking="Owner-global advisory receipt-key lock, Project then optional task then plan head then native inventory/location in stable table/UUID order including PON parent OLT; approval participant retains locks until commit.",
                idempotency="Owner-global unique key and normalized intent fingerprint; current authorization before historical receipt replay; no stale fallback on new keys.",
                retries="Retry whole command only for database concurrency failures. Authorization, stale origin/head/policy, ambiguous scope and conflicting keys are final.",
            ),
            errors=ErrorContract(
                domain_codes=tuple(
                    f"{OWNER}.{x}"
                    for x in (
                        "denied",
                        "scope_mismatch",
                        "invalid_request",
                        "stale_state",
                        "idempotency_conflict",
                    )
                )
                + owner_command_boundary_error_codes(OWNER),
                mapping_owner="Future staff dispatch adapters; no public route in this slice",
                fail_closed_on=(
                    "unresolved project policy",
                    "inactive mismatched superseded origin",
                    "stale approval or changed engineering binding",
                    "independence violation under selected policy",
                    "current on_hold Project or blocked/done task",
                    "revoked approval",
                    "conflicting idempotency key",
                ),
            ),
            events=EventContract(
                event_types=("project_work_order.issued",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Opaque native issuance/work-order/origin/plan IDs and exact digest; no criteria content or file payload.",
                replay="Historical typed receipt only after current authorization; no new event or mutated approval on retry.",
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUTOVER_READY,
                new_owner=OWNER,
                old_owner="No native approved project infrastructure issuance",
                verification="Migration676 additive immutable issuance receipt/provenance with exact origin and approval composite foreign keys; no backfill.",
                cutover_gate="Coordinator must pass real fresh/predecessor PostgreSQL migration and concurrency acceptance before adoption; no local PG claim.",
                fallback_retirement="No template/status/vendor-route/file-name inference or unsafe StoredFile UUID fallback.",
            ),
            steward="network engineering and project operations",
            design_refs=(
                "docs/designs/PROJECT_ENGINEERING_PLANS.md",
                "docs/designs/PROJECT_INFRASTRUCTURE_HANDOFF.md",
            ),
            test_refs=(
                "tests/test_project_work_order_handoff.py",
                "tests/architecture/test_project_work_order_handoff_boundary.py",
                "tests/integration/test_project_work_order_issuance_acceptance.py",
            ),
        ),
    ),
)
