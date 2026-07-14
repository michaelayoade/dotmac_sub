"""Single source of white-label brand configuration.

Brand fields (name, colours, support email, app URL, mobile payment scheme) are
deployment-static and read from a flat JSON file at the repository root
(``brand.json``) so that the same file can be consumed by the Flutter mobile app
via ``flutter build --dart-define-from-file=../brand.json``.

Resolution order (lowest to highest precedence):
    built-in defaults  <  brand.json  <  environment variable (same JSON key)

The file path can be overridden with the ``BRAND_CONFIG_PATH`` environment
variable. Values are cached for the process lifetime; brand config is static per
deployment, so a restart is required to pick up changes.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from app.services.brand_theme import (
    DEFAULT_HEX,
    DEFAULT_SECONDARY_HEX,
    DEFAULT_SEMANTIC_COLORS,
)

logger = logging.getLogger(__name__)

# Friendly key -> JSON/env key. The friendly keys are what callers and templates
# use (e.g. ``brand.primary_color``); the JSON keys are the flat upper-case names
# in brand.json that Flutter's --dart-define-from-file also reads.
_KEY_MAP: dict[str, str] = {
    "name": "BRAND_NAME",
    "product_name": "BRAND_PRODUCT_NAME",
    "legal_name": "BRAND_LEGAL_NAME",
    "tagline": "BRAND_TAGLINE",
    "primary_color": "BRAND_PRIMARY_COLOR",
    "secondary_color": "BRAND_SECONDARY_COLOR",
    "semantic_positive_color": "BRAND_SEMANTIC_POSITIVE_COLOR",
    "semantic_info_color": "BRAND_SEMANTIC_INFO_COLOR",
    "semantic_warning_color": "BRAND_SEMANTIC_WARNING_COLOR",
    "semantic_negative_color": "BRAND_SEMANTIC_NEGATIVE_COLOR",
    "semantic_neutral_color": "BRAND_SEMANTIC_NEUTRAL_COLOR",
    "support_email": "BRAND_SUPPORT_EMAIL",
    "from_email": "BRAND_FROM_EMAIL",
    "from_name": "BRAND_FROM_NAME",
    "app_url": "BRAND_APP_URL",
    "payment_scheme": "BRAND_PAYMENT_SCHEME",
}

# Built-in defaults so an unconfigured deployment still renders sanely. These are
# intentionally the current DotMac values; replace brand.json to white-label.
_DEFAULTS: dict[str, str] = {
    "name": "DotMac",
    "product_name": "DotMac Subs",
    "legal_name": "Dotmac Technologies",
    "tagline": "Sign in to manage your service",
    "primary_color": DEFAULT_HEX,
    "secondary_color": DEFAULT_SECONDARY_HEX,
    **{
        f"semantic_{tone}_color": color
        for tone, color in DEFAULT_SEMANTIC_COLORS.items()
    },
    "support_email": "support@dotmac.ng",
    "from_email": "noreply@dotmac.ng",
    "from_name": "DotMac Selfcare",
    "app_url": "https://selfcare.dotmac.io",
    "payment_scheme": "dotmacpay",
}


def _config_path() -> Path:
    override = os.getenv("BRAND_CONFIG_PATH")
    if override:
        return Path(override)
    # branding_config.py -> services -> app -> <repo root>
    return Path(__file__).resolve().parents[2] / "brand.json"


def _load_file() -> dict[str, object]:
    path = _config_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.info("brand.json not found at %s; using built-in brand defaults", path)
    except (OSError, ValueError) as exc:
        logger.warning("Failed to read brand.json at %s: %s; using defaults", path, exc)
    return {}


@lru_cache(maxsize=1)
def get_brand() -> dict[str, str]:
    """Return the resolved brand config as a friendly-keyed dict (cached)."""
    raw = _load_file()
    brand = dict(_DEFAULTS)
    for friendly, json_key in _KEY_MAP.items():
        # env var wins, then brand.json, then the existing default
        value = os.getenv(json_key)
        if not (isinstance(value, str) and value.strip()):
            file_value = raw.get(json_key)
            value = file_value if isinstance(file_value, str) else None
        if isinstance(value, str) and value.strip():
            brand[friendly] = value.strip()
    return brand


def reset_brand_cache() -> None:
    """Clear the cached brand config (primarily for tests)."""
    get_brand.cache_clear()
