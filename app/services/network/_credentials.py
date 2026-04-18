"""Credential provider protocol for the network domain.

Defines a minimal Protocol + DTO used by network services that need
to consult PPPoE/access credentials WITHOUT importing the catalog
ORM model directly. Keeping this file free of subscription-domain
imports is what allows the network package to satisfy the
import-linter "Network domain must not import from subscription
domain" contract.

Concrete adapters live OUTSIDE ``app.services.network`` (see
``app.services.network_credential_bridge``) where importing
``app.models.catalog`` is permitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class PppoeCredential:
    """DTO exposing just the credential fields the network domain consumes.

    Kept intentionally small — add fields only when a network-domain
    caller actually needs them. Concrete adapters are responsible for
    building this DTO from whatever persistence layer they talk to.
    """

    subscriber_id: UUID
    username: str
    secret_hash: str | None
    is_active: bool


@runtime_checkable
class PppoeCredentialProvider(Protocol):
    """Read-only provider used by network services to look up credentials.

    Implementations are expected to return only active credentials unless
    otherwise noted. This protocol is deliberately small; extend it as
    new call sites arrive (currently DCP-4 pppoe_health; DCP-7 will
    consume it from provisioning_enforcement).
    """

    def get_by_username(self, username: str) -> PppoeCredential | None:
        """Return the active credential with ``username``, or ``None``."""
        ...

    def get_by_subscriber_id(
        self, subscriber_id: UUID
    ) -> PppoeCredential | None:
        """Return the active credential for ``subscriber_id``, or ``None``."""
        ...

    def get_active_by_subscriber_ids(
        self, subscriber_ids: Iterable[UUID]
    ) -> dict[UUID, PppoeCredential]:
        """Bulk-load active credentials keyed by subscriber id.

        Used by pppoe_health to avoid N+1 lookups when classifying a
        page of ONTs. Subscribers with no active credential are simply
        absent from the returned dict.
        """
        ...
