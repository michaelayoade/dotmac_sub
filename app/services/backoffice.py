"""Sub-local anti-corruption boundary for replaceable back-office systems.

Sub domain owners call this module using Sub business concepts. Provider
selection and provider-specific imports stay here, so replacing Dotmac ERP with
Zoho (or another back-office product) does not change Sub domain services.

This is not an enterprise-wide capability or shared runtime service. It is a
local outbound port owned and deployed by Sub.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DOTMAC_ERP_PROVIDER = "dotmac_erp"


class BackofficeUnavailableError(RuntimeError):
    """The configured local back-office adapter cannot serve the request."""


@dataclass(frozen=True)
class BackofficeEnqueueResult:
    """Outcome of asking the configured local adapter to stage delivery."""

    status: str
    provider: str | None = None
    event: object | None = None

    @property
    def requires_attention(self) -> bool:
        return self.status == "not_enqueued"


class BackofficeGateway(Protocol):
    """Read-only capabilities currently consumed outside connector code."""

    def __enter__(self) -> BackofficeGateway: ...

    def __exit__(self, *args: object) -> None: ...

    def list_inventory_warehouses(self) -> list[dict]: ...

    def list_inventory(
        self,
        *,
        search: str | None = None,
        category_code: str | None = None,
        warehouse_id: str | None = None,
        include_zero_stock: bool = False,
        only_below_reorder: bool = False,
        only_with_available_serials: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict: ...

    def get_inventory_item(self, item_id: str) -> dict | None: ...

    def list_inventory_categories(self) -> list[dict]: ...

    def list_available_serials(
        self,
        *,
        item_code: str,
        warehouse_code: str,
        limit: int = 100,
        offset: int = 0,
    ) -> dict: ...

    def get_ncc_financials(
        self,
        *,
        year: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        as_of_date: str | None = None,
    ) -> dict: ...

    def get_ncc_staff_headcount(self) -> dict: ...


def provider_name(db: Session) -> str:
    """Resolve Sub's configured outbound adapter without leaking it to domains."""
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    value = settings_spec.resolve_value(
        db, SettingDomain.integration, "backoffice_provider"
    )
    return str(value or DOTMAC_ERP_PROVIDER).strip().lower()


def _flow_owned_by_sub(db: Session, flow: str) -> bool:
    # The ownership table controls which originator may enqueue each migrated
    # flow. It does not confer authority on any back-office provider.
    from app.models.field_erp_sync import flow_owned_by_sub

    return flow_owned_by_sub(db, flow)


def external_material_fulfilment_active(db: Session) -> bool:
    """Return whether local issue/fulfil compatibility transitions are retired."""
    return _flow_owned_by_sub(db, "material_request")


def build_gateway(db: Session) -> BackofficeGateway:
    """Build the configured provider adapter for bounded read operations."""
    provider = provider_name(db)
    if provider == DOTMAC_ERP_PROVIDER:
        from app.services.dotmac_erp.client import build_erp_client

        return build_erp_client(db)
    raise BackofficeUnavailableError(
        f"Back-office provider '{provider}' has no installed Sub adapter"
    )


def _enqueue_with_provider(
    db: Session,
    *,
    flow: str,
    source: Any,
) -> BackofficeEnqueueResult:
    """Delegate one source intent to its configured provider adapter.

    The source record remains Sub's durable business fact. An unavailable
    provider never changes the source decision; repair/reconciliation can retry
    once an adapter is configured.
    """
    if not _flow_owned_by_sub(db, flow):
        return BackofficeEnqueueResult(status="not_owned")

    provider = provider_name(db)
    if provider != DOTMAC_ERP_PROVIDER:
        raise BackofficeUnavailableError(
            f"Back-office provider '{provider}' has no installed adapter for {flow}"
        )

    event: object | None
    if flow == "expense_claim":
        from app.services.dotmac_erp.expense_sync import enqueue_expense_claim

        event = enqueue_expense_claim(db, source)
    elif flow == "material_request":
        from app.services.dotmac_erp.material_sync import enqueue_material_request

        event = enqueue_material_request(db, source)
    elif flow == "purchase_order":
        from app.services.dotmac_erp.purchase_order_sync import enqueue_purchase_order

        event = enqueue_purchase_order(db, source)
    elif flow == "purchase_invoice":
        from app.services.dotmac_erp.purchase_invoice_sync import (
            enqueue_purchase_invoice,
        )

        event = enqueue_purchase_invoice(db, source)
    else:
        raise ValueError(f"Unsupported back-office flow: {flow}")
    return BackofficeEnqueueResult(
        status="enqueued" if event is not None else "not_enqueued",
        provider=provider,
        event=event,
    )


def enqueue_expense_claim(db: Session, request: Any) -> BackofficeEnqueueResult:
    return _enqueue_with_provider(db, flow="expense_claim", source=request)


def enqueue_material_support(db: Session, request: Any) -> BackofficeEnqueueResult:
    return _enqueue_with_provider(db, flow="material_request", source=request)


def enqueue_purchase_order(db: Session, project: Any) -> BackofficeEnqueueResult:
    return _enqueue_with_provider(db, flow="purchase_order", source=project)


def enqueue_purchase_invoice(db: Session, invoice: Any) -> BackofficeEnqueueResult:
    return _enqueue_with_provider(db, flow="purchase_invoice", source=invoice)
