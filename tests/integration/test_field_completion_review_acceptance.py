"""Authored migrated-PostgreSQL acceptance; no local or metadata concurrency claim."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.field_attachment import FieldAttachment
from app.models.field_completion_review import (
    CompletionEnrollment,
    CompletionReviewDecision,
    CompletionReviewReceipt,
    CompletionSubmission,
)
from app.models.field_job_event import FieldJobEvent
from app.models.project_work_order_issuance import ProjectWorkOrderIssuance
from app.models.rbac import Permission, SystemUserPermission
from app.models.stored_file import StoredFile
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.dispatch import WorkOrderHeaderCreate
from app.services import project_work_order_handoff
from app.services.domain_errors import DomainError
from app.services.events import Event, EventType
from app.services.field import completion_review as owner
from app.services.field.completion_review_contracts import (
    READ,
    WRITE,
    CompletionPolicyScope,
    CompletionPolicyScopeKind,
    CompletionProvenance,
    CompletionReviewOutcome,
    CompletionReviewQuery,
    CompletionWorkspacePurpose,
    CompletionWorkspaceQuery,
    CriterionObservation,
    CriterionResult,
    CriterionVerdict,
    DecideCompletionReview,
    EnrollCompletionReview,
    FieldSubmissionPreviewQuery,
    IssuedCompletionCriterion,
    PublishCompletionCriteria,
    PublishCompletionReviewPolicy,
    ReviewState,
    RevokeCompletionReview,
    SubmitCompletionEvidence,
)
from app.services.field.execution_contracts import ApplyFieldTransition, FieldEvent
from app.services.field.transitions import field_transitions
from app.services.field.work_order_access import FieldAccessError
from app.services.file_storage import file_uploads
from app.services.object_storage import StreamResult
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.project_work_order_handoff_contracts import ProjectWorkOrderIssued
from app.services.work_order_assignment_contracts import (
    TechnicianAssignmentTarget,
    WorkOrderAssignmentCommand,
)
from app.services.work_order_commands import work_order_commands
from tests.integration.test_project_engineering_plan_acceptance import NativeScope
from tests.integration.test_project_engineering_plan_acceptance import (
    native_engine as engineering_native_engine,
)
from tests.integration.test_project_engineering_plan_acceptance import (
    scope as engineering_scope,
)
from tests.integration.test_project_work_order_issuance_acceptance import (
    approved_command,  # noqa: F401
)
from tests.staff_identity_fixtures import add_bound_staff_user

native_engine = engineering_native_engine
scope = engineering_scope


class SyntheticEvidenceStorage:
    def stream(self, key: str) -> StreamResult:
        return StreamResult(
            chunks=iter((b"proof",)), content_type="application/pdf", content_length=5
        )

    def delete(self, key: str) -> None:
        pytest.fail("Retained object deletion must be refused before transport")


def context(actor: UUID, scope: str) -> CommandContext:
    return CommandContext(
        command_id=uuid4(),
        correlation_id=uuid4(),
        actor=str(actor),
        scope=scope,
        reason="Synthetic migrated completion acceptance",
        idempotency_key=str(uuid4()),
    )


@dataclass(frozen=True)
class Prepared:
    submission: SubmitCompletionEvidence
    reviewer: UUID
    access: PermissionAdmission


def prepare(
    engine: Engine,
    scope: NativeScope,
    *,
    native: bool = False,
    already_issued: ProjectWorkOrderIssued | None = None,
    require_legacy_evidence: bool = False,
) -> Prepared:
    actual_issuance_id: UUID | None = None
    if native:
        from tests.integration.test_field_lifecycle_acceptance import _customer

        with Session(engine) as db:
            customer_id = _customer(db).id
            db.commit()
            work = work_order_commands.create(
                db,
                WorkOrderHeaderCreate(
                    title="Native customer completion", subscriber_id=customer_id
                ),
            )
            work_order_id, public_id = work.id, work.public_id
    else:
        if already_issued is None:
            issued_command = approved_command(engine, scope)
            with Session(engine) as db:
                issued = project_work_order_handoff.issue(db, command=issued_command)
        else:
            issued = already_issued
        work_order_id, public_id, actual_issuance_id = (
            issued.work_order_id,
            issued.public_id,
            issued.issuance_id,
        )
    policy_scope = (
        CompletionPolicyScope(
            kind=CompletionPolicyScopeKind.work_order, work_order_public_id=public_id
        )
        if native
        else CompletionPolicyScope(
            kind=CompletionPolicyScopeKind.project, project_id=scope.project_id
        )
    )
    criteria_ids = (
        (uuid4(), uuid4()) if native else _issued_criteria(engine, actual_issuance_id)
    )
    with Session(engine) as db:
        technician, _ = add_bound_staff_user(db)
        profile = TechnicianProfile(
            person_id=technician.id, system_user_id=technician.id, is_active=True
        )
        db.add(profile)
        permissions = db.scalars(
            select(Permission).where(
                Permission.key.in_({READ, WRITE, "operations:dispatch:assign"}),
                Permission.is_active.is_(True),
            )
        ).all()
        assert {value.key for value in permissions} == {
            READ,
            WRITE,
            "operations:dispatch:assign",
        }
        for permission in permissions:
            db.add(
                SystemUserPermission(
                    system_user_id=scope.reviewer_id, permission_id=permission.id
                )
            )
        db.flush()
        user_id, profile_id = technician.id, profile.id
        row = db.get(WorkOrder, work_order_id)
        assert row is not None
        revision = row.updated_at
        db.commit()
    admission = PermissionAdmission(
        scope.reviewer_id,
        "system_user",
        frozenset({READ, WRITE, "operations:dispatch:assign"}),
    )
    with Session(engine) as db:
        assigned = work_order_commands.assign(
            db,
            command=WorkOrderAssignmentCommand(
                access=admission,
                work_order_public_id=public_id,
                target=TechnicianAssignmentTarget(profile_id),
                expected_revision=revision,
                reason="Assign synthetic completion actor",
            ),
            context=context(scope.reviewer_id, "operations:dispatch:assign"),
        )
        assert assigned.assignment_generation is not None
    with Session(engine) as db:
        policy = owner.publish_policy(
            db,
            command=PublishCompletionReviewPolicy(
                context=context(scope.reviewer_id, WRITE),
                access=admission,
                scope=policy_scope,
                expected_generation=0,
                independent_reviewer_required=True,
            ),
        )
    assert policy.policy_revision_id is not None
    with Session(engine) as db:
        enrolled = owner.enroll(
            db,
            command=EnrollCompletionReview(
                context=context(scope.reviewer_id, WRITE),
                access=admission,
                work_order_public_id=public_id,
                provenance=CompletionProvenance.native_work_order
                if native
                else CompletionProvenance.engineering_issuance,
                engineering_issuance_id=actual_issuance_id,
                policy_scope=policy_scope,
                criteria=tuple(
                    IssuedCompletionCriterion(
                        id=value,
                        order=order,
                        statement="Native authored criterion",
                        verification_method="Inspect installation",
                    )
                    for order, value in enumerate(criteria_ids)
                )
                if native
                else (),
            ),
        )
    assert (
        enrolled.enrollment_id is not None and enrolled.criteria_revision_id is not None
    )
    if require_legacy_evidence:
        # Fresh migrated clone: exercise the default photo/signoff gate unchanged.
        with Session(engine) as db:
            requirements = field_transitions.completion_requirements(db)
            assert requirements.evidence_required is True
            assert requirements.minimum_photo_count == 1
            assert requirements.customer_signoff_required is True
    else:
        # This synthetic fixture explicitly configures legacy photo/signoff requirements
        # off; the real field owner still records the exact assigned completion outcome.
        with Session(engine) as db:
            setting = db.scalar(
                select(DomainSetting).where(
                    DomainSetting.domain == SettingDomain.field,
                    DomainSetting.key == "completion_requires_evidence",
                    DomainSetting.is_active.is_(True),
                )
            )
            if setting is None:
                db.add(
                    DomainSetting(
                        domain=SettingDomain.field,
                        key="completion_requires_evidence",
                        value_type="boolean",
                        value_text="false",
                        scope_kind="platform",
                        tenant_id=None,
                    )
                )
            else:
                setting.value_text, setting.value_json = "false", None
            db.commit()
    with Session(engine) as db:
        field_transitions.apply(
            db,
            ApplyFieldTransition(
                context=context(user_id, "field:execution"),
                requester_system_user_id=user_id,
                public_id=public_id,
                event=FieldEvent.start,
                client_event_id=uuid4(),
                assignment_generation=assigned.assignment_generation,
            ),
        )
    if require_legacy_evidence:
        completion = ApplyFieldTransition(
            context=context(user_id, "field:execution"),
            requester_system_user_id=user_id,
            public_id=public_id,
            event=FieldEvent.complete,
            client_event_id=uuid4(),
            assignment_generation=assigned.assignment_generation,
        )
        with Session(engine) as db:
            with pytest.raises(FieldAccessError, match="at least one photo"):
                field_transitions.apply(db, completion)
        # Synthetic attachment metadata proves the gate, not object-store transport.
        # Neither attachment is a fabricated fiber measurement or topology record.
        for kind in ("photo", "signature"):
            with Session(engine) as db:
                file = StoredFile(
                    entity_type="field_attachment",
                    entity_id=public_id,
                    original_filename=f"{kind}.jpg",
                    storage_key_or_relative_path=f"synthetic/radio-{uuid4().hex}",
                    file_size=5,
                    content_type="image/jpeg",
                    checksum=hashlib.sha256(b"proof").hexdigest(),
                    storage_provider="s3",
                )
                db.add(file)
                db.flush()
                db.add(
                    FieldAttachment(
                        work_order_mirror_id=work_order_id,
                        stored_file_id=file.id,
                        kind=kind,
                        file_name=f"{kind}.jpg",
                        mime_type="image/jpeg",
                        size_bytes=5,
                        uploaded_by_person_id=user_id,
                        uploaded_by_system_user_id=user_id,
                    )
                )
                db.commit()
            if kind == "photo":
                with Session(engine) as db:
                    with pytest.raises(FieldAccessError, match="customer signature"):
                        field_transitions.apply(db, completion)
    with Session(engine) as db:
        completed = field_transitions.apply(
            db,
            ApplyFieldTransition(
                context=context(user_id, "field:execution"),
                requester_system_user_id=user_id,
                public_id=public_id,
                event=FieldEvent.complete,
                client_event_id=uuid4(),
                assignment_generation=assigned.assignment_generation,
            ),
        )
    return Prepared(
        SubmitCompletionEvidence(
            context=context(user_id, "field:completion:submit"),
            requester_system_user_id=user_id,
            enrollment_id=enrolled.enrollment_id,
            criteria_revision_id=enrolled.criteria_revision_id,
            completed_event_id=completed.event.id,
            assignment_generation=assigned.assignment_generation,
            expected_generation=0,
            policy_revision_id=policy.policy_revision_id,
            observations=tuple(
                CriterionObservation(value, "Inspected exact issued criterion")
                for value in criteria_ids
            ),
        ),
        scope.reviewer_id,
        admission,
    )


def _issued_criteria(engine: Engine, issuance_id: UUID | None) -> tuple[UUID, ...]:
    from app.models.project_engineering_plan import EngineeringPlanCriterion

    assert issuance_id is not None
    with Session(engine) as db:
        issuance = db.get(ProjectWorkOrderIssuance, issuance_id)
        assert issuance is not None
        return tuple(
            db.scalars(
                select(EngineeringPlanCriterion.id).where(
                    EngineeringPlanCriterion.revision_id == issuance.revision_id
                )
            ).all()
        )


def review(
    prepared: Prepared, submitted: CompletionReviewOutcome
) -> DecideCompletionReview:
    assert submitted.submission_id is not None
    return DecideCompletionReview(
        context=context(prepared.reviewer, WRITE),
        access=prepared.access,
        enrollment_id=prepared.submission.enrollment_id,
        submission_id=submitted.submission_id,
        expected_generation=1,
        policy_revision_id=prepared.submission.policy_revision_id,
        results=tuple(
            CriterionResult(
                value.criterion_id, CriterionVerdict.passed, "Reviewed exact evidence"
            )
            for value in prepared.submission.observations
        ),
    )


def test_concurrent_duplicate_submission_and_review_have_one_receipt(
    native_engine: Engine, scope: NativeScope
) -> None:
    prepared = prepare(native_engine, scope)

    def race_submit() -> CompletionReviewOutcome:
        with Session(native_engine) as db:
            gate.wait(timeout=10)
            return owner.submit_evidence(db, command=prepared.submission)

    gate = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(race_submit) for _ in range(2)]
        outcomes = [future.result(timeout=30) for future in futures]
    assert outcomes[0].submission_id == outcomes[1].submission_id
    assert sorted(value.replayed for value in outcomes) == [False, True]
    command = review(prepared, outcomes[0])
    gate = Barrier(2)

    def race_review() -> CompletionReviewOutcome:
        with Session(native_engine) as db:
            gate.wait(timeout=10)
            return owner.decide(db, command=command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(race_review) for _ in range(2)]
        outcomes = [future.result(timeout=30) for future in futures]
    assert outcomes[0].decision_id == outcomes[1].decision_id
    assert sorted(value.replayed for value in outcomes) == [False, True]
    with Session(native_engine) as db:
        assert db.scalar(select(func.count()).select_from(CompletionSubmission)) == 1
        assert (
            db.scalar(select(func.count()).select_from(CompletionReviewDecision)) == 1
        )


def test_immutable_review_and_retained_object_alias(
    native_engine: Engine, scope: NativeScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_uploads, "storage", SyntheticEvidenceStorage())
    prepared = prepare(native_engine, scope)
    with Session(native_engine) as db:
        issuance = db.get(CompletionEnrollment, prepared.submission.enrollment_id)
        assert issuance is not None
        file = StoredFile(
            entity_type="field_attachment",
            entity_id=issuance.public_id,
            original_filename="proof.pdf",
            storage_key_or_relative_path="synthetic/retained-object",
            file_size=5,
            content_type="application/pdf",
            checksum=hashlib.sha256(b"proof").hexdigest(),
            storage_provider="s3",
        )
        alias = StoredFile(
            entity_type="field_attachment",
            entity_id=issuance.public_id,
            original_filename="proof.pdf",
            storage_key_or_relative_path=file.storage_key_or_relative_path,
            file_size=5,
            content_type="application/pdf",
            checksum=hashlib.sha256(b"proof").hexdigest(),
            storage_provider="s3",
        )
        db.add_all([file, alias])
        db.flush()
        attachment = FieldAttachment(
            work_order_mirror_id=issuance.work_order_id,
            stored_file_id=file.id,
            kind="document",
            file_name="proof.pdf",
            mime_type="application/pdf",
            size_bytes=5,
            uploaded_by_person_id=prepared.submission.requester_system_user_id,
            uploaded_by_system_user_id=prepared.submission.requester_system_user_id,
        )
        db.add(attachment)
        db.flush()
        attachment_id, alias_id = attachment.id, alias.id
        db.commit()
    command = replace(
        prepared.submission,
        observations=(
            replace(
                prepared.submission.observations[0], attachment_ids=(attachment_id,)
            ),
            *prepared.submission.observations[1:],
        ),
    )
    with Session(native_engine) as db:
        submitted = owner.submit_evidence(db, command=command)

    class Storage:
        def delete(self, key: str) -> None:
            pytest.fail("Retained object deletion must be refused before transport")

    monkeypatch.setattr(file_uploads, "storage", Storage())
    with Session(native_engine) as db:
        alias = db.get(StoredFile, alias_id)
        assert alias is not None
        with pytest.raises(DomainError, match="must be retained"):
            file_uploads.soft_delete(db=db, file=alias, hard_delete_object=True)
        db.rollback()
        with pytest.raises(DBAPIError):
            db.execute(
                text("UPDATE completion_submissions SET reason='changed' WHERE id=:id"),
                {"id": submitted.submission_id},
            )
            db.commit()
        db.rollback()
        with pytest.raises(DBAPIError):
            db.execute(
                text("UPDATE field_attachments SET is_active=false WHERE id=:id"),
                {"id": attachment_id},
            )
            db.commit()
        db.rollback()


def test_outbox_failure_rolls_back_completion_receipt_and_evidence(
    native_engine: Engine, scope: NativeScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare(native_engine, scope)

    def fail(
        _db: Session,
        event_type: EventType,
        payload: dict[str, object],
        *,
        actor: str | None,
    ) -> Event:
        raise RuntimeError("Injected completion outbox failure")

    monkeypatch.setattr(owner, "emit_event", fail)
    with Session(native_engine) as db:
        with pytest.raises(RuntimeError, match="outbox failure"):
            owner.submit_evidence(db, command=prepared.submission)
        assert (
            db.scalar(
                select(CompletionReviewReceipt).where(
                    CompletionReviewReceipt.idempotency_key
                    == prepared.submission.context.idempotency_key
                )
            )
            is None
        )
        assert (
            db.scalar(
                select(CompletionSubmission).where(
                    CompletionSubmission.enrollment_id
                    == prepared.submission.enrollment_id
                )
            )
            is None
        )


def test_submission_vs_file_deactivation_cannot_both_win(
    native_engine: Engine, scope: NativeScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_uploads, "storage", SyntheticEvidenceStorage())
    prepared = prepare(native_engine, scope)
    with Session(native_engine) as db:
        issuance = db.get(CompletionEnrollment, prepared.submission.enrollment_id)
        assert issuance is not None
        file = StoredFile(
            entity_type="field_attachment",
            entity_id=issuance.public_id,
            original_filename="race.pdf",
            storage_key_or_relative_path="synthetic/retention-race",
            file_size=5,
            content_type="application/pdf",
            checksum=hashlib.sha256(b"proof").hexdigest(),
            storage_provider="s3",
        )
        db.add(file)
        db.flush()
        attachment = FieldAttachment(
            work_order_mirror_id=issuance.work_order_id,
            stored_file_id=file.id,
            kind="document",
            file_name="race.pdf",
            mime_type="application/pdf",
            size_bytes=5,
            uploaded_by_person_id=prepared.submission.requester_system_user_id,
        )
        db.add(attachment)
        db.flush()
        file_id, attachment_id = file.id, attachment.id
        db.commit()
    command = replace(
        prepared.submission,
        observations=(
            replace(
                prepared.submission.observations[0], attachment_ids=(attachment_id,)
            ),
            *prepared.submission.observations[1:],
        ),
    )
    gate = Barrier(2)

    def submit() -> CompletionReviewOutcome | DomainError:
        with Session(native_engine) as db:
            gate.wait(timeout=10)
            try:
                return owner.submit_evidence(db, command=command)
            except DomainError as exc:
                return exc

    def delete() -> bool | DomainError:
        with Session(native_engine) as db:
            record = db.get(StoredFile, file_id)
            assert record is not None
            gate.wait(timeout=10)
            try:
                file_uploads.stage_soft_delete(db=db, file=record)
                db.commit()
                return True
            except DomainError as exc:
                db.rollback()
                return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit)
        second = pool.submit(delete)
        submitted, deleted = first.result(timeout=30), second.result(timeout=30)
    assert isinstance(submitted, DomainError) != isinstance(deleted, DomainError)
    if isinstance(submitted, DomainError):
        assert submitted.code == "operations.field_completion.evidence_unavailable"
    else:
        assert (
            isinstance(deleted, DomainError)
            and deleted.code == "files.evidence_retained"
        )


def test_completion_review_does_not_reuse_new_issuance_status_gate(
    native_engine: Engine, scope: NativeScope
) -> None:
    from app.models.project import Project, ProjectTask
    from app.services.field import completion_project_scope
    from app.services.field.completion_review_contracts import ReviewState
    from tests.integration.test_project_field_completion_acceptance import (
        apply_command,
    )

    prepared = prepare(native_engine, scope)
    with Session(native_engine) as db:
        submitted = owner.submit_evidence(db, command=prepared.submission)
    with Session(native_engine) as db:
        accepted = owner.decide(db, command=review(prepared, submitted))
        assert accepted.state == ReviewState.accepted

    # Terminal enrolled scope requires actual accepted evidence and the named
    # lifecycle action. Never bypass migration682's generic completion guards.
    # apply_command explicitly grants this test staff the canonical project
    # permissions and obtains exact current preview tokens for each action.
    for task_scope in (True, False):
        command = apply_command(
            native_engine, scope, prepared.reviewer, task=task_scope
        )
        with Session(native_engine) as db:
            completion_project_scope.apply_accepted_project_completion(
                db, command=command
            )
    with Session(native_engine) as db:
        assert db.get(Project, scope.project_id).status == "completed"
        assert db.get(ProjectTask, scope.task_id).status == "done"
    with Session(native_engine) as db:
        revoked = owner.revoke(
            db,
            command=RevokeCompletionReview(
                context=context(prepared.reviewer, WRITE),
                access=prepared.access,
                enrollment_id=accepted.enrollment_id,
                decision_id=accepted.decision_id,
                expected_generation=accepted.generation,
            ),
        )
    with Session(native_engine) as db:
        resubmitted = owner.submit_evidence(
            db,
            command=replace(
                prepared.submission,
                context=context(
                    prepared.submission.requester_system_user_id,
                    "field:completion:submit",
                ),
                expected_generation=revoked.generation,
            ),
        )
    with Session(native_engine) as db:
        reviewed_again = owner.decide(
            db,
            command=replace(
                review(prepared, resubmitted),
                expected_generation=resubmitted.generation,
            ),
        )
        assert reviewed_again.state == ReviewState.accepted
        assert reviewed_again.submission_id != accepted.submission_id
        assert db.get(Project, scope.project_id).status == "completed"
        assert db.get(ProjectTask, scope.task_id).status == "done"


def test_current_staff_permission_is_required_before_review_replay(
    native_engine: Engine, scope: NativeScope
) -> None:
    prepared = prepare(native_engine, scope)
    with Session(native_engine) as db:
        submitted = owner.submit_evidence(db, command=prepared.submission)
    command = review(prepared, submitted)
    with Session(native_engine) as db:
        owner.decide(db, command=command)
    with Session(native_engine) as db:
        permission = db.scalar(select(Permission).where(Permission.key == WRITE))
        assert permission is not None
        grant = db.scalar(
            select(SystemUserPermission).where(
                SystemUserPermission.system_user_id == prepared.reviewer,
                SystemUserPermission.permission_id == permission.id,
            )
        )
        assert grant is not None
        db.delete(grant)
        db.commit()
    with Session(native_engine) as db:
        with pytest.raises(DomainError) as denied:
            owner.decide(db, command=command)
        assert denied.value.code == "auth.staff_permission_admission.denied"


def test_authoritative_reviewer_person_required_before_decision(
    native_engine: Engine, scope: NativeScope
) -> None:
    prepared = prepare(native_engine, scope)
    with Session(native_engine) as db:
        submitted = owner.submit_evidence(db, command=prepared.submission)
    with Session(native_engine) as db:
        reviewer = db.get(SystemUser, prepared.reviewer)
        assert reviewer is not None
        reviewer.person_party_id = None
        reviewer.party_bound_at = None
        reviewer.party_binding_source = None
        reviewer.party_binding_reason = None
        db.commit()
    with Session(native_engine) as db:
        with pytest.raises(DomainError) as denied:
            owner.decide(db, command=review(prepared, submitted))
        assert denied.value.code == "auth.staff_permission_admission.denied"
        assert (
            db.scalar(select(func.count()).select_from(CompletionReviewDecision)) == 0
        )


def test_native_field_preview_and_write_only_review_workspace(
    native_engine: Engine, scope: NativeScope
) -> None:
    prepared = prepare(native_engine, scope)
    with Session(native_engine) as db:
        issuance = db.get(CompletionEnrollment, prepared.submission.enrollment_id)
        assert issuance is not None
        public_id = issuance.public_id
        preview = owner.field_submission_preview(
            db,
            query=FieldSubmissionPreviewQuery(
                requester_system_user_id=prepared.submission.requester_system_user_id,
                work_order_public_id=public_id,
            ),
        )
        assert preview.submission_available
        assert (
            preview.assignment_generation == prepared.submission.assignment_generation
        )
        assert {criterion.id for criterion in preview.criteria} == {
            observation.criterion_id for observation in prepared.submission.observations
        }
    with Session(native_engine) as db:
        submitted = owner.submit_evidence(db, command=prepared.submission)
    with Session(native_engine) as db:
        workspace = owner.workspace(
            db,
            query=CompletionWorkspaceQuery(
                access=PermissionAdmission(
                    prepared.reviewer, "system_user", frozenset({WRITE})
                ),
                work_order_public_id=public_id,
                purpose=CompletionWorkspacePurpose.decide,
            ),
        )
        assert workspace.decide_readiness.is_ready and workspace.history is None
        assert workspace.current_submission is not None
        assert workspace.current_submission.id == submitted.submission_id


def test_native_field_preview_denies_removed_principal_before_history(
    native_engine: Engine, scope: NativeScope
) -> None:
    prepared = prepare(native_engine, scope)
    with Session(native_engine) as db:
        owner.submit_evidence(db, command=prepared.submission)
    with Session(native_engine) as db:
        issuance = db.get(CompletionEnrollment, prepared.submission.enrollment_id)
        user = db.get(SystemUser, prepared.submission.requester_system_user_id)
        assert issuance is not None and user is not None
        public_id = issuance.public_id
        user.is_active = False
        db.commit()
    with Session(native_engine) as db:
        with pytest.raises(DomainError) as denied:
            owner.field_submission_preview(
                db,
                query=FieldSubmissionPreviewQuery(
                    requester_system_user_id=prepared.submission.requester_system_user_id,
                    work_order_public_id=public_id,
                ),
            )
        assert denied.value.code == "operations.field_work_order_access.denied"


def test_native_customer_completion_enrollment_and_acceptance_without_engineering(
    native_engine: Engine, scope: NativeScope
) -> None:
    prepared = prepare(native_engine, scope, native=True)
    with Session(native_engine) as db:
        enrolled = db.get(CompletionEnrollment, prepared.submission.enrollment_id)
        assert enrolled is not None
        assert enrolled.provenance == CompletionProvenance.native_work_order
        assert enrolled.engineering_issuance_id is None
        assert (
            db.scalar(
                select(ProjectWorkOrderIssuance.id).where(
                    ProjectWorkOrderIssuance.work_order_id == enrolled.work_order_id
                )
            )
            is None
        )
    with Session(native_engine) as db:
        submitted = owner.submit_evidence(db, command=prepared.submission)
    with Session(native_engine) as db:
        accepted = owner.decide(db, command=review(prepared, submitted))
        assert accepted.state == ReviewState.accepted
    with Session(native_engine) as db:
        current = owner.current(
            db,
            query=CompletionReviewQuery(
                access=prepared.access, enrollment_id=prepared.submission.enrollment_id
            ),
        )
        assert current.state == ReviewState.accepted
        history = owner.history(
            db,
            query=CompletionReviewQuery(
                access=prepared.access, enrollment_id=prepared.submission.enrollment_id
            ),
        )
        assert (
            history.submissions[0].completed_event_id
            == prepared.submission.completed_event_id
        )
        assert {
            criterion.statement for criterion in history.submissions[0].criteria
        } == {"Native authored criterion"}


def test_event_mutation_winning_row_lock_refuses_old_submission(
    native_engine: Engine, scope: NativeScope
) -> None:
    """The insert trigger must re-read an event changed while submission waited."""
    from threading import Event as Signal
    from time import monotonic

    prepared = prepare(native_engine, scope, native=True)
    ready = Signal()
    pids: list[int] = []

    def submit_waiting() -> bool:
        with native_engine.connect() as connection:
            pid = connection.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(pid, int)
            pids.append(pid)
            connection.commit()
            with Session(bind=connection) as db:
                ready.set()
                try:
                    owner.submit_evidence(db, command=prepared.submission)
                except (DBAPIError, DomainError):
                    return False
                return True

    with Session(native_engine) as mutation, ThreadPoolExecutor(max_workers=1) as pool:
        mutation.execute(text("SET LOCAL statement_timeout='12s'"))
        mutation.execute(
            text("SELECT id FROM field_job_events WHERE id=:id FOR UPDATE"),
            {"id": prepared.submission.completed_event_id},
        )
        future = pool.submit(submit_waiting)
        try:
            assert ready.wait(10)
            deadline = monotonic() + 10
            with native_engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as observer:
                while monotonic() < deadline:
                    waiting = observer.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=:pid AND wait_event_type='Lock' AND query ILIKE '%completion_submissions%')"
                        ),
                        {"pid": pids[0]},
                    )
                    if waiting:
                        break
                    Signal().wait(0.02)
                else:
                    pytest.fail("Submission did not wait on the completed event row")
            mutation.execute(
                text(
                    "UPDATE field_job_events SET event='unable_to_complete',new_status='canceled' WHERE id=:id"
                ),
                {"id": prepared.submission.completed_event_id},
            )
            mutation.commit()
            assert not future.result(timeout=15)
        finally:
            mutation.rollback()
    with Session(native_engine) as db:
        assert (
            db.scalar(
                select(CompletionSubmission.id).where(
                    CompletionSubmission.enrollment_id
                    == prepared.submission.enrollment_id
                )
            )
            is None
        )


def test_submission_winning_row_lock_refuses_waiting_event_mutation(
    native_engine: Engine, scope: NativeScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event as Signal
    from time import monotonic

    prepared = prepare(native_engine, scope, native=True)
    inserted, release, mutation_ready = Signal(), Signal(), Signal()
    pids: list[int] = []
    original = owner.emit_event

    def pause_after_insert(
        db: Session,
        event_type: EventType,
        payload: dict[str, object],
        *,
        actor: str | None,
    ) -> Event:
        result = original(db, event_type, payload, actor=actor)
        inserted.set()
        assert release.wait(15), "Submission test release timed out"
        return result

    monkeypatch.setattr(owner, "emit_event", pause_after_insert)

    def submit_first() -> CompletionReviewOutcome:
        with Session(native_engine) as db:
            return owner.submit_evidence(db, command=prepared.submission)

    def mutate_waiting() -> bool:
        with native_engine.connect() as connection:
            pid = connection.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(pid, int)
            pids.append(pid)
            connection.commit()
            with Session(bind=connection) as db:
                mutation_ready.set()
                try:
                    db.execute(
                        text(
                            "UPDATE field_job_events SET new_status='canceled' WHERE id=:id"
                        ),
                        {"id": prepared.submission.completed_event_id},
                    )
                    db.commit()
                except DBAPIError:
                    db.rollback()
                    return False
                return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        submitting = pool.submit(submit_first)
        try:
            assert inserted.wait(10)
            mutating = pool.submit(mutate_waiting)
            assert mutation_ready.wait(10)
            deadline = monotonic() + 10
            with native_engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as observer:
                while monotonic() < deadline:
                    if observer.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=:pid AND wait_event_type='Lock')"
                        ),
                        {"pid": pids[0]},
                    ):
                        break
                    Signal().wait(0.02)
                else:
                    pytest.fail("Event mutation did not wait on submission event lock")
            release.set()
            assert submitting.result(timeout=15).submission_id is not None
            assert not mutating.result(timeout=15)
        finally:
            release.set()


@pytest.mark.parametrize("drift", ["criteria", "completed_event"])
def test_native_historical_withdrawal_retains_original_submission_receipt_tokens(
    native_engine: Engine, scope: NativeScope, drift: str
) -> None:
    prepared = prepare(native_engine, scope, native=True)
    with Session(native_engine) as db:
        submitted = owner.submit_evidence(db, command=prepared.submission)
    with Session(native_engine) as db:
        accepted = owner.decide(db, command=review(prepared, submitted))
    assert accepted.decision_id is not None
    if drift == "criteria":
        with Session(native_engine) as db:
            owner.publish_criteria(
                db,
                command=PublishCompletionCriteria(
                    context=context(prepared.reviewer, WRITE),
                    access=prepared.access,
                    enrollment_id=prepared.submission.enrollment_id,
                    expected_generation=1,
                    criteria=tuple(
                        IssuedCompletionCriterion(
                            id=value.criterion_id,
                            order=order,
                            statement="Replacement native criterion",
                            verification_method="New native inspection",
                        )
                        for order, value in enumerate(prepared.submission.observations)
                    ),
                ),
            )
    else:
        # A synthetic later native outcome isolates historical withdrawal from
        # execution transition eligibility; the original submission is unchanged.
        with Session(native_engine) as db:
            previous = db.get(FieldJobEvent, prepared.submission.completed_event_id)
            assert previous is not None
            db.add(
                FieldJobEvent(
                    work_order_mirror_id=previous.work_order_mirror_id,
                    person_id=previous.person_id,
                    system_user_id=previous.system_user_id,
                    author_vendor_user_id=previous.author_vendor_user_id,
                    author_technician_id=previous.author_technician_id,
                    event="complete",
                    new_status="completed",
                    assignment_generation=previous.assignment_generation,
                    client_event_id=uuid4(),
                    occurred_at=datetime.now(UTC),
                    received_at=datetime.now(UTC) + timedelta(seconds=1),
                )
            )
            db.commit()
    command = RevokeCompletionReview(
        context=context(prepared.reviewer, WRITE),
        access=prepared.access,
        enrollment_id=prepared.submission.enrollment_id,
        decision_id=accepted.decision_id,
        expected_generation=accepted.generation,
    )
    with Session(native_engine) as db:
        withdrawn = owner.revoke(db, command=command)
        assert withdrawn.state == ReviewState.revoked
        assert (
            withdrawn.criteria_revision_id == prepared.submission.criteria_revision_id
        )
        assert withdrawn.completed_event_id == prepared.submission.completed_event_id
        assert withdrawn.criteria_generation == 1
    with Session(native_engine) as db:
        replayed = owner.revoke(db, command=command)
        assert (
            replayed.replayed
            and replayed.completed_event_id == withdrawn.completed_event_id
        )
