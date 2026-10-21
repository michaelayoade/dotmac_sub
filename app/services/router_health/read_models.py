"""Read-only view models for the router health UI (``network.router_health``).

Reads open ``network:router-health:`` alerts and the signal rows; writes nothing.
Never uses ``Router.status``. Evidence is rendered from the structured,
allowlisted ``details.evidence`` mapping that the reconciler stored.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.admin_alert import AdminAlert
from app.models.network_monitoring import AlertStatus
from app.models.router_health import RouterHealthSignal
from app.models.router_management import Router, RouterConfigSnapshot
from app.services.router_health.contracts import (
    ALERT_MODE_SHADOW,
    FINGERPRINT_ROOT,
    HealthPriority,
)
from app.services.router_health.runtime_checks import SIGNAL_ACCESS
from app.services.router_health.settings import load_settings

PRIORITY_ORDER: tuple[str, ...] = tuple(p.value for p in HealthPriority)
_RANK = {value: index for index, value in enumerate(PRIORITY_ORDER)}
LEVEL_OK = "ok"
LEVEL_NO_DATA = "no_data"
HEALTH_FILTERS: tuple[str, ...] = (*PRIORITY_ORDER, LEVEL_OK, LEVEL_NO_DATA)
_LABELS = {
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    LEVEL_OK: "OK",
    LEVEL_NO_DATA: "No data",
}
#: status_badge colour variant per level (see components/ui/macros.html).
_TONES = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "info",
    LEVEL_OK: "active",
    LEVEL_NO_DATA: "neutral",
}


@dataclass(frozen=True)
class HealthBadge:
    level: str
    label: str
    tone: str
    open_count: int = 0


@dataclass(frozen=True)
class FindingView:
    title: str
    impact: str
    priority: str
    layer: str
    check: str
    evidence: tuple[tuple[str, str], ...]
    fix: str
    known_issue: str | None
    target_url: str
    first_seen_at: datetime
    last_seen_at: datetime
    mode: str
    router_id: UUID | None = None
    router_name: str | None = None


@dataclass(frozen=True)
class RouterHealthDetail:
    findings: tuple[FindingView, ...]
    badge: HealthBadge
    last_snapshot_at: datetime | None
    last_runtime_poll_at: datetime | None
    alert_mode: str


@dataclass(frozen=True)
class PostureSummary:
    total: int
    counts: dict[str, int]
    alert_mode: str
    #: Empty unless the viewer holds ``router:read`` (evidence has management IPs).
    top: tuple[FindingView, ...] = field(default_factory=tuple)
    detail_visible: bool = False


def _badge(level: str, count: int = 0) -> HealthBadge:
    return HealthBadge(
        level=level, label=_LABELS[level], tone=_TONES[level], open_count=count
    )


def _evidence_rows(evidence: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(evidence, dict):
        return ()
    rows: list[tuple[str, str]] = []
    for key in sorted(evidence):
        value = evidence[key]
        if isinstance(value, list):
            rendered = ", ".join(str(item) for item in value)
        elif isinstance(value, bool):
            rendered = "yes" if value else "no"
        elif value is None:
            rendered = "-"
        else:
            rendered = str(value)
        rows.append((str(key).replace("_", " "), rendered))
    return tuple(rows)


def _view(alert: AdminAlert, router_names: dict[UUID, str]) -> FindingView:
    details = alert.details or {}
    raw_router = details.get("router_id")
    router_id: UUID | None = None
    if isinstance(raw_router, str):
        try:
            router_id = UUID(raw_router)
        except ValueError:
            router_id = None
    return FindingView(
        title=alert.title,
        impact=str(details.get("impact") or alert.summary or ""),
        priority=str(details.get("priority") or "low"),
        layer=str(details.get("layer") or ""),
        check=str(details.get("check") or ""),
        evidence=_evidence_rows(details.get("evidence")),
        fix=str(details.get("fix") or ""),
        known_issue=(
            str(details["known_issue"]) if details.get("known_issue") else None
        ),
        target_url=alert.target_url or "/admin/alerts?category=network",
        first_seen_at=alert.first_seen_at,
        last_seen_at=alert.last_seen_at,
        mode=str(details.get("mode") or ALERT_MODE_SHADOW),
        router_id=router_id,
        router_name=router_names.get(router_id) if router_id else None,
    )


def _sort_key(view: FindingView) -> tuple[int, str, str]:
    return (_RANK.get(view.priority, len(_RANK)), view.title, view.check)


def _open_alerts(db: Session) -> list[AdminAlert]:
    return list(
        db.execute(
            select(AdminAlert).where(
                AdminAlert.fingerprint.like(f"{FINGERPRINT_ROOT}%"),
                AdminAlert.status != AlertStatus.resolved,
            )
        ).scalars()
    )


def _router_names(db: Session) -> dict[UUID, str]:
    return {rid: name for rid, name in db.execute(select(Router.id, Router.name))}


def _views_by_router(db: Session) -> dict[UUID, list[FindingView]]:
    names = _router_names(db)
    grouped: dict[UUID, list[FindingView]] = defaultdict(list)
    for alert in _open_alerts(db):
        view = _view(alert, names)
        if view.router_id is not None:
            grouped[view.router_id].append(view)
    for views in grouped.values():
        views.sort(key=_sort_key)
    return grouped


def _routers_with_data(db: Session, router_ids: Iterable[UUID]) -> set[UUID]:
    ids = list(router_ids)
    if not ids:
        return set()
    with_signal = {
        rid
        for (rid,) in db.execute(
            select(RouterHealthSignal.router_id)
            .where(RouterHealthSignal.router_id.in_(ids))
            .distinct()
        )
    }
    with_snapshot = {
        rid
        for (rid,) in db.execute(
            select(RouterConfigSnapshot.router_id)
            .where(RouterConfigSnapshot.router_id.in_(ids))
            .distinct()
        )
    }
    return with_signal | with_snapshot


def _badge_for(views: Sequence[FindingView], has_data: bool) -> HealthBadge:
    if views:
        return _badge(views[0].priority, len(views))
    return _badge(LEVEL_OK if has_data else LEVEL_NO_DATA)


def router_health_badges(
    db: Session, router_ids: Iterable[UUID]
) -> dict[UUID, HealthBadge]:
    """Worst open priority per router (one alerts query, two existence queries)."""
    ids = list(router_ids)
    grouped = _views_by_router(db)
    with_data = _routers_with_data(db, ids)
    return {rid: _badge_for(grouped.get(rid, []), rid in with_data) for rid in ids}


def router_health_findings(db: Session, router_id: UUID) -> tuple[FindingView, ...]:
    return tuple(_views_by_router(db).get(router_id, []))


def router_health_detail(db: Session, router_id: UUID) -> RouterHealthDetail:
    views = tuple(_views_by_router(db).get(router_id, []))
    last_snapshot = db.scalar(
        select(func.max(RouterConfigSnapshot.created_at)).where(
            RouterConfigSnapshot.router_id == router_id
        )
    )
    last_poll = db.scalar(
        select(RouterHealthSignal.last_observed_at).where(
            RouterHealthSignal.router_id == router_id,
            RouterHealthSignal.signal == SIGNAL_ACCESS,
            RouterHealthSignal.scope == "router",
        )
    )
    has_data = last_snapshot is not None or last_poll is not None
    return RouterHealthDetail(
        findings=views,
        badge=_badge_for(views, has_data),
        last_snapshot_at=last_snapshot,
        last_runtime_poll_at=last_poll,
        alert_mode=load_settings(db).alert_mode,
    )


def filter_router_ids_by_health(
    badges: dict[UUID, HealthBadge], health: str | None
) -> set[UUID] | None:
    """Router ids whose badge level matches ``health`` (None: no filter)."""
    if not health or health not in HEALTH_FILTERS:
        return None
    return {rid for rid, badge in badges.items() if badge.level == health}


def router_posture_summary(
    db: Session, *, include_detail: bool, limit: int = 20
) -> PostureSummary:
    """Fleet counts by priority; the top findings only when ``include_detail``."""
    names = _router_names(db)
    views = sorted((_view(alert, names) for alert in _open_alerts(db)), key=_sort_key)
    counts = dict.fromkeys(PRIORITY_ORDER, 0)
    for view in views:
        counts[view.priority] = counts.get(view.priority, 0) + 1
    return PostureSummary(
        total=len(views),
        counts=counts,
        alert_mode=load_settings(db).alert_mode,
        top=tuple(views[:limit]) if include_detail else (),
        detail_visible=include_detail,
    )


def level_label(level: str) -> str:
    return _LABELS.get(level, level.title())


def current_alert_mode(db: Session) -> str:
    return load_settings(db).alert_mode
