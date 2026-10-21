"""Protected engineering replacement proof and exact follow-up owner boundary."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.infrastructure import InfrastructureReference, InfrastructureType
from app.services import project_work_order_handoff as handoff
from app.services.domain_errors import DomainError
from app.services.field import completion_review as owner
from app.services.field.completion_review_contracts import (
    CompletionProvenance,
    CurrentReplacementEvidenceQuery,
    EngineeringReplacementScope,
    RevokeFailedScopeReplacement,
)
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import EngineeringTechnology
from app.services.project_work_order_handoff_contracts import (
    CompletionIssuanceEvidence,
    CompletionIssuanceEvidenceQuery,
    CompletionIssuancePurpose,
)
from app.services.work_order_templates import WorkOrderPurpose
from tests.test_failed_scope_replacement import (
    ReplacementRig,
    context,
)
from tests.test_failed_scope_replacement import (
    replacement_rig as native_replacement_rig,
)

replacement_rig = native_replacement_rig


def engineering_pair():
    shared = dict(
        project_id=uuid4(),
        task_id=uuid4(),
        origin_ticket_id=None,
        subscriber_id=None,
        provenance=CompletionProvenance.engineering_issuance,
    )
    return tuple(
        SimpleNamespace(
            **shared,
            id=uuid4(),
            work_order_id=uuid4(),
            public_id=str(uuid4()),
            engineering_issuance_id=uuid4(),
        )
        for _ in range(2)
    )


def receipt(item):
    return CompletionIssuanceEvidence(
        issuance_id=item.engineering_issuance_id,
        work_order_id=item.work_order_id,
        public_id=item.public_id,
        project_id=item.project_id,
        task_id=item.task_id,
        plan_id=uuid4(),
        revision_id=uuid4(),
        approval_id=uuid4(),
        digest="a" * 64,
        infrastructure=InfrastructureReference(
            type=InfrastructureType.pon_port, id=uuid4()
        ),
        purpose=WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT,
        technology=EngineeringTechnology.fiber,
    )


def test_engineering_scope_accepts_exact_project_task_with_no_ticket():
    source, target = engineering_pair()
    assert owner._replacement_scope(source, target) == EngineeringReplacementScope(
        source.project_id, source.task_id
    )
    source.task_id = target.task_id = None
    assert owner._replacement_scope(source, target).task_id is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", uuid4()),
        ("task_id", uuid4()),
        ("origin_ticket_id", uuid4()),
        ("subscriber_id", uuid4()),
        ("provenance", CompletionProvenance.native_work_order),
    ],
)
def test_engineering_scope_refuses_mixing_and_scope_drift(field, value):
    source, target = engineering_pair()
    setattr(target, field, value)
    with pytest.raises(DomainError):
        owner._replacement_scope(source, target)


@pytest.mark.parametrize(
    "field",
    [
        "revision_id",
        "approval_id",
        "digest",
        "plan_id",
        "infrastructure",
        "purpose",
        "technology",
    ],
)
def test_engineering_proof_refuses_any_receipt_equivalence_drift(monkeypatch, field):
    source, target = engineering_pair()
    first = receipt(source)
    second = replace(
        first,
        issuance_id=target.engineering_issuance_id,
        work_order_id=target.work_order_id,
        public_id=target.public_id,
    )
    values = dict(
        revision_id=uuid4(),
        approval_id=uuid4(),
        digest="b" * 64,
        plan_id=uuid4(),
        infrastructure=InfrastructureReference(
            type=InfrastructureType.pon_port, id=uuid4()
        ),
        purpose=WorkOrderPurpose.REPAIR,
        technology=EngineeringTechnology.radio,
    )
    second = replace(second, **{field: values[field]})
    monkeypatch.setattr(
        handoff,
        "completion_issuance_evidence",
        lambda db, query: first if query.issuance_id == first.issuance_id else second,
    )
    calls = []
    monkeypatch.setattr(
        owner, "_engineering_current", lambda *args, **kwargs: calls.append(kwargs)
    )
    with pytest.raises(DomainError):
        owner._engineering_replacement_proof(
            None,
            source=source,
            replacement=target,
            access=PermissionAdmission(uuid4(), "system_user", frozenset()),
            purpose="write",
        )
    assert calls == []


@pytest.mark.parametrize("purpose", ["stage", "read", "write"])
def test_engineering_proof_calls_completion_freshness_for_both_scopes(
    monkeypatch, purpose
):
    source, target = engineering_pair()
    first = receipt(source)
    second = replace(
        first,
        issuance_id=target.engineering_issuance_id,
        work_order_id=target.work_order_id,
        public_id=target.public_id,
    )
    queries = []

    def resolve(db, *, query):
        queries.append(query)
        return first if query.issuance_id == first.issuance_id else second

    monkeypatch.setattr(handoff, "completion_issuance_evidence", resolve)
    calls = []
    monkeypatch.setattr(
        owner, "_engineering_current", lambda *args, **kwargs: calls.append(kwargs)
    )
    owner._engineering_replacement_proof(
        None,
        source=source,
        replacement=target,
        access=PermissionAdmission(uuid4(), "system_user", frozenset()),
        purpose=purpose,
    )
    assert [call["enrollment"] for call in calls] == [source, target]
    assert all(call["purpose"] == purpose for call in calls)
    assert all(
        query.purpose
        == (
            CompletionIssuancePurpose.read
            if purpose == "read"
            else CompletionIssuancePurpose.review
        )
        for query in queries
    )


@pytest.mark.parametrize("purpose", list(CompletionIssuancePurpose))
def test_protected_issuance_read_revalidates_exact_identity_without_issuance_gate(
    monkeypatch, purpose
):
    source, _ = engineering_pair()
    proof = receipt(source)
    row = SimpleNamespace(
        **{
            name: getattr(proof, name)
            for name in (
                "work_order_id",
                "public_id",
                "project_id",
                "task_id",
                "plan_id",
                "revision_id",
                "approval_id",
                "digest",
                "purpose",
                "technology",
            )
        },
        id=proof.issuance_id,
        infrastructure_type=proof.infrastructure.type,
        infrastructure_id=proof.infrastructure.id,
    )
    db = SimpleNamespace(scalar=lambda query: row)
    calls = []
    monkeypatch.setattr(
        handoff,
        "revalidate_staff_admission",
        lambda db, **kw: calls.append(kw) or "actor",
    )
    query = CompletionIssuanceEvidenceQuery(
        access=PermissionAdmission(uuid4(), "system_user", frozenset()),
        purpose=purpose,
        issuance_id=proof.issuance_id,
        expected_work_order_id=proof.work_order_id,
        expected_public_id=proof.public_id,
    )
    assert handoff.completion_issuance_evidence(db, query=query) == proof
    assert len(calls) == 2
    assert all(
        call["permission"]
        == "operations:completion-review:"
        + ("read" if purpose == CompletionIssuancePurpose.read else "write")
        for call in calls
    )
    with pytest.raises(DomainError):
        handoff.completion_issuance_evidence(
            db, query=replace(query, expected_work_order_id=uuid4())
        )


def test_followup_proof_binds_exact_failure_and_current_head(
    replacement_rig: ReplacementRig,
):
    rig = replacement_rig
    result = owner.review_failed_scope_replacement(rig.db, command=rig.command)
    source = owner._enrollment(rig.db, rig.command.source_enrollment_id)
    query = CurrentReplacementEvidenceQuery(
        access=rig.command.access,
        source_work_order_id=source.work_order_id,
        decision_id=result.decision_id,
        failure_basis=rig.command.failure_basis,
    )
    proof = owner.current_replacement_evidence(rig.db, query=query)
    assert proof.generation == result.generation
    with pytest.raises(DomainError):
        owner.current_replacement_evidence(
            rig.db, query=replace(query, source_work_order_id=uuid4())
        )
    rig.db.rollback()
    owner.revoke_failed_scope_replacement(
        rig.db,
        command=RevokeFailedScopeReplacement(
            context=context(rig.staff),
            access=rig.command.access,
            source_enrollment_id=rig.command.source_enrollment_id,
            decision_id=result.decision_id,
            expected_generation=result.generation,
        ),
    )
    with pytest.raises(DomainError):
        owner.current_replacement_evidence(rig.db, query=query)


@pytest.mark.parametrize(
    "drift", [None, "text", "order", "revision", "coverage", "work_scope"]
)
def test_engineering_canonical_criteria_and_work_scope_are_exact(monkeypatch, drift):
    from app.models.field_completion_review import CompletionCriteriaRevision
    from app.models.work_order import WorkOrder
    from app.services.field.completion_review_contracts import CriterionCoverage

    source, target = engineering_pair()
    first = receipt(source)
    second = replace(
        first,
        issuance_id=target.engineering_issuance_id,
        work_order_id=target.work_order_id,
        public_id=target.public_id,
    )
    source_revision, target_revision = uuid4(), uuid4()
    criterion_id = uuid4()
    canonical = SimpleNamespace(
        id=criterion_id,
        order=0,
        statement="Exact canonical engineering criterion",
        verification_method="Inspect canonical evidence",
    )
    target_criterion = SimpleNamespace(**vars(canonical))
    if drift == "text":
        target_criterion.statement = "Different scope"
    if drift == "order":
        target_criterion.order = 1
    revisions = {
        source_revision: SimpleNamespace(
            enrollment_id=source.id, engineering_revision_id=first.revision_id
        ),
        target_revision: SimpleNamespace(
            enrollment_id=target.id,
            engineering_revision_id=first.revision_id
            if drift != "revision"
            else uuid4(),
        ),
    }

    class Db:
        def __init__(self):
            self.calls = 0

        def get(self, model, identity, **kw):
            if model == CompletionCriteriaRevision:
                return revisions[identity]
            if model == WorkOrder:
                return SimpleNamespace(
                    purpose=first.purpose,
                    access_technology=first.technology,
                    work_order_kind="infrastructure",
                )
            return first if identity == first.issuance_id else second

        def scalars(self, query):
            self.calls += 1
            return SimpleNamespace(
                all=lambda: [target_criterion if self.calls == 3 else canonical]
            )

    monkeypatch.setattr(
        owner, "_scope_matches", lambda row, enrollment: drift != "work_scope"
    )
    decision = SimpleNamespace(
        source_criteria_revision_id=source_revision,
        replacement_criteria_revision_id=target_revision,
    )
    coverage = (
        CriterionCoverage(
            criterion_id, uuid4() if drift == "coverage" else criterion_id
        ),
    )
    if drift is None:
        owner._engineering_replacement_criteria(
            Db(),
            source=source,
            replacement=target,
            decision=decision,
            coverage=coverage,
        )
    else:
        with pytest.raises(DomainError):
            owner._engineering_replacement_criteria(
                Db(),
                source=source,
                replacement=target,
                decision=decision,
                coverage=coverage,
            )


def test_historical_revoke_remains_available_after_ticket_closes(
    replacement_rig: ReplacementRig,
):
    from app.models.support import Ticket

    rig = replacement_rig
    approved = owner.review_failed_scope_replacement(rig.db, command=rig.command)
    ticket = rig.db.get(Ticket, rig.ticket)
    assert ticket is not None
    ticket.status = "closed"
    rig.db.commit()
    revoked = owner.revoke_failed_scope_replacement(
        rig.db,
        command=RevokeFailedScopeReplacement(
            context=context(rig.staff),
            access=rig.command.access,
            source_enrollment_id=rig.command.source_enrollment_id,
            decision_id=approved.decision_id,
            expected_generation=approved.generation,
        ),
    )
    assert revoked.generation == approved.generation + 1


def test_protected_issuance_read_rejects_unknown_purpose_before_db_or_admission(
    monkeypatch,
):
    def unexpected(*args, **kwargs):
        pytest.fail("Unknown purpose must fail before admission or receipt query")

    monkeypatch.setattr(handoff, "revalidate_staff_admission", unexpected)
    query = CompletionIssuanceEvidenceQuery(
        access=PermissionAdmission(uuid4(), "system_user", frozenset()),
        purpose="unknown",
        issuance_id=uuid4(),
        expected_work_order_id=uuid4(),
        expected_public_id="unknown",
    )
    with pytest.raises(DomainError):
        handoff.completion_issuance_evidence(
            SimpleNamespace(scalar=unexpected), query=query
        )
