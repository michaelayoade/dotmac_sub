"""Extend the existing completion owner with issued-evidence review authority."""

from dataclasses import replace

from app.services.field.completion_review_contracts import CONCERN, OWNER
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
)

REVIEW_CONCERNS = (
    CONCERN,
    "completion review policy revisions",
    "completion review current and historical queries",
    "explicit native completion enrollment",
    "immutable completion criteria revisions",
    "ticket completion acceptance basis",
    "linked ticket completion enrollment presence",
    "reviewed failed completion scope replacement",
    "failed completion replacement workspace",
    "current reviewed replacement evidence",
    "exact required scoped completion acceptance basis",
    "native shared infrastructure replacement proof",
)
REVIEW_DEPENDENCIES = (
    "network.outage_completion_evidence",
    "operations.project_work_order_handoff",
    "operations.work_order_commands",
    "operations.project_engineering_plans",
    "operations.project_lifecycle",
    "auth.staff_permission_admission",
    "operations.field_work_order_access",
    "events.dispatcher",
    "observability.audit_log",
    "files.evidence_retention",
)


def with_completion_review(contract: ServiceContract) -> ServiceContract:
    inputs = (
        AuthorityInput(
            name="exact shared infrastructure completion evidence",
            owner="network.outage_completion_evidence",
            kind=AuthorityKind.AUTHORITATIVE_RECORD,
            source="All actual issued audience matching required scopes and typed exact dual issuance replacement coverage: same incident/canonical infrastructure ticket/target/classification/project/task, frozen original audience subset of replacement audience, no current topology or fabricated subscriber.",
        ),
        AuthorityInput(
            name="exact issued engineering criteria",
            owner="operations.project_work_order_handoff",
            kind=AuthorityKind.AUTHORITATIVE_RECORD,
            source="Actual migration676 issuance paired with exact immutable675 criteria/revision/approval/digest; no fabricated or substituted engineering history.",
        ),
        AuthorityInput(
            name="native completion enrollment and authored criteria",
            owner=OWNER,
            kind=AuthorityKind.AUTHORITATIVE_RECORD,
            source="Explicit immutable CompletionEnrollment has checked engineering_issuance/native_work_order provenance. Completion owns native operator-authored statement/method criteria revisions and explicit policy scope; native jobs never receive fabricated engineering records.",
        ),
        AuthorityInput(
            name="fresh completion review staff admission",
            owner="auth.staff_permission_admission",
            kind=AuthorityKind.CONTROL_INPUT,
            source="Current active staff/Person RBAC and leave admission for operations:completion-review:write/read; review is distinct from dispatch and engineering approval.",
        ),
        AuthorityInput(
            name="completion review evidence and policy",
            owner=OWNER,
            kind=AuthorityKind.AUTHORITATIVE_RECORD,
            source="Explicit independent-reviewer policy revision, immutable native submissions, observations, retained attachment checksums, criterion verdicts, decisions/revocations and owner-global receipts; generation-controlled head. Typed policy/workspace/field-preview queries expose immutable issued criterion prose, exact assignment/head/policy generations and domain-owned ActionReadiness. READ history and WRITE-purpose current-only previews remain distinct; policy publication targets an explicit Project or WorkOrder scope; separate enrollment pins that scope. Submission records the exact canonical completed field event, criteria revision and assignment generation. Per-submission history retains original criterion statement/method even when native criterion IDs are reused in later revisions.",
        ),
        AuthorityInput(
            name="current completion engineering freshness",
            owner="operations.project_engineering_plans",
            kind=AuthorityKind.AUTHORITATIVE_RECORD,
            source="Distinct typed completion participant validates exact issued approval/revision/digest and binding freshness; capture/revocation/history do not require current engineering acceptance. Protected retained-evidence content is exact submission/attachment lineage; READ serves history, WRITE serves only current pending review, never general private files.",
        ),
        AuthorityInput(
            name="reviewed failed scope replacement lineage",
            owner=OWNER,
            kind=AuthorityKind.AUTHORITATIVE_RECORD,
            source="Immutable supervisor decisions pin exact authoritative unable event or rejected submission/decision, both assignment generations, current criteria/policy revisions and exhaustive source criterion mapping. Native same non-null ticket/customer and exact project/task provenance; engineering project/task scope uses exact authoritative issuance/approval/digest/target/purpose/technology proof, current completion approval and identical canonical criteria with identity coverage. Mixed provenance or changed approved engineering scope refuses. One active predecessor/successor, no cycles; immutable revocation and ordered support basis lineage retain every original required scope.",
        ),
    )
    assert contract.transaction is not None
    assert contract.errors is not None
    assert contract.events is not None
    assert contract.migration is not None
    return replace(
        contract,
        concerns=(
            *contract.concerns,
            *(
                ConcernContract(
                    name=name,
                    role=OwnerRole.RESOLVER
                    if name
                    in (
                        REVIEW_CONCERNS[2],
                        REVIEW_CONCERNS[5],
                        REVIEW_CONCERNS[6],
                        REVIEW_CONCERNS[8],
                        REVIEW_CONCERNS[9],
                        REVIEW_CONCERNS[10],
                    )
                    else OwnerRole.COMMAND_WRITER,
                    input_names=tuple(value.name for value in inputs),
                    canonical_writer=None
                    if name
                    in (
                        REVIEW_CONCERNS[2],
                        REVIEW_CONCERNS[5],
                        REVIEW_CONCERNS[6],
                        REVIEW_CONCERNS[8],
                        REVIEW_CONCERNS[9],
                        REVIEW_CONCERNS[10],
                    )
                    else OWNER,
                )
                for name in REVIEW_CONCERNS
            ),
        ),
        authoritative_inputs=(*contract.authoritative_inputs, *inputs),
        transaction=replace(
            contract.transaction,
            boundary=contract.transaction.boundary
            + " app.services.field.completion_review implements the same owner; one executor atomically stages review/replacement receipt, immutable facts/decisions, generation head, audit and version-1 outbox. No downstream operational or financial writes.",
            locking="Enrollment uses canonical WorkOrder linkage fence before exact origin Ticket admission, Project/Task and WorkOrder; terminal origin refuses new enrollment. Ordinary review never locks Ticket. Replacement takes canonical linkage fences before native Ticket or engineering historical Project/task scope, sorted engineering targets/WorkOrders, criteria/policy/review heads and replacement heads. Fence serializes absent-head insertion, cycles, fan-in and support timers; admission revalidates after waits. Frozen assignment/criteria/policy/failure lineage is revalidated on every support basis read. Ticket basis staging requires active named support.ticket_lifecycle. The separate public preview_ticket_completion_basis(SupportTicketCompletionPreviewQuery) freshly revalidates support:ticket:update WRITE-purpose staff admission before/after waits, shares the same basis kernel, and needs no executor or completion READ permission. Both resolve exact origin_ticket_id only, all Project roots then Tasks/engineering/WorkOrders in stable order; linked-enrollment presence has no acceptance or staff impersonation authority. Review-global receipt advisory lock first. Submission locks canonical user/profile, Project then optional Task, WorkOrder and exact current actor/member/generation, completion policy/head, sorted StoredFiles then attachments. Staff decision locks Project/Task, distinct engineering plan/target participant, WorkOrder, completion policy/head; fresh staff admission is rechecked after waits. Project serializes submission/review lock suborders. Revocation uses historical scope without current approval. Canonical file deletion locks StoredFile and refuses retained evidence before object deletion or metadata mutation.",
            idempotency="One owner-global review receipt key and normalized typed intent fingerprint across policy/enrollment/criteria/submission/decision/revocation. Authorization before historical replay; submission additionally requires exact current member and assignment generation. Exact receipt replay has no repeated audit/event.",
        ),
        errors=replace(
            contract.errors,
            domain_codes=(
                *contract.errors.domain_codes,
                *(
                    f"{OWNER}.{code}"
                    for code in (
                        "replacement_conflict",
                        "replacement_cycle",
                        "replacement_stale",
                        "replacement_required",
                        "replacement_candidate_required",
                        "failure_basis_stale",
                        "engineering_replacement_proof_required",
                        "idempotency_conflict",
                        "already_enrolled",
                        "enrollment_required",
                        "criteria_required",
                        "stale_criteria",
                        "completion_required",
                        "completion_pending",
                        "provenance_mismatch",
                        "participant_required",
                        "origin_ticket_terminal",
                        "assignment_generation_mismatch",
                        "invalid_request",
                        "denied",
                        "not_found",
                        "stale_policy",
                        "stale_state",
                        "criterion_mismatch",
                        "evidence_unavailable",
                        "scope_mismatch",
                        "independent_reviewer_required",
                    )
                ),
                "files.evidence_retained",
                "files.not_found",
                "files.evidence_unavailable",
            ),
        ),
        events=replace(
            contract.events,
            event_types=(
                *contract.events.event_types,
                "field.completion_review_recorded",
            ),
        ),
        migration=replace(
            contract.migration,
            verification=(contract.migration.verification or "")
            + " Migration677 adds immutable completion review history and retention guards. Migration679 adds reviewed replacement decisions/coverage/heads/receipts and immutable effective support basis lineage; neither backfills acceptance.",
            cutover_gate=(contract.migration.cutover_gate or "")
            + " Completion requires real fresh/predecessor PostgreSQL migration and concurrency acceptance; authored source/SQLite is not deployed-schema evidence.",
        ),
        design_refs=(*contract.design_refs, "docs/designs/FIELD_COMPLETION_REVIEW.md"),
        test_refs=(
            *contract.test_refs,
            "tests/test_field_completion_review.py",
            "tests/test_failed_scope_replacement.py",
            "tests/integration/test_failed_scope_replacement_acceptance.py",
            "tests/architecture/test_failed_scope_replacement_boundary.py",
            "tests/architecture/test_field_completion_review_boundary.py",
            "tests/integration/test_field_completion_review_acceptance.py",
        ),
    )


def retention_service() -> SOTService:
    return SOTService(
        name="files.evidence_retention",
        module="app.services.file_storage",
        owns=("immutable retained object references", "retained evidence streams"),
        notes="Storage owns immutable retention references and canonical physical-key deletion admission. Review verdicts stay with operations.field_completion. Only that named outer owner may register completion references; deletion never interprets acceptance.",
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="retained evidence streams",
                    role=OwnerRole.RESOLVER,
                    input_names=("typed retained object reference",),
                ),
                ConcernContract(
                    name="immutable retained object references",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("typed retained object reference",),
                    canonical_writer="files.evidence_retention",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="typed retained object reference",
                    owner="files.evidence_retention",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Immutable StoredFileRetention provider/object references registered atomically by the named completion command; no reverse dependency on completion eligibility/history.",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary="stage_completion_retention requires active operations.field_completion and flushes only; canonical deletion checks the same retained object references before S3 effects or metadata changes. stream_retained_evidence is a typed internal read participant: caller owns admission; storage verifies exact ledger lineage and buffers at most canonical attachment limit with checksum/size verification before returning bytes.",
                locking="Sorted provider/physical-key advisory locks, all alias StoredFile rows in UUID order, then attachment rows. No WorkOrder lock is acquired by storage. Stale ORM input is refreshed under these locks.",
                idempotency="Composite stored_file_id/reference_id preserves one immutable registration. Object aliases share physical retention.",
                retries="Outer owner retries whole database transactions; physical object deletion retains its existing non-transactional transport limitation.",
            ),
            errors=ErrorContract(
                domain_codes=(
                    "files.not_found",
                    "files.evidence_unavailable",
                    "files.evidence_retained",
                    "files.retention_participant_required",
                    "files.prepared_object_missing",
                    "files.storage_unavailable",
                ),
                mapping_owner="File and completion adapters",
                fail_closed_on=(
                    "retained object alias",
                    "deleted file",
                    "missing named outer owner",
                ),
            ),
            events=EventContract(
                event_types=("field.completion_review_recorded",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Named outer completion owner stages one atomic event containing the immutable submission reference; storage never interprets a verdict.",
                replay="Immutable reference and outer owner receipt reconstruct the same registration.",
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="files.evidence_retention",
                verification="Migration677 immutable storage-retention ledger and common-key guards.",
                cutover_gate="Focused retention tests and migrated PostgreSQL races required.",
            ),
            steward="private file storage",
            design_refs=("docs/designs/FIELD_COMPLETION_REVIEW.md",),
            test_refs=(
                "tests/test_field_completion_review.py",
                "tests/architecture/test_field_completion_review_boundary.py",
                "tests/integration/test_field_completion_review_acceptance.py",
            ),
        ),
    )
