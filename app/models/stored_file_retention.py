"""Canonical immutable storage-retention references, independent of review decisions."""

from uuid import UUID

from sqlalchemy import ForeignKey, String, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class StoredFileRetention(Base):
    __tablename__ = "stored_file_retentions"
    stored_file_id: Mapped[UUID] = mapped_column(
        ForeignKey("stored_files.id", ondelete="RESTRICT"), primary_key=True
    )
    reference_id: Mapped[UUID] = mapped_column(primary_key=True)
    source_owner: Mapped[str] = mapped_column(String(80), nullable=False)


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Storage retention references are immutable")


event.listen(StoredFileRetention, "before_update", _immutable)
event.listen(StoredFileRetention, "before_delete", _immutable)
