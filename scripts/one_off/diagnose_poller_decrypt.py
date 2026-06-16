"""Diagnose decrypt drift in the bandwidth poller.

Reports the active CREDENTIAL_ENCRYPTION_KEY fingerprint and walks every
active MikroTik NAS device that the poller would attempt to decrypt,
printing the prefix type and decrypt outcome per row.

Run inside each container to compare:
    docker exec dotmac_sub_app                  python -m scripts.one_off.diagnose_poller_decrypt
    docker exec dotmac_sub_bandwidth_poller     python -m scripts.one_off.diagnose_poller_decrypt

If the "active key fingerprint" differs between containers, the poller
is running with a stale key. If it matches but rows still fail, the
stored ciphertext was encrypted under a key you no longer have.
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass

from sqlalchemy import select

from app.db import SessionLocal
from app.models.catalog import NasDevice, NasDeviceStatus, NasVendor
from app.services.credential_crypto import (
    _ENCRYPTION_KEY_ENV,
    decrypt_credential,
    get_encryption_key,
)


def _fingerprint(key: bytes | str | None) -> str:
    if not key:
        return "<none>"
    if isinstance(key, str):
        key = key.encode("ascii", errors="replace")
    return hashlib.sha256(key).hexdigest()[:12]


@dataclass
class RowResult:
    device_id: str
    hostname: str
    prefix: str
    status: str
    detail: str


def _classify(value: str | None) -> str:
    if not value:
        return "empty"
    if value.startswith("enc:"):
        return "enc"
    if value.startswith("plain:"):
        return "plain"
    return "legacy"


def _check_row(device: NasDevice) -> RowResult:
    pw = device.api_password
    prefix = _classify(pw)
    try:
        decrypted = decrypt_credential(pw)
    except Exception as exc:
        return RowResult(
            device_id=str(device.id),
            hostname=device.name or device.management_ip or "",
            prefix=prefix,
            status="FAIL",
            detail=str(exc),
        )
    if pw and not decrypted:
        return RowResult(
            device_id=str(device.id),
            hostname=device.name or device.management_ip or "",
            prefix=prefix,
            status="EMPTY",
            detail="decrypt returned empty",
        )
    return RowResult(
        device_id=str(device.id),
        hostname=device.name or device.management_ip or "",
        prefix=prefix,
        status="OK",
        detail=f"len={len(decrypted or '')}",
    )


def main() -> int:
    env_key = os.environ.get(_ENCRYPTION_KEY_ENV)
    active_key = get_encryption_key()

    print("=" * 72)
    print("CREDENTIAL_ENCRYPTION_KEY diagnostic")
    print("=" * 72)
    print(f"env var present       : {'yes' if env_key else 'no'}")
    print(f"env var fingerprint   : {_fingerprint(env_key)}")
    print(f"active key fingerprint: {_fingerprint(active_key)}")
    if env_key and active_key and env_key.encode("ascii", errors="replace") != (
        active_key if isinstance(active_key, bytes) else active_key.encode("ascii")
    ):
        print("WARNING: active key does NOT match env var — falling back to DB/OpenBao")
    print()

    db = SessionLocal()
    try:
        stmt = select(NasDevice).where(
            NasDevice.vendor == NasVendor.mikrotik,
            NasDevice.status == NasDeviceStatus.active,
            NasDevice.is_active.is_(True),
        )
        devices = list(db.scalars(stmt).all())
    finally:
        db.close()

    if not devices:
        print("No active MikroTik NAS devices found.")
        return 0

    results = [_check_row(d) for d in devices]

    by_status: dict[str, int] = {}
    by_prefix: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        by_prefix[r.prefix] = by_prefix.get(r.prefix, 0) + 1

    print(f"Active MikroTik NAS rows: {len(results)}")
    print(f"  by prefix : {by_prefix}")
    print(f"  by status : {by_status}")
    print()

    failures = [r for r in results if r.status != "OK"]
    if failures:
        print(f"Failures ({len(failures)}):")
        print(f"{'device_id':<38} {'host':<24} {'prefix':<8} {'status':<6} detail")
        print("-" * 110)
        for r in failures:
            print(
                f"{r.device_id:<38} {r.hostname[:24]:<24} "
                f"{r.prefix:<8} {r.status:<6} {r.detail}"
            )
    else:
        print("All rows decrypt successfully.")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
