"""RADIUS session resolver SOT.

This service answers "is the customer online now, and where?" using the
canonical `radius_active_sessions` table. It does not decide entitlement or
outage impact; higher layers compose those answers.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.radius_active_session import RadiusActiveSession
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services.common import coerce_uuid
from app.services.network.identity import NetworkIdentity, identity_for_radius_session


@dataclass(frozen=True)
class RadiusSessionResolution:
    subscriber_id: object
    sessions: tuple[RadiusActiveSession, ...]
    primary_session: RadiusActiveSession | None
    primary_identity: NetworkIdentity | None

    @property
    def is_online(self) -> bool:
        return self.primary_session is not None


def list_active_sessions_for_subscriber(
    db: Session,
    subscriber_id,
    *,
    limit: int = 20,
) -> list[RadiusActiveSession]:
    return list(
        db.scalars(
            select(RadiusActiveSession)
            .where(RadiusActiveSession.subscriber_id == coerce_uuid(subscriber_id))
            .order_by(
                RadiusActiveSession.last_update.desc().nullslast(),
                RadiusActiveSession.session_start.desc(),
                RadiusActiveSession.id,
            )
            .limit(limit)
        ).all()
    )


def resolve_subscriber_radius_sessions(
    db: Session,
    subscriber_id,
    *,
    limit: int = 20,
) -> RadiusSessionResolution:
    sessions = tuple(
        list_active_sessions_for_subscriber(db, subscriber_id, limit=limit)
    )
    primary = sessions[0] if sessions else None
    return RadiusSessionResolution(
        subscriber_id=coerce_uuid(subscriber_id),
        sessions=sessions,
        primary_session=primary,
        primary_identity=identity_for_radius_session(db, primary) if primary else None,
    )


def active_session_count_for_subscriber(db: Session, subscriber_id) -> int:
    return int(
        db.scalar(
            select(func.count(RadiusActiveSession.id)).where(
                RadiusActiveSession.subscriber_id == coerce_uuid(subscriber_id)
            )
        )
        or 0
    )


def open_accounting_session_query(db: Session):
    """Base query for open imported accounting-session mirrors.

    ``RadiusAccountingSession`` is a lagging accounting mirror, not the
    canonical live view. Some screens still need its framed IP/session metadata;
    centralizing that query keeps "open session" semantics consistent while the
    live ``radius_active_sessions`` view remains the online-now SOT.
    """
    return db.query(RadiusAccountingSession).filter(
        RadiusAccountingSession.session_end.is_(None),
        RadiusAccountingSession.status_type != AccountingStatus.stop,
    )


def order_open_accounting_sessions_newest(query):
    return query.order_by(
        RadiusAccountingSession.last_update_at.desc().nullslast(),
        RadiusAccountingSession.session_start.desc().nullslast(),
        RadiusAccountingSession.created_at.desc(),
    )


def latest_open_accounting_session_for_subscription(
    db: Session,
    subscription_id,
    *,
    access_credential_id=None,
) -> RadiusAccountingSession | None:
    query = open_accounting_session_query(db)
    if access_credential_id is not None:
        from sqlalchemy import or_

        query = query.filter(
            or_(
                RadiusAccountingSession.subscription_id == coerce_uuid(subscription_id),
                RadiusAccountingSession.access_credential_id
                == coerce_uuid(access_credential_id),
            )
        )
    else:
        query = query.filter(
            RadiusAccountingSession.subscription_id == coerce_uuid(subscription_id)
        )
    return order_open_accounting_sessions_newest(query).first()


def latest_open_accounting_sessions_by_subscription(
    db: Session,
    subscription_ids,
) -> dict[object, RadiusAccountingSession]:
    ids = [coerce_uuid(subscription_id) for subscription_id in subscription_ids]
    if not ids:
        return {}
    rows = (
        open_accounting_session_query(db)
        .filter(RadiusAccountingSession.subscription_id.in_(ids))
        .order_by(
            RadiusAccountingSession.subscription_id.asc(),
            RadiusAccountingSession.last_update_at.desc().nullslast(),
            RadiusAccountingSession.session_start.desc().nullslast(),
            RadiusAccountingSession.created_at.desc(),
        )
        .all()
    )
    sessions: dict[object, RadiusAccountingSession] = {}
    for row in rows:
        if row.subscription_id is not None and row.subscription_id not in sessions:
            sessions[row.subscription_id] = row
    return sessions


def live_framed_ips_by_subscription(db: Session, subscription_ids) -> dict:
    sessions = latest_open_accounting_sessions_by_subscription(db, subscription_ids)
    return {
        subscription_id: session.framed_ip_address
        for subscription_id, session in sessions.items()
        if session.framed_ip_address
    }
