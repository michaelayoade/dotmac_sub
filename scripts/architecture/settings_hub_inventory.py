"""Read-only source census for the hub migration; does not load the application.

Run with ``python -m scripts.architecture.settings_hub_inventory``. No live
database, environment values, credentials, or network connections are read.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from app.services.settings_hub_catalog import TAB_NAMES, SettingsTab, setting_section

ROOT = Path(__file__).resolve().parents[2]
DESTINATION = ROOT / "docs" / "settings-hub-migration-register.json"


@dataclass(frozen=True, slots=True)
class SettingEntry:
    domain: str
    key: str
    kind: str
    tab: str
    section: str
    destination: str
    source: str
    metadata: dict[str, str]
    disposition: str
    source_references: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ActionEntry:
    method: str
    path: str
    tab: str
    source: str
    handler: str
    permissions: tuple[str, ...]
    calls: tuple[str, ...]
    disposition: str


@dataclass(slots=True)
class MigrationRegister:
    schema_version: int = 1
    source_base_revision: str = "7c52f50e3c74be098df11a442a8c79e7a1d1ea1a"
    scope: str = "Source census; stored deployment-only keys require a separate read-only database reconciliation."
    settings: list[SettingEntry] = field(default_factory=list)
    actions: list[ActionEntry] = field(default_factory=list)
    form_fields: list[dict[str, str]] = field(default_factory=list)
    source_hashes: dict[str, str] = field(default_factory=dict)
    retired_routes: list[dict[str, str]] = field(default_factory=list)


def source_fingerprint(path: Path) -> str:
    """Text identity is independent of checkout line-ending conversion."""
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def expression(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant):
        return str(node.value)
    return ast.unparse(node)


def literal_assignment(module: ast.Module, name: str) -> ast.AST:
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return node.value
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value
        ):
            return node.value
    raise ValueError(f"Missing declaration: {name}")


def declared_settings(root: Path) -> tuple[list[SettingEntry], list[SettingEntry]]:
    """Enumerate literal specs, the three registered factories, and tombstones.

    Factory comprehensions are expanded from their literal threshold tuples.
    An unknown factory fails the census rather than silently losing settings.
    """
    path = root / "app/services/settings_spec.py"
    module = tree(path)
    retired_node = literal_assignment(module, "_RETIRED_FEATURE_ALIAS_SPECS")
    assert isinstance(retired_node, ast.Call)
    retired_values = retired_node.args[0]
    assert isinstance(retired_values, ast.Set)
    retired: set[tuple[str, str]] = set()
    for pair in retired_values.elts:
        assert isinstance(pair, ast.Tuple)
        retired.add((expression(pair.elts[0]), expression(pair.elts[1])))
    records: list[tuple[str, dict[str, str]]] = []
    sources = [path, *(root / "app/services/settings_specs").glob("*.py")]
    known_factories = {
        "build_provisioning_specs",
        "build_integration_specs",
        "build_router_health_specs",
    }
    invoked = {
        node.func.id
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("build_")
    }
    if invoked != known_factories:
        raise ValueError(f"Settings factories changed: {invoked ^ known_factories}")
    for source in sources:
        parsed = tree(source)
        for node in ast.walk(parsed):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Name)
                or node.func.id not in {"SettingSpec", "setting_spec"}
            ):
                continue
            fields = {
                item.arg: expression(item.value) for item in node.keywords if item.arg
            }
            if "domain" not in fields or "key" not in fields:
                continue
            if fields["key"] == "key":
                if source.name != "router_health.py":
                    raise ValueError(
                        f"Unrecognised generated spec: {source}:{node.lineno}"
                    )
                rows = ast.literal_eval(literal_assignment(parsed, "_INTEGER_SETTINGS"))
                for key, label, default, minimum, maximum in rows:
                    records.append(
                        (
                            f"{source.relative_to(root).as_posix()}:{node.lineno}",
                            {
                                **fields,
                                "key": key,
                                "label": label,
                                "default": str(default),
                                "min_value": str(minimum),
                                "max_value": str(maximum),
                            },
                        )
                    )
            else:
                records.append(
                    (f"{source.relative_to(root).as_posix()}:{node.lineno}", fields)
                )
    active: list[SettingEntry] = []
    tombstones: list[SettingEntry] = []
    identities: set[tuple[str, str]] = set()
    for source_location, metadata in records:
        domain, key = metadata["domain"], metadata["key"]
        if (domain, key) in identities:
            raise ValueError(f"Duplicate setting: {domain}.{key}")
        identities.add((domain, key))
        section = setting_section(domain=domain, key=key)
        removed = (domain, key) in retired
        entry = SettingEntry(
            domain=domain,
            key=key,
            kind="retired-alias" if removed else "registered",
            tab=section.tab.value,
            section=section.id,
            destination=section.url,
            source=source_location,
            metadata={
                name: value
                for name, value in metadata.items()
                if name != "default" or metadata.get("is_secret") != "True"
            },
            disposition="Retired; preserve tombstone, never restore an editor"
            if removed
            else "Mapped to hub; preserve registered type, validation, tenant scope and owner-command writes",
        )
        (tombstones if removed else active).append(entry)
    return sorted(active, key=lambda row: (row.domain, row.key)), tombstones


def extra_settings(root: Path, known: set[tuple[str, str]]) -> list[SettingEntry]:
    """Reconcile literal seed identities and specialised configuration key lists."""
    entries: dict[tuple[str, str], SettingEntry] = {}
    baseline_path = root / "tests/architecture/unowned_setting_surface_baseline.txt"
    baseline = {
        line.strip()
        for line in baseline_path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    paths = [
        root / "app/services/settings_seed.py",
        root / "app/services/web_system_config.py",
        root / "app/services/web_system_company_info.py",
    ]
    for path in paths:
        parsed = tree(path)
        key_lists: dict[str, list[str]] = {}
        domains: dict[str, str] = {"OUTAGE_AUTO_NOTIFY_KEYS": "network_monitoring"}
        inline_pairs: list[tuple[str, str, int, str]] = []
        for node in ast.walk(parsed):
            if isinstance(node, ast.Assign) and isinstance(
                node.value, (ast.List, ast.Tuple)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.endswith("_KEYS"):
                        key_lists[target.id] = [
                            item.value
                            for item in node.value.elts
                            if isinstance(item, ast.Constant)
                            and isinstance(item.value, str)
                        ]
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_read_settings"
                and len(node.args) >= 3
            ):
                if isinstance(node.args[1], ast.Attribute) and isinstance(
                    node.args[2], ast.Name
                ):
                    domains[node.args[2].id] = node.args[1].attr
                elif isinstance(node.args[1], ast.Attribute) and isinstance(
                    node.args[2], ast.List
                ):
                    inline_pairs.extend(
                        (
                            node.args[1].attr,
                            item.value,
                            node.lineno,
                            "inline specialised form",
                        )
                        for item in node.args[2].elts
                        if isinstance(item, ast.Constant)
                        and isinstance(item.value, str)
                    )
        if path.name == "web_system_company_info.py":
            domains["COMPANY_KEYS"] = "billing"
        pairs: list[tuple[str, str, int, str]] = inline_pairs
        for name, keys in key_lists.items():
            if name not in domains:
                if set(keys) <= {pair[1] for pair in inline_pairs}:
                    continue
                raise ValueError(f"Unmapped specialised list: {path}:{name}")
            pairs.extend((domains[name], key, 1, name) for key in keys)
        for node in ast.walk(parsed):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Attribute)
                or node.func.attr != "ensure_by_key"
            ):
                continue
            receiver = node.func.value
            if not isinstance(receiver, ast.Name) or not receiver.id.endswith(
                "_settings"
            ):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "key"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    pairs.append(
                        (
                            receiver.id.removesuffix("_settings"),
                            keyword.value.value,
                            node.lineno,
                            "seed",
                        )
                    )
        for domain, key, line, origin in pairs:
            if (domain, key) in known:
                continue
            section = setting_section(domain=domain, key=key)
            disposition = "Preserve specialised/seed owner; not a generic SettingSpec"
            destinations = {
                "PREFERENCE_KEYS": "/admin/system/config/preferences",
                "PORTAL_KEYS": "/admin/system/config/portal",
                "BILLING_KEYS": "/admin/system/config/billing",
                "DIRECT_BANK_TRANSFER_KEYS": "/admin/system/config/direct-bank-transfer",
                "REMINDER_KEYS": "/admin/system/config/reminders",
                "BILLING_NOTIF_KEYS": "/admin/system/config/billing-notifications",
                "PLAN_CHANGE_KEYS": "/admin/system/config/plan-change",
                "RADIUS_KEYS": "/admin/system/config/radius",
                "NAS_TYPE_KEYS": "/admin/system/config/nas-types",
                "COMPANY_KEYS": "/admin/system/company-info",
                "OUTAGE_AUTO_NOTIFY_KEYS": "/admin/system/config/monitoring",
                "inline specialised form": "/admin/system/config/monitoring",
            }
            destination = destinations.get(origin, section.url)
            if origin == "inline specialised form" and domain == "auth":
                destination = "/admin/system/config/preferences"
            if origin == "seed":
                disposition = "Bootstrap-only identity absent from active specs; inventory retained, do not invent a generic editor"
            if key in baseline:
                disposition = "Existing unowned-control debt; retain specialised surface, require owner verification before asserting runtime effect"
            entries[(domain, key)] = SettingEntry(
                domain=domain,
                key=key,
                kind="specialised-or-seeded",
                tab=section.tab.value,
                section=section.id,
                destination=destination,
                source=f"{path.relative_to(root).as_posix()}:{line}",
                metadata={"origin": origin},
                disposition=disposition,
            )
    return sorted(entries.values(), key=lambda row: (row.domain, row.key))


def feature_settings(root: Path) -> list[SettingEntry]:
    entries = []
    path = root / "app/services/module_manager.py"
    modules = ast.literal_eval(literal_assignment(tree(path), "MODULE_KEY_MAP"))
    for module, key in modules.items():
        entries.append(
            SettingEntry(
                "modules",
                key,
                "module",
                "system",
                "optional-modules",
                "/admin/system/modules",
                f"{path.relative_to(root).as_posix()}:22",
                {"module": module},
                "Preserve module manager owner and canonical control resolution",
            )
        )
    path = root / "app/services/control_registry.py"
    for node in ast.walk(literal_assignment(tree(path), "_FEATURE_CONTROLS")):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Control"
        ):
            fields = {
                item.arg: expression(item.value)
                for item in node.keywords
                if item.arg and item.arg != "legacy"
            }
            key = fields["key"]
            entries.append(
                SettingEntry(
                    "modules",
                    key.replace(".", "_"),
                    "feature-or-safety",
                    "system",
                    "optional-features",
                    "/admin/system/modules",
                    f"{path.relative_to(root).as_posix()}:{node.lineno}",
                    fields,
                    "Preserve control registry owner; do not revive historical aliases",
                )
            )
    return entries


def specialised_settings(root: Path, known: set[tuple[str, str]]) -> list[SettingEntry]:
    """Include non-spec identities and parameterised SMTP profile namespaces."""
    entries: list[SettingEntry] = []
    path = root / "app/services/support_ticket_settings.py"
    for node in tree(path).body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.endswith("_KEY"):
                    entries.append(
                        SettingEntry(
                            "workflow",
                            node.value.value,
                            "specialised",
                            "operations",
                            "ticket-settings",
                            "/admin/system/ticket-settings",
                            f"{path.relative_to(root).as_posix()}:{node.lineno}",
                            {"write_owner": "support.ticket_configuration"},
                            "Preserve specialised ticket configuration owner and typed commands",
                        )
                    )
    path = root / "app/services/email.py"
    parsed = tree(path)
    sender_prefix = ast.literal_eval(
        literal_assignment(parsed, "SMTP_SENDER_KEY_PREFIX")
    )
    activity_prefix = ast.literal_eval(
        literal_assignment(parsed, "SMTP_ACTIVITY_KEY_PREFIX")
    )
    default_key = ast.literal_eval(
        literal_assignment(parsed, "SMTP_DEFAULT_SENDER_KEY_SETTING")
    )
    activities = ast.literal_eval(literal_assignment(parsed, "SMTP_ACTIVITY_CHOICES"))
    writer = next(
        node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name == "upsert_smtp_sender"
    )
    values = literal_assignment(ast.Module(body=writer.body, type_ignores=[]), "values")
    assert isinstance(values, ast.Dict)
    fields = [ast.literal_eval(key) for key in values.keys if key is not None] + [
        "password"
    ]
    keys = [
        default_key,
        *(activity_prefix + key for key, _label in activities),
        *(sender_prefix + "{sender_key}." + name for name in fields),
    ]
    for key in keys:
        entries.append(
            SettingEntry(
                "notification",
                key,
                "specialised",
                "notifications",
                "smtp-senders",
                "/admin/system/email#smtp-senders",
                f"{path.relative_to(root).as_posix()}:247",
                {
                    "write_owner": "app.services.email",
                    "is_secret": str(key.endswith(".password")),
                },
                "Preserve default sender, activity mapping, and each sender profile field; dynamic sender instances are deployment data",
            )
        )
    path = root / "app/services/web_system_restore_tool.py"
    key = ast.literal_eval(literal_assignment(tree(path), "RETENTION_DAYS_KEY"))
    entries.append(
        SettingEntry(
            "subscriber",
            key,
            "specialised",
            "operations",
            "restore",
            "/admin/system/tools/restore",
            f"{path.relative_to(root).as_posix()}:49",
            {"write_owner": "app.services.web_system_restore_tool"},
            "Preserve recovery-retention editor and owner validation",
        )
    )
    path = root / "app/services/domain_settings.py"
    operational = literal_assignment(tree(path), "_UNAUDITED_OPERATIONAL_KEYS")
    assert isinstance(operational, ast.Call)
    for domain, key in sorted(ast.literal_eval(operational.args[0])):
        if (domain, key) not in known:
            entries.append(
                SettingEntry(
                    domain,
                    key,
                    "operational-state",
                    "logs",
                    "operational-state",
                    "/admin/system/control-plane",
                    f"{path.relative_to(root).as_posix()}:{operational.lineno}",
                    {"write_owner": "Existing operational task owner"},
                    "Cursor/counter/job state, not administrator configuration; retain storage and diagnostics, never expose a configuration writer",
                )
            )
    return [row for row in entries if (row.domain, row.key) not in known]


def route_tab(path: str) -> str:
    if any(
        token in path
        for token in (
            "/users",
            "/roles",
            "/permissions",
            "/api-keys",
            "/secrets",
            "/sessions",
        )
    ):
        return "security"
    if any(token in path for token in ("/email", "/notifications", "/templates")):
        return "notifications"
    if any(
        token in path
        for token in (
            "/billing",
            "/bank-accounts",
            "/tax",
            "/reminders",
            "/plan-change",
            "/starter-ledger",
            "/direct-bank-transfer",
            "/payment-methods",
        )
    ):
        return "billing"
    if any(
        token in path
        for token in (
            "/network",
            "/radius",
            "/walled-garden",
            "/monitoring",
            "/nas-types",
            "/customer-regions",
        )
    ):
        return "network"
    if any(token in path for token in ("/catalog", "/resellers")):
        return "catalog"
    if any(token in path for token in ("/integrations", "/inbox")):
        return "integrations"
    if any(
        token in path
        for token in ("/ticket-settings", "/service-teams", "/tools", "/imports")
    ):
        return "operations"
    if any(
        token in path for token in ("/audit", "/scheduler", "/control-plane", "/logs")
    ):
        return "logs"
    return "system"


def action_inventory(root: Path) -> list[ActionEntry]:
    """Enumerate routes on existing hub destinations and their action families."""
    selected = {
        "system",
        "catalog_settings",
        "integrations",
        "notifications",
        "custom_fields",
        "customer_connection_types",
        "customer_regions",
        "resellers",
        "service_teams",
        "system_whats_new",
        "billing_channels",
        "billing_collection_accounts",
        "payment_configuration_actions",
        "billing_reporting",
        "inbox",
    }
    entries = []
    for path in sorted((root / "app/web/admin").glob("*.py")):
        if path.stem not in selected:
            continue
        parsed = tree(path)
        prefixes: dict[str, str] = {}
        for node in parsed.body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "APIRouter"
            ):
                prefix = next(
                    (
                        str(ast.literal_eval(item.value))
                        for item in node.value.keywords
                        if item.arg == "prefix"
                    ),
                    "",
                )
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        prefixes[target.id] = "/admin" + prefix
        for node in parsed.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (
                    not isinstance(decorator, ast.Call)
                    or not isinstance(decorator.func, ast.Attribute)
                    or not isinstance(decorator.func.value, ast.Name)
                    or decorator.func.attr
                    not in {"get", "post", "put", "patch", "delete"}
                    or not decorator.args
                    or not isinstance(decorator.args[0], ast.Constant)
                ):
                    continue
                resolved_prefix = prefixes.get(decorator.func.value.id)
                if resolved_prefix is None:
                    raise ValueError(f"Unresolved router: {path}:{node.lineno}")
                route = resolved_prefix + str(decorator.args[0].value)
                if path.stem == "inbox" and "/settings" not in route:
                    continue
                permissions = tuple(
                    sorted(
                        {
                            str(call.args[0].value)
                            for call in ast.walk(decorator)
                            if isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Name)
                            and call.func.id == "require_permission"
                            and call.args
                            and isinstance(call.args[0], ast.Constant)
                        }
                    )
                )
                calls = tuple(
                    sorted(
                        {
                            ast.unparse(call.func)
                            for call in ast.walk(node)
                            if isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Attribute)
                            and (
                                "service" in ast.unparse(call.func)
                                or "_admin." in ast.unparse(call.func)
                            )
                        }
                    )
                )
                variants = [(route, route_tab(route))]
                if route.endswith("/settings-hub/edit/{section_id}"):
                    active, _ = declared_settings(root)
                    sections = {row.section: row.tab for row in active}
                    variants = [
                        (route.replace("{section_id}", section), tab)
                        for section, tab in sections.items()
                        if decorator.func.attr != "post"
                        or section not in {"company", "branding-values"}
                    ]
                for resolved_route, tab in variants:
                    entries.append(
                        ActionEntry(
                            decorator.func.attr.upper(),
                            resolved_route,
                            tab,
                            f"{path.relative_to(root).as_posix()}:{node.lineno}",
                            node.name,
                            permissions,
                            calls,
                            "Preserved specialised action"
                            if "/settings-hub" not in route
                            else "Canonical hub action",
                        )
                    )
    return sorted(entries, key=lambda row: (row.tab, row.path, row.method))


def build_register(root: Path) -> MigrationRegister:
    register = MigrationRegister()
    active, retired = declared_settings(root)
    known = {(row.domain, row.key) for row in active + retired}
    register.settings = (
        active + retired + extra_settings(root, known) + feature_settings(root)
    )
    register.settings += specialised_settings(
        root, {(row.domain, row.key) for row in register.settings}
    )
    owners: dict[str, str] = {}
    for path in (root / "app/services/sot_registry/domains").rglob("*.py"):
        for node in ast.walk(tree(path)):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Name)
                or node.func.id != "DomainSOT"
            ):
                continue
            fields = {item.arg: item.value for item in node.keywords if item.arg}
            if "domain" in fields and "setting_domains" in fields:
                owner = ast.literal_eval(fields["domain"])
                for domain in ast.literal_eval(fields["setting_domains"]):
                    owners[domain] = owner
    references: dict[str, set[str]] = {row.key: set() for row in register.settings}
    excluded = {"settings_spec.py", "settings_seed.py", "settings_hub_catalog.py"}
    for folder in ("app", "templates"):
        for path in (root / folder).rglob("*"):
            if (
                path.suffix not in {".py", ".html"}
                or path.name in excluded
                or "settings_specs" in path.parts
            ):
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                for key in re.findall(r"""["']([a-z][a-z0-9_]+)["']""", line):
                    if key in references:
                        references[key].add(
                            f"{path.relative_to(root).as_posix()}:{number}"
                        )
    enriched = []
    for row in register.settings:
        writer = row.metadata.get("write_owner", "control.settings_form_updates")
        if row.kind == "module":
            writer = "control.module_manager"
        elif row.kind == "feature-or-safety":
            writer = "control.feature_registry"
        elif row.kind == "specialised-or-seeded":
            writer = row.source.split(":")[0].removesuffix(".py").replace("/", ".")
        enriched.append(
            replace(
                row,
                metadata={
                    **row.metadata,
                    "setting_domain_owner": owners.get(
                        row.domain, "Declared by specialised owner"
                    ),
                    "write_owner": writer,
                    "previous_editor": f"/admin/system/settings?domain={row.domain}"
                    if row.kind == "registered"
                    else "Specialised surface, bootstrap, or retired control; see source",
                },
                source_references=tuple(sorted(references[row.key])),
            )
        )
    enriched = [
        replace(
            row,
            metadata={
                **row.metadata,
                "consumer_verification": "No literal consumer reference found; verify existing runtime ownership before claiming this control changes behaviour",
            },
        )
        if row.kind == "registered" and not row.source_references
        else row
        for row in enriched
    ]
    register.settings = enriched
    register.actions = action_inventory(root)
    for row in active:
        old = f"/admin/system/settings?domain={row.domain}"
        item = {
            "method": "GET/POST",
            "old": old,
            "new": row.destination,
            "tab": row.tab,
            "status": "Old route removed; update caller to the section-specific form",
        }
        if item not in register.retired_routes:
            register.retired_routes.append(item)
    register.retired_routes.extend(
        [
            {
                "method": "GET/POST",
                "old": "/admin/system/settings",
                "new": "/admin/system/settings-hub",
                "tab": "system",
                "status": "Entry route removed; saves use section-specific forms",
            },
            {
                "method": "GET/POST",
                "old": "/admin/system/settings?domain=branding",
                "new": "/admin/system/branding",
                "tab": "system",
                "status": "Specialised editor retained",
            },
            {
                "method": "GET/POST",
                "old": "/admin/system/settings?domain=enforcement",
                "new": "/admin/system/settings-hub?category=network",
                "tab": "network",
                "status": "All eight fields assigned to RADIUS, Usage & FUP and Network sections; no cross-domain fallback form",
            },
        ]
    )
    for old, new, tab in (
        ("/settings/branding", "/branding", "system"),
        ("/settings/smtp-senders", "/email/smtp-senders", "notifications"),
        (
            "/settings/smtp-senders/default",
            "/email/smtp-senders/default",
            "notifications",
        ),
        (
            "/settings/smtp-senders/{sender_key}/test",
            "/email/smtp-senders/{sender_key}/test",
            "notifications",
        ),
        (
            "/settings/smtp-senders/{sender_key}/delete",
            "/email/smtp-senders/{sender_key}/delete",
            "notifications",
        ),
        (
            "/settings/smtp-senders/activities",
            "/email/smtp-senders/activities",
            "notifications",
        ),
        (
            "/settings/starter-ledger-live",
            "/settings-hub/actions/starter-ledger-live",
            "billing",
        ),
        ("/settings/bank-accounts", "/settings-hub/bank-accounts", "billing"),
        (
            "/settings/bank-accounts/{bank_account_id}",
            "/settings-hub/bank-accounts/{bank_account_id}",
            "billing",
        ),
        (
            "/settings/bank-accounts/{bank_account_id}/deactivate",
            "/settings-hub/bank-accounts/{bank_account_id}/deactivate",
            "billing",
        ),
    ):
        register.retired_routes.append(
            {
                "method": "POST",
                "old": "/admin/system" + old,
                "new": "/admin/system" + new,
                "tab": tab,
                "status": "Adapter moved; existing domain owner retained",
            }
        )
    form_templates = set((root / "templates/admin/system").rglob("*.html"))
    for source in {row.source.split(":")[0] for row in register.actions}:
        for node in ast.walk(tree(root / source)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "TemplateResponse"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                candidate = root / "templates" / node.args[0].value
                if candidate.is_file():
                    form_templates.add(candidate)
    for path in sorted(form_templates):
        for name in sorted(
            set(
                re.findall(
                    r'<(?:input|select|textarea)\b[^>]*\bname="([^"]+)"',
                    path.read_text(encoding="utf-8"),
                )
            )
        ):
            register.form_fields.append(
                {
                    "source": path.relative_to(root).as_posix(),
                    "name": name,
                    "tab": route_tab("/admin/system/" + path.stem.replace("_", "-")),
                    "disposition": "Dynamic fields expand from registered specs; static fields stay with their specialised action",
                }
            )
    sources = {row.source.split(":")[0] for row in register.settings} | {
        row.source.split(":")[0] for row in register.actions
    }
    sources |= {
        "app/services/settings_hub_catalog.py",
        "app/services/web_system_settings_hub.py",
        "app/services/web_system_settings_forms.py",
    }
    register.source_hashes = {
        source: source_fingerprint(root / source) for source in sorted(sources)
    }
    assert all(
        row.tab in {tab.value for tab in SettingsTab} for row in register.settings
    )
    assert all(row.destination for row in register.settings)
    assert len({(row.domain, row.key) for row in register.settings}) == len(
        register.settings
    )
    return register


def main() -> None:
    register = build_register(ROOT)
    DESTINATION.write_text(
        json.dumps(asdict(register), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    counts = {
        tab.value: sum(row.tab == tab.value for row in register.settings)
        for tab in SettingsTab
    }
    print(
        json.dumps(
            {
                "settings": len(register.settings),
                "registered": sum(
                    row.kind == "registered" for row in register.settings
                ),
                "actions": len(register.actions),
                "tabs": {
                    TAB_NAMES[SettingsTab(key)]: value for key, value in counts.items()
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
