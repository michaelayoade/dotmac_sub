"""TR-069 server profile binding functions via OLT SSH."""

from __future__ import annotations

import logging

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network.olt_ssh_ont._common import _SSH_CONNECTION_ERRORS
from app.services.network.olt_ssh_ont.iphost import _run_ont_config_command

logger = logging.getLogger(__name__)


def bind_tr069_server_profile(
    olt: OLTDevice, fsp: str, ont_id: int, profile_id: int
) -> tuple[bool, str]:
    """Bind a TR-069 server profile to an ONT via OLT SSH."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]
    logger.info(
        "TR-069 bind requested: olt=%s fsp=%s ont_id=%s profile_id=%s",
        olt.name,
        fsp,
        ont_id,
        profile_id,
    )

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

        cmd = f"ont tr069-server-config {port_num} {ont_id} profile-id {profile_id}"
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        if core.is_error_output(output) and "unknown command" in output.lower():
            # Some Huawei builds expect only the ONT ID after entering
            # `interface gpon F/S`; the port is already implied by context.
            fallback_cmd = f"ont tr069-server-config {ont_id} profile-id {profile_id}"
            fallback_output = core._run_huawei_cmd(
                channel, fallback_cmd, prompt=config_prompt
            )
            if not core.is_error_output(fallback_output):
                output = fallback_output
        if core.is_error_output(output):
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            logger.warning(
                "TR-069 profile bind failed: olt=%s fsp=%s ont_id=%s profile_id=%s output=%s",
                olt.name,
                fsp,
                ont_id,
                profile_id,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        reset_out = core._run_huawei_cmd(
            channel, f"ont reset {port_num} {ont_id}", prompt=r"[#)]\s*$|y/n"
        )
        if "y/n" in reset_out:
            channel.send("y\n")
            reset_out += core._read_until_prompt(channel, config_prompt, timeout_sec=8)

        if core.is_error_output(reset_out):
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            if "ont is not online" in reset_out.lower():
                logger.info(
                    "TR-069 bind succeeded but reset skipped because ONT is offline: olt=%s fsp=%s ont_id=%s profile_id=%s",
                    olt.name,
                    fsp,
                    ont_id,
                    profile_id,
                )
                return (
                    True,
                    f"TR-069 profile {profile_id} bound to ONT {ont_id}; ONT is offline, so reset will occur when it next boots.",
                )
            logger.warning(
                "TR-069 bind succeeded but reset failed: olt=%s fsp=%s ont_id=%s profile_id=%s output=%s",
                olt.name,
                fsp,
                ont_id,
                profile_id,
                reset_out.strip()[-150:],
            )
            return (
                False,
                f"TR-069 profile bound but reset failed: {reset_out.strip()[-150:]}",
            )

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        logger.info(
            "TR-069 bind succeeded and reset triggered: olt=%s fsp=%s ont_id=%s profile_id=%s",
            olt.name,
            fsp,
            ont_id,
            profile_id,
        )
        return (
            True,
            f"TR-069 profile {profile_id} bound to ONT {ont_id} (reset triggered)",
        )
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error binding TR-069 profile on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def unbind_tr069_server_profile(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
) -> tuple[bool, str]:
    """Best-effort removal of an ONT TR-069 server profile binding."""
    parts = fsp.split("/")
    port_num = parts[2] if len(parts) > 2 else "0"
    return _run_ont_config_command(
        olt,
        fsp,
        f"undo ont tr069-server-config {port_num} {ont_id}",
        success_message="TR-069 profile binding cleared",
    )
