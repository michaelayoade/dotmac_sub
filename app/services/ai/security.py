"""Credential resolution and secret redaction for the AI transport.

Two DELIBERATE divergences from dotmac_crm, both forced by Sub's own contracts:

1. **Credentials resolve through OpenBao, not the environment.** CRM's
   ``resolve_provider_api_key`` read ``os.getenv(env_var)`` first and fell back
   to the stored setting. Sub's ``SettingSpec`` docstring is explicit that
   ``env_var`` is "an optional bootstrap or migration input consumed by the
   settings seed/sync paths. Runtime resolvers never consult it as an
   override." Porting CRM's env-first behaviour would break that contract, so
   the key comes from the setting, which may hold an OpenBao reference
   (``bao://mount/path#field``) resolved via ``secrets.resolve_secret``.
   ``AI_SOT.md``: "Provider credentials resolve through ``secrets`` (OpenBao),
   never settings rows."

2. **``AI_ENABLED`` survives as an env KILL SWITCH only.** It can force AI off
   but can never turn it on — enabling is the stored ``integration.ai_enabled``
   setting's decision. A one-way switch is not a runtime override of stored
   policy; it is an operator's emergency stop, which ``AI_SOT.md`` calls for
   ("Declared as a transport, with a kill switch").

Nothing here ever logs, returns, or raises a secret value.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.secrets import is_secret_ref, resolve_secret
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(DEEPSEEK_API_KEY|OPENAI_API_KEY|VLLM_API_KEY"
    r"|VOICE_TRANSCRIPTION_API_KEY|authorization)\s*[:=]\s*([^\s,;]+)"
)
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


def ai_enabled(db: Session) -> bool:
    """AI enablement is a stored decision, resolved through the named
    resolver. It deliberately has NO env override: this repo forbids direct
    env decision inputs (tests/architecture/test_decision_input_ownership),
    and the emergency stop already exists as the default-OFF ``ai.generation``
    control, which an operator can flip without a deploy."""
    value = resolve_value(db, SettingDomain.integration, "ai_enabled")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_VALUES
    return False


def redact_secret_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return _SK_TOKEN_RE.sub("sk-<redacted>", text)


def is_deepseek_endpoint(base_url: str | None) -> bool:
    if not base_url:
        return False
    parsed = urlparse(base_url.strip())
    host = (parsed.netloc or parsed.path).lower()
    return "deepseek" in host


def resolve_provider_api_key(*, configured_api_key: object | None) -> str | None:
    """Resolve the stored provider key, following an OpenBao/env reference.

    ``configured_api_key`` is the stored ``integration`` setting. It may be a
    plaintext key or a secret reference (``bao://…``, ``env://…``);
    ``resolve_secret`` handles both and passes plaintext through.

    Fails CLOSED and quietly: an unresolvable reference yields ``None``, which
    the gateway treats as "no key" — an endpoint with ``require_api_key`` then
    reports itself not ready rather than calling a provider unauthenticated.
    The failure is logged WITHOUT the reference's value.
    """
    raw = configured_api_key
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        resolved = resolve_secret(text)
    except Exception as exc:
        # Never let a secret-store failure escape into a caller's stack, and
        # never log the reference's resolved value.
        logger.warning(
            "ai_provider_key_unresolved is_reference=%s error=%s",
            is_secret_ref(text),
            redact_secret_text(type(exc).__name__),
        )
        return None
    if resolved is None:
        return None
    return str(resolved).strip() or None
