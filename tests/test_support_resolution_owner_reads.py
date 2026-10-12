"""Protected support resolution previews and inert, idempotent request outcomes."""

from dataclasses import dataclass, replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.support import SupportResolutionRequest, TicketAccessToken
from app.services import project_engineering_plans, support
from app.services.domain_errors import DomainError
from app.services.field import completion_review
from app.services.field.completion_review_contracts import (
    SupportTicketCompletionPreviewQuery,
    TicketCompletionBasis,
)
from app.services.owner_commands import CommandContext, owner_command_active
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    CurrentEngineeringPlanApproval,
    SupportCompletionEngineeringBatchQuery,
    SupportCompletionEngineeringPreviewBatchQuery,
)
from app.services.staff_permission_admission import AuthorizedStaffPerson
from app.services.support_resolution_contracts import (
    SupportResolutionPreviewQuery,
    SupportResolutionRequestCommand,
)
from tests.test_support_completion_gate import GateRig
from tests.test_support_completion_gate import rig as gate_rig

rig = gate_rig


@dataclass(kw_only=True)
class ProtectedRig:
    gate: GateRig
    access: PermissionAdmission
    person_id: UUID
    denied: bool = False
    checks: int = 0


@pytest.fixture
def protected(rig: GateRig, monkeypatch: pytest.MonkeyPatch) -> ProtectedRig:
    state = ProtectedRig(
        gate=rig,
        access=PermissionAdmission(
            uuid4(), "system_user", frozenset({"support:ticket:update"})
        ),
        person_id=uuid4(),
    )

    def admission(
        db: Session, *, admission: PermissionAdmission, permission: str, write: bool
    ) -> AuthorizedStaffPerson:
        assert db is rig.db and permission == "support:ticket:update" and write
        state.checks += 1
        if state.denied or permission not in admission.admitted_permissions:
            raise DomainError(
                code="auth.staff_permission_admission.denied",
                message="Current permission refused",
                retryable=False,
            )
        return AuthorizedStaffPerson(admission.principal_id, state.person_id)

    monkeypatch.setattr(support, "revalidate_staff_admission", admission)
    monkeypatch.setattr(completion_review, "revalidate_staff_admission", admission)
    monkeypatch.setattr(
        project_engineering_plans, "revalidate_staff_admission", admission
    )
    return state


def command(state: ProtectedRig) -> SupportResolutionRequestCommand:
    identity = uuid4()
    return SupportResolutionRequestCommand(
        context=CommandContext(
            command_id=identity,
            correlation_id=identity,
            actor=str(state.access.principal_id),
            scope="support.ticket:request_resolution_confirmation",
            reason="Ask customer to confirm the repair",
            idempotency_key=str(identity),
        ),
        ticket_id=state.gate.ticket.id,
        actor_id=state.access.principal_id,
        access=state.access,
    )


def test_preview_has_exact_write_purpose_without_executor_or_persistence(
    protected: ProtectedRig,
) -> None:
    state = protected
    db = state.gate.db
    assert not owner_command_active(db)
    result = support.resolution_request_preview(
        db,
        query=SupportResolutionPreviewQuery(
            ticket_id=state.gate.ticket.id, access=state.access
        ),
    )
    assert result.readiness.is_ready
    assert result.readiness.action_key == "support.request_resolution_confirmation"
    assert result.current_request_id is None and result.deadline is None
    assert state.checks >= 4
    assert not owner_command_active(db)
    assert db.scalar(select(func.count()).select_from(SupportResolutionRequest)) == 0
    assert db.scalar(select(func.count()).select_from(TicketAccessToken)) == 0


def test_preview_blocker_comes_from_completion_owner_without_copied_policy(
    protected: ProtectedRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(
        db: Session, *, query: SupportTicketCompletionPreviewQuery
    ) -> TicketCompletionBasis:
        assert db is protected.gate.db and query.ticket_id == protected.gate.ticket.id
        assert not owner_command_active(db)
        raise DomainError(
            code="operations.field_completion.completion_pending",
            message="Current completion acceptance is required",
            retryable=False,
        )

    monkeypatch.setattr(
        completion_review, "preview_ticket_completion_basis", unavailable
    )
    result = support.resolution_request_preview(
        protected.gate.db,
        query=SupportResolutionPreviewQuery(
            ticket_id=protected.gate.ticket.id, access=protected.access
        ),
    )
    assert not result.readiness.is_ready
    assert result.readiness.blockers[0].owner == "operations.field_completion"
    assert result.readiness.blockers[0].code.endswith("completion_pending")


def test_preview_revalidates_permission_after_basis_wait(
    protected: ProtectedRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def changed(
        db: Session, *, query: SupportTicketCompletionPreviewQuery
    ) -> TicketCompletionBasis:
        protected.denied = True
        return TicketCompletionBasis(ticket_id=query.ticket_id, entries=())

    monkeypatch.setattr(completion_review, "preview_ticket_completion_basis", changed)
    with pytest.raises(DomainError, match="permission refused"):
        support.resolution_request_preview(
            protected.gate.db,
            query=SupportResolutionPreviewQuery(
                ticket_id=protected.gate.ticket.id, access=protected.access
            ),
        )


def test_typed_request_returns_stable_native_id_without_token_and_auth_before_replay(
    protected: ProtectedRig,
) -> None:
    intent = command(protected)
    first = support.request_resolution(protected.gate.db, command=intent)
    assert first.status == "pending_confirmation" and not first.replayed
    assert first.deadline > first.requested_at
    assert not hasattr(first, "token") and not hasattr(first, "token_id")
    retry = support.request_resolution(protected.gate.db, command=intent)
    assert retry.replayed and retry.request_id == first.request_id
    assert retry.deadline == first.deadline
    preview = support.resolution_request_preview(
        protected.gate.db,
        query=SupportResolutionPreviewQuery(
            ticket_id=intent.ticket_id, access=protected.access
        ),
    )
    assert preview.current_request_id == first.request_id
    protected.gate.db.rollback()
    protected.denied = True
    with pytest.raises(DomainError, match="permission refused"):
        support.request_resolution(protected.gate.db, command=intent)


def test_request_actor_mismatch_refuses_before_native_request(
    protected: ProtectedRig,
) -> None:
    intent = replace(command(protected), actor_id=uuid4())
    with pytest.raises(DomainError) as refusal:
        support.request_resolution(protected.gate.db, command=intent)
    assert refusal.value.code == "resolution_forbidden"
    assert (
        protected.gate.db.scalar(
            select(func.count()).select_from(SupportResolutionRequest)
        )
        == 0
    )


def test_support_preview_participant_refuses_missing_exact_update_permission(
    protected: ProtectedRig,
) -> None:
    denied = PermissionAdmission(
        protected.access.principal_id,
        "system_user",
        frozenset({"operations:completion-review:read"}),
    )
    with pytest.raises(DomainError, match="permission refused"):
        completion_review.preview_ticket_completion_basis(
            protected.gate.db,
            query=SupportTicketCompletionPreviewQuery(
                ticket_id=protected.gate.ticket.id, access=denied
            ),
        )


@pytest.mark.parametrize("grace_hours", [-1, 2**31, 10**20, 2**31 - 1])
def test_request_refuses_unrepresentable_grace_before_persistence(
    protected: ProtectedRig, grace_hours: int
) -> None:
    intent = replace(command(protected), grace_hours=grace_hours)
    with pytest.raises(DomainError) as refusal:
        support.request_resolution(protected.gate.db, command=intent)
    assert refusal.value.code == "invalid_grace_hours"
    assert not refusal.value.retryable
    assert (
        protected.gate.db.scalar(
            select(func.count()).select_from(SupportResolutionRequest)
        )
        == 0
    )
    assert (
        protected.gate.db.scalar(select(func.count()).select_from(TicketAccessToken))
        == 0
    )


def test_engineering_preview_revalidates_support_write_after_shared_kernel(
    protected: ProtectedRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def waited(
        db: Session, *, query: SupportCompletionEngineeringBatchQuery
    ) -> tuple[CurrentEngineeringPlanApproval, ...]:
        assert db is protected.gate.db and query.plans == ()
        assert not owner_command_active(db)
        protected.denied = True
        return ()

    monkeypatch.setattr(
        project_engineering_plans, "_support_completion_batch_locked", waited
    )
    with pytest.raises(DomainError, match="permission refused"):
        project_engineering_plans.preview_current_batch_for_support_completion(
            protected.gate.db,
            query=SupportCompletionEngineeringPreviewBatchQuery(
                plans=(), access=protected.access
            ),
        )
    assert not owner_command_active(protected.gate.db)
