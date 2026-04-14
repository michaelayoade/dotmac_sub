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

if TYPE_CHECKING:
    from app.models.network import OLTDevice

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured Error Detection
# ---------------------------------------------------------------------------


class ErrorCode(Enum):
    """Structured error codes for OLT CLI responses.

    Using structured error codes instead of string matching provides:
    - Consistent error handling across the codebase
    - Explicit handling of idempotent success (ALREADY_EXISTS)
    - Better localization support (patterns can match multiple languages)
    """

    NONE = "none"  # No error
    ALREADY_EXISTS = "already_exists"  # Resource exists - idempotent success
    VLAN_NOT_EXIST = "vlan_not_exist"  # VLAN not configured on OLT
    ONT_OFFLINE = "ont_offline"  # ONT is not online
    ONT_NOT_EXIST = "ont_not_exist"  # ONT not found/authorized
    PARAMETER_ERROR = "parameter_error"  # Invalid parameter
    UNKNOWN_COMMAND = "unknown_command"  # Command not recognized
    PERMISSION_DENIED = "permission_denied"  # Insufficient privileges
    RESOURCE_BUSY = "resource_busy"  # Resource locked/in use
    INDEX_OUT_OF_RANGE = "index_out_of_range"  # Index exceeds limits
    PROFILE_NOT_EXIST = "profile_not_exist"  # Profile not found
    CONNECTION_ERROR = "connection_error"  # SSH/transport error
    TIMEOUT = "timeout"  # Command timeout
    UNKNOWN_ERROR = "unknown_error"  # Unrecognized error


# Error patterns mapped to ErrorCode
# Patterns are checked in order; first match wins
# Includes English and Chinese (Huawei OLT) error messages
_ERROR_PATTERNS: list[tuple[str, ErrorCode]] = [
    # Idempotent success - resource already exists
    (r"service virtual port has existed already", ErrorCode.ALREADY_EXISTS),
    (r"already exists", ErrorCode.ALREADY_EXISTS),
    (r"conflicted service virtual port index", ErrorCode.ALREADY_EXISTS),
    (r"tr069.*server.*profile.*already.*bindw", ErrorCode.ALREADY_EXISTS),
    # VLAN errors
    (r"vlan.*does not exist", ErrorCode.VLAN_NOT_EXIST),
    (r"vlan.*not.*configured", ErrorCode.VLAN_NOT_EXIST),
    (r"vlan.*is not exist", ErrorCode.VLAN_NOT_EXIST),
    # ONT errors
    (r"ont is not online", ErrorCode.ONT_OFFLINE),
    (r"ont.*offline", ErrorCode.ONT_OFFLINE),
    (r"ont.*does not exist", ErrorCode.ONT_NOT_EXIST),
    (r"ont.*is not exist", ErrorCode.ONT_NOT_EXIST),
    (r"ont.*not found", ErrorCode.ONT_NOT_EXIST),
    # Profile errors
    (r"profile.*does not exist", ErrorCode.PROFILE_NOT_EXIST),
    (r"profile.*is not exist", ErrorCode.PROFILE_NOT_EXIST),
    (r"tr069.*server.*profile.*does not exist", ErrorCode.PROFILE_NOT_EXIST),
    # Index/range errors
    (r"index.*out of range", ErrorCode.INDEX_OUT_OF_RANGE),
    (r"exceeds.*maximum", ErrorCode.INDEX_OUT_OF_RANGE),
    (r"ip-index.*invalid", ErrorCode.INDEX_OUT_OF_RANGE),
    # Parameter errors
    (r"% parameter error", ErrorCode.PARAMETER_ERROR),
    (r"invalid parameter", ErrorCode.PARAMETER_ERROR),
    (r"invalid input", ErrorCode.PARAMETER_ERROR),
    # Command errors
    (r"% unknown command", ErrorCode.UNKNOWN_COMMAND),
    (r"unrecognized", ErrorCode.UNKNOWN_COMMAND),
    (r"command not found", ErrorCode.UNKNOWN_COMMAND),
    (r"incomplete command", ErrorCode.UNKNOWN_COMMAND),
    # Permission errors
    (r"permission denied", ErrorCode.PERMISSION_DENIED),
    (r"access denied", ErrorCode.PERMISSION_DENIED),
    # Resource busy
    (r"resource.*busy", ErrorCode.RESOURCE_BUSY),
    (r"locked", ErrorCode.RESOURCE_BUSY),
    # Chinese error messages (Huawei OLT)
    (r"\u5931\u8d25", ErrorCode.UNKNOWN_ERROR),  # 失败 (failure)
    (r"\u9519\u8bef", ErrorCode.UNKNOWN_ERROR),  # 错误 (error)
    # Generic error patterns (last resort)
    (r"failure", ErrorCode.UNKNOWN_ERROR),
    (r"failed", ErrorCode.UNKNOWN_ERROR),
    (r"error:", ErrorCode.UNKNOWN_ERROR),
]


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
    lower = output.lower()

    for pattern, code in _ERROR_PATTERNS:
        if re.search(pattern, lower, re.IGNORECASE):
            # ALREADY_EXISTS is treated as success (idempotent)
            is_success = code == ErrorCode.ALREADY_EXISTS
            return CommandResult(
                success=is_success,
                output=output,
                error_code=code,
                message=f"{'Idempotent success' if is_success else 'Error'}: {code.value}",
            )

    # No error pattern matched - assume success
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

    def run_command(
        self,
        command: str,
        *,
        timeout_sec: float = 12.0,
        require_mode: CliMode | None = None,
    ) -> CommandResult:
        """Execute a command on the OLT.

        Args:
            command: CLI command to execute.
            timeout_sec: Timeout for command response.
            require_mode: If set, ensure we're in this mode before running command.

        Returns:
            CommandResult with success/error classification.
        """
        from app.services.network.olt_ssh import (
            _run_huawei_cmd,
        )

        try:
            # Mode transitions if needed
            if require_mode == CliMode.CONFIG and self.current_mode != CliMode.CONFIG:
                self._enter_config_mode()
            elif require_mode == CliMode.ENABLE and self.current_mode == CliMode.USER:
                self._enter_enable_mode()

            # Execute command
            output = _run_huawei_cmd(
                self.channel,
                command,
                prompt=self.prompt_regex,
            )

            return parse_command_result(output)

        except Exception as exc:
            logger.error(
                "Error executing command on OLT %s: %s", self.olt.name, exc
            )
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
        return self.run_command(command, timeout_sec=timeout_sec, require_mode=CliMode.CONFIG)

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

            if stop_on_error and not result.success and not result.is_idempotent_success:
                break

        return results

    def _enter_enable_mode(self) -> None:
        """Enter enable/privileged mode."""
        from app.services.network.olt_ssh import _read_until_prompt

        self.channel.send("enable\n")
        _read_until_prompt(self.channel, r"#\s*$", timeout_sec=5)
        self.current_mode = CliMode.ENABLE

    def _enter_config_mode(self) -> None:
        """Enter global config mode."""
        from app.services.network.olt_ssh import _run_huawei_cmd

        if self.current_mode == CliMode.USER:
            self._enter_enable_mode()

        _run_huawei_cmd(self.channel, "config", prompt=r"[#)]\s*$")
        self.current_mode = CliMode.CONFIG

    def exit_config_mode(self) -> None:
        """Exit from config mode back to enable mode."""
        from app.services.network.olt_ssh import _run_huawei_cmd

        if self.current_mode == CliMode.CONFIG:
            _run_huawei_cmd(self.channel, "quit", prompt=r"#\s*$")
            self.current_mode = CliMode.ENABLE


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
    from app.services.network.olt_ssh import (
        _open_shell,
        _read_until_prompt,
    )

    transport: Transport | None = None

    try:
        transport, channel, policy = _open_shell(olt)

        session = OltSession(
            olt=olt,
            transport=transport,
            channel=channel,
            prompt_regex=policy.prompt_regex,
            current_mode=CliMode.USER,
        )

        # Enter enable mode and set terminal length
        session._enter_enable_mode()
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

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
