"""Service for contract signatures (click-to-sign workflow)."""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.contracts import ContractSignature
from app.models.legal import LegalDocument, LegalDocumentType
from app.models.provisioning import ServiceOrder
from app.models.subscriber import SubscriberAccount
from app.schemas.contracts import ContractSignatureCreate
from app.services.common import coerce_uuid


class ContractSignatures:
    """Service for managing contract signatures."""

    @staticmethod
    def create(db: Session, payload: ContractSignatureCreate) -> ContractSignature:
        """Create a new contract signature record.

        Args:
            db: Database session
            payload: Contract signature data

        Returns:
            Created ContractSignature

        Raises:
            HTTPException: If account not found
        """
        # Validate account exists
        account = db.get(SubscriberAccount, payload.account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")

        # Validate service order if provided
        if payload.service_order_id:
            service_order = db.get(ServiceOrder, payload.service_order_id)
            if not service_order:
                raise HTTPException(status_code=404, detail="Service order not found")

        data = payload.model_dump()
        if not data.get("signed_at"):
            data["signed_at"] = datetime.now(timezone.utc)

        signature = ContractSignature(**data)
        db.add(signature)
        db.commit()
        db.refresh(signature)
        return signature

    @staticmethod
    def get(db: Session, signature_id: str) -> ContractSignature:
        """Get a contract signature by ID.

        Args:
            db: Database session
            signature_id: Signature ID

        Returns:
            ContractSignature

        Raises:
            HTTPException: If signature not found
        """
        signature = db.get(ContractSignature, coerce_uuid(signature_id))
        if not signature:
            raise HTTPException(status_code=404, detail="Contract signature not found")
        return signature

    @staticmethod
    def get_for_service_order(
        db: Session, service_order_id: str
    ) -> ContractSignature | None:
        """Get the contract signature for a service order.

        Args:
            db: Database session
            service_order_id: Service order ID

        Returns:
            ContractSignature or None if not signed
        """
        return (
            db.query(ContractSignature)
            .filter(ContractSignature.service_order_id == coerce_uuid(service_order_id))
            .filter(ContractSignature.is_active.is_(True))
            .order_by(ContractSignature.signed_at.desc())
            .first()
        )

    @staticmethod
    def is_signed(db: Session, service_order_id: str) -> bool:
        """Check if a service order has a signed contract.

        Args:
            db: Database session
            service_order_id: Service order ID

        Returns:
            True if signed, False otherwise
        """
        signature = ContractSignatures.get_for_service_order(db, service_order_id)
        return signature is not None

    @staticmethod
    def list_for_account(
        db: Session, account_id: str, limit: int = 100, offset: int = 0
    ) -> list[ContractSignature]:
        """List all contract signatures for an account.

        Args:
            db: Database session
            account_id: Account ID
            limit: Max results to return
            offset: Number of results to skip

        Returns:
            List of ContractSignature objects
        """
        return (
            db.query(ContractSignature)
            .filter(ContractSignature.account_id == coerce_uuid(account_id))
            .filter(ContractSignature.is_active.is_(True))
            .order_by(ContractSignature.signed_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_contract_template(
        db: Session, document_type: LegalDocumentType = LegalDocumentType.terms_of_service
    ) -> LegalDocument | None:
        """Get the current published contract template.

        Args:
            db: Database session
            document_type: Type of legal document to retrieve

        Returns:
            LegalDocument or None if not found
        """
        return (
            db.query(LegalDocument)
            .filter(LegalDocument.document_type == document_type)
            .filter(LegalDocument.is_current.is_(True))
            .filter(LegalDocument.is_published.is_(True))
            .first()
        )


contract_signatures = ContractSignatures()
