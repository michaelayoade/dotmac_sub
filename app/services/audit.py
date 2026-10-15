import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal, cast

from fastapi import HTTPException, Request, Response
from sqlalchemy import func, or_, select, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import DetachedInstanceError

from app.models.audit import AuditActorType, AuditEvent
from app.schemas.audit import AuditEventCreate, AuditEventRead
from app.services.common import apply_ordering, apply_pagination
from app.services.response import ListResponseMixin
from app.services.session_hooks import run_after_commit

logger = logging.getLogger(__name__)

AuditEventOrderField = Literal[
    "occurred_at",
    "action",
    "entity_type",
    "status_code",
]
AuditEventOrderDirection = Literal["asc", "desc"]


@dataclass(frozen=True)
class AuditR1ParityReport:
    """Aggregate-only evidence for the kernel audit R1 dual-write."""

    total_rows: int
    historical_rows_without_created_at: int
    r1_rows: int
    missing_details: int
    metadata_mismatches: int
    ip_address_mismatches: int
    user_agent_mismatches: int
    unknown_actor_types: int
    missing_required_actor_ids: int
    user_actor_rows: int
    user_actor_rows_with_party: int
    user_actor_rows_without_party: int
    automated_actor_rows_with_party: int

    @property
    def blocking_mismatches(self) -> int:
        return sum(
            (
                self.missing_details,
                self.metadata_mismatches,
                self.ip_address_mismatches,
                self.user_agent_mismatches,
                self.unknown_actor_types,
                self.missing_required_actor_ids,
                self.automated_actor_rows_with_party,
            )
        )

    @property
    def actor_party_projection_status(self) -> str:
        if not self.user_actor_rows:
            return "not_observed"
        if not self.user_actor_rows_with_party:
            return "none"
        if self.user_actor_rows_without_party:
            return "partial"
        return "complete"

    @property
    def status(self) -> str:
        if self.blocking_mismatches:
            return "drift"
        if not self.r1_rows:
            return "no_r1_rows"
        return "parity"

    def as_dict(self) -> dict[str, int | str]:
        return {
            **asdict(self),
            "blocking_mismatches": self.blocking_mismatches,
            "status": self.status,
            "actor_party_projection_status": self.actor_party_projection_status,
        }

    @classmethod
    def from_mapping(cls, row: RowMapping) -> "AuditR1ParityReport":
        return cls(
            total_rows=int(row["total_rows"]),
            historical_rows_without_created_at=int(
                row["historical_rows_without_created_at"]
            ),
            r1_rows=int(row["r1_rows"]),
            missing_details=int(row["missing_details"]),
            metadata_mismatches=int(row["metadata_mismatches"]),
            ip_address_mismatches=int(row["ip_address_mismatches"]),
            user_agent_mismatches=int(row["user_agent_mismatches"]),
            unknown_actor_types=int(row["unknown_actor_types"]),
            missing_required_actor_ids=int(row["missing_required_actor_ids"]),
            user_actor_rows=int(row["user_actor_rows"]),
            user_actor_rows_with_party=int(row["user_actor_rows_with_party"]),
            user_actor_rows_without_party=int(row["user_actor_rows_without_party"]),
            automated_actor_rows_with_party=int(row["automated_actor_rows_with_party"]),
        )


@dataclass(frozen=True, slots=True)
class AuditEventPageQuery:
    """Typed filters, ordering, and page window for canonical audit reads."""

    actor_id: str | None = None
    actor_search: str | None = None
    actor_type: AuditActorType | None = None
    action: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    entity_id_missing: bool = False
    request_id: str | None = None
    request_id_missing: bool = False
    is_success: bool | None = None
    status_code: int | None = None
    is_active: bool | None = None
    occurred_from: datetime | None = None
    occurred_before: datetime | None = None
    order_by: AuditEventOrderField = "occurred_at"
    order_dir: AuditEventOrderDirection = "desc"
    limit: int = 100
    offset: int = 0

    def __post_init__(self) -> None:
        if self.order_by not in {
            "occurred_at",
            "action",
            "entity_type",
            "status_code",
        }:
            raise ValueError("unsupported audit order field")
        if self.order_dir not in {"asc", "desc"}:
            raise ValueError("audit order direction must be asc or desc")
        if self.limit < 1:
            raise ValueError("audit query limit must be positive")
        if self.offset < 0:
            raise ValueError("audit query offset cannot be negative")
        if self.entity_id and self.entity_id_missing:
            raise ValueError("entity ID and missing-entity-ID filters conflict")
        if self.request_id and self.request_id_missing:
            raise ValueError("request ID and missing-request-ID filters conflict")
        if (
            self.occurred_from
            and self.occurred_before
            and self.occurred_from >= self.occurred_before
        ):
            raise ValueError("audit date range must end after it starts")


@dataclass(frozen=True, slots=True)
class AuditEventPage:
    """Stable audit read model with a total from the identical filter scope."""

    events: tuple[AuditEventRead, ...]
    total: int


@dataclass(frozen=True, slots=True)
class AuditEventFilterOptionsQuery:
    """Bound for distinct values offered by the audit filter UI."""

    limit: int = 500

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > 1000:
            raise ValueError("audit filter option limit must be between 1 and 1000")


@dataclass(frozen=True, slots=True)
class AuditEventFilterOptions:
    """Distinct values observed in active audit records."""

    actions: tuple[str, ...]
    entity_types: tuple[str, ...]


_AUDIT_R1_PARITY_QUERY = text(
    """
    SELECT
        count(*) AS total_rows,
        count(*) FILTER (WHERE created_at IS NULL)
            AS historical_rows_without_created_at,
        count(*) FILTER (WHERE created_at IS NOT NULL) AS r1_rows,
        count(*) FILTER (
            WHERE created_at IS NOT NULL AND details IS NULL
        ) AS missing_details,
        count(*) FILTER (
            WHERE created_at IS NOT NULL
              AND details IS NOT NULL
              AND NOT (
                  (details - ARRAY['ip_address', 'user_agent'])
                  @> (COALESCE(metadata::jsonb, '{}'::jsonb)
                      - ARRAY['ip_address', 'user_agent'])
              )
        ) AS metadata_mismatches,
        count(*) FILTER (
            WHERE created_at IS NOT NULL
              AND details IS NOT NULL
              AND (
                  (ip_address IS NOT NULL
                   AND details ->> 'ip_address' IS DISTINCT FROM ip_address)
                  OR (ip_address IS NULL AND details ? 'ip_address')
              )
        ) AS ip_address_mismatches,
        count(*) FILTER (
            WHERE created_at IS NOT NULL
              AND details IS NOT NULL
              AND (
                  (user_agent IS NOT NULL
                   AND details ->> 'user_agent' IS DISTINCT FROM user_agent)
                  OR (user_agent IS NULL AND details ? 'user_agent')
              )
        ) AS user_agent_mismatches,
        count(*) FILTER (
            WHERE created_at IS NOT NULL
              AND (
                  actor_type IS NULL
                  OR actor_type::text NOT IN ('system', 'user', 'api_key', 'service')
              )
        ) AS unknown_actor_types,
        count(*) FILTER (
            WHERE created_at IS NOT NULL
              AND actor_type::text IN ('user', 'api_key', 'service')
              AND (actor_id IS NULL OR btrim(actor_id) = '')
        ) AS missing_required_actor_ids,
        count(*) FILTER (
            WHERE created_at IS NOT NULL AND actor_type::text = 'user'
        ) AS user_actor_rows,
        count(*) FILTER (
            WHERE created_at IS NOT NULL
              AND actor_type::text = 'user'
              AND actor_party_id IS NOT NULL
        ) AS user_actor_rows_with_party,
        count(*) FILTER (
            WHERE created_at IS NOT NULL
              AND actor_type::text = 'user'
              AND actor_party_id IS NULL
        ) AS user_actor_rows_without_party,
        count(*) FILTER (
            WHERE created_at IS NOT NULL
              AND actor_type::text IN ('system', 'service')
              AND actor_party_id IS NOT NULL
        ) AS automated_actor_rows_with_party
    FROM audit_events
    """
)


class AuditEvents(ListResponseMixin):
    @staticmethod
    def _event_data(payload: AuditEventCreate) -> dict[str, object]:
        """Build the one persistence shape used by every audit writer.

        R1 keeps ``metadata`` as Sub's readable legacy surface while
        dual-writing the kernel's ``details`` JSONB. Column values win over
        same-named JSON keys so parity remains deterministic and repairable.
        """

        data = payload.model_dump()
        if payload.occurred_at is None:
            data.pop("occurred_at", None)

        details = dict(payload.metadata_ or {})
        details.update(payload.details or {})
        if payload.ip_address is not None:
            details["ip_address"] = payload.ip_address
        else:
            details.pop("ip_address", None)
        if payload.user_agent is not None:
            details["user_agent"] = payload.user_agent
        else:
            details.pop("user_agent", None)
        data["details"] = details
        return data

    @classmethod
    def _build_event(cls, payload: AuditEventCreate) -> AuditEvent:
        """The only application constructor for ``AuditEvent`` rows."""

        return cls._build_event_from_data(cls._event_data(payload))

    @staticmethod
    def _build_event_from_data(data: dict[str, object]) -> AuditEvent:
        return AuditEvent(**data)

    @staticmethod
    def parse_actor_type(value: str | None) -> AuditActorType | None:
        if value is None:
            return None
        try:
            return AuditActorType(value)
        except ValueError as exc:
            allowed = ", ".join(sorted(a.value for a in AuditActorType))
            raise HTTPException(
                status_code=400,
                detail=f"Invalid actor_type. Allowed: {allowed}",
            ) from exc

    @staticmethod
    def r1_parity(db: Session) -> AuditR1ParityReport:
        """Measure R1 drift without retrieving forensic row content."""

        row = db.execute(_AUDIT_R1_PARITY_QUERY).mappings().one()
        return AuditR1ParityReport.from_mapping(row)

    @staticmethod
    def create(db: Session, payload: AuditEventCreate) -> AuditEvent:
        event = AuditEvents._build_event(payload)
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    @staticmethod
    def record(
        db: Session,
        payload: AuditEventCreate,
        *,
        defer_until_commit: bool = True,
    ) -> AuditEvent:
        """Record an audit event. Alias for create with optional deferred commit."""
        data = AuditEvents._event_data(payload)
        if not defer_until_commit:
            event = AuditEvents._build_event_from_data(data)
            db.add(event)
            db.commit()
            db.refresh(event)
            return event

        pending_event = AuditEvents._build_event_from_data(data)

        def _persist(callback_db: Session) -> None:
            event = AuditEvents._build_event_from_data(data)
            callback_db.add(event)
            callback_db.commit()

        run_after_commit(db, _persist)
        return pending_event

    @staticmethod
    def stage(db: Session, payload: AuditEventCreate) -> AuditEvent:
        """Stage an audit row in the caller's current transaction."""
        event = AuditEvents._build_event(payload)
        db.add(event)
        return event

    @staticmethod
    def get(db: Session, event_id: str):
        event = db.get(AuditEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Audit event not found")
        return event

    @staticmethod
    def _filtered_statement(query: AuditEventPageQuery):
        """Build the one filter scope shared by audit rows and their total."""

        stmt = select(AuditEvent)
        if query.actor_id:
            stmt = stmt.where(AuditEvent.actor_id == query.actor_id)
        if query.actor_search:
            term = query.actor_search.strip()
            if term:
                stmt = stmt.where(
                    or_(
                        AuditEvent.actor_label.ilike(f"%{term}%"),
                        AuditEvent.actor_id == term,
                    )
                )
        if query.actor_type:
            stmt = stmt.where(AuditEvent.actor_type == query.actor_type)
        if query.action:
            stmt = stmt.where(AuditEvent.action == query.action)
        if query.entity_type:
            stmt = stmt.where(AuditEvent.entity_type == query.entity_type)
        if query.entity_id:
            stmt = stmt.where(AuditEvent.entity_id == query.entity_id)
        elif query.entity_id_missing:
            stmt = stmt.where(
                or_(AuditEvent.entity_id.is_(None), AuditEvent.entity_id == "")
            )
        if query.request_id:
            stmt = stmt.where(AuditEvent.request_id == query.request_id)
        elif query.request_id_missing:
            stmt = stmt.where(
                or_(AuditEvent.request_id.is_(None), AuditEvent.request_id == "")
            )
        if query.is_success is not None:
            stmt = stmt.where(AuditEvent.is_success == query.is_success)
        if query.status_code is not None:
            stmt = stmt.where(AuditEvent.status_code == query.status_code)
        if query.is_active is None:
            stmt = stmt.where(AuditEvent.is_active.is_(True))
        else:
            stmt = stmt.where(AuditEvent.is_active == query.is_active)
        if query.occurred_from:
            stmt = stmt.where(AuditEvent.occurred_at >= query.occurred_from)
        if query.occurred_before:
            stmt = stmt.where(AuditEvent.occurred_at < query.occurred_before)
        return stmt

    @classmethod
    def query_page(cls, db: Session, query: AuditEventPageQuery) -> AuditEventPage:
        """Return rows and total from one canonical filtered audit scope."""

        filtered = cls._filtered_statement(query)
        total = int(
            db.scalar(select(func.count()).select_from(filtered.subquery())) or 0
        )
        ordered = apply_ordering(
            filtered,
            query.order_by,
            query.order_dir,
            {
                "occurred_at": AuditEvent.occurred_at,
                "action": AuditEvent.action,
                "entity_type": AuditEvent.entity_type,
                "status_code": AuditEvent.status_code,
            },
        )
        rows = db.scalars(apply_pagination(ordered, query.limit, query.offset)).all()
        return AuditEventPage(
            events=tuple(AuditEventRead.model_validate(row) for row in rows),
            total=total,
        )

    @staticmethod
    def filter_options(
        db: Session,
        query: AuditEventFilterOptionsQuery,
    ) -> AuditEventFilterOptions:
        """Return bounded filter suggestions from values present in audit rows."""

        actions = tuple(
            str(value)
            for value in db.scalars(
                select(AuditEvent.action)
                .where(
                    AuditEvent.is_active.is_(True),
                    AuditEvent.action != "",
                )
                .distinct()
                .order_by(AuditEvent.action)
                .limit(query.limit)
            ).all()
        )
        entity_types = tuple(
            str(value)
            for value in db.scalars(
                select(AuditEvent.entity_type)
                .where(
                    AuditEvent.is_active.is_(True),
                    AuditEvent.entity_type != "",
                )
                .distinct()
                .order_by(AuditEvent.entity_type)
                .limit(query.limit)
            ).all()
        )
        return AuditEventFilterOptions(actions=actions, entity_types=entity_types)

    @staticmethod
    def list(
        db: Session,
        actor_id: str | None = None,
        actor_search: str | None = None,
        actor_type: AuditActorType | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        request_id: str | None = None,
        is_success: bool | None = None,
        status_code: int | None = None,
        is_active: bool | None = None,
        order_by: str = "occurred_at",
        order_dir: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ):
        query = AuditEventPageQuery(
            actor_id=actor_id,
            actor_search=actor_search,
            actor_type=actor_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_id_missing=False,
            request_id=request_id,
            request_id_missing=False,
            is_success=is_success,
            status_code=status_code,
            is_active=is_active,
            order_by=cast(AuditEventOrderField, order_by),
            order_dir=cast(AuditEventOrderDirection, order_dir),
            limit=limit,
            offset=offset,
        )
        stmt = AuditEvents._filtered_statement(query)
        stmt = apply_ordering(
            stmt,
            query.order_by,
            query.order_dir,
            {
                "occurred_at": AuditEvent.occurred_at,
                "action": AuditEvent.action,
                "entity_type": AuditEvent.entity_type,
                "status_code": AuditEvent.status_code,
            },
        )
        return list(db.scalars(apply_pagination(stmt, query.limit, query.offset)).all())

    @staticmethod
    def log_request(db: Session, request: Request, response: Response):
        payload = AuditEvents.build_request_payload(request, response)
        event = AuditEvents._build_event(payload)
        db.add(event)
        db.commit()

    @staticmethod
    def build_request_payload(request: Request, response: Response) -> AuditEventCreate:
        actor_type = request.headers.get("x-actor-type")
        actor_id = request.headers.get("x-actor-id")
        if not actor_type:
            actor_type = getattr(request.state, "actor_type", None)
        if not actor_id:
            actor_id = getattr(request.state, "actor_id", None)
        if not actor_type:
            actor_type = AuditActorType.system.value
        request_id = request.headers.get("x-request-id")
        entity_id = request.headers.get("x-entity-id")
        ip_address = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        try:
            resolved_actor_type = AuditActorType(actor_type)
        except ValueError:
            resolved_actor_type = AuditActorType.system
        try:
            query_params = dict(request.query_params)
        except KeyError:
            query_params = {}
        sensitive = {"token", "password", "secret", "api_key", "api_token"}
        metadata: dict[str, object] = {
            "path": request.url.path,
            "query": {
                key: "<redacted>" if key.lower() in sensitive else value
                for key, value in query_params.items()
            },
        }
        person = getattr(request.state, "user", None)
        if person:
            # request.state.user can be detached by middleware ordering; read only
            # already-loaded values to avoid lazy-loading on a dead Session.
            state = getattr(person, "__dict__", {})
            try:
                first_name = (state.get("first_name") or "").strip()
                last_name = (state.get("last_name") or "").strip()
                display_name = (state.get("display_name") or "").strip()
                if not display_name:
                    display_name = f"{first_name} {last_name}".strip()
                email = state.get("email")
            except DetachedInstanceError:
                display_name = None
                email = None
            except Exception:
                display_name = None
                email = None
            if display_name:
                metadata["actor_name"] = display_name
            if email:
                metadata["actor_email"] = email
        payload = AuditEventCreate(
            actor_type=resolved_actor_type,
            actor_id=actor_id,
            action=request.method,
            entity_type=request.url.path,
            entity_id=entity_id,
            status_code=response.status_code,
            is_success=response.status_code < 400,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
            metadata_=metadata,
        )
        return payload


audit_events = AuditEvents()
