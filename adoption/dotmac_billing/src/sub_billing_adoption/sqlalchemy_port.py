"""Flush-only adapter from the shadow coordinator to Billing services."""

from __future__ import annotations

from uuid import UUID

from dotmac_billing import AcceptRatedObligationV1, AcceptSettlementV1
from dotmac_billing.service import (
    accept_rated_obligation,
    accept_settlement,
    create_billing_account,
)
from dotmac_kernel.cache import TenantScope
from sqlalchemy.orm import Session

from sub_billing_adoption.shadow import (
    AcceptedBillingFactRefV1,
    BillingAccountRefV1,
    ShadowAccountSeedV1,
)


class SqlAlchemyShadowBillingPort:
    """Mutate and flush only; ``dotmac_kernel.db`` owns the transaction."""

    __slots__ = (
        "_accepted_confirmation_evidence",
        "_accepted_source_kinds",
        "_db",
    )

    def __init__(
        self,
        db: Session,
        *,
        accepted_source_kinds: frozenset[str],
        accepted_confirmation_evidence: frozenset[str],
    ) -> None:
        self._db = db
        self._accepted_source_kinds = accepted_source_kinds
        self._accepted_confirmation_evidence = accepted_confirmation_evidence

    def ensure_account(self, seed: ShadowAccountSeedV1) -> BillingAccountRefV1:
        row = create_billing_account(
            self._db,
            scope=TenantScope(seed.tenant_id),
            external_account_ref=seed.external_account_ref,
            currency=seed.currency,
            minor_units=seed.minor_units,
        )
        return BillingAccountRefV1(
            billing_account_id=UUID(str(row.id)),
            external_account_ref=str(row.external_account_ref),
            currency=str(row.currency),
        )

    def accept_obligation(
        self, command: AcceptRatedObligationV1
    ) -> AcceptedBillingFactRefV1:
        row = accept_rated_obligation(
            self._db,
            scope=command.scope,
            command=command,
            accepted_source_kinds=self._accepted_source_kinds,
        )
        return AcceptedBillingFactRefV1(
            fact_id=UUID(str(row.id)),
            source_identity=(
                f"{command.source_system}:{command.source_fact_id}:"
                f"{command.source_fact_version}"
            ),
        )

    def accept_settlement(
        self, command: AcceptSettlementV1
    ) -> AcceptedBillingFactRefV1:
        row = accept_settlement(
            self._db,
            scope=command.scope,
            command=command,
            accepted_confirmation_evidence=self._accepted_confirmation_evidence,
        )
        return AcceptedBillingFactRefV1(
            fact_id=UUID(str(row.id)),
            source_identity=(
                f"{command.source_system}:{command.source_settlement_key}:"
                f"{command.source_version}"
            ),
        )


__all__ = ["SqlAlchemyShadowBillingPort"]
