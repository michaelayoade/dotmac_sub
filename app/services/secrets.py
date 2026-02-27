import os
import re
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException


def is_openbao_ref(value: str | None) -> bool:
    if not value:
        return False
    return value.startswith(("bao://", "openbao://", "vault://"))


def _openbao_config():
    addr = os.getenv("OPENBAO_ADDR") or os.getenv("VAULT_ADDR")
    token = os.getenv("OPENBAO_TOKEN") or os.getenv("VAULT_TOKEN")
    namespace = os.getenv("OPENBAO_NAMESPACE") or os.getenv("VAULT_NAMESPACE")
    kv_version = os.getenv("OPENBAO_KV_VERSION", "2")
    if not addr:
        raise HTTPException(status_code=500, detail="OpenBao address not configured")
    if not token:
        raise HTTPException(status_code=500, detail="OpenBao token not configured")
    return addr.rstrip("/"), token, namespace, kv_version


def _parse_ref(reference: str) -> tuple[str, str, str]:
    parsed = urlparse(reference)
    mount = parsed.netloc
    path = parsed.path.lstrip("/")
    field = parsed.fragment or "value"
    if not mount or not path:
        raise HTTPException(status_code=500, detail="Invalid OpenBao reference")
    return mount, path, field


def _is_safe_openbao_component(value: str) -> bool:
    if re.match(r"^[a-zA-Z0-9_/-]+$", value) is None:
        return False
    lowered = value.lower()
    return (
        ".." not in value
        and "%2e" not in lowered
        and "%2f" not in lowered
        and "%252e" not in lowered
    )


def resolve_openbao_ref(reference: str) -> str:
    addr, token, namespace, kv_version = _openbao_config()
    mount, path, field = _parse_ref(reference)
    if not _is_safe_openbao_component(mount) or not _is_safe_openbao_component(path):
        raise ValueError(
            "Invalid OpenBao reference: unsafe characters in mount or path"
        )
    if str(kv_version) == "1":
        url = f"{addr}/v1/{mount}/{path}"
    else:
        url = f"{addr}/v1/{mount}/data/{path}"
    headers = {"X-Vault-Token": token}
    if namespace:
        headers["X-Vault-Namespace"] = namespace
    try:
        response = httpx.get(url, headers=headers, timeout=5.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=500, detail="OpenBao request failed") from exc
    payload = response.json()
    data = payload.get("data", {})
    if str(kv_version) == "1":
        secret_data = data
    else:
        secret_data = data.get("data", {})
    if field not in secret_data:
        raise HTTPException(status_code=500, detail="OpenBao secret field not found")
    return str(secret_data[field])


def resolve_secret(value: str | None) -> str | None:
    if not value:
        return value
    if is_openbao_ref(value):
        return resolve_openbao_ref(value)
    return value
