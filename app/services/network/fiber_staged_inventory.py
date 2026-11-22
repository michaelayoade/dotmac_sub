"""Bounded, read-only browsing of the latest staged fiber source identities.

The complete coverage and field reports remain the authority for readiness and
digests. This projection only selects source facts for an interactive list.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import String, cast, func, literal, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)


@dataclass(frozen=True, slots=True)
class StagedFeaturePage:
    features: tuple[FiberTopologyStagedFeature, ...]
    total: int
    page: int
    per_page: int
    total_pages: int
    query: str


def latest_staged_feature_page(
    db: Session,
    *,
    asset_types: frozenset[str],
    query: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> StagedFeaturePage:
    """Search after latest-version selection, then fetch only one feature page."""

    feature = FiberTopologyStagedFeature
    batch = FiberTopologySourceBatch
    cleaned = str(query or "").strip()[:160]
    size = per_page if per_page in (25, 50, 100) else 25
    identity = func.coalesce(
        func.nullif(feature.external_id, ""),
        literal("feature:") + cast(feature.id, String),
    )
    ranked = (
        select(
            feature.id.label("feature_id"),
            feature.asset_type.label("asset_type"),
            feature.external_id.label("external_id"),
            feature.display_name.label("display_name"),
            batch.source_system.label("source_system"),
            batch.profile.label("profile"),
            func.row_number()
            .over(
                partition_by=(batch.source_system, feature.asset_type, identity),
                order_by=(
                    batch.created_at.desc(),
                    feature.created_at.desc(),
                    feature.id.desc(),
                ),
            )
            .label("version_rank"),
        )
        .join(batch, feature.batch_id == batch.id)
        .where(feature.asset_type.in_(asset_types))
        .subquery()
    )
    matching = select(ranked.c.feature_id).where(ranked.c.version_rank == 1)
    if cleaned:
        escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        matching = matching.where(
            or_(
                ranked.c.display_name.ilike(pattern, escape="\\"),
                ranked.c.external_id.ilike(pattern, escape="\\"),
                ranked.c.source_system.ilike(pattern, escape="\\"),
                ranked.c.profile.ilike(pattern, escape="\\"),
                ranked.c.asset_type.ilike(pattern, escape="\\"),
            )
        )
    total = int(db.scalar(select(func.count()).select_from(matching.subquery())) or 0)
    total_pages = max(1, (total + size - 1) // size)
    selected_page = min(max(page, 1), total_pages)
    ids = list(
        db.scalars(
            matching.order_by(
                ranked.c.source_system,
                ranked.c.asset_type,
                ranked.c.external_id,
                ranked.c.feature_id,
            )
            .limit(size)
            .offset((selected_page - 1) * size)
        )
    )
    if not ids:
        features: tuple[FiberTopologyStagedFeature, ...] = ()
    else:
        by_id = {
            row.id: row
            for row in db.scalars(
                select(feature)
                .options(joinedload(feature.batch))
                .where(feature.id.in_(ids))
            ).all()
        }
        features = tuple(by_id[feature_id] for feature_id in ids)
    return StagedFeaturePage(features, total, selected_page, size, total_pages, cleaned)


__all__ = ["StagedFeaturePage", "latest_staged_feature_page"]
