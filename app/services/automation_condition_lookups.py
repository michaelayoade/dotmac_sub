"""Typed, bounded lookup data for Automation Center condition authoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.elements import ColumnElement

from app.db import Base
from app.models.customer_experience import CustomerExperienceHandoff
from app.models.field_material import FieldInventoryWarehouse, FieldMaterialRequest
from app.models.project import Project, ProjectTask
from app.models.sales import Lead, Pipeline, Quote, SalesOrder
from app.models.service_team import ServiceTeam
from app.models.support import Ticket
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services import customer_search, support_ticket_settings
from app.services.automation_contracts import AutomationLookupKey

MAX_LOOKUP_LIMIT = 20
_SqlExpression = ColumnElement[Any] | InstrumentedAttribute[Any]


@dataclass(frozen=True, slots=True)
class AutomationLookupOption:
    ref: str
    label: str


class _LookupRow(Protocol):
    id: UUID
    __dict__: dict[str, object]


def _limit(value: int) -> int:
    return max(1, min(int(value or MAX_LOOKUP_LIMIT), MAX_LOOKUP_LIMIT))


def _term(value: str) -> str:
    escaped = (
        value.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def _uuid(value: str) -> UUID | None:
    try:
        return UUID(value.strip())
    except (AttributeError, ValueError):
        return None


def _rows(
    db: Session,
    model: type[Base],
    id_column: _SqlExpression,
    label_columns: tuple[_SqlExpression, ...],
    q: str,
    limit: int,
    *,
    active_column: _SqlExpression | None = None,
) -> tuple[AutomationLookupOption, ...]:
    statement = select(model)
    if active_column is not None:
        statement = statement.where(active_column.is_(True))
    if q.strip():
        like = _term(q)
        filters: list[ColumnElement[bool]] = [
            column.ilike(like) for column in label_columns
        ]
        parsed = _uuid(q)
        if parsed is not None:
            filters.append(id_column == parsed)
        statement = statement.where(or_(*filters))
    result = db.scalars(statement.order_by(id_column).limit(limit)).all()
    return tuple(
        AutomationLookupOption(
            ref=str(cast(_LookupRow, row).id),
            label=_label(cast(_LookupRow, row), label_columns),
        )
        for row in result
    )


def _label(row: _LookupRow, columns: tuple[_SqlExpression, ...]) -> str:
    values = [
        str(row.__dict__.get(column.key or "", "") or "").strip() for column in columns
    ]
    return next((value for value in values if value), str(row.id))


def _distinct_values(
    db: Session,
    model: type[Base],
    column: _SqlExpression,
    q: str,
    limit: int,
    *,
    active_column: _SqlExpression | None = None,
) -> tuple[AutomationLookupOption, ...]:
    statement = select(column).where(column.is_not(None)).distinct().order_by(column)
    if active_column is not None:
        statement = statement.where(active_column.is_(True))
    if q.strip():
        statement = statement.where(column.ilike(_term(q)))
    values = db.scalars(statement.limit(limit)).all()
    return tuple(
        AutomationLookupOption(ref=str(value), label=str(value)) for value in values
    )


def lookup_options(
    db: Session, key: AutomationLookupKey, *, q: str = "", limit: int = MAX_LOOKUP_LIMIT
) -> tuple[AutomationLookupOption, ...]:
    """Return only current, bounded values for a declared lookup key."""

    limit = _limit(limit)
    if key is AutomationLookupKey.customer:
        page = customer_search.query_customers(
            db, customer_search.CustomerSearchQuery(term=q, limit=limit)
        )
        return tuple(
            AutomationLookupOption(ref=str(item.id), label=item.label)
            for item in page.items
        )
    if key is AutomationLookupKey.service_team:
        return _rows(
            db,
            ServiceTeam,
            ServiceTeam.id,
            (ServiceTeam.name,),
            q,
            limit,
            active_column=ServiceTeam.is_active,
        )
    if key is AutomationLookupKey.project:
        return _rows(
            db,
            Project,
            Project.id,
            (Project.name, Project.code, Project.number),
            q,
            limit,
            active_column=Project.is_active,
        )
    if key is AutomationLookupKey.project_task:
        return _rows(
            db,
            ProjectTask,
            ProjectTask.id,
            (ProjectTask.title, ProjectTask.number),
            q,
            limit,
        )
    if key is AutomationLookupKey.work_order:
        return _rows(
            db,
            WorkOrder,
            WorkOrder.id,
            (WorkOrder.title, WorkOrder.public_id),
            q,
            limit,
        )
    if key is AutomationLookupKey.material_request:
        return _rows(
            db,
            FieldMaterialRequest,
            FieldMaterialRequest.id,
            (FieldMaterialRequest.client_ref,),
            q,
            limit,
        )
    if key is AutomationLookupKey.pipeline:
        return _rows(
            db,
            Pipeline,
            Pipeline.id,
            (Pipeline.name,),
            q,
            limit,
            active_column=Pipeline.is_active,
        )
    if key is AutomationLookupKey.lead:
        return _rows(db, Lead, Lead.id, (Lead.title,), q, limit)
    if key is AutomationLookupKey.quote:
        return _rows(
            db, Quote, Quote.id, (Quote.project_type, Quote.currency), q, limit
        )
    if key is AutomationLookupKey.sales_order:
        return _rows(
            db,
            SalesOrder,
            SalesOrder.id,
            (SalesOrder.order_number,),
            q,
            limit,
            active_column=SalesOrder.is_active,
        )
    if key is AutomationLookupKey.system_user:
        return _rows(
            db,
            SystemUser,
            SystemUser.id,
            (
                SystemUser.display_name,
                SystemUser.first_name,
                SystemUser.last_name,
                SystemUser.email,
            ),
            q,
            limit,
            active_column=SystemUser.is_active,
        )
    if key is AutomationLookupKey.cx_handoff:
        return _rows(
            db,
            CustomerExperienceHandoff,
            CustomerExperienceHandoff.id,
            (CustomerExperienceHandoff.status,),
            q,
            limit,
        )
    if key is AutomationLookupKey.ticket_type:
        return tuple(
            AutomationLookupOption(ref=value, label=value)
            for value in support_ticket_settings.list_ticket_type_options(db)
            if not q.strip() or q.casefold() in value.casefold()
        )[:limit]
    if key is AutomationLookupKey.project_type:
        return _distinct_values(
            db, Project, Project.project_type, q, limit, active_column=Project.is_active
        )
    if key is AutomationLookupKey.project_name:
        return _distinct_values(
            db, Project, Project.name, q, limit, active_column=Project.is_active
        )
    if key is AutomationLookupKey.region:
        project_values = _distinct_values(
            db, Project, Project.region, q, limit, active_column=Project.is_active
        )
        ticket_values = _distinct_values(
            db, Ticket, Ticket.region, q, limit, active_column=Ticket.is_active
        )
        return tuple(
            sorted(
                {item.ref: item for item in (*project_values, *ticket_values)}.values(),
                key=lambda item: item.label.casefold(),
            )[:limit]
        )
    if key is AutomationLookupKey.lead_source:
        return _distinct_values(db, Lead, Lead.lead_source, q, limit)
    if key is AutomationLookupKey.currency:
        quote_values = _distinct_values(db, Quote, Quote.currency, q, limit)
        order_values = _distinct_values(db, SalesOrder, SalesOrder.currency, q, limit)
        return tuple(
            sorted(
                {item.ref: item for item in (*quote_values, *order_values)}.values(),
                key=lambda item: item.label.casefold(),
            )[:limit]
        )
    if key is AutomationLookupKey.warehouse:
        statement = select(
            FieldInventoryWarehouse.code, FieldInventoryWarehouse.name
        ).where(FieldInventoryWarehouse.is_active.is_(True))
        if q.strip():
            like = _term(q)
            statement = statement.where(
                or_(
                    FieldInventoryWarehouse.code.ilike(like),
                    FieldInventoryWarehouse.name.ilike(like),
                )
            )
        return tuple(
            AutomationLookupOption(ref=str(code), label=str(name))
            for code, name in db.execute(
                statement.order_by(FieldInventoryWarehouse.name).limit(limit)
            ).all()
        )
    if key is AutomationLookupKey.support_system:
        return _distinct_values(
            db, FieldMaterialRequest, FieldMaterialRequest.support_system, q, limit
        )
    if key is AutomationLookupKey.support_status:
        return _distinct_values(
            db, FieldMaterialRequest, FieldMaterialRequest.support_status, q, limit
        )
    return ()


def value_exists(db: Session, key: AutomationLookupKey, value: object) -> bool:
    """Validate a selected lookup ref against the same canonical provider."""

    raw = str(value or "").strip()
    if not raw:
        return False
    return any(item.ref == raw for item in lookup_options(db, key, q=raw, limit=1))


__all__ = ["AutomationLookupOption", "lookup_options", "value_exists"]
