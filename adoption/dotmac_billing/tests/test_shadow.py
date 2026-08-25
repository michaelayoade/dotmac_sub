from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from dotmac_billing import (
    AcceptRatedObligationV1,
    AcceptSettlementV1,
    MoneyV1,
    ServicePeriodEvidenceV1,
    ServicePeriodStatus,
    SettlementFundingLane,
)

from sub_billing_adoption.errors import AdoptionErrorCode, BillingAdoptionError
from sub_billing_adoption.shadow import (
    AcceptedBillingFactRefV1,
    BillingAccountRefV1,
    ShadowAccountSeedV1,
    ShadowBundleV1,
    ShadowObligationInputV1,
    ShadowSettlementInputV1,
    ShadowTopologyV1,
    run_shadow,
)


class RecordingPort:
    def __init__(self) -> None:
        self.obligations: list[AcceptRatedObligationV1] = []
        self.settlements: list[AcceptSettlementV1] = []

    def ensure_account(self, seed: ShadowAccountSeedV1) -> BillingAccountRefV1:
        return BillingAccountRefV1(uuid4(), seed.external_account_ref, seed.currency)

    def accept_obligation(
        self, command: AcceptRatedObligationV1
    ) -> AcceptedBillingFactRefV1:
        self.obligations.append(command)
        return AcceptedBillingFactRefV1(uuid4(), command.source_fact_id)

    def accept_settlement(
        self, command: AcceptSettlementV1
    ) -> AcceptedBillingFactRefV1:
        self.settlements.append(command)
        return AcceptedBillingFactRefV1(uuid4(), command.source_settlement_key)


def _bundle(tenant_id: UUID) -> ShadowBundleV1:
    instant = datetime(2026, 8, 17, tzinfo=UTC)
    money = MoneyV1(Decimal("100.00"), "NGN", 2)
    return ShadowBundleV1(
        tenant_id=tenant_id,
        topology=ShadowTopologyV1(
            source_database_identity="captured-sub-snapshot:sha256:source",
            shadow_database_identity="observe:sub-billing-shadow:run-1",
            product_routes_mounted=False,
            outbound_delivery_enabled=False,
        ),
        accounts=(ShadowAccountSeedV1(tenant_id, "subscriber:1", "NGN", 2),),
        obligations=(
            ShadowObligationInputV1(
                tenant_id=tenant_id,
                external_account_ref="subscriber:1",
                contract_line_ref="subscription:1:line:1",
                contract_version="3",
                charge_component="recurring_service",
                source_system="dotmac-subscriptions",
                source_kind="recurring_charge_occurrence",
                source_fact_id="occurrence:1",
                source_fact_version="1",
                service_period=ServicePeriodEvidenceV1(
                    status=ServicePeriodStatus.VERIFIED,
                    starts_at=instant,
                    ends_at=datetime(2026, 9, 17, tzinfo=UTC),
                ),
                collection_timing="advance",
                pre_tax_amount=money,
                tax_amount=MoneyV1(Decimal("0.00"), "NGN", 2),
                total_amount=money,
                rated_at=instant,
                price_version_id="price:1:v2",
            ),
        ),
        settlements=(
            ShadowSettlementInputV1(
                tenant_id=tenant_id,
                external_account_ref="subscriber:1",
                source_system="dotmac-integrator",
                source_settlement_key="paystack:reference:1",
                source_version="1",
                amount=money,
                occurred_at=instant,
                observed_at=instant,
                confirmation_evidence="verified_provider_webhook",
                funding_lane=SettlementFundingLane.AVAILABLE_CREDIT,
            ),
        ),
    )


def test_shadow_maps_to_the_real_frozen_billing_contracts() -> None:
    port = RecordingPort()
    bundle = _bundle(uuid4())

    receipt = run_shadow(port, bundle)

    assert receipt.account_count == 1
    assert len(receipt.obligation_receipts) == 1
    assert len(receipt.settlement_receipts) == 1
    assert isinstance(port.obligations[0], AcceptRatedObligationV1)
    assert isinstance(port.settlements[0], AcceptSettlementV1)
    assert port.obligations[0].scope == port.settlements[0].scope


def test_shadow_refuses_the_source_database_or_reachable_product_routes() -> None:
    bundle = _bundle(uuid4())
    unsafe = ShadowTopologyV1(
        source_database_identity="same",
        shadow_database_identity="same",
        product_routes_mounted=True,
        outbound_delivery_enabled=False,
    )

    with pytest.raises(BillingAdoptionError) as caught:
        run_shadow(
            RecordingPort(),
            ShadowBundleV1(
                tenant_id=bundle.tenant_id,
                topology=unsafe,
                accounts=bundle.accounts,
                obligations=bundle.obligations,
                settlements=bundle.settlements,
            ),
        )

    assert caught.value.code is AdoptionErrorCode.SHADOW_TOPOLOGY_UNSAFE


def test_shadow_refuses_a_mixed_tenant_bundle() -> None:
    bundle = _bundle(uuid4())
    other = uuid4()

    with pytest.raises(BillingAdoptionError) as caught:
        run_shadow(
            RecordingPort(),
            ShadowBundleV1(
                tenant_id=bundle.tenant_id,
                topology=bundle.topology,
                accounts=(ShadowAccountSeedV1(other, "subscriber:1", "NGN", 2),),
                obligations=bundle.obligations,
                settlements=bundle.settlements,
            ),
        )

    assert caught.value.code is AdoptionErrorCode.MIXED_TENANT_BUNDLE
