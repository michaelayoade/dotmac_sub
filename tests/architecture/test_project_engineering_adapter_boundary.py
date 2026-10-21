"""Prevent adapters and projections acquiring engineering command authority."""

from pathlib import Path

from fastapi.routing import APIRoute

from app.api import project_engineering as api
from app.schemas.project_engineering import EngineeringFormAction
from app.services.form_contracts import FORM_CONTRACT_REGISTRY
from app.web.admin import project_engineering as web


def test_projection_has_no_orm_or_command_executor() -> None:
    source = Path("app/services/project_engineering_projection.py").read_text()
    for forbidden in (
        "app.models",
        "sqlalchemy",
        "execute_owner_command",
        "revalidate_staff_admission",
    ):
        assert forbidden not in source


def test_adapters_do_not_own_transactions_or_business_decisions() -> None:
    for name in (
        "app/api/project_engineering.py",
        "app/web/admin/project_engineering.py",
    ):
        source = Path(name).read_text()
        for forbidden in (
            "app.models",
            "execute_owner_command",
            ".commit(",
            ".rollback(",
            "begin_nested(",
        ):
            assert forbidden not in source


def test_every_form_has_registered_consequence_contract() -> None:
    for action in EngineeringFormAction:
        contract = FORM_CONTRACT_REGISTRY[f"project_engineering.{action.value}"]
        assert contract.consequences
        assert contract.command_owner in {
            "operations.project_engineering_plans",
            "operations.project_work_order_handoff",
        }


def test_issuance_route_has_no_router_engineering_read_floor() -> None:
    for router in (api.router, web.router):
        assert not router.dependencies
        routes = [
            route
            for route in router.routes
            if isinstance(route, APIRoute) and route.path.endswith("/issuance")
        ]
        assert len(routes) == 2
        for route in routes:
            assert len(route.dependant.dependencies) == 2
    assert (
        '("app.api.project_engineering", "router", "api", "user")'
        in Path("app/main.py").read_text()
    )


def test_get_discovery_never_uses_synthetic_owner_command() -> None:
    source = (
        Path("app/services/project_engineering_plans.py")
        .read_text()
        .split("def preview_dispatch(", 1)[1]
    )
    assert "execute_owner_command" not in source
    assert "CurrentEngineeringPlanQuery" in source
    assert 'permission = "operations:dispatch:write"' in source


def test_each_adapter_route_has_its_exact_capability() -> None:
    from app.services.auth_dependencies import permission_requirement
    from app.services.project_engineering_plan_contracts import (
        APPROVE,
        POLICY,
        READ,
        REVOKE,
        WRITE,
    )
    from app.services.project_work_order_handoff_contracts import PERMISSION

    permissions = {
        "engineering": READ,
        "policy": POLICY,
        "draft": WRITE,
        "review": APPROVE,
        "revoke": REVOKE,
        "issuance": PERMISSION,
    }
    for router in (api.router, web.router):
        for route in router.routes:
            assert isinstance(route, APIRoute)
            expected = permissions[route.path.rsplit("/", 1)[-1]]
            requirements = [
                requirement
                for dependency in route.dependant.dependencies
                if (requirement := permission_requirement(dependency.call)) is not None
            ]
            assert len(requirements) == 1
            assert requirements[0].read_any_of == (expected,)
            assert requirements[0].write_any_of == (expected,)
