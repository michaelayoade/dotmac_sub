"""Aggregation — merge every provider into one ranked queue.

The aggregator knows nothing about tickets, conversations or work orders. It
resolves scope + snoozes once, asks each registered provider for its items, then
ranks and bands them. Adding a source never touches this file.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.workqueue import snooze as snooze_service
from app.services.workqueue.permissions import WorkqueuePrincipal, can_act_on_item
from app.services.workqueue.providers import load_builtin_providers
from app.services.workqueue.providers.base import WorkqueueProvider
from app.services.workqueue.scope import WorkqueueScope, get_workqueue_scope
from app.services.workqueue.scoring_config import (
    WorkqueueScoringConfig,
    load_scoring_config,
)
from app.services.workqueue.types import (
    MUTATING_ACTIONS,
    ItemKind,
    WorkqueueItem,
    WorkqueueSection,
    WorkqueueView,
)

logger = logging.getLogger(__name__)


def _rank_key(item: WorkqueueItem, config: WorkqueueScoringConfig):
    # Hottest score first, then most recent activity, then a stable kind/id
    # tie-break so equal items never shuffle between requests.
    return (
        -item.score,
        -item.happened_at.timestamp(),
        config.kind_rank(item.item_kind),
        str(item.item_id),
    )


def _authorize(item: WorkqueueItem, scope: WorkqueueScope) -> WorkqueueItem:
    """Attach ``can_act`` and strip actions the principal may not take."""
    allowed = can_act_on_item(
        scope.principal,
        item_assignee_id=item.assigned_person_id,
        audience=scope.audience,
    )
    actions = tuple(
        action for action in item.actions if allowed or action not in MUTATING_ACTIONS
    )
    return replace(item, can_act=allowed, actions=actions)


def collect_items(
    db: Session,
    scope: WorkqueueScope,
    *,
    config: WorkqueueScoringConfig,
    include_snoozed: bool = False,
    now: datetime | None = None,
    providers: tuple[WorkqueueProvider, ...] | None = None,
) -> dict[ItemKind, list[WorkqueueItem]]:
    """Run every provider against a resolved scope and return items by kind."""
    current_time = now or datetime.now(UTC)
    active_providers = providers if providers is not None else load_builtin_providers()

    snoozed: dict[ItemKind, set[UUID]] = (
        {kind: set() for kind in ItemKind}
        if include_snoozed
        else snooze_service.active_snoozed_ids(
            db, user_id=scope.person_id, now=current_time
        )
    )

    by_kind: dict[ItemKind, list[WorkqueueItem]] = {kind: [] for kind in ItemKind}
    for provider in active_providers:
        items = provider.fetch(
            db,
            scope=scope,
            config=config,
            snoozed_ids=snoozed.get(provider.kind, set()),
            now=current_time,
            limit=config.provider_limit,
        )
        logger.debug(
            "workqueue_provider_results person_id=%s kind=%s filters=%s count=%s",
            scope.person_id,
            provider.kind.value,
            scope.applied_filters,
            len(items),
        )
        for item in items:
            by_kind.setdefault(item.item_kind, []).append(_authorize(item, scope))

    for kind, items in by_kind.items():
        items.sort(key=lambda item: _rank_key(item, config))
        by_kind[kind] = items
    return by_kind


def build_workqueue(
    db: Session,
    principal: WorkqueuePrincipal,
    *,
    requested_audience: str | None = None,
    service_team_id: UUID | None = None,
    include_snoozed: bool = False,
    hero_band_size: int | None = None,
    config: WorkqueueScoringConfig | None = None,
    now: datetime | None = None,
    providers: tuple[WorkqueueProvider, ...] | None = None,
) -> WorkqueueView:
    """The ranked, sectioned queue for one principal."""
    scoring = config or load_scoring_config()
    current_time = now or datetime.now(UTC)
    scope = get_workqueue_scope(
        db,
        principal,
        requested_audience=requested_audience,
        service_team_id=service_team_id,
    )
    by_kind = collect_items(
        db,
        scope,
        config=scoring,
        include_snoozed=include_snoozed,
        now=current_time,
        providers=providers,
    )

    ranked = sorted(
        (item for items in by_kind.values() for item in items),
        key=lambda item: _rank_key(item, scoring),
    )
    band = scoring.hero_band_size if hero_band_size is None else hero_band_size

    sections = tuple(
        WorkqueueSection(
            item_kind=kind,
            items=tuple(by_kind.get(kind, ())),
            total=len(by_kind.get(kind, ())),
        )
        for kind in scoring.kind_order
    )
    return WorkqueueView(
        audience=scope.audience,
        generated_at=current_time,
        right_now=tuple(ranked[: max(band, 0)]),
        sections=sections,
    )


def list_workqueue(
    db: Session,
    principal: WorkqueuePrincipal,
    *,
    requested_audience: str | None = None,
    service_team_id: UUID | None = None,
    include_snoozed: bool = False,
    limit: int = 50,
    offset: int = 0,
    config: WorkqueueScoringConfig | None = None,
    now: datetime | None = None,
    providers: tuple[WorkqueueProvider, ...] | None = None,
) -> list[WorkqueueItem]:
    """Flat, ranked queue (the paginated list endpoint's read model)."""
    view = build_workqueue(
        db,
        principal,
        requested_audience=requested_audience,
        service_team_id=service_team_id,
        include_snoozed=include_snoozed,
        hero_band_size=0,
        config=config,
        now=now,
        providers=providers,
    )
    scoring = config or load_scoring_config()
    ranked = sorted(
        (item for section in view.sections for item in section.items),
        key=lambda item: _rank_key(item, scoring),
    )
    return ranked[offset : offset + limit]
