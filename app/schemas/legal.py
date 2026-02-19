"""Pydantic schemas for legal documents."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.legal import LegalDocumentType


class LegalDocumentCreate(BaseModel):
    """Schema for creating a legal document."""

    document_type: LegalDocumentType
    title: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(..., min_length=1, max_length=100)
    version: str = Field(default="1.0", max_length=20)
    summary: str | None = None
    content: str | None = None
    is_published: bool = False
    effective_date: datetime | None = None


class LegalDocumentUpdate(BaseModel):
    """Schema for updating a legal document."""

    title: str | None = Field(None, min_length=1, max_length=200)
    slug: str | None = Field(None, min_length=1, max_length=100)
    version: str | None = Field(None, max_length=20)
    summary: str | None = None
    content: str | None = None
    is_current: bool | None = None
    is_published: bool | None = None
    effective_date: datetime | None = None


class LegalDocumentRead(BaseModel):
    """Schema for reading a legal document."""

    id: UUID
    document_type: LegalDocumentType
    title: str
    slug: str
    version: str
    summary: str | None
    content: str | None
    file_path: str | None
    file_name: str | None
    file_size: int | None
    mime_type: str | None
    is_current: bool
    is_published: bool
    published_at: datetime | None
    effective_date: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LegalDocumentPublic(BaseModel):
    """Public schema for viewing legal documents (limited fields)."""

    title: str
    version: str
    summary: str | None
    content: str | None
    file_path: str | None
    effective_date: datetime | None
    published_at: datetime | None

    model_config = {"from_attributes": True}
