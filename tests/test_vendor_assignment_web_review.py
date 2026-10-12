"""Browser adapters preserve explicit member selection and stale confirmation evidence."""

from datetime import UTC, datetime
from uuid import uuid4

from starlette.requests import Request

from app.services.permission_admission import PermissionAdmission
from app.services.work_order_assignment_contracts import (
    VendorAssignmentCandidate,
    VendorAssignmentCandidates,
    VendorAssignmentTarget,
)
from app.services.work_order_errors import WorkOrderCommandError
from app.web.admin import dispatch_work_orders as routes


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _capture(monkeypatch):
    monkeypatch.setattr(routes, "_ctx", lambda request, db: {"request": request})
    monkeypatch.setattr(
        routes.templates,
        "TemplateResponse",
        lambda name, context, **kwargs: (name, context, kwargs),
    )


def test_vendor_company_selection_opens_person_review_without_assigning(monkeypatch):
    _capture(monkeypatch)
    vendor, member, generation, command = (uuid4() for _ in range(4))
    revision = datetime.now(UTC)
    candidates = VendorAssignmentCandidates(
        work_order_id="demo-work-order",
        revision=revision,
        assignment_generation=generation,
        candidates=(VendorAssignmentCandidate(member, uuid4(), "Demo installer"),),
        vendor_name="Demo vendor",
    )
    monkeypatch.setattr(
        routes.work_orders_service,
        "vendor_candidates_from_form",
        lambda db, **kw: candidates,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Selection must not execute an assignment")

    monkeypatch.setattr(
        routes.work_orders_service, "queue_assignment_from_form", forbidden
    )
    response = routes.queue_dispatch_work_order(
        request=_request(),
        work_order_id="demo-work-order",
        target_selection=f"vendor:{vendor}",
        reason="Attend outage",
        vendor_user_id=None,
        expected_revision=revision,
        expected_assignment_generation=generation,
        command_id=command,
        reload_current=False,
        access=PermissionAdmission(
            uuid4(), "system_user", frozenset({"operations:dispatch:assign"})
        ),
        db=None,
    )
    name, context, _ = response
    assert name.endswith("work_order_vendor_selection.html")
    assert context["candidates"] is candidates
    assert context["command_id"] == command
    assert context["selection"].reason == "Attend outage"


def test_stale_confirmation_preserves_original_member_revision_and_command(monkeypatch):
    _capture(monkeypatch)
    vendor, member, generation, command, principal = (uuid4() for _ in range(5))
    revision = datetime.now(UTC)
    access = PermissionAdmission(
        principal, "system_user", frozenset({"operations:dispatch:assign"})
    )

    def reject(db, *, command, context):
        assert command.target == VendorAssignmentTarget(vendor, member)
        assert command.access == access
        assert context.actor == str(principal)
        raise WorkOrderCommandError(
            "stale_revision", "Assignment changed; review again"
        )

    monkeypatch.setattr(
        routes.work_orders_service, "queue_assignment_from_form", reject
    )
    response = routes.confirm_dispatch_work_order_assignment(
        request=_request(),
        work_order_id="demo-work-order",
        target_selection=f"vendor:{vendor}",
        expected_revision=revision,
        command_id=command,
        reason="Attend outage",
        vendor_user_id=member,
        expected_assignment_generation=generation,
        scheduled_start=None,
        scheduled_end=None,
        access=access,
        db=None,
    )
    name, context, kwargs = response
    assert name.endswith("work_order_assignment_problem.html")
    assert kwargs["status_code"] == 409
    assert context["command_id"] == command
    selection = context["selection"]
    assert (
        selection.vendor_user_id,
        selection.expected_revision,
        selection.expected_assignment_generation,
    ) == (member, revision, generation)
    assert selection.reason == "Attend outage"
