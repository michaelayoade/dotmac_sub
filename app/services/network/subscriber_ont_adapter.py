"""Subscriber-ONT Adapter for unified subscriber-device linking.

This adapter provides a clean interface for managing the relationship between
subscribers, subscriptions, and ONT devices. It abstracts the complexity of:
- OntAssignment management
- CPEDevice lifecycle
- Service address resolution
- Provisioning context resolution

Usage:
    from app.services.network.subscriber_ont_adapter import (
        link_subscriber_to_ont,
        get_subscriber_onts,
        get_ont_subscriber,
        transfer_ont_to_subscriber,
    )

    # Link a subscriber to an ONT
    result = link_subscriber_to_ont(db, subscriber_id, ont_id)

    # Get all ONTs for a subscriber
    onts = get_subscriber_onts(db, subscriber_id)

    # Get the subscriber for an ONT
    subscriber = get_ont_subscriber(db, ont_id)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:

    from app.models.catalog import Subscription
    from app.models.network import OntUnit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result Types
# ---------------------------------------------------------------------------


class LinkResultStatus(str, Enum):
    """Status of a subscriber-ONT link operation."""

    success = "success"
    already_linked = "already_linked"
    ont_not_found = "ont_not_found"
    subscriber_not_found = "subscriber_not_found"
    ont_already_assigned = "ont_already_assigned"
    validation_error = "validation_error"
    error = "error"


@dataclass
class LinkResult:
    """Result of a subscriber-ONT link operation."""

    success: bool
    status: LinkResultStatus
    message: str
    assignment_id: str | None = None
    ont_id: str | None = None
    subscriber_id: str | None = None
    cpe_device_id: str | None = None
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def ok(
        cls,
        message: str,
        *,
        assignment_id: str | None = None,
        ont_id: str | None = None,
        subscriber_id: str | None = None,
        cpe_device_id: str | None = None,
        warnings: list[str] | None = None,
    ) -> LinkResult:
        return cls(
            success=True,
            status=LinkResultStatus.success,
            message=message,
            assignment_id=assignment_id,
            ont_id=ont_id,
            subscriber_id=subscriber_id,
            cpe_device_id=cpe_device_id,
            warnings=warnings or [],
        )

    @classmethod
    def fail(
        cls,
        status: LinkResultStatus,
        message: str,
        *,
        ont_id: str | None = None,
        subscriber_id: str | None = None,
    ) -> LinkResult:
        return cls(
            success=False,
            status=status,
            message=message,
            ont_id=ont_id,
            subscriber_id=subscriber_id,
        )


@dataclass
class SubscriberOntInfo:
    """Information about a subscriber's ONT."""

    ont_id: str
    assignment_id: str
    serial_number: str | None
    model: str | None
    online_status: str | None
    olt_name: str | None
    pon_port: str | None
    service_address: str | None
    assigned_at: datetime | None
    cpe_device_id: str | None = None
    subscription_id: str | None = None  # For future subscription-level binding


@dataclass
class OntSubscriberInfo:
    """Information about an ONT's subscriber."""

    subscriber_id: str
    assignment_id: str
    account_number: str | None
    full_name: str | None
    email: str | None
    phone: str | None
    service_address: str | None
    assigned_at: datetime | None
    subscription_id: str | None = None
    subscription_name: str | None = None


@dataclass
class ProvisioningContext:
    """Resolved context for provisioning operations."""

    subscriber_id: str | None = None
    subscription_id: str | None = None
    ont_id: str | None = None
    ont_serial: str | None = None
    olt_id: str | None = None
    olt_name: str | None = None
    fsp: str | None = None
    ont_id_on_olt: int | None = None
    service_address_id: str | None = None
    nas_device_id: str | None = None

    @property
    def is_complete(self) -> bool:
        """Check if context has minimum required fields for provisioning."""
        return all([
            self.subscriber_id,
            self.ont_id,
            self.olt_id,
            self.fsp,
            self.ont_id_on_olt is not None,
        ])


# ---------------------------------------------------------------------------
# Protocol Definition
# ---------------------------------------------------------------------------


@runtime_checkable
class SubscriberOntLinker(Protocol):
    """Protocol for subscriber-ONT linking operations."""

    def link(
        self,
        db: Session,
        subscriber_id: str,
        ont_id: str,
        *,
        subscription_id: str | None = None,
        service_address_id: str | None = None,
        pon_port_id: str | None = None,
        notes: str | None = None,
    ) -> LinkResult:
        """Link a subscriber to an ONT."""
        ...

    def unlink(
        self,
        db: Session,
        ont_id: str,
        *,
        keep_history: bool = True,
    ) -> LinkResult:
        """Unlink an ONT from its current subscriber."""
        ...

    def transfer(
        self,
        db: Session,
        ont_id: str,
        new_subscriber_id: str,
        *,
        new_subscription_id: str | None = None,
        new_service_address_id: str | None = None,
        notes: str | None = None,
    ) -> LinkResult:
        """Transfer an ONT to a different subscriber."""
        ...

    def get_subscriber_onts(
        self,
        db: Session,
        subscriber_id: str,
        *,
        include_inactive: bool = False,
    ) -> list[SubscriberOntInfo]:
        """Get all ONTs linked to a subscriber."""
        ...

    def get_ont_subscriber(
        self,
        db: Session,
        ont_id: str,
    ) -> OntSubscriberInfo | None:
        """Get the subscriber linked to an ONT."""
        ...

    def resolve_provisioning_context(
        self,
        db: Session,
        *,
        subscriber_id: str | None = None,
        subscription_id: str | None = None,
        ont_id: str | None = None,
    ) -> ProvisioningContext:
        """Resolve full provisioning context from partial identifiers."""
        ...


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


class DefaultSubscriberOntLinker:
    """Default implementation of subscriber-ONT linking operations."""

    def link(
        self,
        db: Session,
        subscriber_id: str,
        ont_id: str,
        *,
        subscription_id: str | None = None,
        service_address_id: str | None = None,
        pon_port_id: str | None = None,
        notes: str | None = None,
    ) -> LinkResult:
        """Link a subscriber to an ONT.

        Creates an OntAssignment and associated CPEDevice. If the ONT is already
        assigned to this subscriber, returns success with already_linked status.

        Args:
            db: Database session.
            subscriber_id: UUID of the subscriber.
            ont_id: UUID of the ONT.
            subscription_id: Optional subscription to associate (for future use).
            service_address_id: Optional service address. If not provided,
                resolves from subscriber's primary address.
            pon_port_id: Optional PON port. If not provided, resolves from
                ONT's discovered board/port or autofind candidate.
            notes: Optional notes for the assignment.

        Returns:
            LinkResult with operation status.
        """
        from app.models.network import OntAssignment, OntUnit
        from app.models.subscriber import Subscriber

        # Validate ONT exists
        ont = db.get(OntUnit, ont_id)
        if not ont:
            return LinkResult.fail(
                LinkResultStatus.ont_not_found,
                f"ONT {ont_id} not found",
                ont_id=ont_id,
            )

        # Validate subscriber exists
        subscriber = db.get(Subscriber, subscriber_id)
        if not subscriber:
            return LinkResult.fail(
                LinkResultStatus.subscriber_not_found,
                f"Subscriber {subscriber_id} not found",
                subscriber_id=subscriber_id,
            )

        # Check for existing active assignment on this ONT
        existing = db.scalars(
            select(OntAssignment).where(
                OntAssignment.ont_unit_id == ont_id,
                OntAssignment.active.is_(True),
            )
        ).first()

        if existing:
            if str(existing.subscriber_id) == subscriber_id:
                # Already linked to this subscriber
                return LinkResult(
                    success=True,
                    status=LinkResultStatus.already_linked,
                    message=f"ONT {ont.serial_number} is already linked to this subscriber",
                    assignment_id=str(existing.id),
                    ont_id=ont_id,
                    subscriber_id=subscriber_id,
                )
            else:
                # Assigned to different subscriber
                return LinkResult.fail(
                    LinkResultStatus.ont_already_assigned,
                    f"ONT {ont.serial_number} is already assigned to another subscriber",
                    ont_id=ont_id,
                    subscriber_id=subscriber_id,
                )

        # Resolve service address if not provided
        resolved_address_id = service_address_id
        if not resolved_address_id:
            resolved_address_id = self._resolve_service_address(db, subscriber_id)

        # Resolve PON port if not provided
        resolved_pon_port_id = pon_port_id
        warnings: list[str] = []
        if not resolved_pon_port_id:
            resolved_pon_port_id, port_warning = self._resolve_pon_port(db, ont)
            if port_warning:
                warnings.append(port_warning)

        # Create assignment
        assignment = OntAssignment(
            ont_unit_id=ont_id,
            subscriber_id=subscriber_id,
            pon_port_id=resolved_pon_port_id,
            service_address_id=resolved_address_id,
            active=True,
            assigned_at=datetime.now(UTC),
            notes=notes,
        )
        db.add(assignment)
        db.flush()

        # Mark ONT as active
        ont.is_active = True
        db.flush()

        # Create/update CPEDevice
        cpe_device_id = self._ensure_cpe_device(
            db,
            ont=ont,
            subscriber_id=subscriber_id,
            service_address_id=resolved_address_id,
            assigned_at=assignment.assigned_at,
        )

        logger.info(
            "Linked subscriber %s to ONT %s (assignment=%s)",
            subscriber_id,
            ont.serial_number,
            assignment.id,
        )

        return LinkResult.ok(
            f"ONT {ont.serial_number} linked to subscriber {subscriber.account_number or subscriber_id}",
            assignment_id=str(assignment.id),
            ont_id=ont_id,
            subscriber_id=subscriber_id,
            cpe_device_id=cpe_device_id,
            warnings=warnings,
        )

    def unlink(
        self,
        db: Session,
        ont_id: str,
        *,
        keep_history: bool = True,
    ) -> LinkResult:
        """Unlink an ONT from its current subscriber.

        Args:
            db: Database session.
            ont_id: UUID of the ONT.
            keep_history: If True, deactivates assignment. If False, deletes it.

        Returns:
            LinkResult with operation status.
        """
        from app.models.network import OntAssignment, OntUnit

        ont = db.get(OntUnit, ont_id)
        if not ont:
            return LinkResult.fail(
                LinkResultStatus.ont_not_found,
                f"ONT {ont_id} not found",
                ont_id=ont_id,
            )

        # Find active assignment
        assignment = db.scalars(
            select(OntAssignment).where(
                OntAssignment.ont_unit_id == ont_id,
                OntAssignment.active.is_(True),
            )
        ).first()

        if not assignment:
            return LinkResult.fail(
                LinkResultStatus.validation_error,
                f"ONT {ont.serial_number} has no active assignment",
                ont_id=ont_id,
            )

        subscriber_id = str(assignment.subscriber_id) if assignment.subscriber_id else None

        if keep_history:
            # Deactivate assignment
            assignment.active = False
            db.flush()
        else:
            # Delete assignment
            db.delete(assignment)
            db.flush()

        # Update ONT status
        ont.is_active = False
        db.flush()

        logger.info(
            "Unlinked ONT %s from subscriber %s (keep_history=%s)",
            ont.serial_number,
            subscriber_id,
            keep_history,
        )

        return LinkResult.ok(
            f"ONT {ont.serial_number} unlinked from subscriber",
            ont_id=ont_id,
            subscriber_id=subscriber_id,
        )

    def transfer(
        self,
        db: Session,
        ont_id: str,
        new_subscriber_id: str,
        *,
        new_subscription_id: str | None = None,
        new_service_address_id: str | None = None,
        notes: str | None = None,
    ) -> LinkResult:
        """Transfer an ONT to a different subscriber.

        Deactivates the current assignment and creates a new one for the
        new subscriber. Maintains assignment history.

        Args:
            db: Database session.
            ont_id: UUID of the ONT.
            new_subscriber_id: UUID of the new subscriber.
            new_subscription_id: Optional subscription for the new assignment.
            new_service_address_id: Optional service address for new subscriber.
            notes: Optional notes for the new assignment.

        Returns:
            LinkResult with operation status.
        """
        from app.models.network import OntUnit
        from app.models.subscriber import Subscriber

        ont = db.get(OntUnit, ont_id)
        if not ont:
            return LinkResult.fail(
                LinkResultStatus.ont_not_found,
                f"ONT {ont_id} not found",
                ont_id=ont_id,
            )

        new_subscriber = db.get(Subscriber, new_subscriber_id)
        if not new_subscriber:
            return LinkResult.fail(
                LinkResultStatus.subscriber_not_found,
                f"Subscriber {new_subscriber_id} not found",
                subscriber_id=new_subscriber_id,
            )

        # Get current assignment to preserve PON port
        from app.models.network import OntAssignment

        current = db.scalars(
            select(OntAssignment).where(
                OntAssignment.ont_unit_id == ont_id,
                OntAssignment.active.is_(True),
            )
        ).first()

        old_subscriber_id = str(current.subscriber_id) if current and current.subscriber_id else None
        pon_port_id = str(current.pon_port_id) if current and current.pon_port_id else None

        # Unlink from current subscriber (if any)
        if current:
            current.active = False
            db.flush()

        # Link to new subscriber
        result = self.link(
            db,
            new_subscriber_id,
            ont_id,
            subscription_id=new_subscription_id,
            service_address_id=new_service_address_id,
            pon_port_id=pon_port_id,
            notes=notes or f"Transferred from subscriber {old_subscriber_id}",
        )

        if result.success:
            logger.info(
                "Transferred ONT %s from subscriber %s to %s",
                ont.serial_number,
                old_subscriber_id,
                new_subscriber_id,
            )

        return result

    def get_subscriber_onts(
        self,
        db: Session,
        subscriber_id: str,
        *,
        include_inactive: bool = False,
    ) -> list[SubscriberOntInfo]:
        """Get all ONTs linked to a subscriber.

        Args:
            db: Database session.
            subscriber_id: UUID of the subscriber.
            include_inactive: If True, includes historical (inactive) assignments.

        Returns:
            List of SubscriberOntInfo objects.
        """
        from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort

        query = select(OntAssignment).where(
            OntAssignment.subscriber_id == subscriber_id,
        )
        if not include_inactive:
            query = query.where(OntAssignment.active.is_(True))

        query = query.order_by(OntAssignment.assigned_at.desc())
        assignments = db.scalars(query).all()

        results: list[SubscriberOntInfo] = []
        for assignment in assignments:
            ont = db.get(OntUnit, str(assignment.ont_unit_id))
            if not ont:
                continue

            # Get OLT info
            olt_name = None
            if ont.olt_id:
                olt = db.get(OLTDevice, str(ont.olt_id))
                olt_name = olt.name if olt else None

            # Get PON port info
            pon_port_str = None
            if assignment.pon_port_id:
                pon_port = db.get(PonPort, str(assignment.pon_port_id))
                pon_port_str = pon_port.name if pon_port else None

            # Get service address
            service_address_str = None
            if assignment.service_address_id:
                service_address_str = self._format_address(db, str(assignment.service_address_id))

            # Get CPE device if exists
            cpe_device_id = self._find_cpe_device_id(db, ont.serial_number)

            results.append(
                SubscriberOntInfo(
                    ont_id=str(ont.id),
                    assignment_id=str(assignment.id),
                    serial_number=ont.serial_number,
                    model=ont.model,
                    online_status=ont.online_status.value if ont.online_status else None,
                    olt_name=olt_name,
                    pon_port=pon_port_str,
                    service_address=service_address_str,
                    assigned_at=assignment.assigned_at,
                    cpe_device_id=cpe_device_id,
                )
            )

        return results

    def get_ont_subscriber(
        self,
        db: Session,
        ont_id: str,
    ) -> OntSubscriberInfo | None:
        """Get the subscriber linked to an ONT.

        Args:
            db: Database session.
            ont_id: UUID of the ONT.

        Returns:
            OntSubscriberInfo if ONT is assigned, None otherwise.
        """
        from app.models.network import OntAssignment
        from app.models.subscriber import Subscriber

        assignment = db.scalars(
            select(OntAssignment).where(
                OntAssignment.ont_unit_id == ont_id,
                OntAssignment.active.is_(True),
            )
        ).first()

        if not assignment or not assignment.subscriber_id:
            return None

        subscriber = db.get(Subscriber, str(assignment.subscriber_id))
        if not subscriber:
            return None

        # Format full name
        full_name = " ".join(
            filter(None, [subscriber.first_name, subscriber.last_name])
        ) or None

        # Get service address
        service_address_str = None
        if assignment.service_address_id:
            service_address_str = self._format_address(db, str(assignment.service_address_id))

        # Get active subscription (if any)
        subscription_id = None
        subscription_name = None
        active_sub = self._get_active_subscription(db, str(subscriber.id))
        if active_sub:
            subscription_id = str(active_sub.id)
            subscription_name = active_sub.catalog_offer.name if active_sub.catalog_offer else None

        return OntSubscriberInfo(
            subscriber_id=str(subscriber.id),
            assignment_id=str(assignment.id),
            account_number=subscriber.account_number,
            full_name=full_name,
            email=subscriber.email,
            phone=subscriber.phone,
            service_address=service_address_str,
            assigned_at=assignment.assigned_at,
            subscription_id=subscription_id,
            subscription_name=subscription_name,
        )

    def resolve_provisioning_context(
        self,
        db: Session,
        *,
        subscriber_id: str | None = None,
        subscription_id: str | None = None,
        ont_id: str | None = None,
    ) -> ProvisioningContext:
        """Resolve full provisioning context from partial identifiers.

        Given any combination of subscriber_id, subscription_id, or ont_id,
        resolves the complete context needed for provisioning operations.

        Resolution order:
        1. If ont_id provided, resolve subscriber from ONT's assignment
        2. If subscription_id provided, resolve subscriber from subscription
        3. If subscriber_id provided, resolve ONT from subscriber's active assignment
        4. Resolve OLT context from ONT

        Args:
            db: Database session.
            subscriber_id: Optional subscriber UUID.
            subscription_id: Optional subscription UUID.
            ont_id: Optional ONT UUID.

        Returns:
            ProvisioningContext with resolved fields (may be partial if resolution fails).
        """
        from app.models.catalog import Subscription
        from app.models.network import OLTDevice, OntAssignment, OntUnit

        context = ProvisioningContext()

        # Start with provided IDs
        resolved_subscriber_id = subscriber_id
        resolved_subscription_id = subscription_id
        resolved_ont_id = ont_id

        # If subscription provided, get subscriber
        if resolved_subscription_id and not resolved_subscriber_id:
            subscription = db.get(Subscription, resolved_subscription_id)
            if subscription:
                resolved_subscriber_id = str(subscription.subscriber_id)
                context.subscription_id = resolved_subscription_id
                # Also get NAS device if configured
                if subscription.provisioning_nas_device_id:
                    context.nas_device_id = str(subscription.provisioning_nas_device_id)

        # If ONT provided, get subscriber from assignment
        if resolved_ont_id and not resolved_subscriber_id:
            assignment = db.scalars(
                select(OntAssignment).where(
                    OntAssignment.ont_unit_id == resolved_ont_id,
                    OntAssignment.active.is_(True),
                )
            ).first()
            if assignment and assignment.subscriber_id:
                resolved_subscriber_id = str(assignment.subscriber_id)
                if assignment.service_address_id:
                    context.service_address_id = str(assignment.service_address_id)

        # If subscriber provided but no ONT, get ONT from assignment
        if resolved_subscriber_id and not resolved_ont_id:
            assignment = db.scalars(
                select(OntAssignment).where(
                    OntAssignment.subscriber_id == resolved_subscriber_id,
                    OntAssignment.active.is_(True),
                )
            ).first()
            if assignment:
                resolved_ont_id = str(assignment.ont_unit_id)
                if assignment.service_address_id:
                    context.service_address_id = str(assignment.service_address_id)

        # Set resolved IDs
        context.subscriber_id = resolved_subscriber_id
        if not context.subscription_id:
            context.subscription_id = resolved_subscription_id
        context.ont_id = resolved_ont_id

        # Resolve ONT details
        if resolved_ont_id:
            ont = db.get(OntUnit, resolved_ont_id)
            if ont:
                context.ont_serial = ont.serial_number
                context.olt_id = str(ont.olt_id) if ont.olt_id else None

                # Get OLT name
                if context.olt_id:
                    olt = db.get(OLTDevice, context.olt_id)
                    context.olt_name = olt.name if olt else None

                # Resolve FSP and ONT ID on OLT
                context.fsp, context.ont_id_on_olt = self._resolve_ont_olt_position(db, ont)

        # If we have subscriber but no subscription, get active subscription
        if resolved_subscriber_id and not context.subscription_id:
            active_sub = self._get_active_subscription(db, resolved_subscriber_id)
            if active_sub:
                context.subscription_id = str(active_sub.id)
                if active_sub.provisioning_nas_device_id and not context.nas_device_id:
                    context.nas_device_id = str(active_sub.provisioning_nas_device_id)

        return context

    # ---------------------------------------------------------------------------
    # Private Helper Methods
    # ---------------------------------------------------------------------------

    def _resolve_service_address(
        self,
        db: Session,
        subscriber_id: str,
    ) -> str | None:
        """Resolve primary service address for a subscriber."""
        from app.models.subscriber import Address

        # Try to find primary address
        address = db.scalars(
            select(Address).where(
                Address.subscriber_id == subscriber_id,
                Address.is_primary.is_(True),
            )
        ).first()

        if address:
            return str(address.id)

        # Fall back to any address
        address = db.scalars(
            select(Address).where(
                Address.subscriber_id == subscriber_id,
            ).limit(1)
        ).first()

        return str(address.id) if address else None

    def _resolve_pon_port(
        self,
        db: Session,
        ont: OntUnit,
    ) -> tuple[str | None, str | None]:
        """Resolve PON port for an ONT.

        Returns:
            (pon_port_id, warning_message)
        """
        from app.models.network import OltAutofindCandidate, PonPort

        # Try ONT's discovered board/port
        if ont.board is not None and ont.port is not None and ont.olt_id:
            port_name = f"{ont.board}/{ont.port}"
            pon_port = db.scalars(
                select(PonPort).where(
                    PonPort.olt_id == ont.olt_id,
                    PonPort.name.ilike(f"%{port_name}%"),
                )
            ).first()
            if pon_port:
                return str(pon_port.id), None

        # Try autofind candidate
        if ont.serial_number:
            candidate = db.scalars(
                select(OltAutofindCandidate).where(
                    OltAutofindCandidate.serial_number == ont.serial_number,
                    OltAutofindCandidate.status == "pending",
                ).order_by(OltAutofindCandidate.last_seen_at.desc())
            ).first()
            if candidate and candidate.pon_port_id:
                return str(candidate.pon_port_id), None

        return None, "PON port could not be auto-resolved; assignment created without port binding"

    def _ensure_cpe_device(
        self,
        db: Session,
        *,
        ont: OntUnit,
        subscriber_id: str,
        service_address_id: str | None,
        assigned_at: datetime | None,
    ) -> str | None:
        """Create or update CPEDevice for the ONT assignment."""
        from app.models.network import CPEDevice

        if not ont.serial_number:
            return None

        # Find existing CPE by serial
        cpe = db.scalars(
            select(CPEDevice).where(
                CPEDevice.serial_number == ont.serial_number,
            )
        ).first()

        if cpe:
            # Update existing
            cpe.subscriber_id = subscriber_id
            cpe.service_address_id = service_address_id
            if assigned_at:
                cpe.installed_at = assigned_at
            db.flush()
            return str(cpe.id)
        else:
            # Create new
            cpe = CPEDevice(
                serial_number=ont.serial_number,
                subscriber_id=subscriber_id,
                service_address_id=service_address_id,
                device_type="ont",
                model=ont.model,
                manufacturer=ont.manufacturer,
                installed_at=assigned_at,
            )
            db.add(cpe)
            db.flush()
            return str(cpe.id)

    def _find_cpe_device_id(
        self,
        db: Session,
        serial_number: str | None,
    ) -> str | None:
        """Find CPEDevice ID by serial number."""
        if not serial_number:
            return None

        from app.models.network import CPEDevice

        cpe = db.scalars(
            select(CPEDevice).where(
                CPEDevice.serial_number == serial_number,
            )
        ).first()

        return str(cpe.id) if cpe else None

    def _format_address(
        self,
        db: Session,
        address_id: str,
    ) -> str | None:
        """Format an address for display."""
        from app.models.subscriber import Address

        address = db.get(Address, address_id)
        if not address:
            return None

        parts = [
            address.street_address,
            address.city,
            address.state,
        ]
        return ", ".join(filter(None, parts)) or None

    def _get_active_subscription(
        self,
        db: Session,
        subscriber_id: str,
    ) -> Subscription | None:
        """Get active subscription for a subscriber."""
        from app.models.catalog import Subscription, SubscriptionStatus

        return db.scalars(
            select(Subscription).where(
                Subscription.subscriber_id == subscriber_id,
                Subscription.status == SubscriptionStatus.active,
            ).order_by(Subscription.created_at.desc())
        ).first()

    def _resolve_ont_olt_position(
        self,
        db: Session,
        ont: OntUnit,
    ) -> tuple[str | None, int | None]:
        """Resolve ONT's position on OLT (FSP and ONT ID).

        Returns:
            (fsp, ont_id_on_olt)
        """
        import re

        # Try board/port for FSP
        fsp = None
        if ont.board is not None and ont.port is not None:
            fsp = f"0/{ont.board}/{ont.port}"

        # Parse ONT ID from external_id
        ont_id_on_olt = None
        if ont.external_id:
            ext = ont.external_id.strip()
            if ext.isdigit():
                ont_id_on_olt = int(ext)
            else:
                # Try patterns like "huawei:4194320640.5" or "generic:5"
                match = re.match(r"^(?:[a-z0-9_-]+:)?(?:\d+\.)*(\d+)$", ext, re.IGNORECASE)
                if match:
                    ont_id_on_olt = int(match.group(1))
                elif "." in ext:
                    dot_part = ext.rsplit(".", 1)[-1]
                    if dot_part.isdigit():
                        ont_id_on_olt = int(dot_part)

        return fsp, ont_id_on_olt


# ---------------------------------------------------------------------------
# Module-Level Functions (Convenience API)
# ---------------------------------------------------------------------------

_default_linker: SubscriberOntLinker | None = None


def get_subscriber_ont_linker() -> SubscriberOntLinker:
    """Get the default subscriber-ONT linker instance."""
    global _default_linker
    if _default_linker is None:
        _default_linker = DefaultSubscriberOntLinker()
    return _default_linker


def set_subscriber_ont_linker(linker: SubscriberOntLinker) -> None:
    """Set a custom subscriber-ONT linker (for testing)."""
    global _default_linker
    _default_linker = linker


def link_subscriber_to_ont(
    db: Session,
    subscriber_id: str,
    ont_id: str,
    *,
    subscription_id: str | None = None,
    service_address_id: str | None = None,
    pon_port_id: str | None = None,
    notes: str | None = None,
) -> LinkResult:
    """Link a subscriber to an ONT.

    Convenience function that delegates to the default linker.
    """
    return get_subscriber_ont_linker().link(
        db,
        subscriber_id,
        ont_id,
        subscription_id=subscription_id,
        service_address_id=service_address_id,
        pon_port_id=pon_port_id,
        notes=notes,
    )


def unlink_ont(
    db: Session,
    ont_id: str,
    *,
    keep_history: bool = True,
) -> LinkResult:
    """Unlink an ONT from its current subscriber.

    Convenience function that delegates to the default linker.
    """
    return get_subscriber_ont_linker().unlink(db, ont_id, keep_history=keep_history)


def transfer_ont_to_subscriber(
    db: Session,
    ont_id: str,
    new_subscriber_id: str,
    *,
    new_subscription_id: str | None = None,
    new_service_address_id: str | None = None,
    notes: str | None = None,
) -> LinkResult:
    """Transfer an ONT to a different subscriber.

    Convenience function that delegates to the default linker.
    """
    return get_subscriber_ont_linker().transfer(
        db,
        ont_id,
        new_subscriber_id,
        new_subscription_id=new_subscription_id,
        new_service_address_id=new_service_address_id,
        notes=notes,
    )


def get_subscriber_onts(
    db: Session,
    subscriber_id: str,
    *,
    include_inactive: bool = False,
) -> list[SubscriberOntInfo]:
    """Get all ONTs linked to a subscriber.

    Convenience function that delegates to the default linker.
    """
    return get_subscriber_ont_linker().get_subscriber_onts(
        db,
        subscriber_id,
        include_inactive=include_inactive,
    )


def get_ont_subscriber(
    db: Session,
    ont_id: str,
) -> OntSubscriberInfo | None:
    """Get the subscriber linked to an ONT.

    Convenience function that delegates to the default linker.
    """
    return get_subscriber_ont_linker().get_ont_subscriber(db, ont_id)


def resolve_provisioning_context(
    db: Session,
    *,
    subscriber_id: str | None = None,
    subscription_id: str | None = None,
    ont_id: str | None = None,
) -> ProvisioningContext:
    """Resolve full provisioning context from partial identifiers.

    Convenience function that delegates to the default linker.
    """
    return get_subscriber_ont_linker().resolve_provisioning_context(
        db,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        ont_id=ont_id,
    )


# ---------------------------------------------------------------------------
# Quick Lookup Functions
# ---------------------------------------------------------------------------


def is_ont_assigned(db: Session, ont_id: str) -> bool:
    """Check if an ONT has an active assignment."""
    from app.models.network import OntAssignment

    return db.scalars(
        select(OntAssignment.id).where(
            OntAssignment.ont_unit_id == ont_id,
            OntAssignment.active.is_(True),
        ).limit(1)
    ).first() is not None


def get_subscriber_ont_count(db: Session, subscriber_id: str) -> int:
    """Get count of active ONTs for a subscriber."""
    from sqlalchemy import func

    from app.models.network import OntAssignment

    return db.scalar(
        select(func.count(OntAssignment.id)).where(
            OntAssignment.subscriber_id == subscriber_id,
            OntAssignment.active.is_(True),
        )
    ) or 0


def find_ont_by_subscriber_and_address(
    db: Session,
    subscriber_id: str,
    service_address_id: str,
) -> str | None:
    """Find ONT ID for a subscriber at a specific address."""
    from app.models.network import OntAssignment

    assignment = db.scalars(
        select(OntAssignment).where(
            OntAssignment.subscriber_id == subscriber_id,
            OntAssignment.service_address_id == service_address_id,
            OntAssignment.active.is_(True),
        )
    ).first()

    return str(assignment.ont_unit_id) if assignment else None
