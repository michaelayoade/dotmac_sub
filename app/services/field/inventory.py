"""Field inventory lookup over the native field material catalog."""

from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.field_material import FieldInventoryItem


def serialize_item(item: FieldInventoryItem) -> dict:
    metadata = item.metadata_ or {}
    return {
        "id": item.id,
        "crm_item_id": item.crm_item_id,
        "sku": item.sku,
        "name": item.name,
        "unit": item.unit,
        "description": metadata.get("description"),
        "category": metadata.get("category"),
        "is_active": item.is_active,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


class FieldInventoryLookup:
    @staticmethod
    def list_items(
        db: Session,
        *,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        safe_limit = max(1, min(int(limit or 50), 200))
        safe_offset = max(0, int(offset or 0))
        query = db.query(FieldInventoryItem).filter(
            FieldInventoryItem.is_active.is_(True)
        )
        term = (search or "").strip()
        if term:
            like = f"%{term}%"
            query = query.filter(
                or_(
                    FieldInventoryItem.name.ilike(like),
                    FieldInventoryItem.sku.ilike(like),
                    FieldInventoryItem.crm_item_id.ilike(like),
                )
            )
        rows = (
            query.order_by(
                FieldInventoryItem.name.asc(), FieldInventoryItem.created_at.asc()
            )
            .offset(safe_offset)
            .limit(safe_limit)
            .all()
        )
        return [serialize_item(item) for item in rows]

    @staticmethod
    def list_locations() -> list[dict]:
        return []


field_inventory_lookup = FieldInventoryLookup()
