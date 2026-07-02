"""SSH-based RouterOS configuration export.

RouterOS 7.x cannot deliver a config export over the REST API: inline
``POST /rest/export`` returns an empty array, and while the file-based export
(``POST /rest/export {"file": ...}``) writes a real ``.rsc`` on the router, the
file's contents are not readable back over REST (there is a small-file cap on
the ``contents`` property). The reliable transport is SSH — the ``/export``
command returns the full config as text.

This uses the dedicated ``dotmac-ops`` SSH automation identity (fleet-wide,
key-only, ``ssh`` policy) — NOT the ``snap-api`` REST/API/poller identity, which
is intentionally ``!ssh``. Keeping the two planes separate is deliberate.
"""

from __future__ import annotations

import logging

import paramiko

from app.config import settings

logger = logging.getLogger(__name__)


class RouterConfigExportError(RuntimeError):
    """Raised when the SSH config export fails or returns no config."""


def _load_private_key(key_path: str, passphrase: str | None = None):
    """Load an SSH private key, trying the common RouterOS-compatible types."""
    last_error: Exception | None = None
    for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_cls.from_private_key_file(key_path, password=passphrase or None)
        except Exception as exc:  # wrong key type / format — try the next
            last_error = exc
    raise RouterConfigExportError(
        f"could not load SSH private key {key_path!r}: {last_error}"
    )


def export_config_via_ssh(
    router,
    *,
    username: str | None = None,
    port: int | None = None,
    key_path: str | None = None,
    command: str = "/export",
    timeout: int = 30,
) -> str:
    """Return the router's full config as text via ``ssh <router> /export``.

    Raises :class:`RouterConfigExportError` on connection/auth failure or when
    the command returns no config (so the caller records a failure rather than
    storing a blank snapshot).
    """
    username = username or settings.router_config_ssh_username
    port = port or settings.router_config_ssh_port
    key_path = key_path or settings.router_config_ssh_key_path
    if not key_path:
        raise RouterConfigExportError("ROUTER_CONFIG_SSH_KEY_PATH is not configured")

    pkey = _load_private_key(key_path)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=router.management_ip,
            port=port,
            username=username,
            pkey=pkey,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        exit_status = stdout.channel.recv_exit_status()
    except RouterConfigExportError:
        raise
    except Exception as exc:
        raise RouterConfigExportError(
            f"SSH {command} to {getattr(router, 'name', router.management_ip)} "
            f"failed: {exc}"
        ) from exc
    finally:
        client.close()

    if not out.strip():
        raise RouterConfigExportError(
            f"SSH {command} to {getattr(router, 'name', router.management_ip)} "
            f"returned no config (exit={exit_status}, stderr={err.strip()[:200]!r})"
        )
    return out
