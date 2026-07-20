"""Ephemeral signing support for prepaid reconstruction tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services import prepaid_funding_attestation
from app.services.prepaid_funding_attestation import (
    RECONSTRUCTION_MANIFEST_SCHEMA,
    candidate_cohort_sha256,
    canonical_payload_sha256,
    sign_prepaid_funding_manifest,
)

_PRIVATE_KEY = Ed25519PrivateKey.generate()
_PRIVATE_KEY_PEM = _PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("ascii")
_PUBLIC_KEY_PEM = (
    _PRIVATE_KEY.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode("ascii")
)


def ephemeral_private_signing_key_pem() -> str:
    """Return the process-local test key; never persist or print it."""
    return _PRIVATE_KEY_PEM


def ephemeral_public_signing_key_pem() -> str:
    """Return the matching process-local public test key."""
    return _PUBLIC_KEY_PEM


def trust_test_reconstruction_signer(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        prepaid_funding_attestation,
        "resolve_trusted_public_key_pem",
        lambda _db: _PUBLIC_KEY_PEM,
    )


def sign_test_reconstruction_manifest(
    manifest: dict,
    *,
    signed_at: datetime,
) -> dict:
    return sign_prepaid_funding_manifest(
        manifest,
        private_key_pem=_PRIVATE_KEY_PEM,
        signed_at=signed_at,
    )


def sealed_reconstruction_payload(
    position_at: datetime,
    balances: dict[object, str | Decimal],
    *,
    quarantined: dict[object, str | tuple[str, ...]] | None = None,
    source: str = "splynx-final-plus-native-events:reviewed-test",
    currency: str = "NGN",
) -> dict:
    normalized_balances = {
        str(account_id): Decimal(str(amount)) for account_id, amount in balances.items()
    }
    normalized_quarantine = {
        str(account_id): (
            (reasons,) if isinstance(reasons, str) else tuple(reasons)
        )
        for account_id, reasons in (quarantined or {}).items()
    }
    account_ids = sorted(
        set(normalized_balances) | set(normalized_quarantine), key=str
    )
    cohort_hash = candidate_cohort_sha256(account_ids)
    blocker_manifest = {
        "schema": "dotmac.prepaid_funding_blockers.v1",
        "source": source,
        "captured_at": position_at.isoformat().replace("+00:00", "Z"),
        "financial_handoff_at": position_at.isoformat().replace("+00:00", "Z"),
        "currency": currency,
        "candidate_accounts": len(account_ids),
        "candidate_cohort_sha256": cohort_hash,
        "blockers": [
            {"account_id": account_id, "reason": reason}
            for account_id in sorted(normalized_quarantine)
            for reason in sorted(set(normalized_quarantine[account_id]))
        ],
    }
    manifest = {
        "schema": RECONSTRUCTION_MANIFEST_SCHEMA,
        "source": source,
        "captured_at": position_at.isoformat().replace("+00:00", "Z"),
        "currency": currency,
        "candidate_accounts": len(account_ids),
        "candidate_cohort_sha256": cohort_hash,
        "blocker_manifest_sha256": canonical_payload_sha256(blocker_manifest),
        "blocker_count": len(normalized_quarantine),
        "blocker_manifest": blocker_manifest,
        "accounts": [
            {
                "account_id": account_id,
                "available_balance": f"{normalized_balances[account_id]:.2f}",
            }
            for account_id in sorted(normalized_balances)
        ],
    }
    return sign_test_reconstruction_manifest(
        manifest,
        signed_at=position_at + timedelta(seconds=1),
    )
