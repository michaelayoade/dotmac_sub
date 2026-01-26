"""Pydantic schemas for contract signatures."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, EmailStr


class ContractSignatureCreate(BaseModel):
    """Schema for creating a contract signature."""

    account_id: UUID
    service_order_id: Optional[UUID] = None
    document_id: Optional[UUID] = None
    signer_name: str = Field(..., min_length=1, max_length=200)
    signer_email: EmailStr
    ip_address: str = Field(..., min_length=1, max_length=45)
    user_agent: Optional[str] = Field(None, max_length=500)
    agreement_text: str = Field(..., min_length=1)
    signed_at: Optional[datetime] = None  # Defaults to now if not provided


class ContractSignatureRead(BaseModel):
    """Schema for reading a contract signature."""

    id: UUID
    account_id: UUID
    service_order_id: Optional[UUID]
    document_id: Optional[UUID]
    signer_name: str
    signer_email: str
    signed_at: datetime
    ip_address: str
    user_agent: Optional[str]
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
