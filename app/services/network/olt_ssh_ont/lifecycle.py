"""ONT lifecycle operations (reboot, factory reset, authorize, deauthorize) via OLT SSH."""

from __future__ import annotations

import logging
import re

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network._common import encode_to_hex_serial
from app.services.network.olt_ssh_ont._common import (
    _SSH_CONNECTION_ERRORS,
    _send_slow,
    _validate_fsp,
    _validate_serial,
)
from app.services.network.olt_validators import ValidationError, validate_ont_id

logger = logging.getLogger(__name__)


def reboot_ont_omci(olt: OLTDevice, fsp: str, ont_id: int) -> tuple[bool, str]:
    """Reboot an ONT via OMCI from the OLT."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    # Validate ont_id before CLI interpolation
    try:
        validate_ont_id(ont_id)
    except ValidationError as e:
        return False, e.message

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )

        channel.send(f"ont reset {port_num} {ont_id}\n")
        output = core._read_until_prompt(
            channel, rf"{config_prompt}|y/n|Y/N", timeout_sec=10
        )
        if "y/n" in output.lower():
            channel.send("y\n")
            output += core._read_until_prompt(channel, config_prompt, timeout_sec=10)

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "ONT reset failed for %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info("ONT %d reset via OMCI on OLT %s", ont_id, olt.name)
        return True, f"ONT {ont_id} reboot command sent via OMCI"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error("Error resetting ONT on OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Error: {exc}"
    finally:
        transport.close()


def factory_reset_ont_omci(olt: OLTDevice, fsp: str, ont_id: int) -> tuple[bool, str]:
    """Full factory reset of an ONT via OMCI from the OLT."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    # Validate ont_id before CLI interpolation
    try:
        validate_ont_id(ont_id)
    except ValidationError as e:
        return False, e.message

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )

        channel.send(f"ont factory-setting-restore {port_num} {ont_id}\n")
        output = core._read_until_prompt(
            channel, rf"{config_prompt}|y/n|Y/N", timeout_sec=10
        )
        if "y/n" in output.lower():
            channel.send("y\n")
            output += core._read_until_prompt(channel, config_prompt, timeout_sec=10)

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "Factory reset failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info("Factory reset ONT %d via OMCI on OLT %s", ont_id, olt.name)
        return True, f"ONT {ont_id} factory reset command sent via OMCI"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error factory-resetting ONT on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def deauthorize_ont(olt: OLTDevice, fsp: str, ont_id: int) -> tuple[bool, str]:
    """Delete an ONT from the OLT so it can be rediscovered via autofind."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    # Validate ont_id before CLI interpolation
    try:
        validate_ont_id(ont_id)
    except ValidationError as e:
        return False, e.message

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )

        delete_out = core._run_huawei_cmd(
            channel,
            f"ont delete {port_num} {ont_id}",
            prompt=r"[#)]\s*$|y/n|Y/N",
        )
        if "y/n" in delete_out.lower():
            channel.send("y\n")
            delete_out += core._read_until_prompt(
                channel, config_prompt, timeout_sec=10
            )

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(delete_out):
            logger.warning(
                "ONT delete failed for %d on OLT %s: %s",
                ont_id,
                olt.name,
                delete_out.strip()[-150:],
            )
            return False, f"OLT rejected: {delete_out.strip()[-150:]}"

        logger.info("Deleted ONT %d from OLT %s on %s", ont_id, olt.name, fsp)
        core._invalidate_olt_read_cache(
            olt, "autofind", "service_ports", "running_config", "ont_info"
        )
        return True, f"ONT {ont_id} deleted from OLT"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error("Error deleting ONT on OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Error: {exc}"
    finally:
        transport.close()


# Alias for backwards compatibility
delete_ont_registration = deauthorize_ont


_DESC_ALLOWED = re.compile(r"[^A-Za-z0-9_.,/\-]+")


def _sanitize_ont_description(value: str | None) -> str:
    """Reduce a description to OLT-safe characters.

    Huawei OLTs accept descriptions up to ~80 chars; spaces and certain symbols
    are unreliable in scripted SSH (and inconsistent across MA5608T/MA5800
    firmware builds). Normalize spaces to underscores and strip everything not
    alphanumeric / ``_ . , / -``. Truncate to 64 chars (leaves margin for the
    surrounding ``desc "..."`` quoting).
    """
    if not value:
        return ""
    candidate = str(value).strip().replace(" ", "_")
    cleaned = _DESC_ALLOWED.sub("", candidate)
    return cleaned[:64]


def authorize_ont(
    olt: OLTDevice,
    fsp: str,
    serial_number: str,
    *,
    line_profile_id: int | None = None,
    service_profile_id: int | None = None,
    description: str | None = None,
) -> tuple[bool, str, int | None]:
    """SSH into OLT and register an ONT via sn-auth on the given port.

    Args:
        olt: The OLT device to connect to.
        fsp: Frame/Slot/Port string, e.g. "0/2/1".
        serial_number: ONT serial in vendor format, e.g. "HWTC-7D4733C3".
        line_profile_id: OLT-local line profile ID resolved before authorization.
        service_profile_id: OLT-local service profile ID resolved before authorization.
        description: Optional description to attach to the ``ont add`` command.
            Empty/None falls back to a serial-derived stub so the ONT row in
            ``display ont info`` never shows ``ONT_NO_DESCRIPTION``.

    Returns:
        Tuple of (success, message, assigned_ont_id).
    """
    from app.services.network import olt_ssh as core

    if line_profile_id is None or service_profile_id is None:
        return (
            False,
            "OLT authorization profiles were not resolved; refusing to use static profile defaults.",
            None,
        )
    line_pid = line_profile_id
    srv_pid = service_profile_id
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err, None
    ok, err = _validate_serial(serial_number)
    if not ok:
        return False, err, None

    try:
        transport, channel, policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    try:
        # Enter enable mode
        channel.send("enable\n")
        core._read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

        # Enter config mode
        config_prompt = r"[#)]\s*$"
        channel.send("config\n")
        core._read_until_prompt(channel, config_prompt, timeout_sec=5)

        # Enter GPON interface for the frame/slot
        parts = fsp.split("/")
        frame_slot = f"{parts[0]}/{parts[1]}"
        port_num = parts[2]

        _send_slow(channel, f"interface gpon {frame_slot}")
        core._read_until_prompt(channel, config_prompt, timeout_sec=5)

        # Authorize the ONT — use hex serial format to avoid terminal corruption
        # when serial numbers contain characters that could be interpreted as
        # escape sequences (e.g. '1B' = ESC in ASCII). Hex format is reliably
        # processed by all OLT terminals.
        sn_clean = encode_to_hex_serial(serial_number) or serial_number.replace("-", "")
        desc_clean = _sanitize_ont_description(description)
        if not desc_clean:
            # Default placeholder so the OLT row never shows ONT_NO_DESCRIPTION.
            from datetime import UTC, datetime
            desc_clean = (
                f"{sn_clean}_authd_{datetime.now(UTC).strftime('%Y%m%d')}"
            )[:64]
        auth_cmd = (
            f"ont add {port_num} sn-auth {sn_clean} omci "
            f"ont-lineprofile-id {line_pid} ont-srvprofile-id {srv_pid} "
            f'desc "{desc_clean}"'
        )
        _send_slow(channel, auth_cmd)
        # With desc supplied we no longer expect the "{ <cr>|desc<K>|ont-type<K> }:"
        # follow-up prompt, but keep the fallback to handle older Huawei firmware
        # builds that still demand a CR confirmation.
        initial = core._read_until_prompt(channel, r"[#)]\s*$|<cr>", timeout_sec=10)
        if "<cr>" in initial:
            channel.send("\n")
            output = core._read_until_prompt(channel, r"[#)]\s*$", timeout_sec=10)
        else:
            output = initial

        # Exit config mode
        channel.send("quit\n")
        core._read_until_prompt(channel, config_prompt, timeout_sec=3)
        channel.send("quit\n")
        core._read_until_prompt(channel, config_prompt, timeout_sec=3)

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
            message = f"ONT {serial_number} authorized on port {fsp}"
            if ont_id is not None:
                message += f" (ONT-ID {ont_id})"
            core._invalidate_olt_read_cache(
                olt, "autofind", "service_ports", "running_config", "ont_info"
            )
            return True, message, ont_id
        if core.is_error_output(output):
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
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error authorizing ONT %s on OLT %s: %s",
            serial_number,
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()
