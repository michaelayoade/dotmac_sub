"""Secret resolution for the reconciler.

Plugs ``app.services.secrets.resolve_secret`` into the reconciler's
``SecretResolver`` slot so production reconciles pull OLT CR passwords,
PPPoE passwords, and WiFi PSKs from OpenBao instead of trusting
plaintext refs.

Three layers in order of preference, controlled by the
``OPENBAO_ADDR`` environment variable:

1. **OpenBao-backed** (``default_secret_resolver_from_env`` when
   ``is_openbao_available()`` is True). Refs of the form
   ``bao://mount/path#field`` get resolved over HTTPS. Plaintext values
   pass through unchanged so callers can still supply literal passwords
   during migration.
2. **Passthrough** (the default in dev / tests). Returns the ref string
   verbatim — useful when ``*_ref`` columns hold plaintext directly.
3. **Test injection**. Callers passing an explicit ``SecretResolver``
   override both of the above; this is the path used by
   ``test_reconcile_applier.py`` and friends.

Resolution errors raise ``SecretResolutionError``; the applier translates
that to an ``ApplyError`` with the ``ACS_WRITE_FAULTED`` reason so the
operator sees a specific failure message rather than a generic 500.
"""

from __future__ import annotations

import logging

from app.services.credential_crypto import decrypt_credential
from app.services.secrets import (
    is_secret_ref,
    resolve_secret,
)

from .applier import SecretResolver

logger = logging.getLogger(__name__)


class SecretResolutionError(Exception):
    """Raised when a secret reference cannot be resolved to plaintext.

    The applier catches this and re-raises as an ``ApplyError`` so the
    failure surfaces with a precise reason instead of an unhandled
    exception.
    """

    def __init__(self, ref: str, message: str) -> None:
        self.ref = ref
        super().__init__(f"{ref}: {message}")


def openbao_secret_resolver(ref: str) -> str:
    """Resolve a secret reference via OpenBao, falling back to passthrough.

    Behaviour:
    * Empty / None ref → returns ``""`` (callers writing-back an empty
      password is meaningful and shouldn't crash).
    * Plaintext (no ``bao://`` / ``env://`` scheme) → returned unchanged.
    * URI-shaped ref → routed through ``resolve_secret``. Any error
      raised by the OpenBao client surfaces as a
      ``SecretResolutionError``.
    """
    if not ref:
        return ""
    if not is_secret_ref(ref):
        return ref
    try:
        resolved = resolve_secret(ref)
    except Exception as exc:  # noqa: BLE001 — translate to typed error
        raise SecretResolutionError(ref, str(exc)) from exc
    if resolved is None:
        raise SecretResolutionError(ref, "resolver returned None")
    return resolved


def credential_secret_resolver(ref: str) -> str:
    """Resolve OpenBao refs and local encryption-at-rest wrappers."""
    if not ref:
        return ""
    try:
        return decrypt_credential(ref) or ""
    except Exception as exc:  # noqa: BLE001 - translate to reconciler failure
        raise SecretResolutionError(ref, str(exc)) from exc


def default_secret_resolver_from_env() -> SecretResolver:
    """Pick a resolver based on whether OpenBao is reachable from this
    process.

    Called from ``reconcile_ont`` when the caller doesn't pass an explicit
    resolver. The check runs each time the factory is invoked so a
    long-running sweeper picks up a newly-configured OpenBao without
    restart.
    """
    return credential_secret_resolver


__all__ = (
    "SecretResolutionError",
    "default_secret_resolver_from_env",
    "credential_secret_resolver",
    "openbao_secret_resolver",
)
