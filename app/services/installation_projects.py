"""Transaction-neutral owner for installation scope creation."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.vendor_routes import InstallationProject, InstallationProjectStatus
from app.services.events import EventType, emit_event


class InstallationScopeError(ValueError):
    def __init__(self, code: str, message: str, *, kind: str = "conflict") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


def ensure_for_project(
    db: Session,
    *,
    project_id: UUID,
    subscriber_id: UUID,
    actor_id: str,
    created_by_person_id: UUID | None = None,
) -> InstallationProject:
    actor = str(actor_id or "").strip()
    if not actor:
        raise InstallationScopeError(
            "actor_required", "Installation-scope actor is required", kind="invalid"
        )
    project = db.get(Project, project_id)
    if project is None:
        raise InstallationScopeError(
            "project_not_found", "Project not found", kind="not_found"
        )
    if project.subscriber_id != subscriber_id:
        raise InstallationScopeError(
            "subscriber_mismatch", "Project and installation Subscriber differ"
        )
    existing = db.scalars(
        select(InstallationProject).where(InstallationProject.project_id == project_id)
    ).one_or_none()
    if existing is not None:
        if existing.subscriber_id != subscriber_id:
            raise InstallationScopeError(
                "existing_scope_mismatch",
                "Installation project conflicts with the Project Subscriber",
            )
        return existing
    installation = InstallationProject(
        project_id=project.id,
        subscriber_id=subscriber_id,
        status=InstallationProjectStatus.draft.value,
        created_by_person_id=created_by_person_id,
        notes="Created by sales.fulfillment from the accepted order scope",
    )
    db.add(installation)
    db.flush()
    emit_event(
        db,
        EventType.installation_scope_created,
        {
            "installation_project_id": str(installation.id),
            "project_id": str(project.id),
            "subscriber_id": str(subscriber_id),
        },
        actor=actor,
        subscriber_id=subscriber_id,
    )
    return installation
