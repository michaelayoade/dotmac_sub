"""Strict preflight checks for Dotmac-owned OLT profile bundles."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.services.network.profile_id_allocator import DEFAULT_PROFILE_ID_RANGE

if TYPE_CHECKING:  # pragma: no cover
    from app.services.network.profile_sync import OfferProfileSyncPlan


DOTMAC_PROFILE_NAME_PREFIX = "DOTMAC_"

_PROFILE_NAME_RE = re.compile(r'\bprofile-name\s+"(?P<name>[^"]+)"', re.IGNORECASE)
_TRAFFIC_TABLE_NAME_RE = re.compile(r'\bname\s+"(?P<name>[^"]+)"', re.IGNORECASE)
_LINE_TCONT_DBA_RE = re.compile(r"\btcont\s+\d+\s+dba-profile-id\s+(?P<id>\d+)\b", re.IGNORECASE)
_GEM_MAPPING_VLAN_RE = re.compile(
    r"\bgem\s+mapping\s+\d+\s+\d+\s+vlan\s+(?P<vlan>\d+)\b",
    re.IGNORECASE,
)
_DBA_CREATE_RE = re.compile(
    r'^dba-profile\s+add\s+profile-id\s+\d+\s+profile-name\s+"(?P<name>[^"]+)"(?:\s|$)',
    re.IGNORECASE,
)
_TRAFFIC_CREATE_RE = re.compile(
    r'^traffic\s+table\s+ip\s+index\s+\d+\s+name\s+"(?P<name>[^"]+)"(?:\s|$)',
    re.IGNORECASE,
)
_LINE_CREATE_RE = re.compile(
    r'^ont-lineprofile\s+gpon\s+profile-id\s+\d+\s+profile-name\s+"(?P<name>[^"]+)"$',
    re.IGNORECASE,
)
_SERVICE_CREATE_RE = re.compile(
    r'^ont-srvprofile\s+gpon\s+profile-id\s+\d+\s+profile-name\s+"(?P<name>[^"]+)"$',
    re.IGNORECASE,
)
_DISALLOWED_MUTATION_RE = re.compile(
    r"\b(delete|remove|undo|modify|rename|reset|erase|clear)\b",
    re.IGNORECASE,
)
_LINE_BODY_RE = re.compile(
    r"^(tcont\s+\d+\s+dba-profile-id\s+\d+|gem\s+add\s+\d+\s+eth\s+tcont\s+\d+|gem\s+mapping\s+\d+\s+\d+\s+vlan\s+\d+|commit|quit)$",
    re.IGNORECASE,
)
_SERVICE_BODY_RE = re.compile(
    r"^(ont-port\s+eth\s+\d+|port\s+vlan\s+eth\s+\d+\s+\d+|commit|quit)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProfileInventory:
    """OLT profile inventory relevant to Dotmac bundle creation."""

    dba_profile_ids: frozenset[int] = frozenset()
    dba_profile_names: frozenset[str] = frozenset()
    traffic_table_ids: frozenset[int] = frozenset()
    traffic_table_names: frozenset[str] = frozenset()
    line_profile_ids: frozenset[int] = frozenset()
    line_profile_names: frozenset[str] = frozenset()
    service_profile_ids: frozenset[int] = frozenset()
    service_profile_names: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ProfileInventoryPreflightResult:
    success: bool
    errors: tuple[str, ...] = ()
    checked_ids: dict[str, int] = field(default_factory=dict)
    checked_names: dict[str, str] = field(default_factory=dict)

    @property
    def message(self) -> str:
        if self.success:
            return "Profile inventory preflight passed"
        return "Profile inventory preflight failed: " + "; ".join(self.errors)


@dataclass(frozen=True)
class DotmacProfileOwnershipResult:
    """Result of validating that a profile apply plan only creates Dotmac profiles."""

    success: bool
    errors: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        if self.success:
            return "Dotmac profile ownership preflight passed"
        return "Dotmac profile ownership preflight failed: " + "; ".join(self.errors)


def build_profile_inventory(
    *,
    dba_profiles: list[Any] | tuple[Any, ...] = (),
    traffic_tables: list[Any] | tuple[Any, ...] = (),
    line_profiles: list[Any] | tuple[Any, ...] = (),
    service_profiles: list[Any] | tuple[Any, ...] = (),
) -> ProfileInventory:
    """Normalize live/imported OLT profile lists into preflight inventory."""
    return ProfileInventory(
        dba_profile_ids=frozenset(_entry_ids(dba_profiles, "profile_id", "DBA")),
        dba_profile_names=frozenset(_entry_names(dba_profiles)),
        traffic_table_ids=frozenset(_entry_ids(traffic_tables, "index", "traffic table")),
        traffic_table_names=frozenset(_entry_names(traffic_tables)),
        line_profile_ids=frozenset(_entry_ids(line_profiles, "profile_id", "line")),
        line_profile_names=frozenset(_entry_names(line_profiles)),
        service_profile_ids=frozenset(
            _entry_ids(service_profiles, "profile_id", "service")
        ),
        service_profile_names=frozenset(_entry_names(service_profiles)),
    )


def validate_offer_profile_sync_plan_inventory(
    sync_plan: OfferProfileSyncPlan,
    inventory: ProfileInventory,
    *,
    id_range: tuple[int, int] = DEFAULT_PROFILE_ID_RANGE,
) -> ProfileInventoryPreflightResult:
    """Fail if a generated Dotmac profile bundle conflicts with OLT inventory."""
    bundle = sync_plan.bundle
    checked_ids = {
        "dba_profile_id": bundle.dba_profile_id,
        "download_traffic_table_id": bundle.download_traffic_table_id,
        "upload_traffic_table_id": bundle.upload_traffic_table_id,
        "line_profile_id": bundle.line_profile_id,
        "service_profile_id": bundle.service_profile_id,
    }
    checked_names = _planned_profile_names(sync_plan)
    errors: list[str] = []

    start, end = id_range
    for label, profile_id in checked_ids.items():
        if profile_id < start or profile_id > end:
            errors.append(f"{label}={profile_id} outside reserved range {start}-{end}")

    if bundle.download_traffic_table_id == bundle.upload_traffic_table_id:
        errors.append("download and upload traffic table IDs must be distinct")

    _fail_if_existing_id(errors, "DBA profile", bundle.dba_profile_id, inventory.dba_profile_ids)
    _fail_if_existing_id(
        errors,
        "download traffic table",
        bundle.download_traffic_table_id,
        inventory.traffic_table_ids,
    )
    _fail_if_existing_id(
        errors,
        "upload traffic table",
        bundle.upload_traffic_table_id,
        inventory.traffic_table_ids,
    )
    _fail_if_existing_id(errors, "line profile", bundle.line_profile_id, inventory.line_profile_ids)
    _fail_if_existing_id(
        errors,
        "service profile",
        bundle.service_profile_id,
        inventory.service_profile_ids,
    )

    _fail_if_bad_name(
        errors,
        "DBA profile",
        checked_names.get("dba", ""),
        inventory.dba_profile_names,
    )
    _fail_if_bad_name(
        errors,
        "download traffic table",
        checked_names.get("traffic_down", ""),
        inventory.traffic_table_names,
    )
    _fail_if_bad_name(
        errors,
        "upload traffic table",
        checked_names.get("traffic_up", ""),
        inventory.traffic_table_names,
    )
    _fail_if_bad_name(
        errors,
        "line profile",
        checked_names.get("line", ""),
        inventory.line_profile_names,
    )
    _fail_if_bad_name(
        errors,
        "service profile",
        checked_names.get("service", ""),
        inventory.service_profile_names,
    )

    _validate_command_dependencies(errors, sync_plan)

    return ProfileInventoryPreflightResult(
        success=not errors,
        errors=tuple(errors),
        checked_ids=checked_ids,
        checked_names=checked_names,
    )


def validate_dotmac_profile_apply_plan(
    apply_plan: Any,
) -> DotmacProfileOwnershipResult:
    """Fail if a saved profile bundle plan can mutate non-Dotmac profiles.

    Bundle apply is intended to create new Dotmac-owned Huawei profile objects
    only. This check intentionally allow-lists the generated create commands and
    their in-profile body commands instead of trying to classify arbitrary CLI.
    """
    errors: list[str] = []
    groups = tuple(getattr(apply_plan, "groups", ()) or ())
    if not groups:
        return DotmacProfileOwnershipResult(
            success=False,
            errors=("apply plan has no command groups",),
        )

    for group in groups:
        step = str(getattr(group, "step", "") or "").casefold()
        commands = tuple(str(command).strip() for command in getattr(group, "commands", ()) or ())
        if not commands:
            errors.append(f"command group {getattr(group, 'step', '')!r} has no commands")
            continue

        if "dba" in step:
            _validate_named_create_group(errors, "DBA profile", commands, _DBA_CREATE_RE)
        elif "traffic" in step:
            _validate_named_create_group(
                errors,
                "traffic table",
                commands,
                _TRAFFIC_CREATE_RE,
            )
        elif "line" in step:
            _validate_profile_body_group(
                errors,
                "line profile",
                commands,
                _LINE_CREATE_RE,
                _LINE_BODY_RE,
            )
        elif "service" in step:
            _validate_profile_body_group(
                errors,
                "service profile",
                commands,
                _SERVICE_CREATE_RE,
                _SERVICE_BODY_RE,
            )
        else:
            errors.append(f"unsupported command group {getattr(group, 'step', '')!r}")

    return DotmacProfileOwnershipResult(success=not errors, errors=tuple(errors))


def _entry_ids(entries: list[Any] | tuple[Any, ...], attr: str, label: str) -> set[int]:
    ids: set[int] = set()
    for entry in entries:
        value = getattr(entry, attr, None)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label} inventory entry {entry!r} has invalid {attr}")
        ids.add(value)
    return ids


def _entry_names(entries: list[Any] | tuple[Any, ...]) -> set[str]:
    names: set[str] = set()
    for entry in entries:
        name = str(getattr(entry, "name", "") or "").strip()
        if name:
            names.add(name.casefold())
    return names


def _planned_profile_names(sync_plan: OfferProfileSyncPlan) -> dict[str, str]:
    commands = sync_plan.apply_plan.commands
    return {
        "dba": _extract_quoted_name(_command_at(commands, 0), _PROFILE_NAME_RE),
        "traffic_down": _extract_quoted_name(
            _command_at(commands, 1),
            _TRAFFIC_TABLE_NAME_RE,
        ),
        "traffic_up": _extract_quoted_name(
            _command_at(commands, 2),
            _TRAFFIC_TABLE_NAME_RE,
        ),
        "line": _extract_quoted_name(_command_at(commands, 3), _PROFILE_NAME_RE),
        "service": _extract_quoted_name(_command_at(commands, 9), _PROFILE_NAME_RE),
    }


def _command_at(commands: tuple[str, ...], index: int) -> str:
    return commands[index] if len(commands) > index else ""


def _extract_quoted_name(command: str, regex: re.Pattern[str]) -> str:
    match = regex.search(command)
    return match.group("name").strip() if match else ""


def _fail_if_existing_id(
    errors: list[str],
    label: str,
    profile_id: int,
    existing_ids: frozenset[int],
) -> None:
    if profile_id in existing_ids:
        errors.append(f"{label} ID {profile_id} already exists on OLT")


def _fail_if_bad_name(
    errors: list[str],
    label: str,
    name: str,
    existing_names: frozenset[str],
) -> None:
    if not name:
        errors.append(f"{label} command is missing a profile name")
        return
    if not name.startswith(DOTMAC_PROFILE_NAME_PREFIX):
        errors.append(f"{label} name {name!r} must start with {DOTMAC_PROFILE_NAME_PREFIX}")
    if name.casefold() in existing_names:
        errors.append(f"{label} name {name!r} already exists on OLT")


def _validate_named_create_group(
    errors: list[str],
    label: str,
    commands: tuple[str, ...],
    create_regex: re.Pattern[str],
) -> None:
    for command in commands:
        if _DISALLOWED_MUTATION_RE.search(command):
            errors.append(f"{label} command is not create-only: {command}")
            continue
        match = create_regex.match(command)
        if match is None:
            errors.append(f"{label} command is not an allowed Dotmac create command: {command}")
            continue
        _validate_dotmac_name(errors, label, match.group("name"))


def _validate_profile_body_group(
    errors: list[str],
    label: str,
    commands: tuple[str, ...],
    create_regex: re.Pattern[str],
    body_regex: re.Pattern[str],
) -> None:
    create_command = commands[0]
    if _DISALLOWED_MUTATION_RE.search(create_command):
        errors.append(f"{label} command is not create-only: {create_command}")
    create_match = create_regex.match(create_command)
    if create_match is None:
        errors.append(f"{label} first command is not an allowed Dotmac create command: {create_command}")
    else:
        _validate_dotmac_name(errors, label, create_match.group("name"))

    for command in commands[1:]:
        if _DISALLOWED_MUTATION_RE.search(command):
            errors.append(f"{label} body command is not create-only: {command}")
            continue
        if body_regex.match(command) is None:
            errors.append(f"{label} body command is not allowed: {command}")


def _validate_dotmac_name(errors: list[str], label: str, name: str) -> None:
    if not name.startswith(DOTMAC_PROFILE_NAME_PREFIX):
        errors.append(f"{label} name {name!r} must start with {DOTMAC_PROFILE_NAME_PREFIX}")


def _validate_command_dependencies(
    errors: list[str],
    sync_plan: OfferProfileSyncPlan,
) -> None:
    bundle = sync_plan.bundle
    commands = sync_plan.apply_plan.commands
    dba_refs = {
        int(match.group("id"))
        for command in commands
        for match in _LINE_TCONT_DBA_RE.finditer(command)
    }
    if dba_refs != {bundle.dba_profile_id}:
        errors.append(
            "line profile DBA reference mismatch: "
            f"expected {bundle.dba_profile_id}, got {sorted(dba_refs)}"
        )

    vlan_refs = {
        int(match.group("vlan"))
        for command in commands
        for match in _GEM_MAPPING_VLAN_RE.finditer(command)
    }
    if vlan_refs != {bundle.vlan_id}:
        errors.append(
            "line profile VLAN mapping mismatch: "
            f"expected {bundle.vlan_id}, got {sorted(vlan_refs)}"
        )
