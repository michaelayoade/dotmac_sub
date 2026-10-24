"""Scan web/api modules for direct ONT/OLT/ACS writers (single-writer ratchet).

The ONT service-configuration lifecycle (configure_ont_service -> reconcile
planner -> applier) is the only writer of ONT/OLT/ACS state. HTTP adapters under
``app/web`` and ``app/api`` must not call device writers themselves. This scan
counts the forbidden call tokens per file; the committed baseline may only
shrink.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("app/web", "app/api")

# token name -> regex over source text
FORBIDDEN: dict[str, re.Pattern[str]] = {
    "reconcile_ont": re.compile(r"\breconcile_ont\("),
    "create_genieacs_client": re.compile(r"\bcreate_genieacs_client\b"),
    "ont_write": re.compile(r"\bont_write\.|\bOntWriteService\b"),
    "service_port_service_write": re.compile(
        r"web_network_service_ports(?:_service)?\.handle_(?:create|delete|clone)\b"
    ),
    "olt_adapter_write": re.compile(
        r"\bconfigure_iphost\b|\bcreate_service_port\b|\bdelete_service_port\b"
        r"|\bget_protocol_adapter\b"
    ),
    "ont_provision_steps_write": re.compile(
        r"\bont_provision_steps\b|\brequest_ont_provisioning\b"
    ),
    "ont_action_setter": re.compile(
        r"web_network_ont_actions_service\.(?:set_|force_|bind_|configure_(?!form_context)|toggle_)\w+"
    ),
    "genieacs_service_write": re.compile(
        r"\bgenieacs_service\.(?:set_|toggle_|configure_|bind_|push_)\w+"
    ),
}


def scan() -> Counter[tuple[str, str]]:
    hits: Counter[tuple[str, str]] = Counter()
    for directory in SCAN_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(ROOT).as_posix()
            for name, pattern in FORBIDDEN.items():
                count = len(pattern.findall(text))
                if count:
                    hits[(rel, name)] = count
    return hits


def render(hits: Counter[tuple[str, str]]) -> str:
    return "".join(
        f"{path} {name} {count}\n" for (path, name), count in sorted(hits.items())
    )


if __name__ == "__main__":
    print(render(scan()), end="")
