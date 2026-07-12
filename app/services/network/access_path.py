"""Single source of truth for customer-to-network access paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.services.common import coerce_uuid
from app.services.topology.customer_path import CustomerPath, resolve_customer_path


@dataclass(frozen=True)
class AccessPathSummary:
    subscription_id: object
    subscriber_id: object | None
    access_kind: str | None
    node_id: object | None
    node_name: str | None
    basestation_id: object | None
    basestation_name: str | None
    gap: str | None
    live_session: bool


def resolve_subscription_access_path(
    db: Session,
    subscription: Subscription | str,
) -> CustomerPath:
    subscription_obj = (
        subscription
        if isinstance(subscription, Subscription)
        else db.get(Subscription, coerce_uuid(subscription))
    )
    if subscription_obj is None:
        raise ValueError("subscription not found")
    return resolve_customer_path(db, subscription_obj)


def summarize_subscription_access_path(
    db: Session,
    subscription: Subscription | str,
) -> AccessPathSummary:
    subscription_obj = (
        subscription
        if isinstance(subscription, Subscription)
        else db.get(Subscription, coerce_uuid(subscription))
    )
    if subscription_obj is None:
        raise ValueError("subscription not found")
    path = resolve_customer_path(db, subscription_obj)
    return AccessPathSummary(
        subscription_id=subscription_obj.id,
        subscriber_id=subscription_obj.subscriber_id,
        access_kind=path.access_device_kind,
        node_id=getattr(path.node, "id", None),
        node_name=getattr(path.node, "name", None),
        basestation_id=getattr(path.basestation, "id", None),
        basestation_name=getattr(path.basestation, "name", None),
        gap=path.gap,
        live_session=path.live_session,
    )


def resolve_subscriber_access_paths(
    db: Session,
    subscriber_id,
    *,
    active_only: bool = True,
) -> dict[object, CustomerPath]:
    stmt = select(Subscription).where(
        Subscription.subscriber_id == coerce_uuid(subscriber_id)
    )
    if active_only:
        stmt = stmt.where(Subscription.status == SubscriptionStatus.active)
    stmt = stmt.order_by(Subscription.created_at.desc())
    paths: dict[object, CustomerPath] = {}
    for subscription in db.scalars(stmt).all():
        paths[subscription.id] = resolve_customer_path(db, subscription)
    return paths


def access_path_scope(path: CustomerPath) -> dict[str, Any | None]:
    return {
        "access_kind": path.access_device_kind,
        "node_id": str(path.node.id) if path.node is not None else None,
        "node_name": getattr(path.node, "name", None),
        "basestation_id": str(path.basestation.id)
        if path.basestation is not None
        else None,
        "basestation_name": getattr(path.basestation, "name", None),
        "gap": path.gap,
        "live_session": path.live_session,
    }
