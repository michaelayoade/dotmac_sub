from __future__ import annotations

import inspect
from typing import Any, get_type_hints

from dotmac_billing import BillingPlane
from dotmac_kernel.planes import ModulePlane

from alembic.script import ScriptDirectory
from sub_billing_adoption.assembly import (
    BILLING_MODULE_PLANES,
    TenantBillingRepository,
    billing_module,
    validate_shadow_composition,
)
from sub_billing_adoption.migrations import (
    composed_version_locations,
    make_shadow_alembic_config,
)


def test_shadow_composes_the_real_billing_module_on_the_tenant_plane_only() -> None:
    selections = validate_shadow_composition()

    assert selections == BILLING_MODULE_PLANES
    assert selections[0].module == billing_module.code == "billing"
    assert tuple(selections[0].planes) == (ModulePlane.TENANT,)
    assert ModulePlane.PLATFORM not in selections[0].planes


def test_repository_boundary_is_concrete_and_fully_typed() -> None:
    repository = TenantBillingRepository()
    hints = get_type_hints(TenantBillingRepository)

    assert repository.plane is BillingPlane.TENANT
    assert hints == {"plane": BillingPlane}
    assert all(annotation not in {Any, object} for annotation in hints.values())
    assert inspect.signature(TenantBillingRepository).parameters == {}


def test_migration_graph_contains_only_kernel_and_billing_lineages() -> None:
    locations = composed_version_locations().split()

    assert len(locations) == 2
    assert any("dotmac_kernel" in location for location in locations)
    assert any("dotmac_billing" in location for location in locations)
    assert len(locations) == len(set(locations))


def test_alembic_loads_each_composed_location_and_the_billing_head() -> None:
    config = make_shadow_alembic_config(
        "postgresql+psycopg://postgres@127.0.0.1/sub_billing_shadow_test"
    )
    scripts = ScriptDirectory.from_config(config)

    assert config.get_main_option("path_separator") == "space"
    assert scripts.version_locations == composed_version_locations().split()
    assert "bi_0001_billing" in scripts.get_heads()
    assert len(tuple(scripts.walk_revisions())) > 1
