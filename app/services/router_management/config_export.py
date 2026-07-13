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

import json
import logging
import os

import paramiko
from paramiko.ssh_exception import AuthenticationException

from app.config import settings
from app.models.router_management import Router

logger = logging.getLogger(__name__)


class RouterConfigExportError(RuntimeError):
    """Raised when the SSH config export fails or returns no config."""


def _export_to_text(data: object) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "\n".join(
            item if isinstance(item, str) else json.dumps(item) for item in data
        )
    if isinstance(data, dict):
        return json.dumps(data)
    return str(data)


def fetch_config_export(router: Router) -> str:
    """Fetch a non-empty RouterOS export through the configured transport."""
    if settings.router_config_export_via_ssh:
        return export_config_via_ssh(router)

    from app.services.router_management.connection import RouterConnectionService

    data = RouterConnectionService.execute(router, "POST", "/export")
    text = _export_to_text(data)
    if not text.strip():
        name = getattr(router, "name", "router")
        raise RouterConfigExportError(
            f"Router {name} returned an empty config export; the REST API "
            "identity may be missing the sensitive policy"
        )
    return text


def _install_host_key_policy(client: paramiko.SSHClient) -> None:
    """Pin router SSH host keys (trust-on-first-use).

    Loading the known_hosts file means paramiko rejects a *changed* host key
    (``BadHostKeyException`` — possible MITM) instead of silently trusting it.
    A first-seen host is added and persisted so it is pinned thereafter. With
    ``router_config_ssh_strict_host_key`` on, unknown hosts are rejected too
    (requires a pre-populated known_hosts).
    """
    known_hosts = getattr(settings, "router_config_ssh_known_hosts_path", "")
    if known_hosts:
        try:
            directory = os.path.dirname(known_hosts)
            if directory:
                os.makedirs(directory, exist_ok=True)
            if not os.path.exists(known_hosts):
                open(known_hosts, "a").close()  # touch so first-seen keys persist
            client.load_host_keys(known_hosts)  # type: ignore[attr-defined]
        except OSError as exc:
            logger.warning("router known_hosts %r unavailable: %s", known_hosts, exc)
    if getattr(settings, "router_config_ssh_strict_host_key", False):
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        # Not blind trust: load_host_keys() above still rejects a *changed* key;
        # AutoAddPolicy only pins a first-seen host (persisted to known_hosts).
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # nosec B507


def _load_private_key(key_path: str, passphrase: str | None = None):
    """Load an SSH private key, trying the common RouterOS-compatible types."""
    last_error: Exception | None = None
    for key_cls in (
        paramiko.Ed25519Key,  # type: ignore[attr-defined]
        paramiko.RSAKey,
        paramiko.ECDSAKey,  # type: ignore[attr-defined]
    ):
        try:
            return key_cls.from_private_key_file(key_path, password=passphrase or None)
        except Exception as exc:  # wrong key type / format — try the next
            last_error = exc
    raise RouterConfigExportError(
        f"could not load SSH private key {key_path!r}: {last_error}"
    )


def _resolve_ssh_password() -> str:
    """Resolve the optional snapshot-user password (may be an OpenBao/secret ref)."""
    raw = getattr(settings, "router_config_ssh_password", "") or ""
    if not raw:
        return ""
    try:
        from app.services.secrets import resolve_secret

        return resolve_secret(raw) or ""
    except Exception:
        logger.warning("router SSH password: secret ref did not resolve", exc_info=True)
        return ""


def export_config_via_ssh(
    router: Router,
    *,
    username: str | None = None,
    port: int | None = None,
    key_path: str | None = None,
    password: str | None = None,
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
    key_path = key_path if key_path is not None else settings.router_config_ssh_key_path
    password = password if password is not None else _resolve_ssh_password()

    # Key-preferred auth: use the SSH key when configured, and fall back to a
    # password (a least-privilege ssh,read snapshot user) either when no key is
    # set or when the router rejects the key — e.g. a not-yet-keyed new router,
    # which can then snapshot immediately via a one-line `/user add password=...`
    # instead of a public-key file import.
    pkey = _load_private_key(key_path) if key_path else None
    if pkey is None and not password:
        raise RouterConfigExportError(
            "no SSH auth configured: set ROUTER_CONFIG_SSH_KEY_PATH or "
            "ROUTER_CONFIG_SSH_PASSWORD"
        )

    base_kwargs = dict(
        hostname=router.management_ip,
        port=port,
        username=username,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    client = paramiko.SSHClient()
    _install_host_key_policy(client)
    try:
        try:
            client.connect(  # type: ignore[call-arg]
                **base_kwargs,
                **({"pkey": pkey} if pkey is not None else {"password": password}),
            )
        except AuthenticationException:
            # Key rejected — fall back to the password if one is configured.
            if pkey is None or not password:
                raise
            client.connect(**base_kwargs, password=password)  # type: ignore[call-arg]
        # Fixed, non-interpolated command (no user input) — not a shell.
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)  # nosec B601
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
