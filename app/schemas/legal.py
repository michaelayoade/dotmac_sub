"""Pydantic schemas for legal documents."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.legal import LegalDocumentType


class LegalDocumentCreate(BaseModel):
    """Schema for creating a legal document."""

    document_type: LegalDocumentType
    title: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(..., min_length=1, max_length=100)
    version: str = Field(default="1.0", max_length=20)
    summary: Optional[str] = None
    content: Optional[str] = None
    is_published: bool = False
    effective_date: Optional[datetime] = None


class LegalDocumentUpdate(BaseModel):
    """Schema for updating a legal document."""

    title: Optional[str] = Field(None, min_length=1, max_length=200)
    slug: Optional[str] = Field(None, min_length=1, max_length=100)
    version: Optional[str] = Field(None, max_length=20)
    summary: Optional[str] = None
    content: Optional[str] = None
    is_current: Optional[bool] = None
    is_published: Optional[bool] = None
    effective_date: Optional[datetime] = None


class LegalDocumentRead(BaseModel):
    """Schema for reading a legal document."""

    id: UUID
    document_type: LegalDocumentType
    title: str
    slug: str
    version: str
    summary: Optional[str]
    content: Optional[str]
    file_path: Optional[str]
    file_name: Optional[str]
    file_size: Optional[int]
    mime_type: Optional[str]
    is_current: bool
    is_published: bool
    published_at: Optional[datetime]
    effective_date: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LegalDocumentPublic(BaseModel):
    """Public schema for viewing legal documents (limited fields)."""

    title: str
    version: str
    summary: Optional[str]
    content: Optional[str]
    file_path: Optional[str]
    effective_date: Optional[datetime]
    published_at: Optional[datetime]

    model_config = {"from_attributes": True}
