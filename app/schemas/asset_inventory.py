from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class AssetCatalogItem(BaseModel):
    id: UUID
    source: str = Field(
        description=(
            "Native source table/domain: field_inventory, field_asset, ont, cpe, "
            "olt, network_device, router."
        )
    )
    asset_type: str
    label: str
    identifier: str | None = None
    status: str | None = None
    vendor: str | None = None
    model: str | None = None
    serial_number: str | None = None
    management_ip: str | None = None
    subscriber_id: UUID | None = None
    assigned_technician_id: UUID | None = None
    assigned_system_user_id: UUID | None = None
    assigned_to: str | None = None
    location: str | None = None
    metadata: dict | None = None
    updated_at: datetime | None = None


class AssetCatalogSummary(BaseModel):
    field_inventory: int
    field_asset: int
    ont: int
    cpe: int
    olt: int
    network_device: int
    router: int
    total: int


class AssetCatalogResponse(BaseModel):
    items: list[AssetCatalogItem]
    count: int
    limit: int
    offset: int
    summary: AssetCatalogSummary
