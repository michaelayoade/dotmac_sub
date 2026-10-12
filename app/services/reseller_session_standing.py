"""Database authority for ordinary reseller cookie session standing.

This owner deliberately excludes legacy administrator impersonation sessions.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.subscriber import Reseller, ResellerUser, Subscriber


@dataclass(frozen=True)
class ResellerSessionStandingQuery:
    auth_session_id: UUID
    principal_type: Literal["subscriber", "reseller_user"]
    principal_id: UUID
    reseller_id: UUID
    subscriber_id: UUID | None
    reseller_user_id: UUID | None


@dataclass(frozen=True)
class ResellerSessionStanding:
    auth_session_id: UUID
    principal_id: UUID
    reseller_id: UUID
    expires_at: datetime


@dataclass(frozen=True)
class ResellerSessionRefreshQuery:
    standing: ResellerSessionStandingQuery
    now: datetime
    created_at: datetime
    sliding_ttl_seconds: int
    absolute_ttl_seconds: int


@dataclass(frozen=True)
class ResellerSessionRefreshOutcome:
    expires_at: datetime
    ttl_seconds: int


def resolve_reseller_session_standing(
    db: Session, *, query: ResellerSessionStandingQuery
) -> ResellerSessionStanding | None:
    """Read fresh scalar DB facts without replacing pending caller ORM edits."""
    with db.no_autoflush:
        row = db.execute(
            select(
                AuthSession.subscriber_id,
                AuthSession.reseller_user_id,
                AuthSession.system_user_id,
                AuthSession.party_id,
                AuthSession.status,
                AuthSession.revoked_at,
                AuthSession.expires_at,
            )
            .where(AuthSession.id == query.auth_session_id)
            .execution_options(populate_existing=True)
        ).one_or_none()
        if (
            row is None
            or row.status != SessionStatus.active
            or row.revoked_at is not None
            or row.expires_at is None
            or row.system_user_id is not None
            or row.party_id is not None
        ):
            return None
        expiry = row.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if expiry <= datetime.now(UTC):
            return None
        reseller = db.execute(
            select(Reseller.is_active)
            .where(Reseller.id == query.reseller_id)
            .execution_options(populate_existing=True)
        ).one_or_none()
        if reseller is None or not reseller.is_active:
            return None
        if query.principal_type == "reseller_user":
            if (
                query.subscriber_id is not None
                or query.reseller_user_id != query.principal_id
                or row.subscriber_id is not None
                or row.reseller_user_id != query.principal_id
            ):
                return None
            user = db.execute(
                select(
                    ResellerUser.subscriber_id,
                    ResellerUser.reseller_id,
                    ResellerUser.is_active,
                )
                .where(ResellerUser.id == query.principal_id)
                .execution_options(populate_existing=True)
            ).one_or_none()
            if (
                user is None
                or not user.is_active
                or user.reseller_id != query.reseller_id
            ):
                return None
            if user.subscriber_id is not None:
                linked = db.execute(
                    select(Subscriber.is_active)
                    .where(Subscriber.id == user.subscriber_id)
                    .execution_options(populate_existing=True)
                ).one_or_none()
                if linked is None or not linked.is_active:
                    return None
        elif query.principal_type == "subscriber":
            if (
                query.reseller_user_id is not None
                or query.subscriber_id != query.principal_id
                or row.reseller_user_id is not None
                or row.subscriber_id != query.principal_id
            ):
                return None
            subscriber = db.execute(
                select(Subscriber.is_active)
                .where(Subscriber.id == query.principal_id)
                .execution_options(populate_existing=True)
            ).one_or_none()
            if subscriber is None or not subscriber.is_active:
                return None
            membership = db.execute(
                select(ResellerUser.reseller_id, ResellerUser.is_active)
                .where(ResellerUser.subscriber_id == query.principal_id)
                .order_by(ResellerUser.created_at.desc())
                .limit(1)
                .execution_options(populate_existing=True)
            ).one_or_none()
            if (
                membership is None
                or not membership.is_active
                or membership.reseller_id != query.reseller_id
            ):
                return None
        else:
            return None
        return ResellerSessionStanding(
            query.auth_session_id, query.principal_id, query.reseller_id, expiry
        )


def resolve_reseller_session_refresh(
    db: Session, *, query: ResellerSessionRefreshQuery
) -> ResellerSessionRefreshOutcome | None:
    standing = resolve_reseller_session_standing(db, query=query.standing)
    if standing is None:
        return None
    expiry = min(
        standing.expires_at,
        query.now + timedelta(seconds=query.sliding_ttl_seconds),
        query.created_at + timedelta(seconds=query.absolute_ttl_seconds),
    )
    if expiry <= query.now:
        return None
    return ResellerSessionRefreshOutcome(
        expiry, max(1, int((expiry - query.now).total_seconds()))
    )
