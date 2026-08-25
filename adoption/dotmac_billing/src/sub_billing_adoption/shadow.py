"""Typed isolated-shadow coordinator over Billing's published commands."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from dotmac_billing import (
    AcceptRatedObligationV1,
    AcceptSettlementV1,
    AppliedFxSnapshotV1,
    AppliedTaxSnapshotV1,
    MoneyV1,
    ServicePeriodEvidenceV1,
    SettlementFundingLane,
)
from dotmac_kernel.cache import TenantScope

from sub_billing_adoption.errors import AdoptionErrorCode, BillingAdoptionError


@dataclass(frozen=True, slots=True)
class ShadowTopologyV1:
    source_database_identity: str
    shadow_database_identity: str
    product_routes_mounted: bool
    outbound_delivery_enabled: bool

    def require_isolated(self) -> None:
        if (
            not self.source_database_identity.strip()
            or not self.shadow_database_identity.strip()
            or self.source_database_identity == self.shadow_database_identity
            or self.product_routes_mounted
            or self.outbound_delivery_enabled
        ):
            raise BillingAdoptionError(
                AdoptionErrorCode.SHADOW_TOPOLOGY_UNSAFE,
                "shadow Billing must use a distinct database with routes and "
                "delivery off",
            )


@dataclass(frozen=True, slots=True)
class ShadowAccountSeedV1:
    tenant_id: UUID
    external_account_ref: str
    currency: str
    minor_units: int


@dataclass(frozen=True, slots=True)
class ShadowObligationInputV1:
    tenant_id: UUID
    external_account_ref: str
    contract_line_ref: str
    contract_version: str
    charge_component: str
    source_system: str
    source_kind: str
    source_fact_id: str
    source_fact_version: str
    service_period: ServicePeriodEvidenceV1
    collection_timing: str
    pre_tax_amount: MoneyV1
    tax_amount: MoneyV1
    total_amount: MoneyV1
    rated_at: datetime
    price_version_id: str
    tax_snapshots: tuple[AppliedTaxSnapshotV1, ...] = ()
    fx_snapshot: AppliedFxSnapshotV1 | None = None
    supersedes_obligation_id: UUID | None = None

    def to_billing_command(
        self, *, billing_account_id: UUID
    ) -> AcceptRatedObligationV1:
        return AcceptRatedObligationV1(
            scope=TenantScope(self.tenant_id),
            billing_account_id=billing_account_id,
            contract_line_ref=self.contract_line_ref,
            contract_version=self.contract_version,
            charge_component=self.charge_component,
            source_system=self.source_system,
            source_kind=self.source_kind,
            source_fact_id=self.source_fact_id,
            source_fact_version=self.source_fact_version,
            service_period=self.service_period,
            collection_timing=self.collection_timing,
            pre_tax_amount=self.pre_tax_amount,
            tax_amount=self.tax_amount,
            total_amount=self.total_amount,
            rated_at=self.rated_at,
            price_version_id=self.price_version_id,
            tax_snapshots=self.tax_snapshots,
            fx_snapshot=self.fx_snapshot,
            supersedes_obligation_id=self.supersedes_obligation_id,
        )


@dataclass(frozen=True, slots=True)
class ShadowSettlementInputV1:
    tenant_id: UUID
    external_account_ref: str
    source_system: str
    source_settlement_key: str
    source_version: str
    amount: MoneyV1
    occurred_at: datetime
    observed_at: datetime
    confirmation_evidence: str
    funding_lane: SettlementFundingLane

    def to_billing_command(self, *, billing_account_id: UUID) -> AcceptSettlementV1:
        return AcceptSettlementV1(
            scope=TenantScope(self.tenant_id),
            billing_account_id=billing_account_id,
            source_system=self.source_system,
            source_settlement_key=self.source_settlement_key,
            source_version=self.source_version,
            amount=self.amount,
            occurred_at=self.occurred_at,
            observed_at=self.observed_at,
            confirmation_evidence=self.confirmation_evidence,
            funding_lane=self.funding_lane,
        )


@dataclass(frozen=True, slots=True)
class ShadowBundleV1:
    tenant_id: UUID
    topology: ShadowTopologyV1
    accounts: tuple[ShadowAccountSeedV1, ...]
    obligations: tuple[ShadowObligationInputV1, ...]
    settlements: tuple[ShadowSettlementInputV1, ...]


@dataclass(frozen=True, slots=True)
class BillingAccountRefV1:
    billing_account_id: UUID
    external_account_ref: str
    currency: str


@dataclass(frozen=True, slots=True)
class AcceptedBillingFactRefV1:
    fact_id: UUID
    source_identity: str


@dataclass(frozen=True, slots=True)
class ShadowRunReceiptV1:
    tenant_id: UUID
    account_count: int
    obligation_receipts: tuple[AcceptedBillingFactRefV1, ...]
    settlement_receipts: tuple[AcceptedBillingFactRefV1, ...]
    input_fingerprint: str


class ShadowBillingPort(Protocol):
    def ensure_account(self, seed: ShadowAccountSeedV1) -> BillingAccountRefV1: ...

    def accept_obligation(
        self, command: AcceptRatedObligationV1
    ) -> AcceptedBillingFactRefV1: ...

    def accept_settlement(
        self, command: AcceptSettlementV1
    ) -> AcceptedBillingFactRefV1: ...


def _account_key(external_ref: str, currency: str) -> tuple[str, str]:
    return external_ref, currency


def _bundle_fingerprint(bundle: ShadowBundleV1) -> str:
    parts = [
        str(bundle.tenant_id),
        bundle.topology.source_database_identity,
        bundle.topology.shadow_database_identity,
    ]
    parts.extend(
        f"account:{row.external_account_ref}:{row.currency}:{row.minor_units}"
        for row in bundle.accounts
    )
    parts.extend(
        "obligation:"
        f"{row.source_system}:{row.source_fact_id}:{row.source_fact_version}:"
        f"{format(row.total_amount.amount, 'f')}:{row.total_amount.currency}"
        for row in bundle.obligations
    )
    parts.extend(
        "settlement:"
        f"{row.source_system}:{row.source_settlement_key}:{row.source_version}:"
        f"{format(row.amount.amount, 'f')}:{row.amount.currency}"
        for row in bundle.settlements
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def run_shadow(port: ShadowBillingPort, bundle: ShadowBundleV1) -> ShadowRunReceiptV1:
    """Drive only the isolated target; no legacy command is dual-written."""

    bundle.topology.require_isolated()
    rows: tuple[
        ShadowAccountSeedV1 | ShadowObligationInputV1 | ShadowSettlementInputV1, ...
    ] = (
        *bundle.accounts,
        *bundle.obligations,
        *bundle.settlements,
    )
    if any(row.tenant_id != bundle.tenant_id for row in rows):
        raise BillingAdoptionError(
            AdoptionErrorCode.MIXED_TENANT_BUNDLE,
            "one shadow bundle may contain exactly one tenant",
            context={"tenant_id": str(bundle.tenant_id)},
        )

    accounts: dict[tuple[str, str], BillingAccountRefV1] = {}
    for seed in bundle.accounts:
        account = port.ensure_account(seed)
        accounts[_account_key(seed.external_account_ref, seed.currency)] = account

    obligation_receipts: list[AcceptedBillingFactRefV1] = []
    for obligation in bundle.obligations:
        obligation_account = accounts.get(
            _account_key(
                obligation.external_account_ref,
                obligation.total_amount.currency,
            )
        )
        if obligation_account is None:
            raise BillingAdoptionError(
                AdoptionErrorCode.UNKNOWN_ACCOUNT,
                "obligation names no seeded account/currency",
                context={"external_account_ref": obligation.external_account_ref},
            )
        obligation_receipts.append(
            port.accept_obligation(
                obligation.to_billing_command(
                    billing_account_id=obligation_account.billing_account_id
                )
            )
        )

    settlement_receipts: list[AcceptedBillingFactRefV1] = []
    for settlement in bundle.settlements:
        settlement_account = accounts.get(
            _account_key(settlement.external_account_ref, settlement.amount.currency)
        )
        if settlement_account is None:
            raise BillingAdoptionError(
                AdoptionErrorCode.UNKNOWN_ACCOUNT,
                "settlement names no seeded account/currency",
                context={"external_account_ref": settlement.external_account_ref},
            )
        settlement_receipts.append(
            port.accept_settlement(
                settlement.to_billing_command(
                    billing_account_id=settlement_account.billing_account_id
                )
            )
        )

    return ShadowRunReceiptV1(
        tenant_id=bundle.tenant_id,
        account_count=len(accounts),
        obligation_receipts=tuple(obligation_receipts),
        settlement_receipts=tuple(settlement_receipts),
        input_fingerprint=_bundle_fingerprint(bundle),
    )


__all__ = [
    "AcceptedBillingFactRefV1",
    "BillingAccountRefV1",
    "ShadowAccountSeedV1",
    "ShadowBillingPort",
    "ShadowBundleV1",
    "ShadowObligationInputV1",
    "ShadowRunReceiptV1",
    "ShadowSettlementInputV1",
    "ShadowTopologyV1",
    "run_shadow",
]
