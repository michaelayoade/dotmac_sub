"""DotMac ERP integration edge (sub → erp.dotmac.io).

Sub is an X-API-Key CLIENT of ERP's existing, idempotent ``/sync/crm/*`` API.
ERP stays the inventory/finance system-of-record; no ERP code changes. See
``client`` for the transport and ``outbox`` for the flag-gated delivery
substrate guarded by ``sync_flow_ownership``.
"""

from __future__ import annotations

from app.services.dotmac_erp.client import (
    DotMacERPAuthError,
    DotMacERPClient,
    DotMacERPError,
    DotMacERPNotFoundError,
    DotMacERPRateLimitError,
    DotMacERPTransientError,
    build_erp_client,
)

__all__ = [
    "DotMacERPAuthError",
    "DotMacERPClient",
    "DotMacERPError",
    "DotMacERPNotFoundError",
    "DotMacERPRateLimitError",
    "DotMacERPTransientError",
    "build_erp_client",
]
