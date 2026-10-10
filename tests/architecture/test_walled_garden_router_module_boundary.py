"""Keep the walled-garden router module render/verify-only and single-owned."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.sot_relationships import all_services

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE = PROJECT_ROOT / "app" / "services" / "walled_garden_router_module.py"
READINESS = PROJECT_ROOT / "app" / "services" / "walled_garden_router_readiness.py"
SCHEMA = PROJECT_ROOT / "app" / "schemas" / "walled_garden.py"
OWNER_FILES = (MODULE, READINESS, SCHEMA)

#: Router transport/write surfaces this PR must never wire.
FORBIDDEN_CALLS = {
    "RouterConnectionService",
    "RouterConfigurationWriteAdapter",
    "RouterSotWriteAdapter",
    "fetch_config_export",
    "capture_from_router",
    "create_push",
    "execute",
    "commit",
    "rollback",
    "add",
    "delete",
    "flush",
}


def _service(name: str):
    return next(item for item in all_services() if item.name == name)


def test_both_owners_are_contracted_read_only() -> None:
    for name, module in (
        (
            "access.walled_garden_router_module",
            "app.services.walled_garden_router_module",
        ),
        (
            "access.walled_garden_router_readiness",
            "app.services.walled_garden_router_readiness",
        ),
    ):
        service = _service(name)
        assert service.module == module
        assert service.contract is not None
        assert service.contract.transaction.mode.value == "read_only"
        assert {c.name for c in service.contract.concerns} == set(service.owns)
        assert service.contract.errors.domain_codes
        assert service.contract.errors.fail_closed_on


def test_declared_error_codes_match_the_enums() -> None:
    from app.services.walled_garden_router_module import WalledGardenModuleErrorCode
    from app.services.walled_garden_router_readiness import (
        WalledGardenReadinessErrorCode,
    )

    module_contract = _service("access.walled_garden_router_module").contract
    readiness_contract = _service("access.walled_garden_router_readiness").contract
    assert module_contract is not None and readiness_contract is not None
    assert set(module_contract.errors.domain_codes) == {
        code.value for code in WalledGardenModuleErrorCode
    }
    assert set(readiness_contract.errors.domain_codes) == {
        code.value for code in WalledGardenReadinessErrorCode
    }


def test_owners_never_contact_routers_or_write() -> None:
    offenders: list[str] = []
    for path in OWNER_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_CALLS:
                offenders.append(f"{path.name}: {node.id}")
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_CALLS:
                offenders.append(f"{path.name}: .{node.attr}")
            if isinstance(node, ast.ImportFrom) and node.module in {
                "fastapi",
                "celery",
                "app.services.router_management.config",
                "app.services.router_management.config_export",
            }:
                offenders.append(f"{path.name}: import {node.module}")
    assert not offenders, offenders


def test_owner_interfaces_do_not_expose_any() -> None:
    for path in OWNER_FILES:
        source = path.read_text(encoding="utf-8")
        assert "from typing import Any" not in source, path.name
        assert "dict[str, Any]" not in source, path.name


def test_only_the_module_owner_mints_walled_garden_tags() -> None:
    minting = []
    for root in (PROJECT_ROOT / "app", PROJECT_ROOT / "scripts"):
        for path in root.rglob("*.py"):
            if path == MODULE or "__pycache__" in path.parts:
                continue
            if '"dotmac-wg:' in path.read_text(encoding="utf-8"):
                minting.append(str(path.relative_to(PROJECT_ROOT)))
    assert not minting, minting


def test_allowed_resources_setting_is_validated_on_every_settings_write() -> None:
    from app.models.domain_settings import SettingDomain
    from app.services.settings_spec import TYPED_JSON_SETTING_VALIDATORS

    assert (
        SettingDomain.radius,
        "walled_garden_allowed_resources",
    ) in TYPED_JSON_SETTING_VALIDATORS
    source = (PROJECT_ROOT / "app" / "services" / "domain_settings.py").read_text(
        encoding="utf-8"
    )
    hook = source.index("def _validate_relationship_change")
    assert "typed_setting_value_error" in source[hook : hook + 1200]
