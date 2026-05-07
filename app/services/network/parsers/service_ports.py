"""Parsers for Huawei OLT service-port command output."""

from __future__ import annotations

import logging

from app.services.network.parsers.loader import (
    ParseResult,
    ServicePortEntry,
)
from app.services.network.parsers.loader import (
    parse_service_port_table as parse_service_port_table_textfsm,
)

logger = logging.getLogger(__name__)


def parse_service_port_table_legacy(output: str) -> list[ServicePortEntry]:
    """Legacy regex parser for ``display service-port`` output.

    Used as fallback when TextFSM parsing fails or returns no rows.
    """
    entries: list[ServicePortEntry] = []
    for line in output.splitlines():
        line = line.strip()
        # Match lines like: "27  201 common   gpon 0/2 /1  0    2     vlan  201  86   86   up"
        # Fields: INDEX VLAN_ID VLAN_ATTR PORT_TYPE F/S/P VPI(ONT) VCI(GEM) FLOW_TYPE FLOW_PARA RX TX STATE
        parts = line.split()
        if len(parts) < 10:
            continue
        try:
            index = int(parts[0])
            vlan_id = int(parts[1])
        except (ValueError, IndexError):
            continue
        try:
            gpon_idx = parts.index("gpon")
        except ValueError:
            continue

        fsp_tokens: list[str] = []
        nums_after_gpon: list[int] = []
        for token in parts[gpon_idx + 1 :]:
            cleaned = token.strip("/").replace("/", "")
            if "/" in token:
                fsp_tokens.append(token)
                continue
            if cleaned.isdigit():
                nums_after_gpon.append(int(cleaned))
            if len(nums_after_gpon) == 2:
                break
        if len(nums_after_gpon) < 2:
            continue
        ont_id, gem_index = nums_after_gpon[0], nums_after_gpon[1]

        state = parts[-1].lower() if parts[-1].lower() in ("up", "down") else "unknown"
        flow_type = ""
        flow_para = ""
        for i, part in enumerate(parts):
            if part in ("vlan", "ppp", "ip", "ip4", "ip6"):
                flow_type = part
                if i + 1 < len(parts):
                    flow_para = parts[i + 1]
                break
        entries.append(
            ServicePortEntry(
                index=index,
                vlan_id=vlan_id,
                ont_id=ont_id,
                gem_index=gem_index,
                flow_type=flow_type,
                flow_para=flow_para,
                state=state,
                fsp="".join(fsp_tokens).replace(" ", ""),
            )
        )
    return entries


def parse_service_port_table(
    output: str, vendor: str = "huawei"
) -> ParseResult[ServicePortEntry]:
    """Parse Huawei ``display service-port`` output with TextFSM and legacy fallback."""
    if not output.strip():
        return parse_service_port_table_textfsm(output, vendor=vendor)

    if "gpon" not in output.lower():
        return ParseResult(
            success=True,
            data=[],
            raw_output=output,
            template_name="display_service_port",
            row_count=0,
        )

    try:
        result = parse_service_port_table_textfsm(output, vendor=vendor)
        if result.success and result.data:
            for entry in result.data:
                entry.fsp = entry.fsp.replace(" ", "")
            return result
        if result.warnings:
            logger.debug("TextFSM service-port warnings: %s", result.warnings)
    except (ValueError, KeyError, IndexError, AttributeError) as exc:
        logger.debug("TextFSM service-port parse failed, using legacy: %s", exc)

    entries = parse_service_port_table_legacy(output)
    return ParseResult(
        success=True,
        data=entries,
        raw_output=output,
        template_name="display_service_port_legacy",
        row_count=len(entries),
    )
