"""Offer-AddOn link management services.

Provides services for managing the many-to-many relationship between
CatalogOffers and AddOns via the OfferAddOn model.
"""

from __future__ import annotations

import builtins
import uuid
from typing import Any, Optional, cast

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import OfferAddOn, AddOn, CatalogOffer
from app.services.common import apply_ordering, apply_pagination
from app.services.query_builders import apply_optional_equals
from app.services.response import ListResponseMixin


class OfferAddOns(ListResponseMixin):
    """Service for managing offer-addon links."""

    @staticmethod
    def list(
        db: Session,
        offer_id: Optional[str] = None,
        add_on_id: Optional[str] = None,
        order_by: str = "add_on_id",
        order_dir: str = "asc",
        limit: int = 100,
        offset: int = 0,
    ) -> builtins.list[OfferAddOn]:
        """List offer-addon links with optional filters."""
        query = db.query(OfferAddOn).options(joinedload(OfferAddOn.add_on))
        query = apply_optional_equals(
            query,
            {
                OfferAddOn.offer_id: offer_id,
                OfferAddOn.add_on_id: add_on_id,
            },
        )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "add_on_id": OfferAddOn.add_on_id,
                "offer_id": OfferAddOn.offer_id,
            },
        )
        return cast(list[OfferAddOn], apply_pagination(query, limit, offset).all())

    @staticmethod
    def get(db: Session, link_id: str) -> OfferAddOn:
        """Get a specific offer-addon link by ID."""
        link = db.get(OfferAddOn, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="Offer-addon link not found")
        return link

    @staticmethod
    def get_by_offer_and_addon(
        db: Session, offer_id: str, add_on_id: str
    ) -> Optional[OfferAddOn]:
        """Get a link by offer and addon IDs."""
        return (
            db.query(OfferAddOn)
            .filter(OfferAddOn.offer_id == offer_id)
            .filter(OfferAddOn.add_on_id == add_on_id)
            .first()
        )

    @staticmethod
    def create(
        db: Session,
        offer_id: str,
        add_on_id: str,
        is_required: bool = False,
        min_quantity: Optional[int] = None,
        max_quantity: Optional[int] = None,
        commit: bool = True,
    ) -> OfferAddOn:
        """Create a new offer-addon link."""
        # Verify offer exists
        offer = db.get(CatalogOffer, offer_id)
        if not offer:
            raise HTTPException(status_code=404, detail="Offer not found")

        # Verify addon exists
        addon = db.get(AddOn, add_on_id)
        if not addon:
            raise HTTPException(status_code=404, detail="Add-on not found")

        # Check for existing link
        existing = OfferAddOns.get_by_offer_and_addon(db, offer_id, add_on_id)
        if existing:
            raise HTTPException(
                status_code=400,
                detail="This add-on is already linked to the offer",
            )

        link = OfferAddOn(
            offer_id=uuid.UUID(offer_id) if isinstance(offer_id, str) else offer_id,
            add_on_id=uuid.UUID(add_on_id) if isinstance(add_on_id, str) else add_on_id,
            is_required=is_required,
            min_quantity=min_quantity,
            max_quantity=max_quantity,
        )
        db.add(link)
        if commit:
            db.commit()
            db.refresh(link)
        return link

    @staticmethod
    def update(
        db: Session,
        link_id: str,
        is_required: Optional[bool] = None,
        min_quantity: Optional[int] = None,
        max_quantity: Optional[int] = None,
        commit: bool = True,
    ) -> OfferAddOn:
        """Update an offer-addon link."""
        link = db.get(OfferAddOn, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="Offer-addon link not found")

        if is_required is not None:
            link.is_required = is_required
        if min_quantity is not None:
            link.min_quantity = min_quantity
        if max_quantity is not None:
            link.max_quantity = max_quantity

        if commit:
            db.commit()
            db.refresh(link)
        return link

    @staticmethod
    def delete(db: Session, link_id: str, commit: bool = True) -> bool:
        """Delete an offer-addon link."""
        link = db.get(OfferAddOn, link_id)
        if not link:
            return False
        db.delete(link)
        if commit:
            db.commit()
        return True

    @staticmethod
    def sync(
        db: Session,
        offer_id: str,
        addon_configs: builtins.list[dict[str, Any]],
        commit: bool = True,
    ) -> builtins.list[OfferAddOn]:
        """
        Sync offer-addon links based on provided configurations.

        This bulk operation replaces all existing links for an offer
        with the new set of configurations.

        Args:
            db: Database session
            offer_id: The offer ID to sync links for
            addon_configs: List of dicts with keys:
                - add_on_id: UUID of the add-on
                - is_required: bool (default False)
                - min_quantity: int or None
                - max_quantity: int or None
            commit: Whether to commit the transaction

        Returns:
            List of created OfferAddOn links
        """
        # Verify offer exists
        offer = db.get(CatalogOffer, offer_id)
        if not offer:
            raise HTTPException(status_code=404, detail="Offer not found")

        # Get existing links for this offer
        existing_links = OfferAddOns.list(db, offer_id=offer_id, limit=1000)
        existing_addon_ids = {str(link.add_on_id) for link in existing_links}
        existing_link_map = {str(link.add_on_id): link for link in existing_links}

        # Process new configurations
        new_addon_ids = set()
        result_links = []

        for config in addon_configs:
            add_on_id = str(config.get("add_on_id", ""))
            if not add_on_id:
                continue

            new_addon_ids.add(add_on_id)
            is_required = config.get("is_required", False)
            min_quantity = config.get("min_quantity")
            max_quantity = config.get("max_quantity")

            if add_on_id in existing_addon_ids:
                # Update existing link
                link = existing_link_map[add_on_id]
                link.is_required = is_required
                link.min_quantity = min_quantity
                link.max_quantity = max_quantity
                result_links.append(link)
            else:
                # Create new link (skip validation since we're syncing)
                addon = db.get(AddOn, add_on_id)
                if not addon:
                    continue  # Skip invalid addon IDs

                link = OfferAddOn(
                    offer_id=uuid.UUID(offer_id) if isinstance(offer_id, str) else offer_id,
                    add_on_id=uuid.UUID(add_on_id),
                    is_required=is_required,
                    min_quantity=min_quantity,
                    max_quantity=max_quantity,
                )
                db.add(link)
                result_links.append(link)

        # Delete links that are no longer in the new set
        for addon_id in existing_addon_ids - new_addon_ids:
            link = existing_link_map[addon_id]
            db.delete(link)

        if commit:
            db.commit()
            for link in result_links:
                db.refresh(link)

        return result_links
