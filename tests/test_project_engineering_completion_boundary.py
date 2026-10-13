"""Fast unit checks for the narrow staff completion engineering capability."""

from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.services import project_engineering_plans as owner
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    CompletionEngineeringPlanQuery,
    CurrentEngineeringPlanApproval,
    CurrentEngineeringPlanQuery,
    SupportCompletionEngineeringBatchQuery,
)


class ResolutionReached(Exception):
    """Stop at the shared freshness boundary without constructing DB facts."""


@dataclass
class ResolutionCall:
    permission: str
    write: bool
    issuance_eligible_required: bool


@dataclass
class BoundaryRig:
    db: Session
    query: CompletionEngineeringPlanQuery
    calls: list[ResolutionCall] = field(default_factory=list)
    active: bool = True


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> BoundaryRig:
    state = BoundaryRig(
        db=Session(),
        query=CompletionEngineeringPlanQuery(
            access=PermissionAdmission(uuid4(), "system_user", frozenset()),
            plan_id=uuid4(),
            project_id=uuid4(),
            task_id=uuid4(),
            expected_revision_id=uuid4(),
            expected_approval_id=uuid4(),
            expected_digest="a" * 64,
        ),
    )

    def active(db: Session, *, owner: str) -> bool:
        assert db is state.db
        assert owner == "operations.field_completion"
        return state.active

    def resolve(
        db: Session,
        *,
        query: CurrentEngineeringPlanQuery,
        permission: str,
        write: bool,
        require_issuance_eligibility: bool = True,
    ) -> CurrentEngineeringPlanApproval:
        assert db is state.db
        assert query is state.query
        state.calls.append(
            ResolutionCall(permission, write, require_issuance_eligibility)
        )
        raise ResolutionReached

    monkeypatch.setattr(owner, "owner_command_active", active)
    monkeypatch.setattr(owner, "_resolve_current_locked", resolve)
    return state


def test_completion_review_uses_fresh_write_authority(boundary: BoundaryRig) -> None:
    with pytest.raises(ResolutionReached):
        owner.stage_current_for_completion(boundary.db, query=boundary.query)
    assert boundary.calls == [
        ResolutionCall("operations:completion-review:write", True, False)
    ]


def test_completion_participant_requires_registered_owner(
    boundary: BoundaryRig,
) -> None:
    boundary.active = False
    with pytest.raises(owner.EngineeringPlanError, match="invalid request"):
        owner.stage_current_for_completion(boundary.db, query=boundary.query)
    assert boundary.calls == []


def test_completion_current_read_has_narrow_read_authority(
    boundary: BoundaryRig,
) -> None:
    boundary.active = False
    with pytest.raises(ResolutionReached):
        owner.resolve_current_for_completion(boundary.db, query=boundary.query)
    assert boundary.calls == [
        ResolutionCall("operations:completion-review:read", False, False)
    ]


def test_ordinary_resolution_preserves_issuance_eligibility(
    boundary: BoundaryRig,
) -> None:
    with pytest.raises(ResolutionReached):
        owner.resolve_current(boundary.db, query=boundary.query)
    assert boundary.calls[0].issuance_eligible_required is True


def test_completion_decision_preview_has_write_capability_without_executor(
    boundary: BoundaryRig,
) -> None:
    boundary.active = False
    with pytest.raises(ResolutionReached):
        owner.resolve_current_for_completion_review(boundary.db, query=boundary.query)
    assert boundary.calls == [
        ResolutionCall("operations:completion-review:write", True, False)
    ]


def test_support_batch_rejects_foreign_outer_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Session()

    def inactive(session: Session, *, owner: str) -> bool:
        assert session is db and owner == "support.ticket_lifecycle"
        return False

    monkeypatch.setattr(owner, "owner_command_active", inactive)
    with pytest.raises(owner.EngineeringPlanError):
        owner.stage_current_batch_for_support_completion(
            db, query=SupportCompletionEngineeringBatchQuery(plans=())
        )
    db.close()
