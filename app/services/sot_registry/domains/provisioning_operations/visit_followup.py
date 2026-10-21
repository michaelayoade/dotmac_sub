"""Accountable failed visits remain separate from chat, issuance and closure."""

from app.services.field.visit_followup_contracts import CONCERN, OWNER
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

INPUTS = (
    AuthorityInput(
        "exact failed field source",
        "operations.field_completion",
        AuthorityKind.AUTHORITATIVE_RECORD,
        "Protected native FieldJobEvent unable_to_complete ID, work-order binding, immutable receipt/assignment and reason; no status/name/chat inference.",
    ),
    AuthorityInput(
        "exact completion rejection and approved replacement",
        "operations.field_completion",
        AuthorityKind.AUTHORITATIVE_RECORD,
        "Protected immutable rejection/submission/enrollment binding. Optional typed current_replacement_evidence owns exact current approved679 lineage and full engineering freshness; followup does not rederive acceptance.",
    ),
    AuthorityInput(
        "current staff authorization",
        "auth.staff_permission_admission",
        AuthorityKind.CONTROL_INPUT,
        "Fresh same-principal dispatch READ plus completion READ for unified projections; dispatch ASSIGN for writes, completion READ for rejected sources, completion WRITE for optional replacement linkage; active native staff Person and leave checks after waits.",
    ),
    AuthorityInput(
        "native work-order fence",
        "operations.work_order_commands",
        AuthorityKind.AUTHORITATIVE_RECORD,
        "Typed canonical WorkOrderLinkageFence before native WorkOrder row lock; protected WorkOrder identity projection; no WorkOrder mutation.",
    ),
    AuthorityInput(
        "active responsible team and native member",
        "operations.service_team_lifecycle",
        AuthorityKind.AUTHORITATIVE_RECORD,
        "Protected ServiceTeam/ServiceTeamMember active exact selected team; optional current native active staff SystemUser/Person nonvendor binding, checked under member lock. Caller explicitly supplies due time.",
    ),
)
SERVICES = (
    SOTService(
        name=OWNER,
        module="app.services.field.visit_followup",
        owns=(
            CONCERN,
            "failed visit pending intake and accountable queue",
            "failed visit responsibility options",
        ),
        depends_on=tuple(dict.fromkeys(x.owner for x in INPUTS))
        + ("events.dispatcher", "observability.audit_log"),
        notes="Human reviewed obligations only; no job, appointment, notifications or customer closure.",
        contract=ServiceContract(
            concerns=tuple(
                ConcernContract(
                    name,
                    OwnerRole.COMMAND_WRITER if name == CONCERN else OwnerRole.RESOLVER,
                    tuple(x.name for x in INPUTS),
                    OWNER if name == CONCERN else None,
                )
                for name in (
                    CONCERN,
                    "failed visit pending intake and accountable queue",
                    "failed visit responsibility options",
                )
            ),
            authoritative_inputs=INPUTS,
            transaction=TransactionContract(
                TransactionMode.OWNER_MANAGED,
                "One execute_owner_command atomically persists obligation/head, append-only revision, receipt, restricted audit and record-only typed custom outbox envelope.",
                "Intent advisory lock; optional completion participant FIRST (Ticket fence→Ticket→Project→ordered WorkOrders); canonical linkage fence→WorkOrder→followup→team→member. Recheck exact source and same-principal admissions after waits; no WO→completion inversion.",
                "Global UUID intent fingerprint binds actor, exact source/revision, responsibility, due and reason. Fresh authorization precedes exact historical replay; conflict refuses.",
                "Retry whole transaction only for database concurrency failures; stale revisions require explicit reload/new intent.",
            ),
            errors=ErrorContract(
                tuple(
                    f"{OWNER}.{code}"
                    for code in (
                        "denied",
                        "invalid_request",
                        "not_found",
                        "source_unavailable",
                        "already_accounted",
                        "stale_revision",
                        "invalid_replacement",
                        "idempotency_conflict",
                        "responsibility_unavailable",
                    )
                )
                + owner_command_boundary_error_codes(OWNER),
                "Staff followup adapter",
                fail_closed_on=(
                    "missing responsibility or due",
                    "non-native/inactive staff",
                    "stale revision",
                    "unapproved replacement",
                    "different principal",
                ),
            ),
            events=EventContract(
                ("custom",),
                1,
                "events.dispatcher",
                "Typed operations.field_visit_followup.recorded.v1 trigger; opaque exact IDs and disposition only. Record-only dispatcher evidence executes no handlers, callbacks or customer actions.",
                "Exact retry emits no second audit/event and retains original revision.",
            ),
            migration=MigrationContract(
                AuthorityMigrationState.CUTOVER_READY,
                OWNER,
                "No accountable native failed-source followup owner",
                "680 additive native tables and immutable source/revisions/receipts; pending intake reads original sources without backfill.",
                "Coordinator real PostgreSQL fresh/predecessor migrations and contention acceptance required.",
                "Chat metadata and same-ticket/job names never establish an obligation or replacement.",
            ),
            steward="field operations",
            design_refs=("docs/designs/FIELD_VISIT_FOLLOWUP.md",),
            test_refs=(
                "tests/test_field_visit_followup.py",
                "tests/integration/test_field_visit_followup_pg.py",
            ),
        ),
    ),
)
