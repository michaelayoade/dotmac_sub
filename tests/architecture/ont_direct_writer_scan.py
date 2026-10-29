"""AST scan for direct ONT/OLT/ACS writers (single-writer ratchet).

Writers of ONT/OLT/ACS state are the ONT service-configuration lifecycle
(``configure_ont_service`` -> reconcile planner -> applier) plus the named owner
modules in ``OWNER_ALLOWLIST``. Every other call into the device-writer set is
counted per (file, category) and may only shrink against the committed baseline.

The scan is AST based: import aliases and from-imports are resolved, so
``steps.rollback_service_ports`` is found through
``from app.services.network import ont_provision_steps as steps``; strings and
docstrings never count.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("app/web", "app/api", "app/services", "app/tasks")

# Owners (lifecycle, planner, applier, sanctioned provisioning owner) and pure
# transport drivers. Calls inside these are the writers, not bypasses.
OWNER_ALLOWLIST: tuple[str, ...] = (
    # lifecycle, planner, applier
    "app/services/network/ont_service_configuration.py",
    "app/services/network/reconcile/",
    # sanctioned provisioning / authorization / commissioning owners
    "app/services/network/ont_provisioning_commands.py",
    "app/services/network/ont_provisioning_execution.py",
    "app/services/network/ont_provision_steps.py",
    "app/services/network/ont_authorization.py",
    "app/services/network/ont_commissioning.py",
    # transport drivers that define the device methods (exact files only)
    "app/services/network/olt_protocol_adapters.py",
    "app/services/genieacs_client.py",
)

OLT_WRITE_METHODS = frozenset(
    {
        "authorize_ont",
        "deauthorize_ont",
        "update_ont_profiles",
        "set_ont_description",
        "configure_iphost",
        "bind_tr069_profile",
        "create_service_port",
        "delete_service_port",
        "configure_management_batch",
        "reboot_ont",
        "factory_reset_ont",
        "configure_internet_config",
        "configure_wan_config",
        "configure_pppoe",
        "configure_port_native_vlan",
        "bind_policy_route",
        "clear_iphost_config",
        "clear_internet_config",
        "clear_wan_config",
        "unbind_tr069_profile",
        "create_tr069_profile",
    }
)
ACS_WRITE_METHODS = frozenset(
    {
        "set_parameter_values",
        "set_parameter_values_and_wait",
        "add_object",
        "delete_object",
        "create_task",
        "create_task_and_wait",
        "reboot_device",
        "reboot_device_and_wait",
        "factory_reset_and_wait",
        "download_and_wait",
        "refresh_object",
        "create_genieacs_client",
    }
)
_SETTER = re.compile(r"^(set_|force_|bind_|configure_(?!form_context)|toggle_)")
_ACS_SERVICE_WRITE = re.compile(
    r"^(set_|toggle_|configure_|bind_|push_|reboot|factory_reset|refresh|download)"
)


def _is_owner(rel: str) -> bool:
    return any(
        rel == entry or (entry.endswith("/") and rel.startswith(entry))
        for entry in OWNER_ALLOWLIST
    )


def _module_name(rel: str) -> str:
    parts = rel[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _aliases(tree: ast.AST, rel: str) -> dict[str, str]:
    package = _module_name(rel).split(".")
    if not rel.endswith("__init__.py"):
        package = package[:-1]
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.asname:
                    aliases[item.asname] = item.name
                else:
                    head = item.name.split(".")[0]
                    aliases[head] = head
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            for item in node.names:
                aliases[item.asname or item.name] = f"{module}.{item.name}"
    return aliases


def _dotted(node: ast.AST, aliases: dict[str, str]) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        head = aliases.get(node.id, node.id)
        return ".".join([head, *reversed(parts)])
    if parts:
        return ".".join(["?", *reversed(parts)])
    return None


_DESIRED_CONFIG_WRITES = frozenset(
    {
        "set_desired_config_value",
        "set_desired_config_values",
        "upsert_ont_desired_config_value",
        "set_access_flag",
        "clear_desired_config",
    }
)


def _categorize(dotted: str, rel: str = "") -> str | None:
    last = dotted.rsplit(".", 1)[-1]
    if (
        dotted.startswith("app.services.network.ont_desired_config.")
        and last in _DESIRED_CONFIG_WRITES
        and rel.startswith(("app/web/", "app/api/"))
    ):
        return "desired_config_write"
    if last == "reconcile_ont":
        return "reconcile_ont"
    if dotted.startswith("app.services.network.ont_features."):
        return "ont_features"
    if dotted.startswith("app.services.network.ont_write."):
        return "ont_write"
    if dotted.startswith("app.services.network.ont_provision_steps."):
        return "provision_steps"
    if dotted.startswith("app.services.network.ont_provisioning_commands."):
        return "provisioning_command"
    if dotted.startswith("app.services.web_network_ont_actions.") and _SETTER.match(
        last
    ):
        return "ont_action_setter"
    if dotted.startswith("app.services.web_network_service_ports.") and re.match(
        r"handle_(create|delete|clone)", last
    ):
        return "service_port_write"
    if dotted.startswith("app.services.genieacs_service.") and _ACS_SERVICE_WRITE.match(
        last
    ):
        return "acs_service_write"
    if last in OLT_WRITE_METHODS:
        return "olt_write"
    if last in ACS_WRITE_METHODS:
        return "acs_write"
    return None


def scan_source(rel: str, text: str) -> Counter[tuple[str, str]]:
    hits: Counter[tuple[str, str]] = Counter()
    tree = ast.parse(text, filename=rel)
    aliases = _aliases(tree, rel)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func, aliases)
        if dotted is None:
            continue
        category = _categorize(dotted, rel)
        if category:
            hits[(rel, category)] += 1
    return hits


def scan() -> Counter[tuple[str, str]]:
    hits: Counter[tuple[str, str]] = Counter()
    for directory in SCAN_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if _is_owner(rel):
                continue
            hits.update(scan_source(rel, path.read_text(encoding="utf-8")))
    return hits


def render(hits: Counter[tuple[str, str]]) -> str:
    return "".join(
        f"{path} {name} {count}\n" for (path, name), count in sorted(hits.items())
    )


if __name__ == "__main__":
    print(render(scan()), end="")
