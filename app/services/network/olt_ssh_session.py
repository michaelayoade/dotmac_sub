"""Reusable OLT SSH session context manager with structured error detection.

This module provides a context manager for SSH connections to OLTs that:
- Maintains a single connection for multiple commands (avoids connection exhaustion)
- Tracks CLI mode state (enable, config, interface)
- Provides structured error detection via ErrorCode enum
- Treats "already exists" as idempotent success, not failure

Example usage:
    with olt_session(olt) as session:
        result = session.run_command("service-port vlan 100 gpon 0/2/1 ont 1 gemport 1")
        if result.success or result.error_code == ErrorCode.ALREADY_EXISTS:
            # Success or idempotent success
            pass
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from paramiko.channel import Channel
from paramiko.ssh_exception import SSHException
from paramiko.transport import Transport

from app.services.network.huawei_cli_response import (
    HuaweiCliErrorCode as ErrorCode,
)
from app.services.network.huawei_cli_response import classify_huawei_cli_response
from app.services.outbound_effects import Effect, require

if TYPE_CHECKING:
    from app.models.network import OLTDevice

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structured Error Detection
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    """Result of executing an OLT CLI command.

    Attributes:
        success: True if command succeeded (including idempotent success).
        output: Raw command output from OLT.
        error_code: Structured error code (NONE for success).
        message: Human-readable message about the result.
    """

    success: bool
    output: str
    error_code: ErrorCode = ErrorCode.NONE
    message: str = ""

    @property
    def is_idempotent_success(self) -> bool:
        """Return True if this is an idempotent success (resource already exists)."""
        return self.error_code == ErrorCode.ALREADY_EXISTS


def parse_command_result(output: str) -> CommandResult:
    """Parse OLT CLI output into a structured CommandResult.

    Checks output against known error patterns and returns appropriate
    ErrorCode. Treats ALREADY_EXISTS as success (idempotent operation).

    Args:
        output: Raw CLI output from OLT.

    Returns:
        CommandResult with success/error classification.
    """
    response = classify_huawei_cli_response(output)
    if response.error_code != ErrorCode.NONE:
        if response.is_idempotent_success:
            label = "Idempotent success"
        else:
            label = "Accepted" if response.accepted else "Error"
        return CommandResult(
            success=response.accepted,
            output=output,
            error_code=response.error_code,
            message=f"{label}: {response.error_code.value}",
        )

    return CommandResult(
        success=True,
        output=output,
        error_code=ErrorCode.NONE,
        message="Command executed successfully",
    )


# ---------------------------------------------------------------------------
# CLI Mode Tracking
# ---------------------------------------------------------------------------


class CliMode(Enum):
    """OLT CLI mode state."""

    USER = "user"  # Initial mode after login
    ENABLE = "enable"  # Privileged mode
    CONFIG = "config"  # Global config mode
    INTERFACE = "interface"  # Interface config mode


# ---------------------------------------------------------------------------
# Command Transmission (delegated to the single owner)
# ---------------------------------------------------------------------------


def _send(
    olt: OLTDevice, channel: Channel, command: str, *, paced: bool = True
) -> None:
    """Delegate to the single owner of Huawei command transmission.

    This module previously kept its own character-by-character sender. Two
    parallel senders meant two answers to "how does a command line reach a
    shelf"; the char-by-char one is exactly the pattern that lets a Huawei
    line editor coalesce spaces. ``send_ont_command`` writes the line
    atomically and paces between commands per the OLT's command profile.
    """
    from app.services.network.olt_ssh_ont._common import send_ont_command

    send_ont_command(olt, channel, command, pace_sec=None if paced else 0.0)


# ---------------------------------------------------------------------------
# OLT Session Context Manager
# ---------------------------------------------------------------------------


@dataclass
class OltSession:
    """Reusable SSH session for OLT operations.

    Maintains a single SSH connection and tracks CLI mode state.
    Commands are executed via run_command(), which handles mode
    transitions and error detection.

    Note: This class should be used via the olt_session() context manager,
    not instantiated directly.
    """

    olt: OLTDevice
    transport: Transport
    channel: Channel
    prompt_regex: str
    current_mode: CliMode = CliMode.USER
    _usable: bool = True

    def run_command(
        self,
        command: str,
        *,
        timeout_sec: float = 12.0,
        require_mode: CliMode | None = None,
        slow_send: bool = True,
    ) -> CommandResult:
        """Execute a command on the OLT.

        Args:
            command: CLI command to execute.
            timeout_sec: Timeout for command response.
            require_mode: Required mode; CONFIG includes GPON interface mode for
                generated configuration batches. ENABLE unwinds both modes.
            slow_send: If True (default), pace after the command line per the
                OLT's command profile. The line itself is always written
                atomically — see ``send_ont_command``, the single owner.

        Returns:
            CommandResult with success/error classification.
        """
        from app.services.network.olt_ssh import (
            _derive_prompt_regex,
            _read_paged_response,
            _strip_pager,
        )

        try:
            if not self._usable:
                raise RuntimeError(
                    "OLT session is unusable after an incomplete command"
                )
            # CONFIG means the configuration tree for existing generated batches:
            # their interface-scoped commands also request CONFIG on each call.
            if require_mode == CliMode.CONFIG and self.current_mode not in (
                CliMode.CONFIG,
                CliMode.INTERFACE,
            ):
                self._enter_config_mode()
            elif require_mode == CliMode.ENABLE and self.current_mode != CliMode.ENABLE:
                self._enter_enable_mode()
            elif require_mode not in (
                None,
                CliMode.CONFIG,
                CliMode.ENABLE,
                self.current_mode,
            ):
                raise ValueError("Unsupported OLT mode requirement")

            target = self._navigation_target(command)
            expected = self.prompt_regex if target is None else target[1]
            pending = expected if target is None else f"{expected}|{self.prompt_regex}"

            logger.debug("OLT command: %r (paced=%s)", command, slow_send)
            _send(self.olt, self.channel, command, paced=slow_send)

            # Read response through the bounded paged reader: answers the pager
            # and argument prompts, strips pager/ANSI, raises if no prompt.
            output = _strip_pager(
                _read_paged_response(
                    self.channel,
                    pending,
                    timeout_sec=max(120.0, timeout_sec),
                    read_timeout_sec=timeout_sec,
                    answer_optional_args=True,
                )
            )

            result = parse_command_result(output)
            if target is not None:
                if not result.success or not re.search(expected, output):
                    self._usable = False
                    return CommandResult(
                        success=False,
                        output=output,
                        error_code=result.error_code
                        if not result.success
                        else ErrorCode.CONNECTION_ERROR,
                        message="OLT navigation did not reach the requested mode",
                    )
                self.prompt_regex = _derive_prompt_regex(output, expected)
                self.current_mode = target[0]
            return result

        except Exception as exc:
            self._usable = False
            logger.error("Error executing command on OLT %s: %s", self.olt.name, exc)
            return CommandResult(
                success=False,
                output="",
                error_code=ErrorCode.CONNECTION_ERROR,
                message=f"Command execution error: {exc}",
            )

    def run_config_command(
        self,
        command: str,
        *,
        timeout_sec: float = 12.0,
    ) -> CommandResult:
        """Execute a command in config mode."""
        if self.current_mode == CliMode.INTERFACE:
            navigation = self.run_command("quit", timeout_sec=timeout_sec)
            if not navigation.success:
                return navigation
        return self.run_command(
            command, timeout_sec=timeout_sec, require_mode=CliMode.CONFIG
        )

    def run_commands(
        self,
        commands: list[str],
        *,
        timeout_sec: float = 12.0,
        require_mode: CliMode | None = None,
        stop_on_error: bool = True,
    ) -> list[CommandResult]:
        """Execute multiple commands sequentially.

        Args:
            commands: List of CLI commands to execute.
            timeout_sec: Timeout per command.
            require_mode: If set, ensure we're in this mode.
            stop_on_error: If True, stop executing on first error.

        Returns:
            List of CommandResults, one per command.
        """
        results: list[CommandResult] = []

        for cmd in commands:
            result = self.run_command(
                cmd, timeout_sec=timeout_sec, require_mode=require_mode
            )
            results.append(result)

            if (
                stop_on_error
                and not result.success
                and not result.is_idempotent_success
            ):
                break

        return results

    def _prompt_with_suffix(self, suffix: str) -> str:
        """Keep the exact host anchor when changing the expected CLI mode."""
        pattern = self.prompt_regex
        if pattern.endswith(r"\)\#\s*$") and r"\(config" in pattern:
            stem = pattern.split(r"\(config", 1)[0]
        else:
            for ending in (r"\#\s*$", r"#\s*$", r">\s*$"):
                if pattern.endswith(ending):
                    stem = pattern.removesuffix(ending)
                    break
            else:
                raise ValueError("Cannot derive an exact OLT navigation prompt")
        return stem + re.escape(suffix) + r"\s*$"

    def _navigation_target(self, command: str) -> tuple[CliMode, str] | None:
        """Describe supported navigation before sending it to the shelf."""
        command = command.strip()
        interface = re.fullmatch(r"interface gpon (\d{1,2})/(\d{1,2})", command)
        if interface:
            if self.current_mode != CliMode.CONFIG:
                raise ValueError("GPON interface entry requires global config mode")
            suffix = f"(config-if-gpon-{interface[1]}/{interface[2]})#"
            return CliMode.INTERFACE, self._prompt_with_suffix(suffix)
        if command.startswith("interface "):
            raise ValueError("Unsupported OLT interface navigation")
        if command == "config":
            if self.current_mode != CliMode.ENABLE:
                raise ValueError("Config entry requires privileged mode")
            return CliMode.CONFIG, self._prompt_with_suffix("(config)#")
        if command == "enable":
            return CliMode.ENABLE, self._prompt_with_suffix("#")
        if command == "return":
            return CliMode.ENABLE, self._prompt_with_suffix("#")
        if command == "quit":
            if self.current_mode == CliMode.INTERFACE:
                return CliMode.CONFIG, self._prompt_with_suffix("(config)#")
            if self.current_mode == CliMode.CONFIG:
                return CliMode.ENABLE, self._prompt_with_suffix("#")
            raise ValueError("Session logout is not a command-mode transition")
        return None

    def _enter_enable_mode(self) -> None:
        """Enter privileged mode, unwinding known configuration modes first."""
        from app.services.network.olt_ssh import _prepare_huawei_read_shell

        if not self._usable:
            raise RuntimeError("OLT session is unusable after an incomplete command")
        if self.current_mode == CliMode.INTERFACE:
            self._require_navigation("quit")
        if self.current_mode == CliMode.CONFIG:
            self.exit_config_mode()
        if self.current_mode == CliMode.USER:
            try:
                self.prompt_regex = _prepare_huawei_read_shell(
                    self.channel, self.prompt_regex
                )
                self.current_mode = CliMode.ENABLE
            except Exception:
                self._usable = False
                raise

    def _require_navigation(self, command: str) -> None:
        result = self.run_command(command, timeout_sec=5)
        if not result.success:
            raise RuntimeError(result.message)

    def _enter_config_mode(self) -> None:
        """Enter global config mode through the same navigation reader."""
        if self.current_mode == CliMode.USER:
            self._enter_enable_mode()
        if self.current_mode == CliMode.INTERFACE:
            self._require_navigation("quit")
        elif self.current_mode != CliMode.CONFIG:
            self._require_navigation("config")

    def exit_config_mode(self) -> None:
        """Return to privileged mode using verified interface/config exits."""
        if self.current_mode == CliMode.INTERFACE:
            self._require_navigation("quit")
        if self.current_mode == CliMode.CONFIG:
            self._require_navigation("quit")


@contextmanager
def olt_session(olt: OLTDevice) -> Iterator[OltSession]:
    """Context manager for OLT SSH sessions.

    Provides a single SSH connection that can be used for multiple commands,
    avoiding connection exhaustion issues.

    Example:
        with olt_session(olt) as session:
            result1 = session.run_config_command("service-port vlan 100 ...")
            result2 = session.run_config_command("service-port vlan 200 ...")

    Args:
        olt: The OLT device to connect to.

    Yields:
        OltSession instance for running commands.

    Raises:
        SSHException: If connection fails.
        ValueError: If OLT credentials are missing.
    """
    require(Effect.device_ssh)
    from app.services.network.olt_ssh import (
        _open_shell,
        _prepare_huawei_read_shell,
    )

    transport: Transport | None = None

    try:
        transport, channel, policy = _open_shell(olt)

        prompt = _prepare_huawei_read_shell(
            channel,
            policy.prompt_regex,
            paging_off_command=policy.paging_off_command,
        )
        session = OltSession(
            olt=olt,
            transport=transport,
            channel=channel,
            prompt_regex=prompt,
            current_mode=CliMode.ENABLE,
        )

        yield session

    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        logger.error("Failed to establish OLT session to %s: %s", olt.name, exc)
        raise

    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
