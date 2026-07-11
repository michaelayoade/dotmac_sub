"""Native projects vertical ported from the CRM (Phase 3 §1.1/§1.2).

CRM shapes (``dotmac_crm/app/models/projects.py``) carried verbatim with the
sub conventions applied:

* PG enums become String columns + app-level enums (Phase 1 convention). The
  CRM enum classes ``TaskStatus``/``TaskPriority``/``TaskDependencyType`` are
  exported here as ``ProjectTaskStatus``/``ProjectTaskPriority``/
  ``ProjectTaskDependencyType`` to avoid clashing with sub's provisioning
  ``TaskStatus`` — the *values* are identical to CRM (§1.7).
* Staff ``people.id`` FKs are dropped and carried as plain UUIDs (§1.8):
  the five ``projects.*_person_id`` roles, ``project_tasks.{assigned_to,
  created_by}_person_id``, both comment tables' ``author_person_id``, and
  ``project_task_assignees.person_id`` (still half of the composite PK).
  Display resolves via the Phase 1 staff map.
* ``projects.subscriber_id`` re-points at sub ``subscribers.id`` (link key 1),
  ``projects.lead_id`` is a real FK to the native ``leads`` table, and
  ``project_tasks.ticket_id`` points at sub ``support_tickets.id`` (Phase 1
  result; the backfill applies the Phase 1 re-key map).
* ``project_tasks.work_order_id`` stays a plain UUID until the Phase 2
  work-order flip adds the FK (§1.2 — CRM work-order UUIDs are the join key
  either way via ``work_order_mirror.crm_work_order_id``).
* Both comment tables gain a ``metadata`` JSON column for import provenance
  (Phase 1 §1.4 pattern) — CRM has no metadata column there.

CRM UUID PKs are kept verbatim by the import (§3.4). This module coexists
with the ``project_mirror`` tables until the Phase 3 contract PR (§3.3).
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ProjectStatus(enum.Enum):
    open = "open"
    planned = "planned"
    active = "active"
    on_hold = "on_hold"
    completed = "completed"
    canceled = "canceled"


class ProjectPriority(enum.Enum):
    lower = "lower"
    low = "low"
    medium = "medium"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class ProjectType(enum.Enum):
    cable_rerun = "cable_rerun"
    fiber_optics_relocation = "fiber_optics_relocation"
    air_fiber_relocation = "air_fiber_relocation"
    fiber_optics_installation = "fiber_optics_installation"
    air_fiber_installation = "air_fiber_installation"
    cross_connect = "cross_connect"


class ProjectTaskStatus(enum.Enum):
    """CRM ``TaskStatus`` (renamed to avoid the provisioning TaskStatus)."""

    backlog = "backlog"
    todo = "todo"
    in_progress = "in_progress"
    blocked = "blocked"
    done = "done"
    canceled = "canceled"


class ProjectTaskPriority(enum.Enum):
    """CRM ``TaskPriority`` (same vocabulary as ProjectPriority)."""

    lower = "lower"
    low = "low"
    medium = "medium"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class ProjectTaskDependencyType(enum.Enum):
    """CRM ``TaskDependencyType``."""

    finish_to_start = "finish_to_start"
    start_to_start = "start_to_start"
    finish_to_finish = "finish_to_finish"
    start_to_finish = "start_to_finish"


class ProjectTemplate(Base):
    __tablename__ = "project_templates"
    __table_args__ = (
        UniqueConstraint("project_type", name="uq_project_templates_project_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    project_type: Mapped[str | None] = mapped_column(String(60))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    tasks = relationship("ProjectTemplateTask", back_populates="template")


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        # Native /me/projects (+ reseller subtree) subscriber scan — partial on
        # is_active (see migration 251). The functional index backs the H1
        # quote→project lookup; its expression matches SQLAlchemy's
        # metadata_['quote_id'].as_string() (CAST(... AS VARCHAR)) so the
        # planner uses it. postgresql_where is ignored on sqlite create_all.
        Index(
            "ix_projects_subscriber_id",
            "subscriber_id",
            postgresql_where=text("is_active"),
        ),
        Index(
            "ix_projects_metadata_quote_id",
            text("CAST((metadata ->> 'quote_id') AS VARCHAR)"),
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(80))
    # Kept non-unique per §1.2 (CRM shape).
    number: Mapped[str | None] = mapped_column(String(40))
    erpnext_id: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    customer_address: Mapped[str | None] = mapped_column(Text)
    project_type: Mapped[str | None] = mapped_column(String(60))
    project_template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_templates.id")
    )
    status: Mapped[str] = mapped_column(
        String(40), default=ProjectStatus.open.value, nullable=False
    )
    priority: Mapped[str] = mapped_column(
        String(40), default=ProjectPriority.normal.value, nullable=False
    )
    # Customer party — sub subscriber (re-pointed via link key 1, §1.2).
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id")
    )
    # Staff person UUIDs carried verbatim, no FK (§1.8; staff map for display).
    # assistant_manager_person_id ≡ "Site Project Coordinator" (§1.2).
    created_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    owner_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    manager_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    project_manager_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    assistant_manager_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    # Real FK — service_teams ported in Phase 1 (§1.2).
    service_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id")
    )
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    region: Mapped[str | None] = mapped_column(String(80))
    tags: Mapped[list | None] = mapped_column(JSON)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber", foreign_keys=[subscriber_id])
    lead = relationship("Lead", foreign_keys=[lead_id])
    service_team = relationship("ServiceTeam", foreign_keys=[service_team_id])
    project_template = relationship("ProjectTemplate")
    tasks = relationship("ProjectTask", back_populates="project")
    comments = relationship("ProjectComment", back_populates="project")


class ProjectTask(Base):
    __tablename__ = "project_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_tasks.id")
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    number: Mapped[str | None] = mapped_column(String(40))
    erpnext_id: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    template_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_template_tasks.id")
    )
    status: Mapped[str] = mapped_column(
        String(40), default=ProjectTaskStatus.todo.value, nullable=False
    )
    priority: Mapped[str] = mapped_column(
        String(40), default=ProjectTaskPriority.normal.value, nullable=False
    )
    # Staff person UUIDs — no FK (§1.8).
    assigned_to_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Phase 1 native tickets table (backfill applies the Phase 1 re-key map).
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id")
    )
    # CRM work-order UUID carried verbatim; becomes a real FK at the Phase 2
    # work-order flip (§1.2). Joins via work_order_mirror.crm_work_order_id
    # until then.
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effort_hours: Mapped[int | None] = mapped_column(Integer)
    tags: Mapped[list | None] = mapped_column(JSON)
    # Preserves the CRM fiber-stage keys: fiber_stage_key, fiber_stage_title,
    # fiber_sla_managed, sla_breached, sla_breached_at (§1.2).
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    project = relationship("Project", back_populates="tasks")
    parent_task = relationship("ProjectTask", remote_side=[id])
    template_task = relationship("ProjectTemplateTask")
    ticket = relationship("Ticket", foreign_keys=[ticket_id])
    comments = relationship("ProjectTaskComment", back_populates="task")
    assignees = relationship(
        "ProjectTaskAssignee",
        back_populates="task",
        cascade="all, delete-orphan",
    )

    @property
    def assigned_to_person_ids(self) -> list[uuid.UUID]:
        if self.assignees:
            return [assignee.person_id for assignee in self.assignees]
        if self.assigned_to_person_id:
            return [self.assigned_to_person_id]
        return []


class ProjectTaskAssignee(Base):
    """Task↔staff assignment fact.

    ``person_id`` is a CRM staff-person UUID and half of the composite PK
    (§1.8 "the nasty one") — the FK is dropped but the UUID stays PK material.
    Names resolve via the Phase 1 staff map; post-flip assignments use sub
    principals.
    """

    __tablename__ = "project_task_assignees"

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    task = relationship("ProjectTask", back_populates="assignees")


class ProjectTemplateTask(Base):
    __tablename__ = "project_template_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_templates.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(40))
    priority: Mapped[str | None] = mapped_column(String(40))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    effort_hours: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    template = relationship("ProjectTemplate", back_populates="tasks")


class ProjectTemplateTaskDependency(Base):
    # Table name is singular in CRM — kept verbatim (§1.1).
    __tablename__ = "project_template_task_dependency"
    __table_args__ = (
        UniqueConstraint(
            "template_task_id",
            "depends_on_template_task_id",
            name="uq_project_template_task_dependency",
        ),
        CheckConstraint(
            "template_task_id <> depends_on_template_task_id",
            name="ck_project_template_task_dependency_no_self",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    template_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_template_tasks.id"), nullable=False
    )
    depends_on_template_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_template_tasks.id"), nullable=False
    )
    dependency_type: Mapped[str] = mapped_column(
        String(40),
        default=ProjectTaskDependencyType.finish_to_start.value,
        nullable=False,
    )
    lag_days: Mapped[int] = mapped_column(Integer, default=0)

    template_task = relationship(
        "ProjectTemplateTask",
        foreign_keys=[template_task_id],
    )
    depends_on_template_task = relationship(
        "ProjectTemplateTask",
        foreign_keys=[depends_on_template_task_id],
    )


class ProjectTaskDependency(Base):
    __tablename__ = "project_task_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "depends_on_task_id",
            name="uq_project_task_dependencies",
        ),
        CheckConstraint(
            "task_id <> depends_on_task_id",
            name="ck_project_task_dependencies_no_self",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_tasks.id"), nullable=False
    )
    depends_on_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_tasks.id"), nullable=False
    )
    dependency_type: Mapped[str] = mapped_column(
        String(40),
        default=ProjectTaskDependencyType.finish_to_start.value,
        nullable=False,
    )
    lag_days: Mapped[int] = mapped_column(Integer, default=0)

    task = relationship("ProjectTask", foreign_keys=[task_id])
    depends_on_task = relationship("ProjectTask", foreign_keys=[depends_on_task_id])


class ProjectTaskComment(Base):
    __tablename__ = "project_task_comments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_tasks.id"), nullable=False
    )
    # Staff person UUID — no FK (§1.8).
    author_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    attachments: Mapped[list | None] = mapped_column(JSON)
    # Not in CRM — added for import provenance (§1.2, Phase 1 §1.4 pattern).
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    task = relationship("ProjectTask", back_populates="comments")


class ProjectComment(Base):
    __tablename__ = "project_comments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )
    # Staff person UUID — no FK (§1.8).
    author_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    attachments: Mapped[list | None] = mapped_column(JSON)
    # Not in CRM — added for import provenance (§1.2, Phase 1 §1.4 pattern).
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    project = relationship("Project", back_populates="comments")
