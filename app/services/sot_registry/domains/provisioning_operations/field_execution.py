"""Native vendor and technician work-order execution contracts."""

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

_EXECUTION_INPUTS = (
    AuthorityInput(
        name="authenticated field execution actor",
        owner="operations.field_work_order_access",
        kind=AuthorityKind.AUTHORITATIVE_RECORD,
        source="Active SystemUser, unambiguous active FieldVendorUser/native Vendor or TechnicianProfile; vendor membership never falls back to technician access.",
    ),
    AuthorityInput(
        name="current native work-order assignment",
        owner="operations.work_order_commands",
        kind=AuthorityKind.AUTHORITATIVE_RECORD,
        source="Locked native WorkOrder and the single current WorkOrderAssignmentQueue target; imported metadata grants no access.",
    ),
    AuthorityInput(
        name="field execution intent",
        owner="auth.permission_gate",
        kind=AuthorityKind.CONTROL_INPUT,
        source="Frozen authenticated command/query, stable client identity and exact normalized payload.",
    ),
)


def execution_contract(
    owner: str,
    concerns: tuple[tuple[str, OwnerRole], ...],
    *,
    mode: TransactionMode = TransactionMode.OWNER_MANAGED,
    codes: tuple[str, ...] = (),
    events: tuple[str, ...] = (),
    tests: tuple[str, ...] = ("tests/test_vendor_field_work_orders.py",),
) -> ServiceContract:
    """Contracts sharing the same native assignment and actor trust boundary."""
    writable = {OwnerRole.COMMAND_WRITER, OwnerRole.AUTHORITATIVE_RECORD}
    return ServiceContract(
        concerns=tuple(
            ConcernContract(
                name=name,
                role=role,
                input_names=tuple(i.name for i in _EXECUTION_INPUTS),
                canonical_writer=owner if role in writable else None,
            )
            for name, role in concerns
        ),
        authoritative_inputs=_EXECUTION_INPUTS,
        transaction=TransactionContract(
            mode=mode,
            boundary="Each public mutation enters execute_owner_command exactly once on a transaction-free session; nested helpers flush only. Queries do not commit."
            if mode == TransactionMode.OWNER_MANAGED
            else "Read-only resolution within the requesting owner transaction.",
            locking="Mutations lock the work order and revalidate actor and current assignment before replay or evidence access.",
            idempotency="Exact client identities replay stable evidence with current authorized job state; changed payloads conflict. Authorization is rechecked on every replay.",
            retries="Retry database serialization/deadlock failures as whole commands; never retry authorization or payload conflicts.",
        ),
        errors=ErrorContract(
            domain_codes=(*codes, *owner_command_boundary_error_codes(owner))
            if mode == TransactionMode.OWNER_MANAGED
            else codes,
            mapping_owner="Field HTTP adapters",
            fail_closed_on=(
                "inactive or ambiguous actor",
                "unassigned or reassigned work order",
                "metadata-only assignment",
                "conflicting replay payload",
            ),
        ),
        events=EventContract(
            event_types=events,
            schema_version=1,
            delivery_owner="events.dispatcher",
            compatibility="Version 1 preserves native work-order and explicit actor identity.",
            replay="Durable owner rows and stable client references reconstruct outcomes.",
        )
        if events
        else None,
        migration=MigrationContract(
            state=AuthorityMigrationState.CUTOVER_READY,
            new_owner=owner,
            old_owner="technician-only field commands and metadata vendor access",
            verification="Migration 668 plus native vendor execution and technician regression tests.",
            cutover_gate="Fresh and predecessor PostgreSQL migration checks and the authenticated vendor journey pass.",
            fallback_retirement="Vendor metadata authorization and fabricated technician/person identity are removed.",
        ),
        steward="field operations",
        design_refs=("docs/designs/VENDOR_WORK_ORDER_EXECUTION.md",),
        test_refs=tests,
    )


FIELD_COMPLETION_CONTRACT = execution_contract(
    "operations.field_completion",
    (
        ("field job completion eligibility", OwnerRole.POLICY),
        ("field completion evidence requirements", OwnerRole.POLICY),
        ("field job completion transitions", OwnerRole.COMMAND_WRITER),
    ),
    events=("work_order.field_outcome_recorded",),
    codes=(
        "operations.field_completion.idempotency_conflict",
        "operations.field_completion.conflict",
        "operations.field_work_order_access.denied",
        "operations.field_work_order_access.not_found",
        "operations.field_work_order_access.invalid_request",
    ),
)

SERVICES = (
    SOTService(
        name="operations.field_work_order_access",
        module="app.services.field.work_order_access",
        owns=(
            "authenticated field execution actor",
            "current work-order field assignment scope",
        ),
        depends_on=(
            "auth.permission_gate",
            "operations.work_order_commands",
            "auth.vendor_user_provisioning",
        ),
        contract=execution_contract(
            "operations.field_work_order_access",
            (
                ("authenticated field execution actor", OwnerRole.RESOLVER),
                ("current work-order field assignment scope", OwnerRole.POLICY),
            ),
            mode=TransactionMode.READ_ONLY,
            codes=(
                "operations.field_work_order_access.denied",
                "operations.field_work_order_access.not_found",
                "operations.field_work_order_access.conflict",
                "operations.field_work_order_access.invalid_request",
            ),
        ),
    ),
    SOTService(
        name="operations.field_worklogs",
        module="app.services.field.worklogs",
        owns=("native field worklog submission",),
        depends_on=("operations.field_work_order_access",),
        contract=execution_contract(
            "operations.field_worklogs",
            (("native field worklog submission", OwnerRole.COMMAND_WRITER),),
            codes=(
                "operations.field_worklogs.invalid_request",
                "operations.field_worklogs.idempotency_conflict",
                "operations.field_worklogs.conflict",
            ),
            events=("field.worklogs_submitted",),
        ),
    ),
    SOTService(
        name="operations.field_attachments",
        module="app.services.field.attachments",
        owns=("native field attachment creation", "native field attachment deletion"),
        depends_on=("operations.field_work_order_access",),
        contract=execution_contract(
            "operations.field_attachments",
            (
                ("native field attachment creation", OwnerRole.COMMAND_WRITER),
                ("native field attachment deletion", OwnerRole.COMMAND_WRITER),
            ),
            codes=(
                "operations.field_attachments.not_found",
                "operations.field_attachments.idempotency_conflict",
            ),
            events=("field.attachment_created", "field.attachment_deleted"),
        ),
    ),
    SOTService(
        name="operations.field_jobs",
        module="app.services.field.jobs",
        owns=("field job location correction",),
        depends_on=("operations.field_work_order_access",),
        contract=execution_contract(
            "operations.field_jobs",
            (("field job location correction", OwnerRole.COMMAND_WRITER),),
            events=("field.job_location_corrected",),
        ),
    ),
)

WORK_ORDER_COMMAND_CONTRACT = ServiceContract(
    concerns=(
        ConcernContract(
            name="native work-order creation and header commands",
            role=OwnerRole.COMMAND_WRITER,
            input_names=(
                "native work-order command",
                "canonical native work-order assignment",
            ),
            canonical_writer="operations.work_order_commands",
        ),
        ConcernContract(
            name="native work-order project binding",
            role=OwnerRole.COMMAND_WRITER,
            input_names=(
                "native work-order command",
                "canonical native work-order assignment",
            ),
            canonical_writer="operations.work_order_commands",
        ),
        ConcernContract(
            name="native work-order project-task binding",
            role=OwnerRole.COMMAND_WRITER,
            input_names=(
                "native work-order command",
                "canonical native work-order assignment",
            ),
            canonical_writer="operations.work_order_commands",
        ),
        ConcernContract(
            name="work-order as-built evidence requirement",
            role=OwnerRole.COMMAND_WRITER,
            input_names=(
                "native work-order command",
                "canonical native work-order assignment",
            ),
            canonical_writer="operations.work_order_commands",
        ),
        ConcernContract(
            name="work-order assignment decisions and projection",
            role=OwnerRole.COMMAND_WRITER,
            input_names=(
                "native work-order command",
                "canonical native work-order assignment",
            ),
            canonical_writer="operations.work_order_commands",
        ),
        ConcernContract(
            name="work-order assignment-queue transitions",
            role=OwnerRole.COMMAND_WRITER,
            input_names=(
                "native work-order command",
                "canonical native work-order assignment",
            ),
            canonical_writer="operations.work_order_commands",
        ),
        ConcernContract(
            name="work-order staff/team tag notification consequence",
            role=OwnerRole.EVENT_POLICY,
            input_names=(
                "native work-order command",
                "canonical native work-order assignment",
            ),
        ),
    ),
    authoritative_inputs=(
        AuthorityInput(
            name="native work-order command",
            owner="auth.permission_gate",
            kind=AuthorityKind.CONTROL_INPUT,
            source="Authenticated typed assignment command and CommandContext, or validated native header command.",
        ),
        AuthorityInput(
            name="canonical native work-order assignment",
            owner="operations.work_order_commands",
            kind=AuthorityKind.AUTHORITATIVE_RECORD,
            source="Native WorkOrder, single active assignment queue row, active native Vendor or TechnicianProfile, and immutable assignment receipt; vendor metadata is provenance only.",
        ),
    ),
    transaction=TransactionContract(
        mode=TransactionMode.OWNER_MANAGED,
        boundary="Public assignment and queue commands enter execute_owner_command once on a transaction-free session. Header commands own their transaction; nested network planning uses registered flush-only _stage_assignment participant.",
        locking="Lock WorkOrder before current assigned queue rows and target; partial unique index prevents competing current assignments.",
        idempotency="CommandContext identity stores exact payload fingerprint and immutable assignment outcome. Preview revision prevents stale confirmation.",
        retries="Retry serialization/deadlock as a complete command; stale preview, target, authorization and replay conflicts require review.",
    ),
    errors=ErrorContract(
        domain_codes=(
            "assignment_idempotency_conflict",
            "assignment_queue_not_found",
            "assignment_rule_unavailable",
            "assignment_target_unavailable",
            "assignment_transaction_required",
            "automated_project_task_rejected",
            "invalid_assignment_queue_status",
            "invalid_assignment_schedule",
            "invalid_assignment_status",
            "invalid_assignment_target",
            "invalid_evidence_policy",
            "origin_ticket_not_found",
            "origin_ticket_subscriber_mismatch",
            "project_binding_immutable",
            "project_not_found",
            "project_subscriber_mismatch",
            "project_subscriber_missing",
            "project_task_binding_immutable",
            "project_task_not_found",
            "project_task_project_mismatch",
            "stale_assignment",
            "work_order_not_assignable",
            "work_order_not_found",
        )
        + owner_command_boundary_error_codes("operations.work_order_commands"),
        mapping_owner="Dispatch API/web, field manager and network coordinator adapters",
        fail_closed_on=(
            "invalid or inactive target",
            "ambiguous target",
            "stale preview",
            "conflicting command identity",
        ),
    ),
    events=EventContract(
        event_types=("work_order.assigned", "work_order.assignment_queue_transitioned"),
        schema_version=1,
        delivery_owner="events.dispatcher",
        compatibility="Additive native target and actor identifiers.",
        replay="Immutable assignment receipts and queue history preserve original outcomes.",
    ),
    migration=MigrationContract(
        state=AuthorityMigrationState.CUTOVER_READY,
        new_owner="operations.work_order_commands",
        old_owner="technician-only assignment and metadata-only vendor hints",
        verification="Migration 668 and assignment scope, replay, concurrency and actor constraint tests.",
        cutover_gate="Fresh/predecessor PostgreSQL checks and vendor/technician journeys pass.",
        fallback_retirement="No automatic metadata backfill; existing unsupported metadata assignments require explicit reviewed owner assignment.",
    ),
    steward="field operations",
    design_refs=("docs/designs/VENDOR_WORK_ORDER_EXECUTION.md",),
    test_refs=(
        "tests/test_vendor_work_order_assignment.py",
        "tests/test_work_order_commands.py",
        "tests/architecture/test_work_order_command_ownership.py",
    ),
)

ASSIGNMENT_RECORDS = SOTService(
    name="operations.work_order_assignment_records",
    module="app.services.work_order_commands",
    owns=("native work-order assignment persistence",),
    depends_on=("operations.work_order_commands",),
    notes="Only operations.work_order_commands and network.fiber_field_verification_jobs may call _stage_assignment inside their existing transaction. This participant never commits, rolls back or invokes an owner executor.",
    contract=ServiceContract(
        concerns=(
            ConcernContract(
                name="native work-order assignment persistence",
                role=OwnerRole.COMMAND_WRITER,
                input_names=("authorized assignment intent",),
                canonical_writer="operations.work_order_assignment_records",
            ),
        ),
        authoritative_inputs=(
            AuthorityInput(
                name="authorized assignment intent",
                owner="operations.work_order_commands",
                kind=AuthorityKind.CONTROL_INPUT,
                source="Typed WorkOrderAssignmentCommand and CommandContext from named command/coordinator callers.",
            ),
        ),
        transaction=TransactionContract(
            mode=TransactionMode.PARTICIPANT,
            boundary="Flush-only _stage_assignment requires an active transaction owned by operations.work_order_commands or network.fiber_field_verification_jobs.",
            locking="Lock native WorkOrder then queue and target; revalidate eligibility before writes.",
            idempotency="Immutable assignment receipt and exact fingerprint are staged atomically with queue state.",
            retries="Only the named outer owner/coordinator retries complete transactions.",
        ),
        events=EventContract(
            event_types=("work_order.assigned",),
            schema_version=1,
            delivery_owner="events.dispatcher",
            compatibility="Native assignment event staged in outer transaction.",
            replay="Immutable assignment receipt.",
        ),
        errors=ErrorContract(
            domain_codes=(
                "assignment_transaction_required",
                "assignment_idempotency_conflict",
                "assignment_revision_conflict",
            ),
            mapping_owner="Named outer work-order command or network coordinator adapters",
            fail_closed_on=(
                "no active caller transaction",
                "stale revision",
                "changed replay payload",
            ),
        ),
        migration=MigrationContract(
            state=AuthorityMigrationState.CUTOVER_READY,
            new_owner="operations.work_order_assignment_records",
            old_owner="assign(commit=False)",
            verification="Nested coordinator and assignment transaction tests.",
            cutover_gate="Registered named callers only and no nested transaction completion.",
            fallback_retirement="Boolean commit mode removed from assignment boundary.",
        ),
        steward="field operations",
        design_refs=("docs/designs/VENDOR_WORK_ORDER_EXECUTION.md",),
        test_refs=(
            "tests/test_vendor_work_order_assignment.py",
            "tests/architecture/test_fiber_field_verification_job_plan_boundary.py",
        ),
    ),
)
