"""Cross-vertical work-lifecycle links ported from the CRM (Phase 3 §1.1).

CRM shape (``dotmac_crm/app/models/work_lifecycle.py`` WorkLink) carried
verbatim with the sub conventions applied:

* PG enums (``WorkEntityType``/``WorkLinkType``) become String columns +
  app-level enums.
* ``created_by_person_id`` is a staff person FK in CRM — FK dropped, UUID
  carried verbatim (§1.8); display via the Phase 1 staff map.
* Rows whose source/target types ∈ {project, project_task, lead,
  sales_order} migrate in the Phase 3 backfill; ``work_order``-typed rows
  stay in CRM until the Phase 2 work-order flip (§1.1, §3.5 step 7). The
  source/target ids are intentionally FK-less polymorphic UUIDs, exactly as
  in CRM.

Only ``work_links`` ports — CRM ``work_outcomes`` stays behind (it keys on
``work_orders``, Phase 2 territory).
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WorkEntityType(enum.Enum):
    ticket = "ticket"
    project = "project"
    project_task = "project_task"
    work_order = "work_order"
    lead = "lead"
    sales_order = "sales_order"
    subscriber = "subscriber"
    internal = "internal"


class WorkLinkType(enum.Enum):
    originated = "originated"
    fulfills = "fulfills"
    blocks = "blocks"
    related = "related"
    resulted_in = "resulted_in"


class WorkLink(Base):
    __tablename__ = "work_links"
    __table_args__ = (
        UniqueConstraint(
            "source_type",
            "source_id",
            "target_type",
            "target_id",
            "link_type",
            name="uq_work_links_source_target_link_type",
        ),
        Index("ix_work_links_source", "source_type", "source_id"),
        Index("ix_work_links_target", "target_type", "target_id"),
        Index("ix_work_links_contract", "contract_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    link_type: Mapped[str] = mapped_column(String(40), nullable=False)
    contract_name: Mapped[str | None] = mapped_column(String(120))
    # Staff person UUID — no FK (§1.8).
    created_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
