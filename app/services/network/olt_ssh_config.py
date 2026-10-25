"""OLT SSH actions for device-level configuration and diagnostics."""

from __future__ import annotations

import logging
import re
import socket

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network.huawei_cli_response import describe_huawei_rejection

logger = logging.getLogger(__name__)

# Specific SSH-related exceptions that can occur during OLT operations
_SSH_CONNECTION_ERRORS = (
    SSHException,
    OSError,
    socket.timeout,
    TimeoutError,
    ConnectionError,
)


def _restore_config_commands(config_text: str) -> list[str]:
    """Normalize backup text into executable Huawei config commands."""
    commands: list[str] = []
    for raw_line in config_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", "!", "//")):
            continue
        lower = stripped.lower()
        if lower in {"return", "end"}:
            continue
        if lower.startswith(
            ("display ", "show ", "ping ", "traceroute ", "screen-length ")
        ):
            continue
        commands.append(stripped)
    return commands


def _save_running_config(
    channel, prompt_regex: str, is_error_output
) -> tuple[bool, str]:
    """Persist the running config, handling Huawei confirmation prompts."""
    from app.services.network.olt_ssh import _read_until_prompt, _run_huawei_cmd

    output = _run_huawei_cmd(channel, "save", prompt=rf"{prompt_regex}|y/n|\[Y/N\]")
    if re.search(r"y/n|\[Y/N\]", output, re.IGNORECASE):
        channel.send("y\n")
        output += _read_until_prompt(channel, prompt_regex, timeout_sec=30)
    if is_error_output(output):
        return False, output.strip()[-200:]
    return True, ""


def restore_config_from_backup(
    olt: OLTDevice, config_text: str, *, persist: bool = True
) -> tuple[bool, str]:
    """Replay a backed-up running config to an OLT over SSH."""
    from app.services.network.olt_ssh import (
        _apply_paging_off,
        _open_shell,
        _read_until_prompt,
        _run_huawei_cmd,
        is_error_output,
    )

    commands = _restore_config_commands(config_text)
    if not commands:
        return False, "Backup contains no restorable configuration commands"

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)
        _apply_paging_off(channel, policy, policy.prompt_regex)

        config_prompt = r"\][ \t]*$"
        system_view = _run_huawei_cmd(channel, "system-view", prompt=config_prompt)
        if is_error_output(system_view):
            return False, describe_huawei_rejection(system_view, action="config mode")

        errors: list[str] = []
        applied = 0
        for command in commands:
            output = _run_huawei_cmd(channel, command, prompt=config_prompt)
            if is_error_output(output):
                errors.append(f"{command}: {output.strip()[-160:]}")
                if len(errors) >= 10:
                    break
                continue
            applied += 1

        quit_output = _run_huawei_cmd(channel, "quit", prompt=policy.prompt_regex)
        if is_error_output(quit_output):
            errors.append(f"quit: {quit_output.strip()[-160:]}")

        if errors:
            return (
                False,
                f"Restore applied {applied}/{len(commands)} commands; errors: {' | '.join(errors[:3])}",
            )
        if persist:
            saved, save_error = _save_running_config(
                channel, policy.prompt_regex, is_error_output
            )
            if not saved:
                return (
                    False,
                    f"Restore applied {applied}/{len(commands)} commands but failed to save startup config: {save_error}",
                )
            return (
                True,
                f"Restored {applied} configuration commands from backup and saved startup config",
            )
        return True, f"Restored {applied} configuration commands from backup"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error restoring config on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()
