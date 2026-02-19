"""Pydantic schemas for contract signatures."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class ContractSignatureCreate(BaseModel):
    """Schema for creating a contract signature."""

    account_id: UUID
    service_order_id: UUID | None = None
    document_id: UUID | None = None
    signer_name: str = Field(..., min_length=1, max_length=200)
    signer_email: EmailStr
    ip_address: str = Field(..., min_length=1, max_length=45)
    user_agent: str | None = Field(None, max_length=500)
    agreement_text: str = Field(..., min_length=1)
    signed_at: datetime | None = None  # Defaults to now if not provided


class ContractSignatureRead(BaseModel):
    """Schema for reading a contract signature."""

    id: UUID
    account_id: UUID
    service_order_id: UUID | None
    document_id: UUID | None
    signer_name: str
    signer_email: str
    signed_at: datetime
    ip_address: str
    user_agent: str | None
    agreement_text: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContractSignRequest(BaseModel):
    """Schema for the contract signing form submission."""

    signer_name: str = Field(..., min_length=1, max_length=200)
    signer_email: EmailStr
    agree: bool = Field(..., description="Must be True to sign")
