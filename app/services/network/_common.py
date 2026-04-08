"""Shared helper functions for network services.

These are re-exported from app.services.common for backwards compatibility.
"""

from __future__ import annotations

import logging
import re

from app.services.common import (
    apply_ordering as _apply_ordering,
)
from app.services.common import (
    apply_pagination as _apply_pagination,
)
from app.services.common import (
    validate_enum as _validate_enum,
)

logger = logging.getLogger(__name__)

__all__ = [
    "_apply_ordering",
    "_apply_pagination",
    "_validate_enum",
    "decode_huawei_hex_serial",
    "encode_to_hex_serial",
    "normalize_mac_address",
]


def decode_huawei_hex_serial(value: str | None) -> str | None:
    """Decode a 16-char hex serial into a human-readable vendor+serial form.

    Huawei (and similar) OLTs sometimes report ONT serials as 16 hex digits
    where the first 8 hex chars are the ASCII vendor prefix.  For example,
    ``485754437D4701C3`` decodes to ``HWTC7D4701C3``.

    Returns the decoded serial or ``None`` if the value is not a valid
    hex-encoded vendor serial.
    """
    raw = str(value or "").strip().upper()
    if not re.fullmatch(r"[0-9A-F]{16}", raw):
        return None
    try:
        vendor_ascii = bytes.fromhex(raw[:8]).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None
    if vendor_ascii.isalpha():
        return f"{vendor_ascii}{raw[8:]}"
    return None


def encode_to_hex_serial(value: str | None) -> str | None:
    """Encode a vendor+serial form to 16-char hex serial.

    Converts human-readable ONT serials like ``HWTCA31A3529`` or ``HWTC-A31A3529``
    into the full 16-character hex form ``48575443A31A3529``.

    Returns the hex serial or ``None`` if the value cannot be encoded.
    """
    raw = str(value or "").strip().upper()
    if not raw:
        return None

    # Already a valid 16-char hex serial
    if re.fullmatch(r"[0-9A-F]{16}", raw):
        return raw

    # Remove common separators (dash, colon, space)
    raw = re.sub(r"[-:\s]", "", raw)

    # Check for vendor prefix pattern (4 ASCII letters + 8 hex digits)
    match = re.fullmatch(r"([A-Z]{4})([0-9A-F]{8})", raw)
    if not match:
        return None

    vendor_prefix = match.group(1)
    serial_suffix = match.group(2)

    try:
        vendor_hex = vendor_prefix.encode("ascii").hex().upper()
    except (ValueError, UnicodeEncodeError):
        return None

    return f"{vendor_hex}{serial_suffix}"


def normalize_mac_address(value: str | None) -> str | None:
    """Return a canonical uppercase colon-separated MAC address."""
    raw = str(value or "").strip()
    if not raw:
        return None
    compact = re.sub(r"[^0-9A-Fa-f]", "", raw)
    if len(compact) != 12 or not re.fullmatch(r"[0-9A-Fa-f]{12}", compact):
        return None
    compact = compact.upper()
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))
