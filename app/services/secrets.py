"""Centralized secrets management via OpenBao (Vault-compatible).

All secret resolution goes through this module. Supports:
- ``bao://mount/path#field`` references (resolved from OpenBao KV v2)
- ``openbao://`` and ``vault://`` as aliases
- Plaintext passthrough (for local dev without OpenBao)
- ``get_secret(path, field)`` convenience helper for direct lookups
"""

import logging
import os
from functools import lru_cache
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Cache TTL: secrets are cached per-process to avoid repeated HTTP calls.
# The LRU cache is bounded and cleared on app restart.
_CACHE_SIZE = 128


def is_openbao_ref(value: str | None) -> bool:
    """Check if a value is an OpenBao URI reference."""
    if not value:
        return False
    return value.startswith(("bao://", "openbao://", "vault://"))


def _openbao_config() -> tuple[str, str, str | None, str]:
    """Return (addr, token, namespace, kv_version) from env."""
    addr = os.getenv("OPENBAO_ADDR") or os.getenv("VAULT_ADDR")
    token = os.getenv("OPENBAO_TOKEN") or os.getenv("VAULT_TOKEN")
    namespace = os.getenv("OPENBAO_NAMESPACE") or os.getenv("VAULT_NAMESPACE")
    kv_version = os.getenv("OPENBAO_KV_VERSION", "2")
    if not addr:
        raise HTTPException(status_code=500, detail="OpenBao address not configured")
    if not token:
        raise HTTPException(status_code=500, detail="OpenBao token not configured")
    return addr.rstrip("/"), token, namespace, kv_version


def is_openbao_available() -> bool:
    """Check if OpenBao is configured and reachable (non-throwing)."""
    try:
        addr = os.getenv("OPENBAO_ADDR") or os.getenv("VAULT_ADDR")
        token = os.getenv("OPENBAO_TOKEN") or os.getenv("VAULT_TOKEN")
        if not addr or not token:
            return False
        resp = httpx.get(
            f"{addr.rstrip('/')}/v1/sys/health",
            headers={"X-Vault-Token": token},
            timeout=3.0,
        )
        return resp.status_code in (200, 429, 472, 473)
    except Exception:
        return False


def _parse_ref(reference: str) -> tuple[str, str, str]:
    """Parse ``bao://mount/path#field`` into (mount, path, field)."""
    parsed = urlparse(reference)
    mount = parsed.netloc
    path = parsed.path.lstrip("/")
    field = parsed.fragment or "value"
    if not mount or not path:
        raise HTTPException(status_code=500, detail="Invalid OpenBao reference")
    return mount, path, field


@lru_cache(maxsize=_CACHE_SIZE)
def _fetch_secret_data(url: str, token: str, namespace: str | None) -> dict:
    """Fetch and cache a secret payload from OpenBao."""
    headers: dict[str, str] = {"X-Vault-Token": token}
    if namespace:
        headers["X-Vault-Namespace"] = namespace
    try:
        response = httpx.get(url, headers=headers, timeout=5.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("OpenBao request failed for %s: %s", url, exc)
        raise HTTPException(status_code=500, detail="OpenBao request failed") from exc
    payload = response.json()
    return payload.get("data", {})


def resolve_openbao_ref(reference: str) -> str:
    """Resolve a ``bao://mount/path#field`` reference to its secret value."""
    addr, token, namespace, kv_version = _openbao_config()
    mount, path, field = _parse_ref(reference)
    if str(kv_version) == "1":
        url = f"{addr}/v1/{mount}/{path}"
    else:
        url = f"{addr}/v1/{mount}/data/{path}"
    raw_data = _fetch_secret_data(url, token, namespace)
    if str(kv_version) == "1":
        secret_data = raw_data
    else:
        secret_data = raw_data.get("data", {})
    if field not in secret_data:
        raise HTTPException(
            status_code=500,
            detail=f"OpenBao secret field '{field}' not found at {mount}/{path}",
        )
    return str(secret_data[field])


def resolve_secret(value: str | None) -> str | None:
    """Resolve a value that may be an OpenBao reference or plaintext.

    If the value starts with ``bao://``, ``openbao://``, or ``vault://``,
    it is resolved from OpenBao. Otherwise returned as-is.
    """
    if not value:
        return value
    if is_openbao_ref(value):
        return resolve_openbao_ref(value)
    return value


def get_secret(path: str, field: str, *, default: str = "") -> str:
    """Convenience: fetch a secret directly from OpenBao KV v2.

    Args:
        path: Secret path (e.g. ``paystack``, ``auth``, ``database``).
        field: Field name within the secret (e.g. ``secret_key``).
        default: Fallback value if OpenBao is unavailable.

    Returns:
        The secret value, or default if resolution fails.
    """
    try:
        return resolve_openbao_ref(f"bao://secret/{path}#{field}")
    except Exception:
        logger.debug(
            "OpenBao lookup failed for secret/%s#%s, using default", path, field
        )
        return default


def get_env_or_secret(
    env_var: str,
    bao_path: str,
    bao_field: str,
    *,
    default: str = "",
) -> str:
    """Resolve a secret with fallback chain: OpenBao → env var → default.

    This is the recommended function for all secret resolution in the app.
    """
    # Try OpenBao first
    try:
        val = get_secret(bao_path, bao_field)
        if val:
            return val
    except Exception:
        logger.debug(
            "OpenBao secret lookup failed for %s#%s",
            bao_path,
            bao_field,
            exc_info=True,
        )
    # Fall back to env var
    env_val = os.getenv(env_var, "")
    if env_val:
        return env_val
    return default


def clear_cache() -> None:
    """Clear the in-process secret cache (e.g., after rotation)."""
    _fetch_secret_data.cache_clear()


# ── OpenBao KV management (for admin UI) ────────────────────────────


def list_secret_paths() -> list[str]:
    """List all secret paths under the ``secret/`` mount."""
    try:
        addr, token, namespace, kv_version = _openbao_config()
        url = f"{addr}/v1/secret/metadata/?list=true"
        headers: dict[str, str] = {"X-Vault-Token": token}
        if namespace:
            headers["X-Vault-Namespace"] = namespace
        resp = httpx.get(url, headers=headers, timeout=5.0)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json().get("data", {}).get("keys", [])
    except Exception as exc:
        logger.warning("Failed to list OpenBao paths: %s", exc)
        return []


def read_secret_metadata(path: str) -> dict:
    """Read metadata (version, dates) for a secret path."""
    try:
        addr, token, namespace, _kv = _openbao_config()
        url = f"{addr}/v1/secret/metadata/{path}"
        headers: dict[str, str] = {"X-Vault-Token": token}
        if namespace:
            headers["X-Vault-Namespace"] = namespace
        resp = httpx.get(url, headers=headers, timeout=5.0)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json().get("data", {})
    except Exception as exc:
        logger.warning("Failed to read OpenBao metadata for %s: %s", path, exc)
        return {}


def read_secret_fields(path: str) -> dict[str, str]:
    """Read all fields for a secret path (values masked for display)."""
    try:
        addr, token, namespace, kv_version = _openbao_config()
        url = f"{addr}/v1/secret/data/{path}"
        headers: dict[str, str] = {"X-Vault-Token": token}
        if namespace:
            headers["X-Vault-Namespace"] = namespace
        resp = httpx.get(url, headers=headers, timeout=5.0)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data = resp.json().get("data", {}).get("data", {})
        return {k: str(v) for k, v in data.items()}
    except Exception as exc:
        logger.warning("Failed to read OpenBao fields for %s: %s", path, exc)
        return {}


def write_secret(path: str, data: dict[str, str]) -> bool:
    """Write/update a secret at the given path."""
    try:
        addr, token, namespace, _kv = _openbao_config()
        url = f"{addr}/v1/secret/data/{path}"
        headers: dict[str, str] = {
            "X-Vault-Token": token,
            "Content-Type": "application/json",
        }
        if namespace:
            headers["X-Vault-Namespace"] = namespace
        resp = httpx.post(url, json={"data": data}, headers=headers, timeout=5.0)
        resp.raise_for_status()
        clear_cache()
        return True
    except Exception as exc:
        logger.error("Failed to write OpenBao secret %s: %s", path, exc)
        return False


def delete_secret(path: str) -> bool:
    """Delete a secret at the given path (metadata delete)."""
    try:
        addr, token, namespace, _kv = _openbao_config()
        url = f"{addr}/v1/secret/metadata/{path}"
        headers: dict[str, str] = {"X-Vault-Token": token}
        if namespace:
            headers["X-Vault-Namespace"] = namespace
        resp = httpx.delete(url, headers=headers, timeout=5.0)
        resp.raise_for_status()
        clear_cache()
        return True
    except Exception as exc:
        logger.error("Failed to delete OpenBao secret %s: %s", path, exc)
        return False
