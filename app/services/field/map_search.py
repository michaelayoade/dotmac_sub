"""Combined job/asset map search for the field app."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.work_order_mirror import WorkOrderMirror
from app.services.field.jobs import _location, _profile_from_principal, _scoped_query
from app.services.field.map_assets import field_map_assets
from app.services.status_presentation import work_order_status_presentation


class FieldMapSearch:
    @staticmethod
    def search(
        db: Session,
        principal: dict[str, Any],
        query: str,
        *,
        limit: int = 20,
    ) -> list[dict]:
        term = (query or "").strip()
        if not term:
            return []

        profile = _profile_from_principal(db, principal)
        results: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in _search_jobs(db, profile, term, limit=limit):
            _append(results, seen, item, limit)
        if len(results) < limit:
            for item in _search_assets(db, term, limit=limit - len(results)):
                _append(results, seen, item, limit)
        return results


def _append(
    results: list[dict],
    seen: set[tuple[str, str]],
    item: dict,
    limit: int,
) -> None:
    key = (str(item["kind"]), str(item["id"]))
    if key in seen or len(results) >= limit:
        return
    seen.add(key)
    results.append(item)


def _search_jobs(db: Session, profile, term: str, *, limit: int) -> list[dict]:
    like = f"%{term}%"
    rows = (
        _scoped_query(db, profile)
        .filter(
            or_(
                WorkOrderMirror.crm_work_order_id.ilike(like),
                WorkOrderMirror.title.ilike(like),
                WorkOrderMirror.description.ilike(like),
                WorkOrderMirror.address.ilike(like),
            )
        )
        .order_by(
            WorkOrderMirror.scheduled_start.asc().nullslast(),
            WorkOrderMirror.created_at.asc(),
        )
        .limit(limit)
        .all()
    )

    items: list[dict] = []
    for row in rows:
        location = _location(row)
        if location.latitude is None or location.longitude is None:
            continue
        items.append(
            {
                "kind": "job",
                "id": row.crm_work_order_id,
                "title": row.title,
                "subtitle": row.address,
                "latitude": location.latitude,
                "longitude": location.longitude,
                "status": row.status,
                "status_presentation": work_order_status_presentation(row.status),
                "address_text": row.address,
            }
        )
    return items


def _search_assets(db: Session, term: str, *, limit: int) -> list[dict]:
    items = field_map_assets.search(db, term, limit=limit)
    return [
        {
            "kind": "asset",
            "id": str(item["id"]),
            "asset_type": item["type"],
            "title": item["title"],
            "subtitle": item["subtitle"],
            "latitude": item["latitude"],
            "longitude": item["longitude"],
            "status": item["status"],
            "address_text": item["subtitle"],
        }
        for item in items
    ]


field_map_search = FieldMapSearch()
