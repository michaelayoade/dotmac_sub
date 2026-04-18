"""ONT lifecycle operations (reboot, factory reset, authorize, deauthorize) via OLT SSH."""

from __future__ import annotations

import logging
import re

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network._common import encode_to_hex_serial
from app.services.network.olt_ssh_ont._common import (
    _SSH_CONNECTION_ERRORS,
    _safe_profile_name,
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
        return True, f"ONT {ont_id} deleted from OLT"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error("Error deleting ONT on OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Error: {exc}"
    finally:
        transport.close()


# Alias for backwards compatibility
delete_ont_registration = deauthorize_ont


def _load_linked_acs_payload(olt: OLTDevice) -> dict[str, object] | None:
    """Load the linked TR-069 ACS server config for an OLT."""
    from app.services.credential_crypto import decrypt_credential

    server = None
    try:
        server = getattr(olt, "tr069_acs_server", None)
    except AttributeError:
        server = None

    if server is None and getattr(olt, "tr069_acs_server_id", None):
        try:
            from app.db import SessionLocal
            from app.models.tr069 import Tr069AcsServer

            with SessionLocal() as db:
                server = db.get(Tr069AcsServer, str(olt.tr069_acs_server_id))
                if server is None:
                    return None
                password = (
                    decrypt_credential(server.cwmp_password)
                    if server.cwmp_password
                    else ""
                )
                return {
                    "name": server.name,
                    "acs_url": server.cwmp_url or "",
                    "username": server.cwmp_username or "",
                    "password": password or "",
                    "inform_interval": server.periodic_inform_interval or 300,
                }
        except (ImportError, LookupError, AttributeError) as exc:
            logger.warning("Failed to load linked ACS for OLT %s: %s", olt.name, exc)
            return None

    if server is None or not getattr(server, "cwmp_url", None):
        return None
    password = (
        decrypt_credential(server.cwmp_password)
        if getattr(server, "cwmp_password", None)
        else ""
    )
    return {
        "name": getattr(server, "name", "ACS"),
        "acs_url": server.cwmp_url or "",
        "username": server.cwmp_username or "",
        "password": password or "",
        "inform_interval": getattr(server, "periodic_inform_interval", None) or 300,
    }


def _auto_bind_tr069_after_authorize(
    olt: OLTDevice, fsp: str, ont_id: int | None
) -> None:
    """Best-effort: bind a newly authorized ONT to the OLT's linked ACS profile.

    Called after successful ONT authorization. Skips when the OLT has no linked
    ACS, and creates the OLT profile if the linked ACS profile does not exist.
    """
    from app.services.network.olt_ssh_ont.tr069 import bind_tr069_server_profile
    from app.services.network.olt_ssh_profiles import (
        create_tr069_server_profile,
        get_tr069_server_profiles,
    )
    from app.services.network.tr069_profile_matching import (
        match_tr069_profile,
        normalize_acs_url,
    )

    if ont_id is None:
        return
    try:
        payload = _load_linked_acs_payload(olt)
        if payload is None or not str(payload.get("acs_url") or "").strip():
            logger.info("Skipping TR-069 auto-bind for OLT %s: no linked ACS", olt.name)
            return

        ok, _msg, profiles = get_tr069_server_profiles(olt)
        if not ok:
            return
        target_url = normalize_acs_url(str(payload["acs_url"]))
        target_username = str(payload.get("username") or "").strip()
        profile = match_tr069_profile(
            profiles,
            acs_url=str(payload["acs_url"]),
            acs_username=target_username,
        )
        profile_id = profile.profile_id if profile else None

        if profile_id is None:
            profile_name = f"ACS {_safe_profile_name(str(payload.get('name') or ''))}"
            ok, msg = create_tr069_server_profile(
                olt,
                profile_name=profile_name,
                acs_url=str(payload["acs_url"]),
                username=target_username,
                password=str(payload.get("password") or ""),
                inform_interval=int(str(payload.get("inform_interval") or 300)),
            )
            if not ok:
                logger.warning(
                    "Auto-create TR-069 profile failed for OLT %s: %s",
                    olt.name,
                    msg,
                )
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
        if profile_id is None:
            logger.warning(
                "Could not resolve TR-069 profile for linked ACS %s on OLT %s",
                target_url,
                olt.name,
            )
            return

        ok, msg = bind_tr069_server_profile(
            olt, fsp=fsp, ont_id=ont_id, profile_id=profile_id
        )
        if ok:
            logger.info(
                "Auto-bound ONT %d on %s to TR-069 profile %d",
                ont_id,
                fsp,
                profile_id,
            )
        else:
            logger.warning(
                "Auto-bind TR-069 failed for ONT %d on %s: %s", ont_id, fsp, msg
            )
    except (*_SSH_CONNECTION_ERRORS, ValueError, RuntimeError) as exc:
        logger.warning("Auto-bind TR-069 error for ONT %d: %s", ont_id, exc)


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
        line_profile_id: OLT-local line profile ID resolved before authorization.
        service_profile_id: OLT-local service profile ID resolved before authorization.

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
        auth_cmd = f"ont add {port_num} sn-auth {sn_clean} omci ont-lineprofile-id {line_pid} ont-srvprofile-id {srv_pid}"
        _send_slow(channel, auth_cmd)
        # Huawei may prompt "{ <cr>|desc<K>|ont-type<K> }:" — send CR to confirm
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
            # Auto-bind to the OLT's linked ACS TR-069 profile if configured.
            _auto_bind_tr069_after_authorize(olt, fsp, ont_id)

            message = f"ONT {serial_number} authorized on port {fsp}"
            if ont_id is not None:
                message += f" (ONT-ID {ont_id})"
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
