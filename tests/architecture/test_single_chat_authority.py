"""Live chat has ONE authority, and no way to grow a second one.

The runbook for the temporary CRM live-chat arrangement (ADR 0006, retired
2026-08-30) closed with a sentence:

    Do not enable two chat writers or add a fallback that writes locally when
    CRM is unavailable.

A sentence in a retired runbook stops anything for about a week. This module is
that sentence as a build failure.

## Why a second chat writer is the specific thing being prevented

`tests/architecture/test_team_inbox_boundaries.py` already forbids a second
module WRITING inbox rows. It would not have caught the CRM arrangement at all,
because that arrangement wrote nothing: the broker returned somebody else's
REST and WebSocket URLs and Sub persisted no conversation. The failure mode is
therefore not "two writers to one table" but "two possible DESTINATIONS behind
one broker call, chosen at runtime" -- and the moment those two destinations
both hold conversations, operator visibility is split and reconciling them is
unbounded work. That is exactly what happened, and the CRM half of the
resulting gap is now unrecoverable.

So the checks below are about the SEAM, not the table:

1. the broker seam resolves to exactly one destination and cannot branch;
2. no setting exists that could select a chat authority;
3. the deleted modules cannot come back;
4. no CURRENT connector manifest declares an external chat-transport
   capability (the historical `dotmac.crm` 1.1.0 pin still does, immutably, and
   `test_the_historical_chat_capability_pin_is_still_there` asserts that on
   purpose -- it is what proves check 4 is testing "current" and not "absent
   everywhere");
5. and `test_the_single_destination_guard_still_bites` proves check 1 actually
   detects a second destination, so it cannot pass by finding nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.integrations.registry import (
    connector_definitions,
    supported_connector_definitions,
)
from app.services.settings_spec import SETTINGS_SPECS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BROKER_SEAM = PROJECT_ROOT / "app/services/chat_session.py"

#: The single module the portal chat broker is allowed to delegate to.
SOLE_CHAT_OWNER = "team_inbox_widget"

#: Module paths deleted with ADR 0006's retirement. A file reappearing at any
#: of these paths is the old arrangement returning under its own name.
RETIRED_CHAT_PATHS = (
    "app/services/chat_session_authority.py",
    "app/services/crm_chat_session.py",
    "scripts/one_off/export_native_chat_for_crm.py",
)

#: Modules that only ever existed to serve an external chat authority.
RETIRED_CHAT_MODULES = (
    "app.services.chat_session_authority",
    "app.services.crm_chat_session",
)

#: Names that only ever existed to serve one. Matched as CODE (identifiers and
#: attribute access), never as text, so the retirement records in ADR 0006, the
#: SOT registry notes and the connector registry's comments can still say what
#: was removed without tripping the guard that removed it.
RETIRED_CHAT_NAMES = frozenset(
    {
        "ChatSessionAuthority",
        "ChatSessionAuthorityDecision",
        "resolve_chat_session_authority",
        "CRM_CHAT_SESSION_CAPABILITY",
        "create_widget_session",
    }
)

#: The immutable published pin that still declares the retired capability.
HISTORICAL_CHAT_PIN = ("dotmac.crm", "1.1.0")


def _searched_sources() -> list[Path]:
    return sorted(
        path
        for root in ("app", "scripts")
        for path in (PROJECT_ROOT / root).rglob("*.py")
    )


def _delegate_targets(source: str) -> set[str]:
    """Every module a `broker_*` call in `source` is dispatched through.

    A delegation looks like ``<module>.broker_<something>(...)``. The module
    half is what matters: two distinct module halves is two destinations, which
    is the shape being forbidden.
    """

    tree = ast.parse(source)
    targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or not func.attr.startswith("broker_"):
            continue
        value = func.value
        if isinstance(value, ast.Name):
            targets.add(value.id)
        elif isinstance(value, ast.Attribute):
            targets.add(value.attr)
        else:  # pragma: no cover - a computed destination is itself a failure
            targets.add(ast.dump(value))
    return targets


def _branching_broker_functions(source: str) -> list[str]:
    """Public `broker_*` functions in `source` that make a runtime choice.

    The selector was one `if`. A conditional inside the seam is the mechanism
    by which a second destination gets chosen, so the seam is required to be
    branch-free -- not merely single-destination today.
    """

    tree = ast.parse(source)
    offenders: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("broker_"):
            continue
        if any(
            isinstance(inner, ast.If | ast.IfExp | ast.Match | ast.Try)
            for inner in ast.walk(node)
        ):
            offenders.append(node.name)
    return offenders


def test_the_broker_seam_has_exactly_one_destination() -> None:
    source = BROKER_SEAM.read_text(encoding="utf-8")
    targets = _delegate_targets(source)

    assert targets == {SOLE_CHAT_OWNER}, (
        "The portal chat broker delegates to more than one destination: "
        f"{sorted(targets)}. Live chat has one authority "
        f"({SOLE_CHAT_OWNER}); a second destination behind the same call is "
        "the CRM arrangement returning. See ADR 0006's retirement record."
    )


def test_the_broker_seam_cannot_branch_on_an_authority() -> None:
    branching = _branching_broker_functions(BROKER_SEAM.read_text(encoding="utf-8"))

    assert not branching, (
        f"Broker function(s) {branching} contain a runtime branch. The seam "
        "must delegate unconditionally: a conditional here is where a second "
        "chat authority gets selected, and where a 'write locally when the "
        "remote is unavailable' fallback gets added."
    )


def test_the_single_destination_guard_still_bites() -> None:
    """A sensitivity proof: the detectors above must fail on a real violation.

    Both checks pass trivially against a file with no `broker_*` calls, so a
    refactor that renamed the seam's functions would silently unmonitor it.
    These two synthetic sources reproduce the exact shape that was removed.
    """

    reintroduced_selector = (
        "def broker_customer_session(db, subscriber_id):\n"
        "    if resolve_chat_session_authority(db).authority == 'crm':\n"
        "        return crm_chat_session.broker_customer_session(db, subscriber_id)\n"
        "    return team_inbox_widget.broker_customer_session_committed(\n"
        "        db, subscriber_id\n"
        "    )\n"
    )

    assert _delegate_targets(reintroduced_selector) == {
        "crm_chat_session",
        "team_inbox_widget",
    }, "the destination detector no longer sees a second destination"
    assert _branching_broker_functions(reintroduced_selector) == [
        "broker_customer_session"
    ], "the branch detector no longer sees a runtime choice"

    local_fallback = (
        "def broker_customer_session(db, subscriber_id):\n"
        "    try:\n"
        "        return remote.broker_customer_session(db, subscriber_id)\n"
        "    except Exception:\n"
        "        return team_inbox_widget.broker_customer_session_committed(\n"
        "            db, subscriber_id\n"
        "        )\n"
    )

    assert _branching_broker_functions(local_fallback) == ["broker_customer_session"], (
        "the branch detector no longer sees a write-locally-on-failure fallback"
    )


def test_no_setting_can_select_a_chat_authority() -> None:
    """No registered setting may name a chat authority.

    Deliberately broader than the one retired key. Narrowing `allowed` to a
    single member, or renaming the key while keeping the concept, would both
    leave an operator-editable control over which system owns live chat --
    which is the thing being forbidden, not the specific string.
    """

    offenders = sorted(
        f"{spec.domain}/{spec.key}"
        for spec in SETTINGS_SPECS
        if "chat" in spec.key and ("authority" in spec.key or "provider" in spec.key)
    )

    assert not offenders, (
        f"Setting(s) {offenders} select a live-chat authority. Live chat has "
        "exactly one owner and nothing chooses it at runtime; retire the spec "
        "instead of narrowing its allowed values."
    )


def test_retired_chat_authority_paths_cannot_return() -> None:
    present = [path for path in RETIRED_CHAT_PATHS if (PROJECT_ROOT / path).exists()]

    assert not present, (
        f"Retired external-chat-authority module(s) returned: {present}. These "
        "were deleted with ADR 0006; a file back at one of these paths is the "
        "temporary arrangement being re-entered."
    )


def _retired_chat_references(source: str) -> set[str]:
    """Retired chat-authority modules and names USED as code in `source`.

    AST, not substring: the point is to forbid the mechanism, not the memory of
    it. `app/services/integrations/registry.py` has to keep building the
    immutable 1.1.0 manifest -- capability id string and all -- and several
    documents and comments have to keep naming what was retired. A text scan
    would force those to be silent about it, which is the opposite of what a
    retirement record is for.
    """

    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(
                alias.name for alias in node.names if alias.name in RETIRED_CHAT_MODULES
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in RETIRED_CHAT_MODULES:
                found.add(module)
            found.update(
                f"{module}.{alias.name}"
                for alias in node.names
                if f"{module}.{alias.name}" in RETIRED_CHAT_MODULES
            )
        elif isinstance(node, ast.Name) and node.id in RETIRED_CHAT_NAMES:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in RETIRED_CHAT_NAMES:
            found.add(node.attr)
    return found


def test_no_application_source_references_the_retired_chat_authority() -> None:
    offenders: list[str] = []
    for path in _searched_sources():
        for reference in _retired_chat_references(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {reference}")

    assert not offenders, (
        "External-chat-authority code reappeared under app/ or scripts/:\n"
        + "\n".join(sorted(offenders))
    )


def test_the_retired_reference_guard_still_bites() -> None:
    """Sensitivity proof for the AST scan above.

    A name-based detector that quietly stops matching is worse than none: it
    reports a clean sweep over a codebase it is no longer looking at.
    """

    revived = (
        "from app.services.chat_session_authority import (\n"
        "    ChatSessionAuthority,\n"
        "    resolve_chat_session_authority,\n"
        ")\n"
        "from app.services import crm_chat_session\n"
        "\n"
        "def broker(db):\n"
        "    if resolve_chat_session_authority(db).authority is ChatSessionAuthority:\n"
        "        return crm_chat_session.mint(db).create_widget_session()\n"
        "    return None\n"
    )

    assert _retired_chat_references(revived) >= {
        "app.services.chat_session_authority",
        "ChatSessionAuthority",
        "resolve_chat_session_authority",
        "create_widget_session",
    }, "the retired-reference detector no longer sees a revived chat authority"

    prose_only = (
        '"""ADR 0006 retired comms.chat_session_authority and '
        'crm.chat_session.v1 on 2026-08-30."""\n'
        'CAPABILITY_ID = "crm.chat_session.v1"\n'
    )

    assert not _retired_chat_references(prose_only), (
        "the detector matches prose and manifest strings, which would force "
        "the retirement records to stop naming what they retired"
    )


def test_no_current_connector_declares_an_external_chat_transport() -> None:
    offenders = sorted(
        f"{definition.key} {definition.version}: {capability.id}"
        for definition in connector_definitions()
        for capability in definition.capabilities
        if "chat" in capability.id
    )

    assert not offenders, (
        f"Current connector manifest(s) declare a chat transport: {offenders}. "
        "Sub's native Team Inbox is the sole live-chat authority; an external "
        "chat capability is a second one whether or not anything calls it yet."
    )


def test_the_historical_chat_capability_pin_is_still_there() -> None:
    """The retired capability must survive in exactly one place: the old pin.

    This is the other half of the check above, and it is why that check reads
    "current" rather than "anywhere". A published manifest digest is immutable
    -- an installation adopts BY digest, so editing `dotmac.crm` 1.1.0 in place
    would make one version name two contracts, and deleting it would make a
    live production pin unidentifiable and take every other CRM capability down
    with it. Retention is not reachability: the runner maps no action to the
    capability, so a 1.1.0-pinned binding fails closed.

    If this assertion ever fails because someone removed 1.1.0, that is the
    error -- not the presence of the capability inside it.
    """

    key, version = HISTORICAL_CHAT_PIN
    historical = [
        definition
        for definition in supported_connector_definitions()
        if (definition.key, definition.version) == (key, version)
    ]

    assert historical, (
        f"The immutable {key} {version} pin is gone. It is the only manifest "
        "that ever declared crm.chat_session.v1 and it must be retained "
        "unedited so existing installations stay identifiable by digest."
    )
    assert any(
        capability.id == "crm.chat_session.v1"
        for definition in historical
        for capability in definition.capabilities
    ), (
        f"The {key} {version} pin was EDITED to drop crm.chat_session.v1. A "
        "published digest is immutable; the removal belongs in a new version "
        "(1.2.0 already has it), not in a rewrite of a shipped one."
    )
    assert all(
        definition not in connector_definitions() for definition in historical
    ), f"{key} {version} must be historical, not current."
