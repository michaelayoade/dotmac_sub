"""The isolated shadow assembly's tenant-only Billing declaration."""

from __future__ import annotations

from dataclasses import dataclass, field

from dotmac_billing import (
    AuthorityBinding,
    BillingPlane,
    CommercialAuthority,
    bind_commercial_authority,
)
from dotmac_billing import module as billing_module
from dotmac_kernel.planes import (
    ModulePlane,
    ModulePlaneSelection,
    validate_module_plane_selections,
)


@dataclass(frozen=True, slots=True)
class TenantBillingRepository:
    """Typed repository declaration; session ownership stays in the kernel."""

    plane: BillingPlane = field(default=BillingPlane.TENANT, init=False)


BILLING_MODULE_PLANES = (
    ModulePlaneSelection(module="billing", planes=(ModulePlane.TENANT,)),
)


def validate_shadow_composition() -> tuple[ModulePlaneSelection, ...]:
    return validate_module_plane_selections((billing_module,), BILLING_MODULE_PLANES)


def install_isolated_shadow_authority() -> AuthorityBinding:
    """Bind Billing inside the standalone shadow process, never Sub app boot."""

    validate_shadow_composition()
    return bind_commercial_authority(
        CommercialAuthority.INTERNAL,
        tenant_repository_factory=TenantBillingRepository,
    )


__all__ = [
    "BILLING_MODULE_PLANES",
    "TenantBillingRepository",
    "billing_module",
    "install_isolated_shadow_authority",
    "validate_shadow_composition",
]
