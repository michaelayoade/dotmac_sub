"""The CRM ticket poller is retired and must not come back.

CRM (dotmac_crm) was decommissioned 2026-08-29. The inbound ticket poller was
deleted after the production observation gate in
``docs/runbooks/CRM_TICKET_CAPABILITY_CUTOVER.md`` passed on 2026-09-27. This
guard pins the five ways it could quietly return: a beat schedule key, a task
name, the canonical control, a setting spec, or an environment read -- plus
the ticket webhook receiver and the cutover tooling that were retired with it.

The scan is static (source text under ``app/`` and ``scripts/``); it never
builds the beat schedule, so it needs no database. ``test_the_guard_bites`` plants each form
in a synthetic tree to prove the detector still matches.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Each pattern names one retired handle. They are written as regexes so the
# planted proof below exercises exactly what the real scan uses.
RETIRED_HANDLES: dict[str, re.Pattern[str]] = {
    "beat schedule key": re.compile(r"""["']crm_ticket_pull(?:_full)?["']"""),
    "task name": re.compile(r"app\.tasks\.crm_ticket_pull\b"),
    "canonical control": re.compile(r"""["']crm\.ticket_pull["']"""),
    "setting spec": re.compile(
        r"""["']crm_ticket_pull_(?:enabled|interval_minutes)["']"""
    ),
    "environment read": re.compile(r"CRM_TICKET_PULL_[A-Z_]+"),
    "ticket webhook receiver": re.compile(r"\b(?:receive_crm_event|TICKET_EVENTS)\b"),
    "cutover tooling": re.compile(
        r"\b(?:crm_ticket_readiness|verify_crm_ticket_readiness"
        r"|reconcile_crm_ticket_capability|resolve_crm_ticket_pull_readiness)\b"
    ),
}

SCANNED_ROOTS = ("app", "scripts")


def _violations(app_dir: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for handle, pattern in RETIRED_HANDLES.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{path.relative_to(app_dir)}:{line} {handle}")
    return found


def test_the_retired_crm_ticket_poller_has_no_handle_left() -> None:
    violations = [
        f"{root}/{entry}"
        for root in SCANNED_ROOTS
        for entry in _violations(ROOT / root)
    ]
    assert not violations, (
        "The CRM ticket poller is retired (CRM_TICKET_CAPABILITY_CUTOVER.md); "
        "these reintroduce one of its handles:\n  " + "\n  ".join(violations)
    )


def test_retired_crm_writer_modules_stay_deleted() -> None:
    for relative in (
        "app/services/crm_ticket_pull.py",
        "app/tasks/crm_ticket_pull.py",
        "app/services/integrations/crm_ticket_readiness.py",
        "scripts/integrations/verify_crm_ticket_readiness.py",
        "scripts/integrations/reconcile_crm_ticket_capability.py",
        "scripts/one_off/import_crm_tickets.py",
        "scripts/one_off/backfill_crm_subscriber_ids.py",
    ):
        assert not (ROOT / relative).exists(), f"{relative} was retired"


def test_current_crm_manifest_and_transport_do_not_expose_ticket_reads() -> None:
    from app.services.crm_client import CRMClient
    from app.services.integrations import crm_capability
    from app.services.integrations.connectors import dotmac_crm
    from app.services.integrations.registry import connector_definition

    current = connector_definition("dotmac.crm")
    assert current is not None
    assert current.capability(dotmac_crm.CRM_TICKET_OBSERVATION_CAPABILITY) is None
    assert dotmac_crm.CRM_TICKET_OBSERVATION_CAPABILITY not in (
        dotmac_crm._ACTIONS_BY_CAPABILITY
    )
    assert dotmac_crm.RETIRED_CRM_CAPABILITIES == frozenset(
        {dotmac_crm.CRM_TICKET_OBSERVATION_CAPABILITY}
    )
    for transport in (CRMClient, crm_capability.CrmCapabilityClient):
        for action in ("list_tickets", "get_ticket", "list_ticket_comments"):
            assert not hasattr(transport, action)


def test_the_guard_bites(tmp_path: Path) -> None:
    """Sensitivity proof: each retired handle is detected when planted."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "\n".join(
            (
                'schedule["crm_ticket_pull"] = {}',
                'schedule["crm_ticket_pull_full"] = {}',
                'task = "app.tasks.crm_ticket_pull.pull_crm_tickets"',
                'is_enabled(db, "crm.ticket_pull")',
                'key = "crm_ticket_pull_enabled"',
                'os.environ["CRM_TICKET_PULL_ENABLED"]',
                "from app.api.crm_webhooks import receive_crm_event",
                "if event in TICKET_EVENTS: pass",
                "from app.services.integrations import crm_ticket_readiness",
            )
        ),
        encoding="utf-8",
    )
    near_miss = tmp_path / "near_miss.py"
    near_miss.write_text(
        'capability = "crm.ticket_observation.v1"\nname = "crm_ticket_id"\n',
        encoding="utf-8",
    )
    found = _violations(tmp_path)
    handles = {entry.split(" ", 1)[1] for entry in found}
    assert handles == set(RETIRED_HANDLES)
    assert all(entry.startswith("planted.py:") for entry in found)
