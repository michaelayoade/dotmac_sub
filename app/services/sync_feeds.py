"""Shared query contract for bounded cross-application sync feeds."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Query

SYNC_FEED_MAX_PAGE_SIZE = 500


def apply_sync_page(
    query: Query,
    model: Any,
    *,
    updated_since: datetime | None,
    limit: int,
    offset: int,
) -> Query:
    """Apply the inclusive watermark and stable ordering used by every feed."""
    if updated_since is not None:
        query = query.filter(model.updated_at >= updated_since)
    return (
        query.order_by(model.updated_at.asc(), model.id.asc())
        .offset(offset)
        .limit(limit)
    )


def sync_page_response(items: list[Any], *, limit: int, offset: int) -> dict[str, Any]:
    """Return the common offset-page envelope consumed by integration clients."""
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}
