"""Non-authoritative fast unit lane: SQLite checks command behavior, never PG locks."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.db import Base
from app.models.party import Party
from app.models.project_engineering_plan import (
    EngineeringPlanCriterion,
    EngineeringPlanDecision,
    EngineeringPlanReceipt,
    EngineeringPlanRevision,
    EngineeringPolicyHead,
    EngineeringPolicyRevision,
    ProjectEngineeringPlan,
)
from app.models.system_user import SystemUser
from app.schemas.infrastructure import InfrastructureReference, InfrastructureType
from app.services import project_engineering_plans as service
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    APPROVE,
    POLICY,
    READ,
    REVOKE,
    SCOPE,
    WRITE,
    CurrentEngineeringPlanQuery,
    DecideEngineeringPlan,
    DraftEngineeringPlan,
    EngineeringCriterion,
    EngineeringPlanContent,
    EngineeringTechnology,
    GeoPoint,
    PlanDecisionKind,
    PublishApprovalPolicy,
    SupportCompletionEngineeringBatchQuery,
    SupportCompletionEngineeringQuery,
    SurveyedPlanLocation,
)
from app.services.projects import (
    ProjectEngineeringOrigin,
    ProjectEngineeringRevocationScope,
)
from app.services.staff_permission_admission import AuthorizedStaffPerson

NOW = datetime(2026, 10, 11, tzinfo=UTC)


@pytest.fixture
def rig(monkeypatch):
    engine = create_engine("sqlite://")
    classes = (
        EngineeringPlanRevision,
        EngineeringPlanCriterion,
        EngineeringPlanDecision,
        EngineeringPlanReceipt,
        EngineeringPolicyHead,
        EngineeringPolicyRevision,
        ProjectEngineeringPlan,
    )
    Base.metadata.create_all(engine, tables=[cls.__table__ for cls in classes])
    author, reviewer, author_person, reviewer_person = (uuid4() for _ in range(4))
    identities = {author: author_person, reviewer: reviewer_person}

    class UnitSession(Session):
        def get(self, entity, ident, **kwargs):
            if entity is SystemUser:
                return SimpleNamespace(person_party_id=identities[ident])
            if entity is Party:
                return SimpleNamespace(party_type="person", status="active")
            return super().get(entity, ident, **kwargs)

    db = UnitSession(engine)
    target = InfrastructureReference(type=InfrastructureType.cabinet, id=uuid4())
    origin = ProjectEngineeringOrigin(
        project_id=uuid4(),
        task_id=uuid4(),
        project_updated_at=NOW,
        task_updated_at=NOW,
        project_template_id=None,
        applied_template_revision=None,
        task_template_id=None,
        task_template_revision=None,
        infrastructure=target,
        issuance_eligible=True,
    )
    state = SimpleNamespace(
        db=db,
        origin=origin,
        author=author,
        reviewer=reviewer,
        identities=identities,
        denied=False,
    )

    def authorize(_db, *, admission, permission, write):
        if state.denied or permission not in admission.admitted_permissions:
            raise service._error("denied")
        return AuthorizedStaffPerson(
            admission.principal_id, identities[admission.principal_id]
        )

    def native_origin(_db, *, project_id, task_id, lock):
        if project_id != state.origin.project_id or task_id not in {
            None,
            state.origin.task_id,
        }:
            raise service._error("invalid_request")
        return replace(
            state.origin, task_id=task_id, task_updated_at=NOW if task_id else None
        )

    monkeypatch.setattr(service, "revalidate_staff_admission", authorize)
    monkeypatch.setattr(service, "engineering_plan_origin", native_origin)
    monkeypatch.setattr(
        service,
        "engineering_plan_revocation_scope",
        lambda _db, *, project_id, task_id: ProjectEngineeringRevocationScope(
            project_id=project_id,
            task_id=task_id,
            project_updated_at=NOW,
            task_updated_at=NOW if task_id else None,
        ),
    )
    monkeypatch.setattr(
        service.infrastructure_catalogue,
        "lock_reference",
        lambda *a, **kw: SimpleNamespace(pon_parent_olt_id=None),
    )
    monkeypatch.setattr(service.audit_adapter, "stage", lambda *a, **kw: None)
    monkeypatch.setattr(service, "emit_event", lambda *a, **kw: None)
    yield state
    db.close()
    engine.dispose()


def access(actor):
    return PermissionAdmission(
        actor, "system_user", frozenset({READ, WRITE, APPROVE, REVOKE, POLICY})
    )


def context(actor, key=None):
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=str(actor),
        scope=SCOPE,
        reason="Reviewed native scope",
        idempotency_key=key or str(uuid4()),
    )


def content(rig, technology=EngineeringTechnology.fiber):
    return EngineeringPlanContent(
        technology=technology,
        infrastructure=rig.origin.infrastructure,
        location=SurveyedPlanLocation(
            description="Reviewed cabinet survey",
            point=GeoPoint(latitude=Decimal("9.1"), longitude=Decimal("7.2")),
        ),
        scope_statement="Reviewed infrastructure preparation",
        criteria=(
            EngineeringCriterion(
                id=uuid4(),
                order=0,
                statement="Meet the signed site design",
                verification_method="Engineering review of measured evidence",
            ),
        ),
    )


def policy(rig, independent=True, generation=0):
    return service.publish_policy(
        rig.db,
        command=PublishApprovalPolicy(
            context=context(rig.reviewer),
            access=access(rig.reviewer),
            project_id=rig.origin.project_id,
            expected_project_updated_at=NOW,
            expected_generation=generation,
            independent_reviewer_required=independent,
        ),
    )


def draft(rig, *, plan_id=None, generation=0, value=None):
    command = DraftEngineeringPlan(
        context=context(rig.author),
        access=access(rig.author),
        plan_id=plan_id or uuid4(),
        project_id=rig.origin.project_id,
        task_id=rig.origin.task_id,
        expected_project_updated_at=NOW,
        expected_task_updated_at=NOW,
        expected_generation=generation,
        content=value or content(rig),
    )
    return command, service.draft_revision(rig.db, command=command)


def decision(
    rig,
    drafted,
    configured,
    *,
    kind=PlanDecisionKind.approve,
    actor=None,
    generation=None,
):
    actor = actor or rig.reviewer
    return DecideEngineeringPlan(
        context=context(actor),
        access=access(actor),
        plan_id=drafted.plan_id,
        project_id=rig.origin.project_id,
        task_id=rig.origin.task_id,
        expected_project_updated_at=NOW,
        expected_task_updated_at=NOW,
        expected_generation=generation
        if generation is not None
        else drafted.generation,
        revision_id=drafted.revision_id,
        expected_digest=drafted.digest,
        expected_policy_revision_id=configured.policy_revision_id,
        decision=kind,
    )


def query(rig, approved):
    return CurrentEngineeringPlanQuery(
        access=access(rig.reviewer),
        plan_id=approved.plan_id,
        project_id=rig.origin.project_id,
        task_id=rig.origin.task_id,
        expected_revision_id=approved.revision_id,
        expected_approval_id=approved.decision_id,
        expected_digest=approved.digest,
    )


@pytest.mark.parametrize("technology", list(EngineeringTechnology))
def test_explicit_approval_and_exact_native_provenance(rig, technology):
    configured = policy(rig)
    _, drafted = draft(rig, value=content(rig, technology))
    approved = service.decide(rig.db, command=decision(rig, drafted, configured))
    resolved = service.resolve_current(rig.db, query=query(rig, approved))
    assert resolved.project_id == rig.origin.project_id
    assert resolved.task_id == rig.origin.task_id
    assert resolved.policy_revision_id == configured.policy_revision_id
    assert resolved.content.technology is technology
    assert resolved.author_person_id == rig.identities[rig.author]
    assert resolved.reviewer_person_id == rig.identities[rig.reviewer]


def test_configurable_independence_freezes_exact_policy(rig):
    configured = policy(rig, independent=True)
    _, drafted = draft(rig)
    with pytest.raises(service.EngineeringPlanError, match="denied"):
        service.decide(
            rig.db, command=decision(rig, drafted, configured, actor=rig.author)
        )
    configured = policy(rig, independent=False, generation=1)
    approved = service.decide(
        rig.db, command=decision(rig, drafted, configured, actor=rig.author)
    )
    policy(rig, independent=True, generation=2)
    resolved = service.resolve_current(rig.db, query=query(rig, approved))
    assert resolved.policy_revision_id == configured.policy_revision_id
    assert resolved.author_person_id == resolved.reviewer_person_id


def test_same_person_with_different_principal_is_not_independent(rig):
    rig.identities[rig.reviewer] = rig.identities[rig.author]
    configured = policy(rig)
    _, drafted = draft(rig)
    with pytest.raises(service.EngineeringPlanError, match="denied"):
        service.decide(rig.db, command=decision(rig, drafted, configured))


def test_draft_preserves_approval_then_new_approval_supersedes_and_revoke_has_no_fallback(
    rig,
):
    configured = policy(rig)
    _, drafted = draft(rig)
    approved = service.decide(rig.db, command=decision(rig, drafted, configured))
    _, newer = draft(rig, plan_id=approved.plan_id, generation=approved.generation)
    assert (
        service.resolve_current(rig.db, query=query(rig, approved)).approval_id
        == approved.decision_id
    )
    rig.db.rollback()  # finish read transaction before the next owner command
    replacement = service.decide(rig.db, command=decision(rig, newer, configured))
    with pytest.raises(service.EngineeringPlanError, match="unapproved"):
        service.resolve_current(rig.db, query=query(rig, approved))
    rig.db.rollback()
    revoked = service.decide(
        rig.db,
        command=decision(rig, replacement, configured, kind=PlanDecisionKind.revoke),
    )
    assert revoked.action == "revoke"
    with pytest.raises(service.EngineeringPlanError, match="unapproved"):
        service.resolve_current(rig.db, query=query(rig, replacement))
    kinds = rig.db.scalars(select(EngineeringPlanDecision.kind)).all()
    assert kinds.count("supersede") == 1


def test_rejection_requires_new_revision(rig):
    configured = policy(rig)
    _, drafted = draft(rig)
    rejected = service.decide(
        rig.db, command=decision(rig, drafted, configured, kind=PlanDecisionKind.reject)
    )
    with pytest.raises(service.EngineeringPlanError, match="stale state"):
        service.decide(rig.db, command=decision(rig, rejected, configured))


def test_historical_retry_requires_current_authorization_and_conflicts_fail(rig):
    configured = policy(rig)
    _, drafted = draft(rig)
    command = decision(rig, drafted, configured)
    approved = service.decide(rig.db, command=command)
    service.decide(
        rig.db,
        command=decision(rig, approved, configured, kind=PlanDecisionKind.revoke),
    )
    replay = service.decide(rig.db, command=command)
    assert replay.replayed and replay.decision_id == approved.decision_id
    rig.denied = True
    with pytest.raises(service.EngineeringPlanError, match="denied"):
        service.decide(rig.db, command=command)
    rig.denied = False
    with pytest.raises(service.EngineeringPlanError, match="idempotency conflict"):
        service.decide(rig.db, command=replace(command, expected_digest="0" * 64))


@pytest.mark.parametrize(
    "change", ["generation", "timestamp", "digest", "policy", "project", "task"]
)
def test_stale_or_mismatched_decisions_fail_closed(rig, change):
    configured = policy(rig)
    _, drafted = draft(rig)
    command = decision(rig, drafted, configured)
    changes = {
        "generation": {"expected_generation": 99},
        "timestamp": {"expected_project_updated_at": datetime(2025, 1, 1, tzinfo=UTC)},
        "digest": {"expected_digest": "0" * 64},
        "policy": {"expected_policy_revision_id": uuid4()},
        "project": {"project_id": uuid4()},
        "task": {"task_id": uuid4()},
    }
    with pytest.raises(service.EngineeringPlanError):
        service.decide(rig.db, command=replace(command, **changes[change]))


def test_current_resolution_denies_ineligible_origin_and_engineering_binding_change(
    rig,
):
    configured = policy(rig)
    _, drafted = draft(rig)
    approved = service.decide(rig.db, command=decision(rig, drafted, configured))
    rig.origin = replace(rig.origin, issuance_eligible=False)
    with pytest.raises(service.EngineeringPlanError, match="unapproved"):
        service.resolve_current(rig.db, query=query(rig, approved))
    rig.origin = replace(
        rig.origin, issuance_eligible=True, applied_template_revision=2
    )
    with pytest.raises(service.EngineeringPlanError, match="stale state"):
        service.resolve_current(rig.db, query=query(rig, approved))


def test_criteria_and_location_are_typed_and_whitespace_not_accepted(rig):
    with pytest.raises(ValidationError):
        GeoPoint(latitude=Decimal("NaN"), longitude=0)
    value = content(rig)
    duplicate = value.model_copy(
        update={"criteria": (value.criteria[0], value.criteria[0])}
    )
    with pytest.raises(service.EngineeringPlanError, match="invalid request"):
        draft(rig, value=duplicate)
    with pytest.raises(service.EngineeringPlanError, match="invalid request"):
        draft(rig, value=value.model_copy(update={"scope_statement": "  "}))
    with pytest.raises(service.EngineeringPlanError, match="invalid request"):
        draft(rig, value=value.model_copy(update={"criteria": ()}))


def test_atomic_failure_rolls_back_revision_and_receipt(rig, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("outbox staging failed")

    monkeypatch.setattr(service, "emit_event", fail)
    with pytest.raises(RuntimeError):
        draft(rig)
    assert rig.db.scalar(select(EngineeringPlanRevision.id)) is None
    assert rig.db.scalar(select(EngineeringPlanReceipt.id)) is None


def test_immutable_orm_evidence(rig):
    _, outcome = draft(rig)
    row = rig.db.get(EngineeringPlanRevision, outcome.revision_id)
    row.digest = "0" * 64
    with pytest.raises(ValueError, match="immutable"):
        rig.db.flush()


def test_canonical_admission_revalidates_current_grants_and_leave(
    db_session, monkeypatch
):
    from app.models.rbac import Permission, SystemUserPermission
    from app.services import erp_staff_access
    from app.services.domain_errors import DomainError
    from app.services.staff_permission_admission import revalidate_staff_admission
    from tests.staff_identity_fixtures import add_bound_staff_user

    user, person = add_bound_staff_user(db_session)
    wildcard = Permission(key="project:engineering_plan:*", is_active=True)
    db_session.add(wildcard)
    db_session.flush()
    grant = SystemUserPermission(system_user_id=user.id, permission_id=wildcard.id)
    db_session.add(grant)
    db_session.commit()
    admitted = PermissionAdmission(user.id, "system_user", frozenset({APPROVE}))
    assert (
        revalidate_staff_admission(
            db_session, admission=admitted, permission=APPROVE, write=True
        ).person_id
        == person.id
    )
    monkeypatch.setattr(
        erp_staff_access, "active_leave_restriction", lambda *a, **kw: object()
    )
    with pytest.raises(DomainError, match="Current staff permission"):
        revalidate_staff_admission(
            db_session, admission=admitted, permission=APPROVE, write=True
        )
    monkeypatch.setattr(
        erp_staff_access, "active_leave_restriction", lambda *a, **kw: None
    )
    db_session.delete(grant)
    db_session.commit()
    with pytest.raises(DomainError, match="Current staff permission"):
        revalidate_staff_admission(
            db_session, admission=admitted, permission=APPROVE, write=True
        )


def test_missing_policy_and_mismatched_structural_target_are_denied(rig):
    _, drafted = draft(rig)
    configured = SimpleNamespace(policy_revision_id=uuid4())
    with pytest.raises(service.EngineeringPlanError, match="stale state"):
        service.decide(rig.db, command=decision(rig, drafted, configured))
    value = content(rig).model_copy(
        update={
            "infrastructure": InfrastructureReference(
                type=InfrastructureType.cabinet, id=uuid4()
            )
        }
    )
    with pytest.raises(service.EngineeringPlanError, match="invalid request"):
        draft(rig, plan_id=drafted.plan_id, generation=drafted.generation, value=value)


def test_revocation_survives_archived_or_removed_operational_origin(rig, monkeypatch):
    configured = policy(rig)
    _, drafted = draft(rig)
    approved = service.decide(rig.db, command=decision(rig, drafted, configured))

    def inactive(*args, **kwargs):
        raise service._error("invalid_request")

    monkeypatch.setattr(service, "engineering_plan_origin", inactive)
    revoked = service.decide(
        rig.db,
        command=decision(rig, approved, configured, kind=PlanDecisionKind.revoke),
    )
    assert revoked.action == "revoke"


def test_authorization_is_rechecked_after_waiting_origin_lock_before_replay(
    rig, monkeypatch
):
    configured = policy(rig)
    _, drafted = draft(rig)
    command = decision(rig, drafted, configured)
    original = service.engineering_plan_revocation_scope

    def waited(*args, **kwargs):
        result = original(*args, **kwargs)
        rig.denied = True
        return result

    monkeypatch.setattr(service, "engineering_plan_revocation_scope", waited)
    with pytest.raises(service.EngineeringPlanError, match="denied"):
        service.decide(rig.db, command=command)
    assert rig.db.scalar(select(EngineeringPlanDecision.id)) is None


def test_person_binding_cannot_change_across_inventory_wait(rig, monkeypatch):
    configured = policy(rig)
    _, drafted = draft(rig)

    def change_identity(*args, **kwargs):
        rig.identities[rig.reviewer] = rig.identities[rig.author]

    monkeypatch.setattr(service, "_scope_locks", change_identity)
    with pytest.raises(service.EngineeringPlanError, match="denied"):
        service.decide(rig.db, command=decision(rig, drafted, configured))
    assert rig.db.scalar(select(EngineeringPlanDecision.id)) is None


def test_protected_current_read_rechecks_authorization_after_scope_wait(
    rig, monkeypatch
):
    configured = policy(rig)
    _, drafted = draft(rig)
    approved = service.decide(rig.db, command=decision(rig, drafted, configured))

    def revoke_grant(*args, **kwargs):
        rig.denied = True

    monkeypatch.setattr(service, "_scope_locks", revoke_grant)
    with pytest.raises(service.EngineeringPlanError, match="denied"):
        service.resolve_current(rig.db, query=query(rig, approved))


def test_exact_locked_pon_parent_is_approval_bearing(rig, monkeypatch):
    rig.origin = replace(
        rig.origin,
        infrastructure=InfrastructureReference(
            type=InfrastructureType.pon_port, id=uuid4()
        ),
    )
    parents = [uuid4()]
    monkeypatch.setattr(
        service.infrastructure_catalogue,
        "lock_reference",
        lambda *a, **kw: SimpleNamespace(pon_parent_olt_id=parents[0]),
    )
    configured = policy(rig)
    _, drafted = draft(rig)
    row = rig.db.get(EngineeringPlanRevision, drafted.revision_id)
    assert row.origin_binding["pon_parent_olt_id"] == str(parents[0])
    rig.db.rollback()
    approved = service.decide(rig.db, command=decision(rig, drafted, configured))
    service.resolve_current(rig.db, query=query(rig, approved))
    parents[0] = uuid4()
    with pytest.raises(service.EngineeringPlanError, match="stale state"):
        service.resolve_current(rig.db, query=query(rig, approved))


def test_parent_observed_after_inventory_wait_invalidates_draft_review(
    rig, monkeypatch
):
    rig.origin = replace(
        rig.origin,
        infrastructure=InfrastructureReference(
            type=InfrastructureType.pon_port, id=uuid4()
        ),
    )
    configured = policy(rig)
    parents = [uuid4()]
    monkeypatch.setattr(
        service.infrastructure_catalogue,
        "lock_reference",
        lambda *a, **kw: SimpleNamespace(pon_parent_olt_id=parents[0]),
    )
    _, drafted = draft(rig)
    original = service.engineering_plan_origin

    def rebind_after_origin(*args, **kwargs):
        origin = original(*args, **kwargs)
        parents[0] = uuid4()
        return origin

    monkeypatch.setattr(service, "engineering_plan_origin", rebind_after_origin)
    with pytest.raises(service.EngineeringPlanError, match="stale state"):
        service.decide(rig.db, command=decision(rig, drafted, configured))


@pytest.mark.parametrize(
    "failure",
    [
        "inactive_user",
        "wrong_type",
        "missing_person",
        "inactive_person",
        "non_person",
        "vendor",
        "forged_grant",
    ],
)
def test_strict_staff_admission_rejects_current_native_ineligibility(
    db_session, failure
):
    from app.models.field_vendor import FieldVendor, FieldVendorUser
    from app.models.rbac import Permission, SystemUserPermission
    from app.models.system_user import SystemUserType
    from app.services.domain_errors import DomainError
    from app.services.staff_permission_admission import revalidate_staff_admission
    from tests.staff_identity_fixtures import add_bound_staff_user

    user, person = add_bound_staff_user(db_session)
    permission = Permission(key=APPROVE, is_active=True)
    db_session.add(permission)
    db_session.flush()
    if failure != "forged_grant":
        db_session.add(
            SystemUserPermission(system_user_id=user.id, permission_id=permission.id)
        )
    if failure == "inactive_user":
        user.is_active = False
    elif failure == "wrong_type":
        user.user_type = SystemUserType.customer
    elif failure == "missing_person":
        user.person_party_id = None
        user.party_bound_at = None
        user.party_binding_source = None
        user.party_binding_reason = None
    elif failure == "inactive_person":
        person.status = "archived"
    elif failure == "non_person":
        person.party_type = "organization"
    elif failure == "vendor":
        vendor = FieldVendor(name="Native contractor")
        db_session.add(vendor)
        db_session.flush()
        db_session.add(
            FieldVendorUser(
                vendor_id=vendor.id, system_user_id=user.id, is_active=False
            )
        )
    db_session.commit()
    with pytest.raises(DomainError, match="Current staff permission"):
        revalidate_staff_admission(
            db_session,
            admission=PermissionAdmission(user.id, "system_user", frozenset({APPROVE})),
            permission=APPROVE,
            write=True,
        )


def test_named_support_batch_preserves_exact_approval_and_revocation(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = policy(rig)
    _, drafted = draft(rig)
    approved = service.decide(rig.db, command=decision(rig, drafted, configured))
    requested = query(rig, approved)
    batch = SupportCompletionEngineeringBatchQuery(
        plans=(
            SupportCompletionEngineeringQuery(
                plan_id=requested.plan_id,
                project_id=requested.project_id,
                task_id=requested.task_id,
                expected_revision_id=requested.expected_revision_id,
                expected_approval_id=requested.expected_approval_id,
                expected_digest=requested.expected_digest,
            ),
        )
    )
    rig.db.rollback()

    def active(db: Session, *, owner: str) -> bool:
        assert db is rig.db
        return owner == "support.ticket_lifecycle"

    with monkeypatch.context() as patched:
        patched.setattr(service, "owner_command_active", active)
        resolved = service.stage_current_batch_for_support_completion(
            rig.db, query=batch
        )
        assert len(resolved) == 1 and resolved[0].approval_id == approved.decision_id
    rig.db.rollback()
    service.decide(
        rig.db,
        command=decision(
            rig,
            drafted,
            configured,
            kind=PlanDecisionKind.revoke,
            generation=approved.generation,
        ),
    )
    with monkeypatch.context() as patched:
        patched.setattr(service, "owner_command_active", active)
        with pytest.raises(service.EngineeringPlanError):
            service.stage_current_batch_for_support_completion(rig.db, query=batch)
