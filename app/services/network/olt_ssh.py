"""OLT SSH connection helpers with model-specific transport policies.

This module provides SSH connectivity and CLI parsing for Huawei OLTs.
Parsing is done via TextFSM templates (see parsers/ subdirectory) with
fallback to legacy regex parsing for robustness.
"""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass

from paramiko.channel import Channel
from paramiko.ssh_exception import SSHException
from paramiko.transport import Transport

logger = logging.getLogger(__name__)

# Specific SSH-related exceptions that can occur during OLT operations
_SSH_CONNECTION_ERRORS = (
    SSHException,
    OSError,
    socket.timeout,
    TimeoutError,
    ConnectionError,
)

from app.models.network import OLTDevice
from app.services.credential_crypto import decrypt_credential
from app.services.network._common import decode_huawei_hex_serial
from app.services.network.olt_command_gen import build_service_port_command

# TextFSM-based parsers (preferred)
try:
    from app.services.network.parsers import parse_autofind as _textfsm_parse_autofind
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
    # Use wider PTY and set terminal type to avoid control sequence issues
    channel.get_pty(term="dumb", width=400, height=50)
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
                        serial_number=e.serial_number
                        or decode_huawei_hex_serial(e.serial_hex)
                        or e.serial_hex,
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
        except (ValueError, KeyError, IndexError, AttributeError) as e:
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
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []

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
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error reading autofind from OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error reading autofind: {exc}", []
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
    fsp: str = ""
    tag_transform: str = ""


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
        fsp_tokens: list[str] = []
        nums_after_gpon: list[int] = []
        for token in parts[gpon_idx + 1 :]:
            # Skip F/S/P fragments like "0/2" or "/1"
            cleaned = token.strip("/").replace("/", "")
            if "/" in token:
                fsp_tokens.append(token)
                continue
            if cleaned.isdigit():
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
                fsp="".join(fsp_tokens).replace(" ", ""),
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
                        fsp=e.fsp.replace(" ", ""),
                        tag_transform=e.tag_transform,
                    )
                    for e in result.data
                ]
            if result.warnings:
                logger.debug("TextFSM service-port warnings: %s", result.warnings)
        except (ValueError, KeyError, IndexError, AttributeError) as e:
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


_HUAWEI_OPTIONAL_ARG_PROMPT = r"\{[^\r\n{}]*\}\s*:?\s*$"


def _needs_huawei_command_confirm(output: str) -> bool:
    """Return true when Huawei CLI is waiting for Enter to accept defaults."""
    return (
        "<cr>" in output.lower()
        or re.search(_HUAWEI_OPTIONAL_ARG_PROMPT, output) is not None
    )


def _run_huawei_cmd(channel: Channel, command: str, prompt: str = r"#\s*$") -> str:
    """Send a command to a Huawei shell, accepting optional-argument prompts."""
    logger.debug("OLT command: %r", command)
    channel.send(f"{command}\n")
    out = _read_until_prompt(
        channel, rf"{prompt}|<cr>|{_HUAWEI_OPTIONAL_ARG_PROMPT}", timeout_sec=12
    )
    if _needs_huawei_command_confirm(out):
        channel.send("\n")
        out = _read_until_prompt(channel, prompt, timeout_sec=12)
    return out


def _run_huawei_paged_cmd(
    channel: Channel, command: str, prompt: str = r"#\s*$", *, timeout_sec: int = 60
) -> str:
    """Send a command and handle pagination (---- More ----) prompts."""
    logger.debug("OLT paged command: %r", command)
    channel.send(f"{command}\n")
    output_parts: list[str] = []
    pager_pattern = r"---- More(?:\s*\([^)]*\)\s*)?----|<cr>|Press any key"
    combined_pattern = rf"{prompt}|{pager_pattern}"

    while True:
        chunk = _read_until_prompt(channel, combined_pattern, timeout_sec=timeout_sec)
        output_parts.append(chunk)

        # Check if we hit a pager prompt
        if "---- More" in chunk or "Press any key" in chunk:
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
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        output = _run_huawei_paged_cmd(channel, f"display service-port port {fsp}")
        entries = _parse_service_port_table(output)
        return True, f"Found {len(entries)} service-ports on {fsp}", entries
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error reading service-ports from OLT %s: %s", olt.name, exc, exc_info=True
        )
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
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

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
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error creating service-ports on OLT %s: %s", olt.name, exc, exc_info=True
        )
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
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

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
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error during firmware upgrade on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


@dataclass
class FirmwareInfo:
    """Firmware version information from OLT."""

    current_version: str | None = None
    standby_version: str | None = None
    running_board: str | None = None
    standby_board: str | None = None
    uptime: str | None = None
    has_dual_image: bool = False


def _parse_firmware_info(output: str) -> FirmwareInfo:
    """Parse firmware version information from 'display version' output."""
    info = FirmwareInfo()
    lines = output.splitlines()

    for line in lines:
        line_lower = line.lower()

        # Look for version patterns
        if "version" in line_lower and "software" in line_lower:
            # Huawei: VRP (R) software, Version X.XXX
            match = re.search(r"Version\s+(\S+)", line, re.IGNORECASE)
            if match:
                info.current_version = match.group(1)

        # Look for uptime
        if "uptime" in line_lower:
            match = re.search(r"uptime[:\s]+(.+)$", line, re.IGNORECASE)
            if match:
                info.uptime = match.group(1).strip()

        # Look for board information
        if "board" in line_lower and ("main" in line_lower or "master" in line_lower):
            info.running_board = line.strip()
        if "board" in line_lower and ("standby" in line_lower or "slave" in line_lower):
            info.standby_board = line.strip()
            info.has_dual_image = True

        # Look for standby version
        if "standby" in line_lower and "version" in line_lower:
            match = re.search(r"Version[:\s]+(\S+)", line, re.IGNORECASE)
            if match:
                info.standby_version = match.group(1)
                info.has_dual_image = True

    return info


def get_firmware_info(olt: OLTDevice) -> tuple[bool, str, FirmwareInfo]:
    """Get current and standby firmware versions from OLT via 'display version'.

    Args:
        olt: The OLT device.

    Returns:
        Tuple of (success, message, FirmwareInfo).
    """
    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", FirmwareInfo()

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

        output = _run_huawei_paged_cmd(
            channel,
            "display version",
            prompt=policy.prompt_regex,
            timeout_sec=30,
        )

        info = _parse_firmware_info(output)

        if not info.current_version:
            return False, "Could not parse firmware version from output", info

        message = f"Running: {info.current_version}"
        if info.standby_version:
            message += f", Standby: {info.standby_version}"

        logger.info(
            "Retrieved firmware info from OLT %s: %s",
            olt.name,
            message,
        )
        return True, message, info
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error getting firmware info from OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}", FirmwareInfo()
    finally:
        transport.close()


def rollback_firmware(olt: OLTDevice) -> tuple[bool, str]:
    """Switch to standby/backup firmware image.

    This issues 'startup system-software' to swap the active and standby images.
    The OLT will boot to the previous image on next reboot.

    Args:
        olt: The OLT device.

    Returns:
        Tuple of (success, message).
    """
    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

        # First check if we have dual image support
        ok, msg, info = get_firmware_info(olt)
        if not info.has_dual_image:
            return False, "OLT does not appear to have dual-image support"
        if not info.standby_version:
            return False, "No standby firmware version available for rollback"

        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

        # Switch startup system software to standby
        # Huawei: startup system-software slave (or backup/standby depending on model)
        cmd = "startup system-software"
        output = _run_huawei_cmd(channel, cmd, prompt=r"#\s*$|y/n|\[Y/N\]")

        # Handle confirmation prompt
        if re.search(r"y/n|\[Y/N\]", output, re.IGNORECASE):
            channel.send("y\n")
            output += _read_until_prompt(channel, policy.prompt_regex, timeout_sec=15)

        if is_error_output(output):
            logger.warning(
                "Firmware rollback failed on OLT %s: %s",
                olt.name,
                output.strip()[-200:],
            )
            return False, f"Rollback command failed: {output.strip()[-200:]}"

        logger.info(
            "Firmware rollback initiated on OLT %s: switching from %s to %s",
            olt.name,
            info.current_version,
            info.standby_version,
        )
        return (
            True,
            f"Rollback scheduled: will boot to {info.standby_version} on next reboot",
        )
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error during firmware rollback on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def test_reachability(olt: OLTDevice, timeout_sec: int = 10) -> tuple[bool, str]:
    """Test if OLT is reachable via SSH.

    Args:
        olt: The OLT device.
        timeout_sec: Connection timeout in seconds.

    Returns:
        Tuple of (reachable, message).
    """
    try:
        transport, channel, policy = _open_shell(olt)
        transport.close()
        return True, f"OLT {olt.name} is reachable via SSH"
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"OLT {olt.name} is not reachable: {exc}"


def fetch_running_config_ssh(olt: OLTDevice) -> tuple[bool, str, str]:
    """Fetch the full running configuration from an OLT via SSH.

    Returns (success, message, config_text).
    """
    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", ""

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

        output = _run_huawei_paged_cmd(
            channel,
            "display current-configuration",
            prompt=policy.prompt_regex,
            timeout_sec=60,
        )

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
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error fetching config from OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}", ""
    finally:
        transport.close()


# Read-only command prefixes allowed for run_cli_command()
_READONLY_COMMAND_PREFIXES = (
    "display",
    "show",
    "dir",
    "pwd",
    "more",
    "ping",
    "tracert",
)

# Dangerous command prefixes that should never be allowed
_DANGEROUS_COMMAND_PREFIXES = (
    "config",
    "undo",
    "delete",
    "reset",
    "reboot",
    "shutdown",
    "format",
    "copy",
    "startup",
    "save",
    "commit",
    "rollback",
    "system",
    "patch",
    "upgrade",
    "restore",
    "ont add",
    "ont delete",
    "service-port",
    "interface",
)


def _validate_readonly_command(command: str) -> tuple[bool, str]:
    """Validate that a CLI command is read-only (display/show only).

    Returns:
        Tuple of (is_valid, error_message).
    """
    normalized = command.strip().lower()

    # Check for dangerous commands first
    for prefix in _DANGEROUS_COMMAND_PREFIXES:
        if normalized.startswith(prefix):
            return (
                False,
                f"Command '{prefix}' is not allowed — only read-only commands permitted",
            )

    # Check for allowed read-only prefixes
    for prefix in _READONLY_COMMAND_PREFIXES:
        if normalized.startswith(prefix):
            return True, ""

    return (
        False,
        f"Command not recognized as read-only — must start with: {', '.join(_READONLY_COMMAND_PREFIXES)}",
    )


def run_cli_command(olt: OLTDevice, command: str) -> tuple[bool, str, str]:
    """Execute a read-only CLI command on an OLT via SSH.

    Only allows commands starting with safe prefixes like 'display', 'show'.
    Config and mutating commands are rejected.

    Args:
        olt: The OLT device to connect to.
        command: The CLI command to run (must be read-only).

    Returns:
        Tuple of (success, message, command_output).
    """
    # Validate command is read-only before executing
    is_valid, error_msg = _validate_readonly_command(command)
    if not is_valid:
        logger.warning(
            "Rejected non-read-only CLI command on OLT %s: %s",
            olt.name,
            command[:100],
        )
        return False, error_msg, ""

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", ""

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
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error running CLI command on OLT %s: %s", olt.name, exc, exc_info=True
        )
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
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

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
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error deleting service-port on OLT %s: %s", olt.name, exc, exc_info=True
        )
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
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

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
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error creating service-port on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


# Backward compatibility re-exports (functions moved to olt_ssh_ont.py)
# These functions are now defined in olt_ssh_ont.py but re-exported here
# to maintain backward compatibility with existing imports.
from app.services.network.olt_ssh_ont import (
    authorize_ont as authorize_ont,
)
from app.services.network.olt_ssh_ont import (
    bind_tr069_server_profile as bind_tr069_server_profile,
)
from app.services.network.olt_ssh_ont import (
    configure_ont_iphost as configure_ont_iphost,
)
from app.services.network.olt_ssh_ont import (
    get_ont_iphost_config as get_ont_iphost_config,
)
from app.services.network.olt_ssh_ont import (
    reboot_ont_omci as reboot_ont_omci,
)

# Backward compatibility re-exports (profile functions moved to olt_ssh_profiles.py)
from app.services.network.olt_ssh_profiles import (
    OltProfileEntry as OltProfileEntry,
)
from app.services.network.olt_ssh_profiles import (
    Tr069ServerProfile as Tr069ServerProfile,
)
from app.services.network.olt_ssh_profiles import (
    _parse_profile_table as _parse_profile_table,
)
from app.services.network.olt_ssh_profiles import (
    _parse_profile_table_legacy as _parse_profile_table_legacy,
)
from app.services.network.olt_ssh_profiles import (
    _parse_tr069_profile_detail as _parse_tr069_profile_detail,
)
from app.services.network.olt_ssh_profiles import (
    _parse_tr069_profile_detail_legacy as _parse_tr069_profile_detail_legacy,
)
from app.services.network.olt_ssh_profiles import (
    create_tr069_server_profile as create_tr069_server_profile,
)
from app.services.network.olt_ssh_profiles import (
    ensure_wan_srvprofile as ensure_wan_srvprofile,
)
from app.services.network.olt_ssh_profiles import (
    get_line_profiles as get_line_profiles,
)
from app.services.network.olt_ssh_profiles import (
    get_service_profiles as get_service_profiles,
)
from app.services.network.olt_ssh_profiles import (
    get_tr069_server_profiles as get_tr069_server_profiles,
)


def _auto_bind_tr069_after_authorize(
    olt: OLTDevice, fsp: str, ont_id: int | None
) -> None:
    """Backward-compatible TR-069 auto-bind helper.

    Kept here so legacy callers and tests that monkeypatch this module's profile
    helpers continue to influence the binding flow.
    """
    from app.services.network.olt_ssh_ont import (
        _load_linked_acs_payload,
        _safe_profile_name,
    )
    from app.services.network.tr069_profile_matching import match_tr069_profile

    if ont_id is None:
        return
    payload = _load_linked_acs_payload(olt)
    if payload is None or not str(payload.get("acs_url") or "").strip():
        return

    ok, _msg, profiles = get_tr069_server_profiles(olt)
    if not ok:
        return
    target_username = str(payload.get("username") or "").strip()
    profile = match_tr069_profile(
        profiles,
        acs_url=str(payload["acs_url"]),
        acs_username=target_username,
    )
    profile_id = profile.profile_id if profile else None

    if profile_id is None:
        ok, _msg = create_tr069_server_profile(
            olt,
            profile_name=f"ACS {_safe_profile_name(str(payload.get('name') or ''))}",
            acs_url=str(payload["acs_url"]),
            username=target_username,
            password=str(payload.get("password") or ""),
            inform_interval=int(str(payload.get("inform_interval") or 300)),
        )
        if not ok:
            return
        ok, _msg, profiles = get_tr069_server_profiles(olt)
        if not ok:
            return
        profile = match_tr069_profile(
            profiles,
            acs_url=str(payload["acs_url"]),
            acs_username=target_username,
        )
        profile_id = profile.profile_id if profile else None

    if profile_id is not None:
        bind_tr069_server_profile(olt, fsp=fsp, ont_id=ont_id, profile_id=profile_id)


def test_connection(olt: OLTDevice) -> tuple[bool, str, str | None]:
    try:
        policy_key, output = run_version_probe(olt)
    except (SSHException, OSError, TimeoutError) as exc:
        return False, f"Connection failed: {type(exc).__name__}: {exc}", None
    if not output.strip():
        return False, "SSH connected but no CLI output returned", policy_key
    return True, "SSH connection test successful", policy_key
