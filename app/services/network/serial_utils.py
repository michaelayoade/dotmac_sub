"""Canonical serial number normalization and variant generation.

Used by GenieACS resolution, monitoring ingestion, VictoriaMetrics queries,
and TR-069 device matching. Single source of truth for all serial
number transformations across the provisioning and monitoring stack.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func


def normalize(value: str | None) -> str:
    """Normalize a serial number: strip non-alphanumeric, uppercase.

    This is the canonical normalization used for matching across systems
    (GenieACS, SNMP, OLT, inventory).

    Examples:
        "HWTC-7D4701C3" → "HWTC7D4701C3"
        "48:57:54:43:7D:47" → "485754437D47"
    """
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


def canonical(value: str | None) -> str:
    """Return canonical serial form with hex vendor prefix decoded to ASCII.

    Always returns the ASCII vendor prefix form (e.g., HWTCABEF7A70) regardless
    of whether the input is hex-encoded (48575443ABEF7A70) or already ASCII.
    Use for matching serials that may be in either format.

    Examples:
        "48575443ABEF7A70" → "HWTCABEF7A70"  (hex decoded)
        "HWTCABEF7A70" → "HWTCABEF7A70"      (unchanged)
        "HWTC-ABEF-7A70" → "HWTCABEF7A70"    (normalized)
    """
    normalized = normalize(value)
    if not normalized:
        return ""
    # If 16-char hex, try to decode vendor prefix to ASCII
    if len(normalized) == 16 and re.fullmatch(r"[0-9A-F]{16}", normalized):
        try:
            vendor_ascii = bytes.fromhex(normalized[:8]).decode("ascii")
            if vendor_ascii.isalpha():
                return vendor_ascii + normalized[8:]
        except (ValueError, UnicodeDecodeError):
            pass
    return normalized


def search_candidates(serial_number: str | None) -> list[str]:
    """Build likely serial variants for cross-system lookup.

    Returns a deduplicated list of serial representations ordered by
    likelihood. Handles Huawei hex-encoded vendor prefix conversion
    (e.g., HWTC ↔ 48575443).

    Used by GenieACS device search, VictoriaMetrics label matching,
    and SNMP ONT matching.
    """
    serial = str(serial_number or "").strip()
    if not serial:
        return []

    candidates: list[str] = []

    def add(value: str | None) -> None:
        value = str(value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    add(serial)
    normalized = normalize(serial)
    add(normalized)

    # Huawei display serials: HWTC7D4701C3 → also try 485754437D4701C3
    # (ASCII vendor prefix hex-encoded, as GenieACS may report)
    if len(normalized) == 12 and normalized[:4].isalpha():
        add(f"{normalized[:4]}-{normalized[4:]}")
        vendor_hex = normalized[:4].encode("ascii").hex().upper()
        add(vendor_hex + normalized[4:])

    # Reverse: if already hex form 485754437D4701C3 → try HWTC7D4701C3
    if len(normalized) == 16 and re.fullmatch(r"[0-9A-F]{16}", normalized):
        try:
            vendor_ascii = bytes.fromhex(normalized[:8]).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            vendor_ascii = ""
        if vendor_ascii.isalpha():
            add(vendor_ascii + normalized[8:])
            add(f"{vendor_ascii}-{normalized[8:]}")

    return candidates


def build_huawei_external_id(
    fsp: str | None, ont_id_on_olt: int | str | None
) -> str | None:
    """Return a Huawei external id scoped to the PON path.

    Huawei ONT IDs are only unique within a PON port. Persisting a bare value
    like ``"0"`` collides across ports, so inventory identifiers include F/S/P.
    """
    if ont_id_on_olt is None:
        return None
    ont_id_text = str(ont_id_on_olt).strip()
    if not ont_id_text:
        return None
    fsp_text = str(fsp or "").strip().replace("/", ".")
    if not fsp_text:
        return ont_id_text
    return f"huawei:{fsp_text}.{ont_id_text}"


def parse_ont_id_on_olt(external_id: str | None) -> int | None:
    """Extract the integer ONT-ID from supported external_id formats.

    Supports:
    - plain integer ("5")
    - prefixed integer ("generic:5")
    - dotted Huawei formats ("huawei:4194320384.5")
    - FSP-like suffixes where the ONT id is the trailing segment ("0/1/6.8")

    Returns None for unparseable values.
    """
    ext = (external_id or "").strip()
    if ext.isdigit():
        return int(ext)
    match = re.match(r"^(?:[a-z0-9_-]+:)?(?:\d+[/.])*(\d+)$", ext, re.IGNORECASE)
    if match:
        return int(match.group(1))
    if "." in ext:
        dot_part = ext.rsplit(".", 1)[-1]
        if dot_part.isdigit():
            return int(dot_part)
    return None


def normalized_serial_sql(column: Any) -> Any:
    """Build a SQL expression that normalizes a serial column for comparison.

    Strips common formatting characters and uppercases. Use in WHERE clauses:

        .where(normalized_serial_sql(Model.serial_number) == normalize("HWTC-123"))
    """
    expr = func.upper(column)
    for token in ("-", " ", ":", ".", "_", "/"):
        expr = func.replace(expr, token, "")
    return expr
