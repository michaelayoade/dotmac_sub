import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ServiceOrderStatus(enum.Enum):
    draft = "draft"
    submitted = "submitted"
    scheduled = "scheduled"
    provisioning = "provisioning"
    active = "active"
    canceled = "canceled"
    failed = "failed"


class AppointmentStatus(enum.Enum):
    proposed = "proposed"
    confirmed = "confirmed"
    completed = "completed"
    no_show = "no_show"
    canceled = "canceled"


class TaskStatus(enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    blocked = "blocked"
    completed = "completed"
    failed = "failed"


class ServiceState(enum.Enum):
    pending = "pending"
    installing = "installing"
    provisioning = "provisioning"
    active = "active"
    suspended = "suspended"
    canceled = "canceled"
    disconnected = "disconnected"


class ProvisioningVendor(enum.Enum):
    mikrotik = "mikrotik"
    huawei = "huawei"
    zte = "zte"
    nokia = "nokia"
    genieacs = "genieacs"
    other = "other"


class ProvisioningStepType(enum.Enum):
    assign_ont = "assign_ont"
    push_config = "push_config"
    confirm_up = "confirm_up"


class ProvisioningRunStatus(enum.Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"


class ServiceOrder(Base):
    __tablename__ = "service_orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id")
    )
    status: Mapped[ServiceOrderStatus] = mapped_column(
        Enum(ServiceOrderStatus), default=ServiceOrderStatus.draft
    )
    order_type: Mapped[str | None] = mapped_column(String(60))  # new_install, upgrade, downgrade, disconnect
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    subscriber = relationship("Subscriber", back_populates="service_orders")
    subscription = relationship("Subscription", back_populates="service_orders")
    appointments = relationship("InstallAppointment", back_populates="service_order")
    tasks = relationship("ProvisioningTask", back_populates="service_order")
    state_transitions = relationship(
        "ServiceStateTransition", back_populates="service_order"
    )
    provisioning_runs = relationship("ProvisioningRun", back_populates="service_order")


class InstallAppointment(Base):
    __tablename__ = "install_appointments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    service_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_orders.id"), nullable=False
    )
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    technician: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus), default=AppointmentStatus.proposed
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_self_install: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    service_order = relationship("ServiceOrder", back_populates="appointments")


class ProvisioningTask(Base):
    __tablename__ = "provisioning_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    service_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_orders.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="provisioning_taskstatus"), default=TaskStatus.pending
    )
    assigned_to: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    service_order = relationship("ServiceOrder", back_populates="tasks")


class ServiceStateTransition(Base):
    __tablename__ = "service_state_transitions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    service_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_orders.id"), nullable=False
    )
    from_state: Mapped[ServiceState | None] = mapped_column(Enum(ServiceState))
    to_state: Mapped[ServiceState] = mapped_column(Enum(ServiceState), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200))
    changed_by: Mapped[str | None] = mapped_column(String(120))
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    service_order = relationship("ServiceOrder", back_populates="state_transitions")


class ProvisioningWorkflow(Base):
    __tablename__ = "provisioning_workflows"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    vendor: Mapped[ProvisioningVendor] = mapped_column(
        Enum(ProvisioningVendor), default=ProvisioningVendor.other
    )
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    steps = relationship("ProvisioningStep", back_populates="workflow")
    runs = relationship("ProvisioningRun", back_populates="workflow")


class ProvisioningStep(Base):
    __tablename__ = "provisioning_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provisioning_workflows.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    step_type: Mapped[ProvisioningStepType] = mapped_column(
        Enum(ProvisioningStepType), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    config: Mapped[dict | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    workflow = relationship("ProvisioningWorkflow", back_populates="steps")


class ProvisioningRun(Base):
    __tablename__ = "provisioning_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provisioning_workflows.id"), nullable=False
    )
    service_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_orders.id")
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id")
    )
    status: Mapped[ProvisioningRunStatus] = mapped_column(
        Enum(ProvisioningRunStatus), default=ProvisioningRunStatus.pending
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_payload: Mapped[dict | None] = mapped_column(JSON)
    output_payload: Mapped[dict | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    workflow = relationship("ProvisioningWorkflow", back_populates="runs")
    service_order = relationship("ServiceOrder", back_populates="provisioning_runs")
    subscription = relationship("Subscription")
