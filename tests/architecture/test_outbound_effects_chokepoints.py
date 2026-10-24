"""Outbound-effect transports are only opened behind ``outbound_effects.require``.

Two guards, no baseline:

1. Any module that imports a raw device/mail/RADIUS transport must call
   ``require``. A new importer fails here until it gates its transport.
2. The named chokepoint functions each contain a ``require(...)`` call, so the
   gate cannot be dropped from the place the connection is actually opened.

``outbound_effects`` itself is environment-only: it must not reach the
database, models, the control registry or settings resolution.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

import pytest

from tests.architecture import source_index

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
GATE_MODULE = APP / "services" / "outbound_effects.py"

# Module names whose import opens (or builds) an outbound connection. Type and
# exception submodules are not transports.
TRANSPORT_ROOTS = {
    "smtplib",
    "ncclient",
    "librouteros",
    "paramiko",
    "routeros_api",
    "pyrad",
    "sshtunnel",
}
# Calls that open or drive a connection. Key loading, host-key policies and
# exception classes are not call sites.
OPENER_NAMES = {
    "SSHClient",
    "Transport",
    "SSHTunnelForwarder",
    "SMTP",
    "SMTP_SSL",
    "LMTP",
    "RouterOsApiPool",
    "RouterOsApi",
    "connect",
    "Client",
    "SendPacket",
}
NON_TRANSPORT_SUBMODULES = {
    "paramiko.ssh_exception",
    "paramiko.channel",
    "routeros_api.exceptions",
    "pyrad.dictionary",
    "pyrad.packet",
}

# (file under app/, qualified function) that must call require().
CHOKEPOINTS = [
    ("services/enforcement.py", "_exchange_coa_disconnect"),
    ("services/enforcement.py", "_attempt_coa_update"),
    ("services/radius_auth.py", "authenticate"),
    ("services/nas/_mikrotik.py", "_mikrotik_routeros_auth"),
    ("services/nas/_mikrotik.py", "_mikrotik_rest_get"),
    ("services/nas/_mikrotik.py", "_mikrotik_routeros_query"),
    ("services/nas/_mikrotik.py", "apply_mikrotik_address_list_via_api"),
    ("services/nas/_mikrotik.py", "remove_mikrotik_address_list_via_api"),
    ("services/nas/_mikrotik.py", "disconnect_mikrotik_pppoe_bulk"),
    ("services/provisioning_adapters.py", "_ssh_client"),
    ("services/provisioning_adapters.py", "_routeros_api"),
    ("services/provisioning_adapters.py", "_netconf_manager"),
    ("services/network/olt_ssh_session.py", "olt_session"),
    ("services/network/olt_ssh_pool.py", "pooled_ssh_connection"),
    ("services/network/olt_ssh.py", "_open_shell"),
    ("poller/mikrotik_poller.py", "MikroTikConnection.connect"),
    ("services/topology/lldp_poller.py", "_read_neighbors_via_binary_api"),
    ("services/topology/lldp_poller.py", "_read_neighbors_via_rest"),
    ("services/router_management/config_export.py", "export_config_via_ssh"),
    ("services/router_management/connection.py", "RouterConnectionService.get_client"),
    ("services/genieacs_client.py", "GenieACSClient._request"),
    ("services/sms.py", "send_sms"),
    ("services/email.py", "_create_smtp_client"),
    ("team_inbox_smtp.py", "smtp_readiness"),
    ("services/web_system_export_tool.py", "_send_export_email"),
    ("services/integrations/connectors/http_webhook.py", "HttpWebhookRunner._request"),
    (
        "services/integrations/connectors/payment_gateway.py",
        "PaymentGatewayRunner._request",
    ),
    ("services/integrations/connectors/nextcloud_talk.py", "_request"),
    (
        "services/integrations/connectors/whatsapp_runtime.py",
        "WhatsAppRuntimeRunner.execute",
    ),
    (
        "services/integrations/connectors/meta_social_runtime.py",
        "MetaSocialRuntimeRunner.execute",
    ),
    (
        "services/integrations/connectors/meta_social_runtime.py",
        "MetaCapiRunner.execute",
    ),
    ("services/meta_pages.py", "_request_with_retry"),
    ("services/meta_pages.py", "_request_with_retry_sync"),
    ("services/dotmac_erp/client.py", "DotMacERPClient._request"),
    ("services/dotmac_erp/client.py", "DotMacERPClient._get_client"),
    ("services/push.py", "send_push"),
    ("services/push.py", "send_push_to_system_user"),
    ("services/notification_adapter.py", "WebhookProvider.send"),
    ("services/nextcloud_talk.py", "NextcloudTalkClient._request"),
    ("services/router_management/inventory.py", "JumpHostInventory.test_connection"),
    (
        "services/router_management/connection.py",
        "RouterConnectionService._get_or_create_tunnel",
    ),
    ("services/network/olt_ssh.py", "_connect_unlimited"),
]


def _is_transport(module: str) -> bool:
    if module in NON_TRANSPORT_SUBMODULES:
        return False
    return module.split(".")[0] in TRANSPORT_ROOTS and not any(
        module.startswith(prefix + ".") for prefix in NON_TRANSPORT_SUBMODULES
    )


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
            # `from paramiko import RSAKey` / `from pyrad import client`
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _requires_imported(tree: ast.AST) -> bool:
    """``require`` must be the one from app.services.outbound_effects."""
    return any(
        isinstance(n, ast.ImportFrom)
        and n.module == "app.services.outbound_effects"
        and any(a.name == "require" and a.asname is None for a in n.names)
        for n in ast.walk(tree)
    )


def _calls_require(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "require"
        for child in ast.walk(node)
    )


def _transport_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(module aliases, imported names) that come from a transport module."""
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_transport(alias.name):
                    modules.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                if _is_transport(node.module) or _is_transport(
                    f"{node.module}.{alias.name}"
                ):
                    names.add(alias.asname or alias.name)
    return modules, names


def _call_sites(tree: ast.Module) -> list[tuple[ast.Call, list[ast.AST]]]:
    """Transport-opening calls, each with its chain of enclosing functions."""
    modules, names = _transport_names(tree)
    found: list[tuple[ast.Call, list[ast.AST]]] = []

    def visit(node: ast.AST, scopes: list[ast.AST]) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            scopes = [*scopes, node]
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in names | modules:
                if func.id in OPENER_NAMES or func.id in modules:
                    found.append((node, scopes))
            elif isinstance(func, ast.Attribute) and func.attr in OPENER_NAMES:
                root = func
                while isinstance(root, ast.Attribute):
                    root = root.value  # type: ignore[assignment]
                if isinstance(root, ast.Name) and root.id in modules | names:
                    found.append((node, scopes))
        for child in ast.iter_child_nodes(node):
            visit(child, scopes)

    visit(tree, [])
    return found


def _find(tree: ast.Module, qualified: str) -> ast.AST | None:
    scope: list[ast.stmt] = tree.body
    node: ast.AST | None = None
    for part in qualified.split("."):
        node = next(
            (
                n
                for n in scope
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and n.name == part
            ),
            None,
        )
        if node is None:
            return None
        scope = node.body  # type: ignore[attr-defined]
    return node


_TEXT_NEEDLES = (
    "smtplib",
    "ncclient",
    "librouteros",
    "paramiko",
    "routeros_api",
    "pyrad",
    "sshtunnel",
    "require(",
)


@functools.cache
def _relevant() -> tuple[tuple[Path, ast.Module], ...]:
    """(path, tree) for the few files that mention a transport or call require().

    Computed once per worker and shared by every test below.
    """
    out = []
    for path in source_index.python_files(APP):
        if path == GATE_MODULE:
            continue
        text = source_index.source_text(path)
        if any(n in text for n in _TEXT_NEEDLES):
            out.append((path, source_index.python_ast(path)))
    return tuple(out)


@functools.cache
def _transport_files() -> tuple[tuple[Path, ast.Module], ...]:
    return tuple(
        (p, t)
        for p, t in _relevant()
        if any(_is_transport(m) for m in _imported_modules(t))
    )


def test_transport_importers_import_gate() -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path, tree in _transport_files()
        if not _requires_imported(tree)
    ]
    assert not offenders, (
        "modules import an outbound transport without importing "
        f"require from outbound_effects: {offenders}"
    )


def test_every_transport_call_site_is_gated() -> None:
    """Per call site, not per module: the enclosing function must call require."""
    offenders = []
    for path, tree in _transport_files():
        sites = _call_sites(tree)
        if not sites:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if not _requires_imported(tree):
            offenders.append(f"{rel}: require not imported from outbound_effects")
            continue
        for call, scopes in sites:
            if not any(_calls_require(scope) for scope in scopes):
                where = (
                    getattr(scopes[-1], "name", "<module>") if scopes else "<module>"
                )
                offenders.append(f"{rel}:{call.lineno} in {where}")
    assert not offenders, "transport call sites without require(): " + "; ".join(
        offenders
    )


@pytest.mark.parametrize(("rel", "qualified"), CHOKEPOINTS)
def test_chokepoint_calls_require(rel: str, qualified: str) -> None:
    tree = source_index.python_ast(APP / rel)
    assert _requires_imported(tree), f"{rel} must import require from outbound_effects"
    node = _find(tree, qualified)
    assert node is not None, f"{rel}:{qualified} not found; update CHOKEPOINTS"
    assert _calls_require(node), f"{rel}:{qualified} must call require(Effect.*)"


def test_every_effect_is_gated_somewhere() -> None:
    from app.services.outbound_effects import Effect

    used: set[str] = set()
    for _path, tree in _relevant():
        if not _requires_imported(tree):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "require"
                and node.args
                and isinstance(node.args[0], ast.Attribute)
            ):
                used.add(node.args[0].attr)
    assert {e.value for e in Effect} - used == set()


def test_gate_is_environment_only() -> None:
    tree = source_index.python_ast(GATE_MODULE)
    imported = _imported_modules(tree)
    forbidden = (
        "sqlalchemy",
        "app.db",
        "app.models",
        "app.services.control_registry",
        "app.services.settings_spec",
        "app.services.domain_settings",
    )
    bad = sorted(
        m for m in imported if any(m == f or m.startswith(f + ".") for f in forbidden)
    )
    assert not bad, f"outbound_effects must not read the database or settings: {bad}"
    assert imported >= {"app.config"}
