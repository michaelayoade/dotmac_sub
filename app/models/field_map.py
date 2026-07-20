import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class FieldMapAssetLocationProvenance(Base):
    """Provenance/confidence of an asset's current coordinate."""

    __tablename__ = "field_map_asset_location_provenance"
    __table_args__ = (
        UniqueConstraint(
            "asset_type",
            "asset_id",
            name="uq_field_map_asset_location_provenance_asset",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    asset_type: Mapped[str] = mapped_column(String(80), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source: Mapped[str | None] = mapped_column(String(32))
    accuracy_m: Mapped[float | None] = mapped_column(Float)
    updated_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
