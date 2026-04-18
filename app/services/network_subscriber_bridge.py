"""Bridge adapter between the network domain and the subscriber domain.

The ``app.services.network`` package is forbidden (via import-linter) from
importing ``app.models.subscriber`` directly. This module lives OUTSIDE the
``network`` package so it can freely import from both domains and provide a
concrete ``SubscriberValidator`` implementation to network services that need
subscriber integration at runtime.

Network services should accept the bridge (or any object implementing the
``SubscriberValidator`` protocol) via their constructor and fall back to a
no-op / standalone path when one is not supplied.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import Select

from app.models.subscriber import Subscriber
from app.validators import network as network_validators

logger = logging.getLogger(__name__)

__all__ = ["DefaultSubscriberValidator", "default_subscriber_validator"]


class DefaultSubscriberValidator:
    """Default ``SubscriberValidator`` implementation using the ORM.

    Delegates existence and address-link checks to ``app.validators.network``
    and performs ONT search joins against the ``Subscriber`` model. Intended
    to be wired into ``app.services.network.olt`` as the production validator.
    """

    def validate_assignment_customer_links(
        self,
        db: Session,
        *,
        subscriber_id: object | None,
        service_address_id: object | None,
    ) -> None:
        """Mirror of the legacy ``_validate_assignment_customer_links`` helper."""
        from fastapi import HTTPException

        if subscriber_id is None:
            if service_address_id is not None:
                raise HTTPException(
                    status_code=400,
                    detail="Service address requires a subscriber",
                )
            return
        network_validators.validate_cpe_device_links(
            db,
            str(subscriber_id),
            str(service_address_id) if service_address_id is not None else None,
        )

    def augment_ont_search(
        self,
        stmt: Select,
        term: str,
        *,
        assignment_alias: Any,
    ) -> tuple[Select, Sequence[Any]]:
        """Add a ``Subscriber`` outer-join to the ONT search query.

        Returns the augmented statement and a list of clause elements to be
        OR'd with the caller's existing search predicates.
        """
        search_subscriber = aliased(Subscriber)
        stmt = stmt.outerjoin(
            search_subscriber,
            search_subscriber.id == assignment_alias.subscriber_id,
        )
        extra_conditions = [
            search_subscriber.display_name.ilike(term),
            search_subscriber.subscriber_number.ilike(term),
            search_subscriber.email.ilike(term),
        ]
        return stmt, extra_conditions

    def get_template_context(
        self,
        db: Session,
        *,
        subscriber_id: object,
    ) -> dict[str, str]:
        """Return subscriber values used by network-domain template rendering."""
        subscriber = db.get(Subscriber, subscriber_id)
        if not subscriber:
            return {}
        return {
            "subscriber_code": getattr(subscriber, "external_code", "") or "",
            "subscriber_name": getattr(subscriber, "name", "") or "",
        }


default_subscriber_validator = DefaultSubscriberValidator()
