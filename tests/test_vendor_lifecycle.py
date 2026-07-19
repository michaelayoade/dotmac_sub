"""Vendor work lifecycle (start/complete) is owned by Sub, not the template.

The awarded vendor may move an approved project to in_progress and an
in_progress project to completed. Authority is enforced against the assigned
vendor and the current status in ``vendor_portal_operations``; the detail
template renders the buttons purely from the start/complete ``Action`` contracts
the same serializer exposes (allowed/reason owned by the backend).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.project import Project
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectStatus,
    Vendor,
)
from app.services.ui_contracts import Action
from app.services.vendor_portal_operations import (
    _serialize_project,
    vendor_portal_operations,
)


def _project_row(status: str, assigned_vendor_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id="p1",
        project_id="prj1",
        project=SimpleNamespace(code="PRJ-1", name="Fiber install"),
        subscriber_id=None,
        assigned_vendor_id=assigned_vendor_id,
        assignment_type=None,
        status=status,
        bidding_open_at=None,
        bidding_close_at=None,
        approved_quote_id=None,
        erp_purchase_order_id=None,
        notes=None,
        created_at=None,
        updated_at=None,
    )


def _start(project: dict) -> Action:
    return project["actions"]["start"]


def _complete(project: dict) -> Action:
    return project["actions"]["complete"]


def test_can_start_only_when_approved_and_owned_by_viewer():
    approved = _project_row(InstallationProjectStatus.approved.value, "v1")
    assert _start(_serialize_project(approved, viewer_vendor_id="v1")).allowed is True
    # Not the viewing vendor's project — blocked with a reason.
    blocked = _start(_serialize_project(approved, viewer_vendor_id="v2"))
    assert blocked.allowed is False
    assert blocked.reason
    # No viewer context (e.g. an admin listing) never offers the action.
    assert _start(_serialize_project(approved)).allowed is False
    # Wrong source status.
    quoted = _project_row(InstallationProjectStatus.quoted.value, "v1")
    assert _start(_serialize_project(quoted, viewer_vendor_id="v1")).allowed is False


def test_can_complete_only_when_in_progress_and_owned_by_viewer():
    in_progress = _project_row(InstallationProjectStatus.in_progress.value, "v1")
    assert (
        _complete(_serialize_project(in_progress, viewer_vendor_id="v1")).allowed
        is True
    )
    assert (
        _complete(_serialize_project(in_progress, viewer_vendor_id="v2")).allowed
        is False
    )
    approved = _project_row(InstallationProjectStatus.approved.value, "v1")
    assert (
        _complete(_serialize_project(approved, viewer_vendor_id="v1")).allowed is False
    )


def _install(db, status: str) -> tuple[InstallationProject, Vendor]:
    project = Project(name="Lifecycle fiber install")
    vendor = Vendor(
        name="Native Vendor", code=f"NV-{uuid4().hex[:6]}", erp_id=str(uuid4())
    )
    db.add_all([project, vendor])
    db.flush()
    install = InstallationProject(
        project_id=project.id, assigned_vendor_id=vendor.id, status=status
    )
    db.add(install)
    db.commit()
    return install, vendor


def test_start_project_moves_approved_to_in_progress(db_session):
    install, vendor = _install(db_session, InstallationProjectStatus.approved.value)
    result = vendor_portal_operations.start_project(
        db_session, str(install.id), vendor_id=str(vendor.id)
    )
    assert result["status"] == InstallationProjectStatus.in_progress.value
    # Fresh eligibility reflects the new state: complete now, not start.
    assert _start(result).allowed is False
    assert _complete(result).allowed is True
    db_session.refresh(install)
    assert install.status == InstallationProjectStatus.in_progress.value


def test_complete_project_moves_in_progress_to_completed(db_session):
    install, vendor = _install(db_session, InstallationProjectStatus.in_progress.value)
    result = vendor_portal_operations.complete_project(
        db_session, str(install.id), vendor_id=str(vendor.id)
    )
    assert result["status"] == InstallationProjectStatus.completed.value
    assert _complete(result).allowed is False


def test_start_rejects_a_vendor_who_does_not_own_the_project(db_session):
    install, _vendor = _install(db_session, InstallationProjectStatus.approved.value)
    with pytest.raises(HTTPException) as exc:
        vendor_portal_operations.start_project(
            db_session, str(install.id), vendor_id=str(uuid4())
        )
    assert exc.value.status_code == 403


def test_start_rejects_a_project_that_is_not_approved(db_session):
    install, vendor = _install(db_session, InstallationProjectStatus.quoted.value)
    with pytest.raises(HTTPException) as exc:
        vendor_portal_operations.start_project(
            db_session, str(install.id), vendor_id=str(vendor.id)
        )
    assert exc.value.status_code == 409


def test_complete_rejects_a_project_that_is_not_in_progress(db_session):
    install, vendor = _install(db_session, InstallationProjectStatus.approved.value)
    with pytest.raises(HTTPException) as exc:
        vendor_portal_operations.complete_project(
            db_session, str(install.id), vendor_id=str(vendor.id)
        )
    assert exc.value.status_code == 409
