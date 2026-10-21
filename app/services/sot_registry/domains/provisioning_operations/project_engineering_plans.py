"""Native engineering approval evidence; independent of work-order issuance."""

from app.services.project_engineering_plan_contracts import CONCERN, OWNER
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
        module="app.services.project_engineering_plans",
        owns=(
            CONCERN,
            "current exact project engineering approval resolution",
            "protected project engineering action and history reads",
            "current approved dispatch preview",
            "current issued engineering completion review",
            "protected support completion engineering preview",
        ),
        depends_on=(
            "operations.project_lifecycle",
            "network.infrastructure_catalogue",
            "auth.staff_permission_admission",
            "events.dispatcher",
            "observability.audit_log",
        ),
        notes="Immutable criteria-content-only native fiber/radio plan approval; no file evidence, work-order issuance, assignment, provisioning or financial authority. Per-Project explicit policy revisions; no global fallback or reviewer-independence bypass.",
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=CONCERN,
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "authenticated staff Person",
                        "native project engineering origin",
                        "immutable engineering plan evidence",
                    ),
                    canonical_writer=OWNER,
                ),
                ConcernContract(
                    name="current exact project engineering approval resolution",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "authenticated staff Person",
                        "native project engineering origin",
                        "immutable engineering plan evidence",
                    ),
                ),
                *(
                    ConcernContract(
                        name=name,
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "authenticated staff Person",
                            "native project engineering origin",
                            "immutable engineering plan evidence",
                        ),
                    )
                    for name in (
                        "protected project engineering action and history reads",
                        "current approved dispatch preview",
                        "current issued engineering completion review",
                        "protected support completion engineering preview",
                    )
                ),
            ),
            authoritative_inputs=(
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
                    owner=OWNER,
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Native revision, ordered criterion, append-only decision, generation head, exact per-Project versioned approval policy and durable historical receipt.",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary="Exactly one execute_owner_command for draft, decision or policy publication; receipt/audit/versioned outbox event atomic. Current resolution never writes and holds native Project/task/head locks within caller read transaction; typed issuance participant requires the active handoff owner and hardwired fresh dispatch admission, sharing the current-approval resolver without granting broader engineering reads. Narrow dispatch GET discovery exposes only current approved evidence without a command executor. Completion review uses distinct hardwired completion-review read/write capabilities; only the write participant requires active operations.field_completion; resolve_current_for_completion_review is the narrow public WRITE-purpose preview sharing freshness without an executor or READ floor. The support-only batch staging participant requires active support.ticket_lifecycle without impersonating a staff permission. The separate public preview_current_batch_for_support_completion(SupportCompletionEngineeringPreviewBatchQuery) freshly revalidates support:ticket:update WRITE-purpose staff admission before and after shared exact freshness checks; it requires no executor, engineering READ floor or generic owner bypass. Both share the canonical batch kernel; it locks all Project/Task roots, all plans, then all native inventory/location/PON parents globally before invoking the shared exact freshness kernel. Completion skips new-issuance status eligibility while preserving exact current approval, origin binding, revocation and target freshness. Protected per-action workspace reads use exact READ/POLICY/WRITE/APPROVE/REVOKE admission; revocation/history retain historical scope.",
                locking="Project then optional task then plan head then policy head then inventory/location in stable native-table/UUID order, including PON parent OLT. Per-Project policy publication locks Project then policy head. Immutable evidence cannot change after reading.",
                idempotency="Owner-global unique key and normalized intent fingerprint; current authorization before historical receipt replay; no stale fallback on new keys.",
                retries="Retry whole command only for database concurrency failures. Authorization, stale origin/head/policy, ambiguous scope and conflicting keys are final.",
            ),
            errors=ErrorContract(
                domain_codes=tuple(
                    f"{OWNER}.{x}"
                    for x in (
                        "denied",
                        "not_found",
                        "invalid_request",
                        "stale_state",
                        "idempotency_conflict",
                        "unapproved",
                    )
                )
                + owner_command_boundary_error_codes(OWNER),
                mapping_owner="Typed project engineering API/admin adapters and completion owner",
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
                event_types=("project_engineering_plan.changed",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Opaque native IDs, exact digest, generation and action; no criteria content or file payload.",
                replay="Historical typed receipt only after current authorization; no new event or mutated approval on retry.",
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUTOVER_READY,
                new_owner=OWNER,
                old_owner="No native engineering plan approval authority",
                verification="Migration675 additive native tables, exact task/project FK and PostgreSQL immutable evidence triggers; focused unit and architecture evidence.",
                cutover_gate="Coordinator must pass real fresh/predecessor PostgreSQL migration and concurrency acceptance before adoption; no local PG claim.",
                fallback_retirement="No template/status/vendor-route/file-name inference or unsafe StoredFile UUID fallback.",
            ),
            steward="network engineering and project operations",
            design_refs=(
                "docs/designs/PROJECT_ENGINEERING_PLANS.md",
                "docs/designs/PROJECT_INFRASTRUCTURE_HANDOFF.md",
            ),
            test_refs=(
                "tests/test_project_engineering_plans.py",
                "tests/architecture/test_project_engineering_plan_boundary.py",
            ),
        ),
    ),
)
