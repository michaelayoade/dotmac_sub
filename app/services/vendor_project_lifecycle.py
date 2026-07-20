"""Canonical participant for vendor installation-project lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectLifecycleEvent,
    InstallationProjectStatus,
)
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event

VendorProjectAction = Literal["start", "complete"]


class VendorProjectLifecycleError(DomainError):
    """Stable failures from the vendor project lifecycle owner."""


def _error(suffix: str, message: str) -> VendorProjectLifecycleError:
    return VendorProjectLifecycleError(
        code=f"operations.vendor_project_lifecycle.{suffix}",
        message=message,
    )


@dataclass(frozen=True, slots=True)
class PreviewVendorProjectLifecycle:
    project_id: str
    vendor_id: str
    action: str


@dataclass(frozen=True, slots=True)
class StageVendorProjectTransition:
    project_id: str
    vendor_id: str
    action: str
    actor_id: str
    actor_type: str


def _project(
    db: Session,
    project_id: str,
    *,
    for_update: bool,
) -> InstallationProject:
    query = db.query(InstallationProject).filter(
        InstallationProject.id == coerce_uuid(project_id)
    )
    if for_update:
        query = query.with_for_update(of=InstallationProject)
    row = query.one_or_none()
    if row is None or not row.is_active:
        raise _error("not_found", "Installation project not found.")
    return row


def _transition(action: str) -> tuple[str, str, str, str]:
    transitions = {
        "start": (
            InstallationProjectStatus.approved.value,
            InstallationProjectStatus.in_progress.value,
            "Start field work",
            "Records that the assigned vendor has begun field work",
        ),
        "complete": (
            InstallationProjectStatus.in_progress.value,
            InstallationProjectStatus.completed.value,
            "Mark field work complete",
            "Records vendor completion for Dotmac review and verification",
        ),
    }
    try:
        return transitions[action]
    except KeyError as exc:
        raise _error("unsupported_action", "Unsupported lifecycle action.") from exc


def preview_project_lifecycle(
    db: Session,
    query: PreviewVendorProjectLifecycle,
    *,
    for_update: bool = False,
) -> dict:
    """Return the owner-validated impact and stale-check state."""

    project = _project(db, query.project_id, for_update=for_update)
    if project.assigned_vendor_id != coerce_uuid(query.vendor_id):
        raise _error("not_assigned", "Project is not assigned to this vendor.")
    expected, target, title, summary = _transition(query.action)
    if project.status != expected:
        label = "approved" if query.action == "start" else "in-progress"
        verb = "started" if query.action == "start" else "completed"
        raise _error(
            "invalid_transition",
            f"Only an {label} project can be {verb}.",
        )
    native_project = project.project
    return {
        "submission_type": f"project_{query.action}",
        "project_id": str(project.id),
        "target_id": str(project.id),
        "title": title,
        "summary": summary,
        "details": [
            ("Project", getattr(native_project, "name", None) or str(project.id)),
            ("Current state", expected.replace("_", " ").title()),
            ("Result", target.replace("_", " ").title()),
            ("Affected", "1 installation project"),
        ],
        "state": {
            "project_id": str(project.id),
            "vendor_id": str(project.assigned_vendor_id),
            "from_status": project.status,
            "to_status": target,
            "updated_at": project.updated_at,
        },
    }


def stage_project_transition(
    db: Session,
    command: StageVendorProjectTransition,
) -> dict:
    """Stage one locked transition and its evidence in the caller transaction."""

    if not command.actor_id.strip() or not command.actor_type.strip():
        raise _error("actor_required", "Lifecycle transition actor is required.")
    preview = preview_project_lifecycle(
        db,
        PreviewVendorProjectLifecycle(
            project_id=command.project_id,
            vendor_id=command.vendor_id,
            action=command.action,
        ),
        for_update=True,
    )
    project = _project(db, command.project_id, for_update=True)
    previous = str(preview["state"]["from_status"])
    target = str(preview["state"]["to_status"])
    event_type = (
        EventType.vendor_project_started
        if command.action == "start"
        else EventType.vendor_project_completed
    )
    project.status = target
    domain_event = emit_event(
        db,
        event_type,
        {
            "schema_version": 1,
            "project_id": str(project.id),
            "native_project_id": str(project.project_id),
            "vendor_id": str(project.assigned_vendor_id),
            "from_status": previous,
            "to_status": target,
            "actor_type": command.actor_type,
            "actor_id": command.actor_id,
        },
        actor=command.actor_id,
        subscriber_id=project.subscriber_id,
        account_id=project.subscriber_id,
    )
    evidence = InstallationProjectLifecycleEvent(
        event_id=domain_event.event_id,
        project_id=project.id,
        vendor_id=project.assigned_vendor_id,
        event_type=domain_event.event_type.value,
        from_status=previous,
        to_status=target,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        occurred_at=domain_event.occurred_at,
    )
    db.add(evidence)
    db.flush()
    return {
        "id": project.id,
        "status": project.status,
        "lifecycle_event_id": str(evidence.id),
        "domain_event_id": str(domain_event.event_id),
        "transitioned_at": domain_event.occurred_at,
    }
