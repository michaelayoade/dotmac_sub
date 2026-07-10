from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.field_material import FieldInventoryItem
from app.services.auth_dependencies import require_user_auth
from app.services.field.inventory import field_inventory_lookup


def _auth() -> dict:
    user_id = str(uuid4())
    return {
        "principal_id": user_id,
        "person_id": user_id,
        "subscriber_id": user_id,
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _item(
    db_session,
    name: str,
    *,
    sku: str | None = None,
    is_active: bool = True,
) -> FieldInventoryItem:
    item = FieldInventoryItem(
        crm_item_id=f"crm-{uuid4().hex[:8]}",
        sku=sku,
        name=name,
        unit="pcs",
        is_active=is_active,
        metadata_={"category": "fiber", "description": f"{name} desc"},
    )
    db_session.add(item)
    db_session.flush()
    return item


def test_field_inventory_lookup_filters_active_items(db_session):
    drop = _item(db_session, "Drop Cable", sku="DROP-001")
    _item(db_session, "Inactive Router", sku="RTR-OLD", is_active=False)
    db_session.commit()

    rows = field_inventory_lookup.list_items(db_session, search="drop")

    assert [row["id"] for row in rows] == [drop.id]
    assert rows[0]["category"] == "fiber"
    assert rows[0]["description"] == "Drop Cable desc"


def test_field_inventory_locations_are_empty_until_full_inventory_port():
    assert field_inventory_lookup.list_locations() == []


def test_field_inventory_api_routes(db_session):
    _item(db_session, "Patch Cord", sku="PATCH-01")
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = _auth
    client = TestClient(app)

    items = client.get("/api/v1/field/inventory/items", params={"q": "patch"})
    assert items.status_code == 200
    assert items.json()["items"][0]["sku"] == "PATCH-01"

    locations = client.get("/api/v1/field/inventory/locations")
    assert locations.status_code == 200
    assert locations.json()["items"] == []
