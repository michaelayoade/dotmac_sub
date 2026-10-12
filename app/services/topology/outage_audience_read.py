"""Field-authorized reads of the outage lifecycle owner's immutable audience.

This capability never re-resolves topology or writes a second audience. A page
is pinned to one immutable revision; issued and latest scope stay distinct.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.network_monitoring import (
    OutageIncidentWorkOrderLink,
    OutageScopeRevision,
)
from app.services.bulk_actions import membership_scope_token
from app.services.domain_errors import DomainError
from app.services.field.work_order_access import (
    FieldWorkOrderScope,
    ResolveFieldActor,
    require_work_order,
    resolve_field_actor,
)
from app.services.topology.outage import (
    latest_scope_revision,
    revision_audience_subscription_ids,
)


class AudienceScope(StrEnum):
    issued = "issued"
    current = "current"


class AudienceKnowledge(StrEnum):
    known = "known"
    unknown = "unknown"


@dataclass(frozen=True, slots=True)
class OutageAudienceQuery:
    requester_system_user_id: UUID
    public_id: str
    link_id: UUID
    scope: AudienceScope = AudienceScope.current
    expected_revision_id: UUID | None = None
    after_subscription_id: UUID | None = None
    limit: int = 50


@dataclass(frozen=True, slots=True)
class AudienceRevision:
    id: UUID
    sequence: int
    effective_at: datetime
    scope_type: str
    scope_id: UUID | None
    membership_token: str
    subscription_count: int | None
    knowledge: AudienceKnowledge


@dataclass(frozen=True, slots=True)
class OutageAudiencePage:
    link_id: UUID
    incident_id: UUID
    issued: AudienceRevision
    current: AudienceRevision | None
    selected_scope: AudienceScope
    subscription_ids: tuple[UUID, ...]
    next_after_subscription_id: UUID | None
    knowledge: AudienceKnowledge


class OutageAudienceReadError(DomainError):
    pass


def _error(code: str, message: str) -> OutageAudienceReadError:
    return OutageAudienceReadError(
        code=f"operations.field_jobs.outage_audience.{code}",
        message=message,
        retryable=False,
    )


def _revision(
    revision: OutageScopeRevision,
) -> tuple[AudienceRevision, tuple[UUID, ...]]:
    # These canonical functions, rather than a second membership resolver,
    # define which immutable members belong to the revision's audience.
    ids = tuple(
        sorted(
            (UUID(value) for value in revision_audience_subscription_ids(revision)),
            key=str,
        )
    )
    token = membership_scope_token(
        f"{revision.new_scope_type}:{revision.new_scope_id}",
        [str(value) for value in ids],
    )
    if (
        revision.new_scope_type
        not in ("node", "basestation", "fdh-cabinet", "unresolved")
        or revision.member_count != len(ids)
        or revision.entered_count
        != sum(member.membership == "entered" for member in revision.members)
        or revision.left_count
        != sum(member.membership == "left" for member in revision.members)
        or revision.membership_token != token
        or any(
            member.membership not in ("entered", "retained", "left")
            for member in revision.members
        )
    ):
        raise _error(
            "inconsistent_revision", "Outage audience evidence is inconsistent"
        )
    knowledge = (
        AudienceKnowledge.unknown
        if revision.new_scope_type == "unresolved" or revision.new_scope_id is None
        else AudienceKnowledge.known
    )
    return AudienceRevision(
        id=revision.id,
        sequence=revision.sequence,
        effective_at=revision.effective_at,
        scope_type=revision.new_scope_type,
        scope_id=revision.new_scope_id,
        membership_token=revision.membership_token,
        subscription_count=len(ids) if knowledge == AudienceKnowledge.known else None,
        knowledge=knowledge,
    ), ids if knowledge == AudienceKnowledge.known else ()


def read_outage_audience(db: Session, query: OutageAudienceQuery) -> OutageAudiencePage:
    if not 1 <= query.limit <= 100:
        raise _error("invalid_page", "Audience page size must be between 1 and 100")
    if query.after_subscription_id is not None and query.expected_revision_id is None:
        raise _error("revision_required", "Audience continuation requires its revision")
    actor = resolve_field_actor(db, ResolveFieldActor(query.requester_system_user_id))
    work_order = require_work_order(db, FieldWorkOrderScope(actor, query.public_id))
    link = (
        db.query(OutageIncidentWorkOrderLink)
        .filter(
            OutageIncidentWorkOrderLink.id == query.link_id,
            OutageIncidentWorkOrderLink.work_order_id == work_order.id,
        )
        .one_or_none()
    )
    if link is None:
        raise _error("not_found", "Job outage context not found")
    issued_row = db.get(OutageScopeRevision, link.scope_revision_id)
    if issued_row is None or issued_row.incident_id != link.incident_id:
        raise _error("issued_revision_missing", "Issued outage audience is unavailable")
    issued, issued_ids = _revision(issued_row)
    if (
        issued.scope_type != link.target_type
        or issued.scope_id != link.target_id
        or issued.sequence != link.scope_revision_sequence
        or issued.membership_token != link.membership_token
    ):
        raise _error(
            "inconsistent_link", "Issued outage audience provenance is inconsistent"
        )
    current_row = latest_scope_revision(db, link.incident_id)
    current, current_ids = (
        _revision(current_row) if current_row is not None else (None, ())
    )
    selected = issued if query.scope == AudienceScope.issued else current
    ids = issued_ids if query.scope == AudienceScope.issued else current_ids
    if query.expected_revision_id is not None and (
        selected is None or selected.id != query.expected_revision_id
    ):
        raise _error(
            "revision_changed", "Outage audience changed; reload the first page"
        )
    if query.after_subscription_id is not None:
        ids = tuple(
            value for value in ids if str(value) > str(query.after_subscription_id)
        )
    page = ids[: query.limit]
    return OutageAudiencePage(
        link_id=link.id,
        incident_id=link.incident_id,
        issued=issued,
        current=current,
        selected_scope=query.scope,
        subscription_ids=page,
        next_after_subscription_id=page[-1] if len(ids) > query.limit else None,
        knowledge=selected.knowledge
        if selected is not None
        else AudienceKnowledge.unknown,
    )
