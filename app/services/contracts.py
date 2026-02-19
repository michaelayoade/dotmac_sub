"""Service for contract signatures (click-to-sign workflow)."""

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.contracts import ContractSignature
from app.models.legal import LegalDocument, LegalDocumentType
from app.models.provisioning import ServiceOrder
from app.models.subscriber import Subscriber
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
        account = db.get(Subscriber, payload.account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")

        # Validate service order if provided
        if payload.service_order_id:
            service_order = db.get(ServiceOrder, payload.service_order_id)
            if not service_order:
                raise HTTPException(status_code=404, detail="Service order not found")

        data = payload.model_dump()
        if not data.get("signed_at"):
            data["signed_at"] = datetime.now(UTC)

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


    @staticmethod
    def get_contract_context(
        db: Session, order_id: str, account_id: str | None
    ) -> dict:
        """Build template context for the contract signing page.

        Returns dict with service_order, contract_html, document_id,
        or raises HTTPException for not-found / access-denied.
        Returns {"redirect": url} if already signed.
        """
        service_order = db.get(ServiceOrder, coerce_uuid(order_id))
        if not service_order:
            raise HTTPException(status_code=404, detail="Service order not found")

        if account_id and str(service_order.subscriber_id) != str(account_id):
            raise HTTPException(status_code=403, detail="Access denied")

        existing = ContractSignatures.get_for_service_order(db, order_id)
        if existing:
            return {"redirect": f"/portal/service-orders/{order_id}?signed=true"}

        contract_template = ContractSignatures.get_contract_template(
            db, LegalDocumentType.terms_of_service
        )
        if contract_template and contract_template.content:
            contract_html = contract_template.content
        else:
            contract_html = (
                "<h2>Service Agreement</h2>"
                "<p>By signing this agreement, you acknowledge and accept "
                "the following terms:</p>"
                "<ol>"
                "<li>You agree to pay all service fees as invoiced.</li>"
                "<li>Service is provided on a month-to-month basis unless "
                "otherwise specified.</li>"
                "<li>Either party may terminate with 30 days written notice.</li>"
                "<li>You agree to use the service in accordance with "
                "applicable laws.</li>"
                "</ol>"
                "<p>This agreement is effective upon signing and remains "
                "in effect until terminated.</p>"
            )

        return {
            "service_order": service_order,
            "contract_html": contract_html,
            "document_id": str(contract_template.id) if contract_template else None,
        }

    @staticmethod
    def sign_contract_for_customer(
        db: Session,
        order_id: str,
        account_id: str | None,
        signer_name: str,
        signer_email: str,
        ip_address: str,
        user_agent: str,
    ) -> ContractSignature | str:
        """Process contract signing for a customer portal user.

        Returns the created ContractSignature, or a redirect URL string
        if the contract was already signed.
        """
        service_order = db.get(ServiceOrder, coerce_uuid(order_id))
        if not service_order:
            raise HTTPException(status_code=404, detail="Service order not found")

        if account_id and str(service_order.subscriber_id) != str(account_id):
            raise HTTPException(status_code=403, detail="Access denied")

        existing = ContractSignatures.get_for_service_order(db, order_id)
        if existing:
            return f"/portal/service-orders/{order_id}?already_signed=true"

        contract_template = ContractSignatures.get_contract_template(
            db, LegalDocumentType.terms_of_service
        )
        agreement_text = ""
        document_id = None
        if contract_template:
            agreement_text = contract_template.content or ""
            document_id = contract_template.id
        else:
            agreement_text = "Default service agreement terms accepted."

        return ContractSignatures.create(
            db,
            ContractSignatureCreate(
                account_id=service_order.subscriber_id,
                service_order_id=service_order.id,
                document_id=document_id,
                signer_name=signer_name.strip(),
                signer_email=signer_email.strip(),
                ip_address=ip_address,
                user_agent=user_agent[:500] if user_agent else None,
                agreement_text=agreement_text,
                signed_at=datetime.now(UTC),
            ),
        )


contract_signatures = ContractSignatures()
