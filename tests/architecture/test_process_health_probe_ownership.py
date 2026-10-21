"""Prevent a parallel heartbeat owner or heavy runtime-dependent probe CLI."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.sot_registry.registry import all_services

ROOT = Path(__file__).resolve().parents[2]


def test_component_probe_owners_are_typed_and_unique() -> None:
    services = {service.name: service for service in all_services()}
    for owner, module in (
        ("runtime.bandwidth_poller_health", "app.services.poller_health"),
        ("runtime.syslog_receiver_health", "app.syslog.listener"),
    ):
        service = services[owner]
        assert service.module == module
        assert service.contract is not None
        assert service.contract.transaction.mode.value == "not_applicable"


def test_poller_probe_reuses_reader_without_redis_writes() -> None:
    tree = ast.parse((ROOT / "app/services/poller_health.py").read_text())
    query = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "query_poller_liveness"
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "load_poller_health"
        for node in ast.walk(query)
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"set", "setex", "publish", "hset", "commit"}
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "app.poller.mikrotik_poller"
        for node in ast.walk(tree)
    )


def test_syslog_probe_imports_no_parser_or_domain_handlers() -> None:
    for path in ("app/syslog/health.py", "app/syslog/__init__.py"):
        tree = ast.parse((ROOT / path).read_text())
        # Package compatibility exports are lazy inside __getattr__/TYPE_CHECKING.
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {
                    "app.syslog.listener",
                    "app.syslog.handlers",
                    "app.syslog.parsers",
                }
    tree = ast.parse((ROOT / "app/syslog/health.py").read_text())
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"sendto", "datagram_received", "commit"}
        for node in ast.walk(tree)
    )


def test_platform_health_adapter_is_stdlib_only_and_nonpersistent() -> None:
    tree = ast.parse(
        (ROOT / "app/services/platform_health_observations.py").read_text()
    )
    imported_roots = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported_roots <= {
        "__future__",
        "dataclasses",
        "datetime",
        "enum",
        "hashlib",
        "json",
        "re",
    }
    forbidden_calls = {"open", "write", "commit", "flush", "publish", "request", "send"}
    assert not any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id in forbidden_calls)
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in forbidden_calls
            )
        )
        for node in ast.walk(tree)
    )


def test_platform_health_projection_is_registered_to_existing_probe_owners() -> None:
    services = {service.name: service for service in all_services()}
    for owner, concern in (
        (
            "runtime.bandwidth_poller_health",
            "bounded Platform Health poller observation projection",
        ),
        (
            "runtime.syslog_receiver_health",
            "bounded Platform Health syslog observation projection",
        ),
    ):
        service = services[owner]
        assert "app.services.platform_health_observations" in (
            service.implementation_modules
        )
        assert service.contract is not None
        projection = next(
            item for item in service.contract.concerns if item.name == concern
        )
        assert projection.role.value == "transport"
        assert "authenticated Platform Health source identity" in projection.input_names


def test_syslog_socket_override_has_one_bootstrap_owner() -> None:
    config = ast.parse((ROOT / "app/config.py").read_text())
    settings = next(
        node
        for node in config.body
        if isinstance(node, ast.ClassDef) and node.name == "Settings"
    )
    assert any(
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "syslog_health_socket"
        for node in settings.body
    )
    health = ast.parse((ROOT / "app/syslog/health.py").read_text())
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.config"
        and any(alias.name == "settings" for alias in node.names)
        for node in health.body
    )
    # Decision-input guard covers direct and aliased environment access globally;
    # the probe also must not import a heavier runtime owner to resolve the path.
    for tree in (config, health):
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith(("app.db", "app.models", "app.syslog.listener"))
            for node in ast.walk(tree)
        )
