"""OLT SSH connection helpers with model-specific transport policies.

This module provides SSH connectivity and CLI parsing for Huawei OLTs.
Parsing is done via TextFSM templates (see parsers/ subdirectory) with
fallback to legacy regex parsing for robustness.
"""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass, field

from paramiko.channel import Channel
from paramiko.ssh_exception import SSHException
from paramiko.transport import Transport

logger = logging.getLogger(__name__)

from app.models.network import OLTDevice
from app.services.credential_crypto import decrypt_credential
from app.services.network.olt_command_gen import build_service_port_command
from app.services.network.olt_validators import (
    ValidationError,
    validate_ip_address,
    validate_ont_id,
    validate_subnet_mask,
    validate_vlan_id,
)

# TextFSM-based parsers (preferred)
try:
    from app.services.network.parsers import parse_autofind as _textfsm_parse_autofind
    from app.services.network.parsers import parse_key_value as _textfsm_parse_key_value
    from app.services.network.parsers import (
        parse_profile_table as _textfsm_parse_profile_table,
    )
    from app.services.network.parsers import (
        parse_service_port_table as _textfsm_parse_service_port_table,
    )

    _TEXTFSM_AVAILABLE = True
except ImportError:
    _TEXTFSM_AVAILABLE = False
    logger.warning("TextFSM parsers not available, using legacy regex parsing")


@dataclass(frozen=True)
class OltSshPolicy:
    key: str
    kex: tuple[str, ...]
    host_key_types: tuple[str, ...]
    ciphers: tuple[str, ...]
    macs: tuple[str, ...]
    prompt_regex: str = r"[>#]\s*$"
    version_command: str = "display version"


_HUAWEI_LEGACY_KEX = (
    "diffie-hellman-group-exchange-sha1",
    "diffie-hellman-group1-sha1",
)
_HUAWEI_HOST_KEYS = ("ssh-rsa",)
_HUAWEI_MACS = ("hmac-sha1",)

_POLICIES: dict[str, OltSshPolicy] = {
    "huawei_ma5608t": OltSshPolicy(
        key="huawei_ma5608t",
        kex=_HUAWEI_LEGACY_KEX,
        host_key_types=_HUAWEI_HOST_KEYS,
        ciphers=("aes128-cbc",),
        macs=_HUAWEI_MACS,
    ),
    "huawei_ma5800": OltSshPolicy(
        key="huawei_ma5800",
        kex=_HUAWEI_LEGACY_KEX,
        host_key_types=_HUAWEI_HOST_KEYS,
        ciphers=("aes256-ctr",),
        macs=_HUAWEI_MACS,
    ),
    "huawei_ma5600": OltSshPolicy(
        key="huawei_ma5600",
        kex=_HUAWEI_LEGACY_KEX,
        host_key_types=_HUAWEI_HOST_KEYS,
        ciphers=("aes128-cbc",),
        macs=_HUAWEI_MACS,
    ),
}


def _normalized(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def resolve_policy(olt: OLTDevice) -> OltSshPolicy:
    vendor = _normalized(olt.vendor)
    model = _normalized(olt.model)
    if vendor == "huawei":
        if "ma5608t" in model:
            return _POLICIES["huawei_ma5608t"]
        if "ma5800" in model:
            return _POLICIES["huawei_ma5800"]
        if "ma5600" in model:
            return _POLICIES["huawei_ma5600"]
    raise ValueError(
        f"No SSH driver policy found for vendor={olt.vendor!r}, model={olt.model!r}"
    )


def _apply_preferred_algorithms(transport: Transport, policy: OltSshPolicy) -> None:
    opts = transport.get_security_options()
    opts.kex = list(policy.kex) + [item for item in opts.kex if item not in policy.kex]
    opts.key_types = list(policy.host_key_types) + [
        item for item in opts.key_types if item not in policy.host_key_types
    ]
    opts.ciphers = list(policy.ciphers) + [
        item for item in opts.ciphers if item not in policy.ciphers
    ]
    opts.digests = list(policy.macs) + [
        item for item in opts.digests if item not in policy.macs
    ]


def _read_until_prompt(
    channel: Channel, prompt_regex: str, timeout_sec: float = 8.0
) -> str:
    import time

    compiled = re.compile(prompt_regex)
    buffer = ""
    deadline = time.monotonic() + timeout_sec
    channel.settimeout(0.8)
    while True:
        if time.monotonic() > deadline:
            return buffer
        try:
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
        except TimeoutError:
            if compiled.search(buffer):
                return buffer
            continue
        if not chunk:
            return buffer
        buffer += chunk
        if compiled.search(buffer):
            return buffer


def run_version_probe(olt: OLTDevice) -> tuple[str, str]:
    """SSH into an OLT and run the version command."""
    transport, channel, policy = _open_shell(olt)
    try:
        channel.send(f"{policy.version_command}\n")
        output = _read_until_prompt(channel, policy.prompt_regex, timeout_sec=12)
        return policy.key, output
    finally:
        transport.close()


_FSP_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{1,3}$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9\-]+$")


def _auto_bind_tr069_after_authorize(
    olt: OLTDevice, fsp: str, ont_id: int | None
) -> None:
    """Best-effort: bind newly authorized ONT to DotMac-ACS TR-069 profile.

    Called after successful ONT authorization. Silently skips if no
    DotMac-ACS profile exists or if binding fails.
    """
    if ont_id is None:
        return
    try:
        ok, _msg, profiles = get_tr069_server_profiles(olt)
        if not ok:
            return
        dotmac_id = None
        for p in profiles:
            if "dotmac" in p.name.lower() or "10.10.41.1" in (p.acs_url or ""):
                dotmac_id = p.profile_id
                break
        if dotmac_id is None:
            return
        ok, msg = bind_tr069_server_profile(
            olt, fsp=fsp, ont_id=ont_id, profile_id=dotmac_id
        )
        if ok:
            logger.info(
                "Auto-bound ONT %d on %s to TR-069 profile %d", ont_id, fsp, dotmac_id
            )
        else:
            logger.warning(
                "Auto-bind TR-069 failed for ONT %d on %s: %s", ont_id, fsp, msg
            )
    except Exception as exc:
        logger.warning("Auto-bind TR-069 error for ONT %d: %s", ont_id, exc)


def _validate_fsp(fsp: str) -> tuple[bool, str]:
    """Validate Frame/Slot/Port format is strictly numeric (e.g. '0/2/1')."""
    if not _FSP_RE.match(fsp):
        return False, f"Invalid F/S/P format: {fsp!r} (expected digits/digits/digits)"
    return True, ""


def _validate_serial(serial_number: str) -> tuple[bool, str]:
    """Validate ONT serial number contains only alphanumeric chars and dashes."""
    if not serial_number or not _SERIAL_RE.match(serial_number):
        return False, f"Invalid serial number format: {serial_number!r}"
    return True, ""


@dataclass
class AutofindEntry:
    """A single ONT discovered via Huawei autofind."""

    fsp: str  # Frame/Slot/Port e.g. "0/2/1"
    serial_number: str  # e.g. "HWTC-7D4733C3"
    serial_hex: str  # e.g. "485754437D4733C3"
    vendor_id: str
    model: str  # EquipmentID e.g. "EG8145V5"
    software_version: str
    mac: str
    equipment_sn: str
    autofind_time: str


def _open_shell(olt: OLTDevice) -> tuple[Transport, Channel, OltSshPolicy]:
    """Open an SSH shell session to an OLT. Caller must close transport."""
    host = (olt.mgmt_ip or olt.hostname or "").strip()
    if not host:
        raise ValueError("Management IP or hostname is required")
    if not olt.ssh_username:
        raise ValueError("SSH username is required")
    if not olt.ssh_password:
        raise ValueError("SSH password is required")

    policy = resolve_policy(olt)
    password = decrypt_credential(olt.ssh_password)
    if not password:
        raise ValueError("SSH password could not be decrypted")

    port = int(olt.ssh_port or 22)
    sock = socket.create_connection((host, port), timeout=20)
    transport = Transport(sock)
    _apply_preferred_algorithms(transport, policy)
    transport.start_client(timeout=20)
    transport.auth_password(username=olt.ssh_username, password=password)
    if not transport.is_authenticated():
        transport.close()
        raise RuntimeError("SSH authentication failed")
    channel = transport.open_session(timeout=20)
    channel.get_pty(width=200)
    channel.invoke_shell()
    _read_until_prompt(channel, policy.prompt_regex, timeout_sec=8)
    channel.send("screen-length 0 temporary\n")
    _read_until_prompt(channel, policy.prompt_regex, timeout_sec=8)
    return transport, channel, policy


def _parse_huawei_autofind_legacy(output: str) -> list[AutofindEntry]:
    """Legacy regex parser for ``display ont autofind all`` output.

    Used as fallback when TextFSM parsing fails.
    """
    entries: list[AutofindEntry] = []
    blocks = re.split(r"-{10,}", output)
    current: dict[str, str] = {}
    for block in blocks:
        lines = block.strip().splitlines()
        for line in lines:
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            current[key.strip()] = value.strip()
        sn_raw = current.get("Ont SN", "")
        if not sn_raw:
            continue
        # Extract "HWTC-7D4733C3" from "485754437D4733C3 (HWTC-7D4733C3)"
        sn_match = re.search(r"\(([^)]+)\)", sn_raw)
        serial_display = sn_match.group(1) if sn_match else sn_raw.split()[0]
        serial_hex = sn_raw.split()[0] if " " in sn_raw else sn_raw
        entries.append(
            AutofindEntry(
                fsp=current.get("F/S/P", ""),
                serial_number=serial_display,
                serial_hex=serial_hex,
                vendor_id=current.get("VendorID", ""),
                model=current.get("Ont EquipmentID", ""),
                software_version=current.get("Ont SoftwareVersion", ""),
                mac=current.get("Ont MAC", ""),
                equipment_sn=current.get("Ont Equipment SN", ""),
                autofind_time=current.get("Ont autofind time", ""),
            )
        )
        current = {}
    return entries


def _parse_huawei_autofind(output: str) -> list[AutofindEntry]:
    """Parse the output of ``display ont autofind all``.

    Uses TextFSM template for robust parsing with fallback to legacy regex.
    """
    if _TEXTFSM_AVAILABLE:
        try:
            result = _textfsm_parse_autofind(output)
            if result.success and result.data:
                # Convert from parser dataclass to local dataclass
                return [
                    AutofindEntry(
                        fsp=e.fsp,
                        serial_number=e.serial_number,
                        serial_hex=e.serial_hex,
                        vendor_id=e.vendor_id,
                        model=e.model,
                        software_version=e.software_version,
                        mac=e.mac,
                        equipment_sn=e.equipment_sn,
                        autofind_time=e.autofind_time,
                    )
                    for e in result.data
                ]
            if result.warnings:
                logger.debug("TextFSM autofind warnings: %s", result.warnings)
        except Exception as e:
            logger.debug("TextFSM autofind parse failed, using legacy: %s", e)

    # Fallback to legacy regex parsing
    return _parse_huawei_autofind_legacy(output)


def get_autofind_onts(olt: OLTDevice) -> tuple[bool, str, list[AutofindEntry]]:
    """SSH into OLT and retrieve unregistered ONTs from autofind table.

    Returns:
        Tuple of (success, message, list of autofind entries).
    """
    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []
    except Exception as exc:
        logger.error("Unexpected error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", []

    try:
        # Enter enable mode and set terminal length
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        channel.send("display ont autofind all\n")
        # Huawei prompts "{ <cr>||<K> }:" — send CR to confirm
        initial = _read_until_prompt(channel, r"#\s*$|<cr>", timeout_sec=10)
        if "<cr>" in initial:
            channel.send("\n")
            output = _read_until_prompt(channel, r"#\s*$", timeout_sec=15)
        else:
            output = initial
        entries = _parse_huawei_autofind(output)
        count = len(entries)
        msg = f"Found {count} unregistered ONT{'s' if count != 1 else ''}"
        return True, msg, entries
    except Exception as exc:
        logger.error("Error reading autofind from OLT %s: %s", olt.name, exc)
        return False, f"Error reading autofind: {exc}", []
    finally:
        transport.close()


def authorize_ont(
    olt: OLTDevice,
    fsp: str,
    serial_number: str,
    *,
    line_profile_id: int | None = None,
    service_profile_id: int | None = None,
) -> tuple[bool, str, int | None]:
    """SSH into OLT and register an ONT via sn-auth on the given port.

    Args:
        olt: The OLT device to connect to.
        fsp: Frame/Slot/Port string, e.g. "0/2/1".
        serial_number: ONT serial in vendor format, e.g. "HWTC-7D4733C3".
        line_profile_id: Optional OLT line profile ID (defaults to 1).
        service_profile_id: Optional OLT service profile ID (defaults to 1).

    Returns:
        Tuple of (success, message, assigned_ont_id).
    """
    line_pid = line_profile_id if line_profile_id is not None else 1
    srv_pid = service_profile_id if service_profile_id is not None else 1
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err, None
    ok, err = _validate_serial(serial_number)
    if not ok:
        return False, err, None

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None
    except Exception as exc:
        logger.error("Unexpected error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", None

    try:
        # Enter enable mode
        channel.send("enable\n")
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

        # Enter config mode
        config_prompt = r"[#)]\s*$"
        channel.send("config\n")
        _read_until_prompt(channel, config_prompt, timeout_sec=5)

        # Enter GPON interface for the frame/slot
        parts = fsp.split("/")
        frame_slot = f"{parts[0]}/{parts[1]}"
        port_num = parts[2]

        channel.send(f"interface gpon {frame_slot}\n")
        _read_until_prompt(channel, config_prompt, timeout_sec=5)

        # Authorize the ONT — sn-auth uses the serial without dashes
        sn_clean = serial_number.replace("-", "")
        channel.send(
            f"ont add {port_num} sn-auth {sn_clean} omci ont-lineprofile-id {line_pid} ont-srvprofile-id {srv_pid}\n"
        )
        # Huawei may prompt "{ <cr>|desc<K>|ont-type<K> }:" — send CR to confirm
        initial = _read_until_prompt(channel, r"[#)]\s*$|<cr>", timeout_sec=10)
        if "<cr>" in initial:
            channel.send("\n")
            output = _read_until_prompt(channel, r"[#)]\s*$", timeout_sec=10)
        else:
            output = initial

        # Exit config mode
        channel.send("quit\n")
        _read_until_prompt(channel, config_prompt, timeout_sec=3)
        channel.send("quit\n")
        _read_until_prompt(channel, config_prompt, timeout_sec=3)

        # Check for success indicators
        ont_id_match = re.search(r"ont-?id\D+(\d+)", output, flags=re.IGNORECASE)
        ont_id = int(ont_id_match.group(1)) if ont_id_match else None

        if "success" in output.lower() or "ont-id" in output.lower():
            logger.info(
                "Authorized ONT %s on OLT %s port %s",
                serial_number,
                olt.name,
                fsp,
            )
            # Auto-bind to DotMac-ACS TR-069 profile if it exists
            _auto_bind_tr069_after_authorize(olt, fsp, ont_id)

            message = f"ONT {serial_number} authorized on port {fsp}"
            if ont_id is not None:
                message += f" (ONT-ID {ont_id})"
            return True, message, ont_id
        if is_error_output(output):
            logger.warning(
                "Failed to authorize ONT %s on OLT %s: %s",
                serial_number,
                olt.name,
                output.strip(),
            )
            return False, f"OLT rejected command: {output.strip()[-200:]}", None

        # Ambiguous — return output for inspection
        logger.info(
            "ONT authorize command sent for %s on OLT %s, output: %s",
            serial_number,
            olt.name,
            output.strip(),
        )
        return True, f"Command sent for {serial_number} on port {fsp}", ont_id
    except Exception as exc:
        logger.error(
            "Error authorizing ONT %s on OLT %s: %s",
            serial_number,
            olt.name,
            exc,
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()


@dataclass
class ServicePortEntry:
    """A single L2 service-port binding on a Huawei OLT."""

    index: int
    vlan_id: int
    ont_id: int  # VPI column = ONT-ID for GPON
    gem_index: int  # VCI column = GEM index for GPON
    flow_type: str  # e.g. "vlan"
    flow_para: str  # e.g. "203"
    state: str  # "up" or "down"


def _parse_service_port_table_legacy(output: str) -> list[ServicePortEntry]:
    """Legacy regex parser for ``display service-port`` output.

    Used as fallback when TextFSM parsing fails.
    """
    entries: list[ServicePortEntry] = []
    for line in output.splitlines():
        line = line.strip()
        # Match lines like: "27  201 common   gpon 0/2 /1  0    2     vlan  201  86   86   up"
        # Fields: INDEX VLAN_ID VLAN_ATTR PORT_TYPE F/S/P VPI(ONT) VCI(GEM) FLOW_TYPE FLOW_PARA RX TX STATE
        parts = line.split()
        if len(parts) < 10:
            continue
        try:
            index = int(parts[0])
            vlan_id = int(parts[1])
        except (ValueError, IndexError):
            continue
        # Find ont_id and gem_index — they follow the "gpon F/S /P" pattern
        # The port reference may be split (e.g. "0/2 /1") so find numerics after "gpon"
        try:
            gpon_idx = parts.index("gpon")
        except ValueError:
            continue
        # After "gpon" and the F/S/P tokens, next two ints are ONT-ID and GEM index
        nums_after_gpon: list[int] = []
        for token in parts[gpon_idx + 1 :]:
            # Skip F/S/P fragments like "0/2" or "/1"
            cleaned = token.strip("/").replace("/", "")
            if cleaned.isdigit() and "/" not in token:
                nums_after_gpon.append(int(cleaned))
            if len(nums_after_gpon) == 2:
                break
        if len(nums_after_gpon) < 2:
            continue
        ont_id, gem_index = nums_after_gpon[0], nums_after_gpon[1]
        # Flow type and state are near the end
        state = parts[-1].lower() if parts[-1].lower() in ("up", "down") else "unknown"
        flow_type = ""
        flow_para = ""
        for i, p in enumerate(parts):
            if p in ("vlan", "ppp", "ip", "ip4", "ip6"):
                flow_type = p
                if i + 1 < len(parts):
                    flow_para = parts[i + 1]
                break
        entries.append(
            ServicePortEntry(
                index=index,
                vlan_id=vlan_id,
                ont_id=ont_id,
                gem_index=gem_index,
                flow_type=flow_type,
                flow_para=flow_para,
                state=state,
            )
        )
    return entries


def _parse_service_port_table(output: str) -> list[ServicePortEntry]:
    """Parse Huawei ``display service-port`` output into structured entries.

    Uses TextFSM template for robust parsing with fallback to legacy regex.
    """
    if "gpon" not in output.lower():
        return []
    if _TEXTFSM_AVAILABLE:
        try:
            result = _textfsm_parse_service_port_table(output)
            if result.success and result.data:
                # Convert from parser dataclass to local dataclass
                return [
                    ServicePortEntry(
                        index=e.index,
                        vlan_id=e.vlan_id,
                        ont_id=e.ont_id,
                        gem_index=e.gem_index,
                        flow_type=e.flow_type,
                        flow_para=e.flow_para,
                        state=e.state,
                    )
                    for e in result.data
                ]
            if result.warnings:
                logger.debug("TextFSM service-port warnings: %s", result.warnings)
        except Exception as e:
            logger.debug("TextFSM service-port parse failed, using legacy: %s", e)

    # Fallback to legacy regex parsing
    return _parse_service_port_table_legacy(output)


_HUAWEI_ERROR_PATTERNS = (
    "failure",
    "error",
    "% parameter error",
    "% unknown command",
    "command not found",
    "invalid",
    "unrecognized",
    "incomplete command",
    "\u5931\u8d25",  # Chinese: "失败" (failure)
    "\u9519\u8bef",  # Chinese: "错误" (error)
)


def is_error_output(output: str) -> bool:
    """Check if Huawei CLI output indicates an error.

    Detects common error patterns across English and Chinese locales.
    """
    lower = output.lower()
    return any(pattern in lower for pattern in _HUAWEI_ERROR_PATTERNS)


def _run_huawei_cmd(channel: Channel, command: str, prompt: str = r"#\s*$") -> str:
    """Send a command to a Huawei shell, handling interactive {<cr>} prompts."""
    channel.send(f"{command}\n")
    out = _read_until_prompt(channel, rf"{prompt}|<cr>", timeout_sec=12)
    if "<cr>" in out:
        channel.send("\n")
        out = _read_until_prompt(channel, prompt, timeout_sec=12)
    return out


def _run_huawei_paged_cmd(
    channel: Channel, command: str, prompt: str = r"#\s*$", *, timeout_sec: int = 60
) -> str:
    """Send a command and handle pagination (---- More ----) prompts."""
    channel.send(f"{command}\n")
    output_parts: list[str] = []
    pager_pattern = r"---- More ----|<cr>|Press any key"
    combined_pattern = rf"{prompt}|{pager_pattern}"

    while True:
        chunk = _read_until_prompt(channel, combined_pattern, timeout_sec=timeout_sec)
        output_parts.append(chunk)

        # Check if we hit a pager prompt
        if "---- More ----" in chunk or "Press any key" in chunk:
            channel.send(" ")  # Send space to continue
            continue
        elif "<cr>" in chunk:
            channel.send("\n")
            continue
        else:
            # Hit the shell prompt - we're done
            break

    return "".join(output_parts)


def get_service_ports(
    olt: OLTDevice, fsp: str
) -> tuple[bool, str, list[ServicePortEntry]]:
    """SSH into OLT and list service-ports on a PON port.

    Returns:
        Tuple of (success, message, list of ServicePortEntry).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err, []

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", []

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        output = _run_huawei_cmd(channel, f"display service-port port {fsp}")
        entries = _parse_service_port_table(output)
        return True, f"Found {len(entries)} service-ports on {fsp}", entries
    except Exception as exc:
        logger.error("Error reading service-ports from OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}", []
    finally:
        transport.close()


def create_service_ports(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    reference_ports: list[ServicePortEntry],
) -> tuple[bool, str]:
    """Create service-ports on an OLT for a new ONT, copying VLAN pattern from reference.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port e.g. "0/2/1".
        ont_id: The ONT-ID assigned by the OLT during authorization.
        reference_ports: Service-port entries from a reference ONT to replicate.

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err
    if not reference_ports:
        return False, "No reference service-ports to replicate"

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        _run_huawei_cmd(channel, "config", prompt=config_prompt)

        created = 0
        errors = 0
        for sp in reference_ports:
            user_vlan: int | str | None = None
            if str(getattr(sp, "flow_para", "") or "").isdigit():
                user_vlan = int(str(sp.flow_para))
            elif getattr(sp, "flow_para", None) in {"untagged", "transparent"}:
                user_vlan = str(sp.flow_para)

            tag_transform = getattr(sp, "tag_transform", None) or "translate"
            if user_vlan == "untagged" and tag_transform == "translate":
                tag_transform = "default"

            cmd = build_service_port_command(
                fsp=fsp,
                ont_id=ont_id,
                gem_index=sp.gem_index,
                vlan_id=sp.vlan_id,
                user_vlan=user_vlan,
                tag_transform=tag_transform,
            )
            output = _run_huawei_cmd(channel, cmd, prompt=config_prompt)
            if is_error_output(output):
                logger.warning(
                    "Service-port VLAN %d GEM %d failed on OLT %s: %s",
                    sp.vlan_id,
                    sp.gem_index,
                    olt.name,
                    output.strip()[-150:],
                )
                errors += 1
            else:
                created += 1

        _run_huawei_cmd(channel, "quit", prompt=config_prompt)

        msg = f"Created {created} service-port(s) for ONT {ont_id} on {fsp}"
        if errors:
            msg += f" ({errors} failed)"
        logger.info(msg)
        return errors == 0, msg
    except Exception as exc:
        logger.error("Error creating service-ports on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()


def upgrade_firmware(
    olt: OLTDevice, file_url: str, *, method: str = "sftp"
) -> tuple[bool, str]:
    """Trigger firmware upgrade on an OLT via SSH.

    Args:
        olt: The OLT device to upgrade.
        file_url: URL/path of the firmware file (e.g. sftp://user:pass@host/path).
        method: Transfer method — sftp, tftp, or ftp.

    Returns:
        Tuple of (success, message).
    """
    # Reject newlines and shell metacharacters in file_url
    if not file_url or "\n" in file_url or "\r" in file_url or ";" in file_url:
        return False, "Invalid firmware URL"

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error(
            "Error connecting to OLT %s for firmware upgrade: %s", olt.name, exc
        )
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        # Huawei firmware upgrade command
        cmd = f"system-software upgrade {file_url}"
        output = _run_huawei_cmd(channel, cmd, prompt=r"#\s*$|y/n")

        # Huawei may ask for confirmation
        if "y/n" in output.lower():
            channel.send("y\n")
            output += _read_until_prompt(channel, r"#\s*$", timeout_sec=30)

        if "success" in output.lower() or "download" in output.lower():
            logger.info("Firmware upgrade initiated on OLT %s: %s", olt.name, file_url)
            return (
                True,
                "Firmware upgrade initiated — OLT will reboot when download completes",
            )
        if is_error_output(output):
            logger.warning(
                "Firmware upgrade failed on OLT %s: %s", olt.name, output.strip()[-200:]
            )
            return False, f"OLT rejected upgrade: {output.strip()[-200:]}"

        logger.info(
            "Firmware upgrade command sent to OLT %s, output: %s",
            olt.name,
            output.strip()[-200:],
        )
        return True, "Firmware upgrade command sent"
    except Exception as exc:
        logger.error("Error during firmware upgrade on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()


def fetch_running_config_ssh(olt: OLTDevice) -> tuple[bool, str, str]:
    """Fetch the full running configuration from an OLT via SSH.

    Returns (success, message, config_text).
    """
    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", ""
    except Exception as exc:
        logger.error("Unexpected error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", ""

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

        channel.send("display current-configuration\n")
        output = _read_until_prompt(channel, policy.prompt_regex, timeout_sec=60)

        # Strip echoed command and trailing prompt
        lines = output.splitlines()
        if lines and "display current-configuration" in lines[0]:
            lines = lines[1:]
        if lines and re.search(policy.prompt_regex, lines[-1]):
            lines = lines[:-1]
        config_text = "\n".join(lines).strip()

        if len(config_text) < 50:
            return (
                False,
                "Config output too short — device may not support this command",
                config_text,
            )
        return True, "Configuration retrieved", config_text
    except Exception as exc:
        logger.error("Error fetching config from OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}", ""
    finally:
        transport.close()


def run_cli_command(olt: OLTDevice, command: str) -> tuple[bool, str, str]:
    """Execute a read-only CLI command on an OLT via SSH.

    Args:
        olt: The OLT device to connect to.
        command: The CLI command to run (must be read-only).

    Returns:
        Tuple of (success, message, command_output).
    """
    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", ""
    except Exception as exc:
        logger.error("Unexpected error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", ""

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

        channel.send(f"{command}\n")
        output = _read_until_prompt(channel, policy.prompt_regex, timeout_sec=30)

        # Strip the echoed command and trailing prompt from the output
        lines = output.splitlines()
        if lines and command in lines[0]:
            lines = lines[1:]
        # Remove trailing prompt line
        if lines and re.search(policy.prompt_regex, lines[-1]):
            lines = lines[:-1]
        clean_output = "\n".join(lines).strip()
        return True, "Command executed", clean_output
    except Exception as exc:
        logger.error("Error running CLI command on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}", ""
    finally:
        transport.close()


def get_service_ports_for_ont(
    olt: OLTDevice, fsp: str, ont_id: int
) -> tuple[bool, str, list[ServicePortEntry]]:
    """Get service-ports for a specific ONT on a PON port.

    Filters the full port list by ONT-ID.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port e.g. "0/2/1".
        ont_id: The ONT-ID to filter by.

    Returns:
        Tuple of (success, message, filtered entries).
    """
    ok, msg, all_ports = get_service_ports(olt, fsp)
    if not ok:
        return ok, msg, []
    filtered = [p for p in all_ports if p.ont_id == ont_id]
    return True, f"Found {len(filtered)} service-port(s) for ONT {ont_id}", filtered


def delete_service_port(olt: OLTDevice, index: int) -> tuple[bool, str]:
    """Delete a service-port from the OLT by index.

    Args:
        olt: The OLT device.
        index: The service-port index to delete.

    Returns:
        Tuple of (success, message).
    """
    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        _run_huawei_cmd(channel, "config", prompt=config_prompt)

        output = _run_huawei_cmd(
            channel, f"undo service-port {index}", prompt=config_prompt
        )

        _run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if is_error_output(output):
            logger.warning(
                "Failed to delete service-port %d on OLT %s: %s",
                index,
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info("Deleted service-port %d on OLT %s", index, olt.name)
        return True, f"Service-port {index} deleted"
    except Exception as exc:
        logger.error("Error deleting service-port on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()


def create_single_service_port(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    gem_index: int,
    vlan_id: int,
    *,
    user_vlan: int | str | None = None,
    tag_transform: str = "translate",
) -> tuple[bool, str]:
    """Create a single service-port on an OLT.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port e.g. "0/2/1".
        ont_id: The ONT-ID assigned by the OLT.
        gem_index: GEM port index.
        vlan_id: VLAN ID for the service-port.

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        _run_huawei_cmd(channel, "config", prompt=config_prompt)

        cmd = build_service_port_command(
            fsp=fsp,
            ont_id=ont_id,
            gem_index=gem_index,
            vlan_id=vlan_id,
            user_vlan=user_vlan,
            tag_transform=tag_transform,
        )
        output = _run_huawei_cmd(channel, cmd, prompt=config_prompt)

        _run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if is_error_output(output):
            logger.warning(
                "Service-port creation failed on OLT %s: %s",
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info(
            "Created service-port VLAN %d GEM %d for ONT %d on OLT %s %s",
            vlan_id,
            gem_index,
            ont_id,
            olt.name,
            fsp,
        )
        return True, f"Service-port created (VLAN {vlan_id}, GEM {gem_index})"
    except Exception as exc:
        logger.error("Error creating service-port on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()


def configure_ont_iphost(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    vlan_id: int,
    ip_mode: str = "dhcp",
    ip_address: str | None = None,
    subnet: str | None = None,
    gateway: str | None = None,
) -> tuple[bool, str]:
    """Configure ONT management IP (IPHOST) via OLT SSH.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port e.g. "0/2/1".
        ont_id: The ONT-ID.
        vlan_id: Management VLAN ID.
        ip_mode: "dhcp" or "static".
        ip_address: Static IP (required if ip_mode="static").
        subnet: Subnet mask (required if ip_mode="static").
        gateway: Default gateway (required if ip_mode="static").

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    # SECURITY: Validate numeric parameters before CLI interpolation
    try:
        validate_ont_id(ont_id)
        validate_vlan_id(vlan_id)
    except ValidationError as e:
        return False, e.message

    # Validate IP addresses for static mode before CLI interpolation
    if ip_mode != "dhcp":
        if not ip_address or not subnet or not gateway:
            return False, "Static IP mode requires ip_address, subnet, and gateway"
        try:
            ip_address = validate_ip_address(ip_address, "ip_address")
            subnet = validate_subnet_mask(subnet, "subnet_mask")
            gateway = validate_ip_address(gateway, "gateway")
        except ValidationError as e:
            return False, e.message

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        _run_huawei_cmd(channel, "config", prompt=config_prompt)
        _run_huawei_cmd(channel, f"interface gpon {frame_slot}", prompt=config_prompt)

        if ip_mode == "dhcp":
            cmd = f"ont ipconfig {port_num} {ont_id} ip-index 0 dhcp vlan {vlan_id}"
        else:
            # ip_address, subnet, gateway already validated above
            cmd = (
                f"ont ipconfig {port_num} {ont_id} "
                f"ip-index 0 static ip-address {ip_address} "
                f"mask {subnet} gateway {gateway} vlan {vlan_id}"
            )

        output = _run_huawei_cmd(channel, cmd, prompt=config_prompt)

        _run_huawei_cmd(channel, "quit", prompt=config_prompt)
        _run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if is_error_output(output):
            logger.warning(
                "IPHOST config failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info(
            "Configured IPHOST for ONT %d on OLT %s (%s VLAN %d)",
            ont_id,
            olt.name,
            ip_mode,
            vlan_id,
        )
        return True, f"Management IP configured ({ip_mode} on VLAN {vlan_id})"
    except Exception as exc:
        logger.error("Error configuring IPHOST on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()


def get_ont_iphost_config(
    olt: OLTDevice, fsp: str, ont_id: int
) -> tuple[bool, str, dict[str, str]]:
    """Query current ONT IPHOST configuration from OLT.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port e.g. "0/2/1".
        ont_id: The ONT-ID.

    Returns:
        Tuple of (success, message, config_dict).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err, {}

    parts = fsp.split("/")
    port_num = parts[2]

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", {}
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", {}

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        cmd = f"display ont ipconfig {parts[0]}/{parts[1]} {port_num} {ont_id}"
        output = _run_huawei_cmd(channel, cmd)

        config: dict[str, str] = {}
        for line in output.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                config[key.strip()] = value.strip()

        return True, "IPHOST config retrieved", config
    except Exception as exc:
        logger.error("Error getting IPHOST config from OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}", {}
    finally:
        transport.close()


def reboot_ont_omci(olt: OLTDevice, fsp: str, ont_id: int) -> tuple[bool, str]:
    """Reboot an ONT via OMCI from the OLT.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port e.g. "0/2/1".
        ont_id: The ONT-ID.

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        _run_huawei_cmd(channel, "config", prompt=config_prompt)
        _run_huawei_cmd(channel, f"interface gpon {frame_slot}", prompt=config_prompt)

        # ont reset may ask for Y/N confirmation
        channel.send(f"ont reset {port_num} {ont_id}\n")
        output = _read_until_prompt(
            channel, rf"{config_prompt}|y/n|Y/N", timeout_sec=10
        )
        if "y/n" in output.lower():
            channel.send("y\n")
            output += _read_until_prompt(channel, config_prompt, timeout_sec=10)

        _run_huawei_cmd(channel, "quit", prompt=config_prompt)
        _run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if is_error_output(output):
            logger.warning(
                "ONT reset failed for %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info("ONT %d reset via OMCI on OLT %s", ont_id, olt.name)
        return True, f"ONT {ont_id} reboot command sent via OMCI"
    except Exception as exc:
        logger.error("Error resetting ONT on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()


def bind_tr069_server_profile(
    olt: OLTDevice, fsp: str, ont_id: int, profile_id: int
) -> tuple[bool, str]:
    """Bind a TR-069 server profile to an ONT via OLT SSH.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port e.g. "0/2/1".
        ont_id: The ONT-ID.
        profile_id: OLT TR-069 server profile ID.

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        _run_huawei_cmd(channel, "config", prompt=config_prompt)

        # Enter the interface context using /slot syntax.
        # Try frame/slot first; if it fails (Board type is invalid),
        # try common alternate slots (0/0, 0/1).
        iface_entered = False
        for candidate_slot in [frame_slot, f"{parts[0]}/1", f"{parts[0]}/0"]:
            output = _run_huawei_cmd(
                channel, f"interface gpon {candidate_slot}", prompt=config_prompt
            )
            if "invalid" not in output.lower() and "error" not in output.lower():
                iface_entered = True
                break

        if not iface_entered:
            _run_huawei_cmd(channel, "quit", prompt=config_prompt)
            return False, f"Could not enter interface gpon context for {fsp}"

        cmd = f"ont tr069-server-config {port_num} {ont_id} profile-id {profile_id}"
        output = _run_huawei_cmd(channel, cmd, prompt=config_prompt)

        if is_error_output(output):
            _run_huawei_cmd(channel, "quit", prompt=config_prompt)
            _run_huawei_cmd(channel, "quit", prompt=config_prompt)
            logger.warning(
                "TR-069 profile bind failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        # Reset the ONT to force an immediate bootstrap inform to the new ACS.
        # The OLT prompts "Are you sure? (y/n)" — send 'y' to confirm.
        reset_out = _run_huawei_cmd(
            channel, f"ont reset {port_num} {ont_id}", prompt=r"[#)]\s*$|y/n"
        )
        if "y/n" in reset_out:
            channel.send("y\n")
            reset_out += _read_until_prompt(channel, config_prompt, timeout_sec=8)

        if "failure" in reset_out.lower() or "error" in reset_out.lower():
            _run_huawei_cmd(channel, "quit", prompt=config_prompt)
            _run_huawei_cmd(channel, "quit", prompt=config_prompt)
            logger.warning(
                "TR-069 profile bound but ONT reset failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                reset_out.strip()[-150:],
            )
            return (
                False,
                f"TR-069 profile bound but reset failed: {reset_out.strip()[-150:]}",
            )

        _run_huawei_cmd(channel, "quit", prompt=config_prompt)
        _run_huawei_cmd(channel, "quit", prompt=config_prompt)

        logger.info(
            "Bound TR-069 profile %d to ONT %d on OLT %s (reset triggered)",
            profile_id,
            ont_id,
            olt.name,
        )
        return (
            True,
            f"TR-069 profile {profile_id} bound to ONT {ont_id} (reset triggered)",
        )
    except Exception as exc:
        logger.error("Error binding TR-069 profile on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()


@dataclass
class OltProfileEntry:
    """A single OLT profile entry (line, service, TR-069, or WAN)."""

    profile_id: int
    name: str
    type: str = ""
    binding_count: int = 0
    extra: dict[str, str] = field(default_factory=dict)


def _parse_profile_table_legacy(
    output: str, id_col: int = 0, name_col: int = 1
) -> list[OltProfileEntry]:
    """Legacy regex parser for Huawei profile display output.

    Used as fallback when TextFSM parsing fails.
    """
    entries: list[OltProfileEntry] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("-") or line.startswith("="):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[id_col])
        except (ValueError, IndexError):
            continue
        name = parts[name_col] if len(parts) > name_col else ""
        entries.append(OltProfileEntry(profile_id=pid, name=name))
    return entries


def _parse_profile_table(
    output: str, id_col: int = 0, name_col: int = 1
) -> list[OltProfileEntry]:
    """Parse Huawei profile display output into structured entries.

    Uses TextFSM template for robust parsing with fallback to legacy regex.
    """
    if _TEXTFSM_AVAILABLE:
        try:
            result = _textfsm_parse_profile_table(output)
            if result.success and result.data:
                # Convert from parser dataclass to local dataclass
                return [
                    OltProfileEntry(
                        profile_id=e.profile_id,
                        name=e.name,
                        type=e.type,
                        binding_count=e.binding_count,
                    )
                    for e in result.data
                ]
            if result.warnings:
                logger.debug("TextFSM profile warnings: %s", result.warnings)
        except Exception as e:
            logger.debug("TextFSM profile parse failed, using legacy: %s", e)

    # Fallback to legacy regex parsing
    return _parse_profile_table_legacy(output, id_col, name_col)


def get_line_profiles(olt: OLTDevice) -> tuple[bool, str, list[OltProfileEntry]]:
    """Query OLT for GPON line profiles.

    Returns:
        Tuple of (success, message, list of profiles).
    """
    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", []

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        output = _run_huawei_cmd(channel, "display ont-lineprofile gpon all")
        entries = _parse_profile_table(output)
        return True, f"Found {len(entries)} line profile(s)", entries
    except Exception as exc:
        logger.error("Error reading line profiles from OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}", []
    finally:
        transport.close()


def get_service_profiles(olt: OLTDevice) -> tuple[bool, str, list[OltProfileEntry]]:
    """Query OLT for GPON service profiles.

    Returns:
        Tuple of (success, message, list of profiles).
    """
    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", []

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        output = _run_huawei_cmd(channel, "display ont-srvprofile gpon all")
        entries = _parse_profile_table(output)
        return True, f"Found {len(entries)} service profile(s)", entries
    except Exception as exc:
        logger.error("Error reading service profiles from OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}", []
    finally:
        transport.close()


@dataclass
class Tr069ServerProfile:
    """A TR-069 server profile with detail fields."""

    profile_id: int
    name: str
    acs_url: str = ""
    acs_username: str = ""
    inform_interval: int = 0
    binding_count: int = 0


_TR069_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-. ]{1,64}$")


def _parse_tr069_profile_detail_legacy(output: str) -> dict[str, str]:
    """Legacy parser for TR-069 profile detail key-value output.

    Used as fallback when TextFSM parsing fails.
    """
    result: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key.strip().lower()] = value.strip()
    return result


def _parse_tr069_profile_detail(output: str) -> dict[str, str]:
    """Parse ``display ont tr069-server-profile profile-id N`` key-value output.

    Uses TextFSM key-value parser with fallback to legacy regex.
    """
    if _TEXTFSM_AVAILABLE:
        try:
            return _textfsm_parse_key_value(output)
        except Exception as e:
            logger.debug("TextFSM key-value parse failed, using legacy: %s", e)

    # Fallback to legacy regex parsing
    return _parse_tr069_profile_detail_legacy(output)


def get_tr069_server_profiles(
    olt: OLTDevice,
) -> tuple[bool, str, list[Tr069ServerProfile]]:
    """Query OLT for TR-069 server profiles with per-profile detail.

    Returns:
        Tuple of (success, message, list of Tr069ServerProfile).
    """
    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", []

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        output = _run_huawei_cmd(channel, "display ont tr069-server-profile all")
        summary = _parse_profile_table(output)

        profiles: list[Tr069ServerProfile] = []
        for entry in summary:
            detail_output = _run_huawei_cmd(
                channel,
                f"display ont tr069-server-profile profile-id {entry.profile_id}",
            )
            detail = _parse_tr069_profile_detail(detail_output)
            profiles.append(
                Tr069ServerProfile(
                    profile_id=entry.profile_id,
                    name=detail.get("profile-name", entry.name),
                    acs_url=detail.get(
                        "url", detail.get("acs url", detail.get("acs-url", ""))
                    ),
                    acs_username=detail.get(
                        "user name", detail.get("acs username", "")
                    ),
                    inform_interval=int(detail.get("inform interval", "0") or "0"),
                    binding_count=int(
                        detail.get("binding times", detail.get("bindnumber", "0"))
                        or "0"
                    ),
                )
            )

        return True, f"Found {len(profiles)} TR-069 server profile(s)", profiles
    except Exception as exc:
        logger.error("Error reading TR-069 profiles from OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}", []
    finally:
        transport.close()


def create_tr069_server_profile(
    olt: OLTDevice,
    *,
    profile_name: str,
    acs_url: str,
    username: str = "",
    password: str = "",
    inform_interval: int = 300,
) -> tuple[bool, str]:
    """Create a new TR-069 server profile on the OLT via SSH.

    Args:
        olt: The OLT device.
        profile_name: Name for the new profile (alphanumeric, dashes, dots, spaces).
        acs_url: The ACS URL (e.g. http://oss.dotmac.ng:7547).
        username: CWMP ACS username.
        password: CWMP ACS password.
        inform_interval: Periodic inform interval in seconds.

    Returns:
        Tuple of (success, message).
    """
    if not _TR069_PROFILE_NAME_RE.match(profile_name):
        return (
            False,
            "Invalid profile name (alphanumeric, dashes, dots, spaces, max 64 chars)",
        )
    if (
        not acs_url
        or "\n" in acs_url
        or "\r" in acs_url
        or ";" in acs_url
        or "|" in acs_url
    ):
        return False, "Invalid ACS URL"
    if username and ("\n" in username or ";" in username or "|" in username):
        return False, "Invalid username"
    if password and ("\n" in password or ";" in password or "|" in password):
        return False, "Invalid password"

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        import time

        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        _run_huawei_cmd(channel, "config", prompt=config_prompt)

        # Huawei MA56xx uses an interactive wizard for `add`.
        # Each prompt looks like: { url<K> }: or { user-password<S> }:
        # We extract the prompt text between { } to determine the response.
        # Key: match ONLY the { } prompt content, not echoed output.
        channel.send(f'ont tr069-server-profile add profile-name "{profile_name}"\n')

        for _attempt in range(8):
            time.sleep(2)
            raw = b""
            while channel.recv_ready():
                raw += channel.recv(4096)
                time.sleep(0.1)
            decoded = raw.decode("ascii", errors="replace")

            # Extract the wizard prompt: { ... }:
            prompt_match = re.search(r"\{([^}]+)\}\s*:", decoded)
            if prompt_match:
                prompt_text = prompt_match.group(1).lower().strip()

                if "url" in prompt_text and "user" not in prompt_text:
                    channel.send(f'url "{acs_url}"\n')
                elif "user-password" in prompt_text or "password" in prompt_text:
                    # <S> type prompt — send raw value, no keyword prefix
                    channel.send(f"{password}\n" if password else "\n")
                elif "user" in prompt_text:
                    channel.send(f'user "{username}"\n' if username else "\n")
                elif "interval" in prompt_text or "inform" in prompt_text:
                    channel.send(f"{inform_interval}\n" if inform_interval else "\n")
                elif "<cr>" in prompt_text:
                    channel.send("\n")
                else:
                    channel.send("\n")
            elif re.search(config_prompt, decoded):
                break  # Back at config prompt — wizard complete

        # Drain remaining output
        time.sleep(1)
        while channel.recv_ready():
            channel.recv(4096)

        # Verify profile was created
        verify_output = _run_huawei_cmd(
            channel, "display ont tr069-server-profile all", prompt=config_prompt
        )
        if profile_name.lower() not in verify_output.lower():
            _run_huawei_cmd(channel, "quit", prompt=config_prompt)
            return (
                False,
                f"Profile '{profile_name}' not found after creation. OLT may have rejected it.",
            )

        _run_huawei_cmd(channel, "quit", prompt=config_prompt)

        logger.info(
            "Created TR-069 profile '%s' on OLT %s with ACS URL %s",
            profile_name,
            olt.name,
            acs_url,
        )
        return True, f"TR-069 profile '{profile_name}' created successfully"
    except Exception as exc:
        logger.error("Error creating TR-069 profile on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()


def test_connection(olt: OLTDevice) -> tuple[bool, str, str | None]:
    try:
        policy_key, output = run_version_probe(olt)
    except (SSHException, OSError) as exc:
        return False, f"Connection failed: {type(exc).__name__}: {exc}", None
    except Exception as exc:
        logger.error("Unexpected error testing OLT %s connection: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", None
    if not output.strip():
        return False, "SSH connected but no CLI output returned", policy_key
    return True, "SSH connection test successful", policy_key
