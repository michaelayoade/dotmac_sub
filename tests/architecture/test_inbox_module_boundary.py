"""The composed inbox modules have one door, one backfill, and a shrinking list.

Three separable claims, each with its own failure mode:

1. **One door.** `dotmac_inbox` and `dotmac_inbox_operations` are imported from
   `app/services/inbox_module/` and its three named companions, and nowhere
   else. A second importer is a second writer — the module's service functions
   own no transaction and check no authorization, so anything holding them
   directly is making decisions the adapter cannot see.
2. **One backfill.** Exactly one module under `app/` constructs the modules'
   mapped classes. That is the declared exception in ADR-0013's invariants, and
   an exception nobody counts is a convention.
3. **A shrinking list.** Nine modules still write columns `dotmac-inbox` owns
   after the P5 switch. The two-directional ratchet stops a tenth appearing and
   stops a fixed one being quietly forgotten.

Every check has a sensitivity case beside it. A guard that has never been
watched fail is a guard that might be checking nothing — and two of these were
rewritten because the first version passed against code it should have caught.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from packaging.version import Version

from app.models.team_inbox import InboxAgentPresence, InboxChannelType
from app.services.inbox_channels import SUB_CHANNELS
from app.services.inbox_module.references import (
    PRESENCE_AWAY_REASON_BY_SUB_STATUS,
    PRESENCE_STATE_BY_SUB_STATUS,
    presence_away_reason,
    presence_state,
    sub_presence_status,
)
from app.services.inbox_projection_reconciler import (
    CONVERSATION_PROJECTION,
    MESSAGE_PROJECTION,
)
from scripts.architecture.inbox_projection_census import (
    CONVERSATION_PROJECTED_COLUMNS,
    MESSAGE_PROJECTED_COLUMNS,
    _ProjectionWriteCounter,
    projection_writer_files,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE = PROJECT_ROOT / "tests/architecture/inbox_projection_writers_baseline.txt"

REGENERATE = (
    "Regenerate with `python -m scripts.architecture.inbox_projection_census "
    "--baseline` in the same change."
)

#: The adapter package, plus the three modules ADR-0013 names as reaching the
#: distributions for a stated reason: the channel declaration (the module's
#: registry is empty until Sub fills it), the backfill (the declared
#: direct-write exception), and the reconciler (it reads module rows to project
#: them).
_PERMITTED_IMPORTERS = frozenset(
    {
        "app/services/inbox_module/__init__.py",
        "app/services/inbox_module/references.py",
        "app/services/inbox_module/conversations.py",
        "app/services/inbox_module/operations.py",
        "app/services/inbox_channels.py",
        "app/services/inbox_backfill.py",
        "app/services/inbox_projection_reconciler.py",
    }
)

#: Only the backfill may construct a module's mapped class.
_PERMITTED_MODULE_ROW_WRITERS = frozenset({"app/services/inbox_backfill.py"})

_MODULE_DISTRIBUTIONS = ("dotmac_inbox", "dotmac_inbox_operations")

#: Names in `dotmac_inbox.models` / `dotmac_inbox_operations.models`. Listed
#: rather than imported so that a class disappearing upstream fails as a missing
#: name in the source scan rather than an ImportError in this test.
_MODULE_ROW_CLASSES = frozenset(
    {
        "Conversation",
        "ConversationReadState",
        "Message",
        "ConversationAssignment",
        "InboxQueue",
        "InboxQueueEntry",
        "InboxAgentPresence",
        "InboxRoundRobinCursor",
        "InboxRoutingDecision",
        "InboxRoutingRule",
        "InboxWorkflowEvent",
    }
)


def _app_modules() -> list[Path]:
    return sorted(PROJECT_ROOT.joinpath("app").rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _imports_a_module_distribution(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name.split(".")[0] in _MODULE_DISTRIBUTIONS
                for alias in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in _MODULE_DISTRIBUTIONS:
                return True
    return False


def _module_row_constructions(tree: ast.AST, imported: set[str]) -> set[str]:
    """Names constructed that were imported FROM a module distribution.

    Resolving through the import rather than matching the bare class name is
    what keeps `InboxQueue` (the module's) apart from any same-named Sub class:
    a name is only a finding if this file imported it from the distribution.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in imported
        ):
            found.add(node.func.id)
    return found


def _imported_row_classes(tree: ast.AST) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module.split(".")[0] not in _MODULE_DISTRIBUTIONS:
            continue
        for alias in node.names:
            if alias.name in _MODULE_ROW_CLASSES:
                imported.add(alias.asname or alias.name)
    return imported


# --------------------------------------------------------------------------
# 1. one door
# --------------------------------------------------------------------------


def test_only_the_adapter_and_its_named_companions_import_the_modules() -> None:
    offenders: list[str] = []
    for path in _app_modules():
        relative = _relative(path)
        if relative in _PERMITTED_IMPORTERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _imports_a_module_distribution(tree):
            offenders.append(relative)

    assert not offenders, (
        "these modules import a composed inbox distribution directly. Route them "
        "through `app/services/inbox_module/` — the module services own no "
        "transaction and check no authorization, so a direct caller is a second "
        "writer that the adapter cannot see:\n  " + "\n  ".join(sorted(offenders))
    )


def test_the_one_door_guard_still_bites() -> None:
    """Sensitivity: the scan must see both import forms."""
    dotted = ast.parse("import dotmac_inbox.service\n")
    from_form = ast.parse(
        "from dotmac_inbox_operations.service import admit_to_queue\n"
    )
    unrelated = ast.parse("from app.models.team_inbox import InboxConversation\n")

    assert _imports_a_module_distribution(dotted)
    assert _imports_a_module_distribution(from_form)
    assert not _imports_a_module_distribution(unrelated)


# --------------------------------------------------------------------------
# 2. one backfill
# --------------------------------------------------------------------------


def test_only_the_backfill_constructs_module_rows() -> None:
    offenders: list[str] = []
    for path in _app_modules():
        relative = _relative(path)
        if relative in _PERMITTED_MODULE_ROW_WRITERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_row_classes(tree)
        if not imported:
            continue
        constructed = _module_row_constructions(tree, imported)
        if constructed:
            offenders.append(f"{relative}: {', '.join(sorted(constructed))}")

    assert not offenders, (
        "these modules construct a composed inbox module's row directly. Only "
        "`app/services/inbox_backfill.py` may, and only because "
        "`create_conversation` cannot be given an id — ADR-0013, invariants:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_the_module_row_construction_guard_still_bites() -> None:
    """Sensitivity: an aliased import must still be caught, a Sub class must not."""
    aliased = ast.parse(
        "from dotmac_inbox.models import Conversation as C\nrow = C(tenant_id=1)\n"
    )
    assert _module_row_constructions(aliased, _imported_row_classes(aliased)) == {"C"}

    same_name_but_subs = ast.parse(
        "from app.models.team_inbox import InboxQueueBinding\n"
        "row = InboxQueueBinding()\n"
    )
    assert not _imported_row_classes(same_name_but_subs)

    presence_alias = ast.parse(
        "from dotmac_inbox_operations.models import InboxAgentPresence as P\n"
        "row = P(agent_reference='agent')\n"
    )
    assert _module_row_constructions(
        presence_alias, _imported_row_classes(presence_alias)
    ) == {"P"}


def test_the_direct_history_bridge_expires_when_owner_seams_are_pinned() -> None:
    """Published source is not enough; the installed exact pin ends the exception."""
    from dotmac_inbox import __version__ as inbox_version
    from dotmac_inbox_operations import __version__ as operations_version

    path = PROJECT_ROOT / "app/services/inbox_backfill.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constructions = _module_row_constructions(tree, _imported_row_classes(tree))
    if Version(inbox_version) >= Version("0.1.0a2"):
        assert not constructions & {
            "Conversation",
            "Message",
            "ConversationReadState",
        }, (
            "dotmac-inbox now supplies typed identity-preserving imports; retire "
            "the direct mapped-class bridge"
        )
    if Version(operations_version) >= Version("0.1.0a4"):
        assert not constructions & {
            "ModuleAgentPresence",
            "ConversationAssignment",
            "InboxQueueEntry",
            "InboxRoundRobinCursor",
        }, (
            "dotmac-inbox-operations now supplies typed identity-preserving "
            "imports; retire the direct mapped-class bridge"
        )


# --------------------------------------------------------------------------
# 2b. one stage branch
# --------------------------------------------------------------------------

#: `inbox_writes` owns the LOCAL/SHADOW/MODULE branch; `inbox_authority` defines
#: it; `inbox_backfill` and the reconciler are stage-independent by design and
#: are NOT exempt — if either starts consulting the stage, that is a finding.
_PERMITTED_STAGE_READERS = frozenset(
    {
        "app/services/inbox_authority.py",
        "app/services/inbox_writes.py",
    }
)


def _reads_the_stage(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "app.services.inbox_authority"
        ):
            if any(
                alias.name in {"resolve_stage", "InboxAuthorityStage", "cutover_record"}
                for alias in node.names
            ):
                return True
    return False


def test_only_the_write_seam_branches_on_the_cutover_stage() -> None:
    """A stage branch at twenty-three call sites is twenty-three chances to differ.

    The failure this prevents is not a crash. It is one rarely-exercised path
    that keeps writing Sub's tables after authority moved, so the reconciler
    overwrites it and the fact disappears — which nobody notices until an
    operator asks where a message went.
    """
    offenders: list[str] = []
    for path in _app_modules():
        relative = _relative(path)
        if relative in _PERMITTED_STAGE_READERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _reads_the_stage(tree):
            offenders.append(relative)

    assert not offenders, (
        "these modules decide inbox authority stage for themselves. The branch "
        "belongs in `app/services/inbox_writes.py` alone (ADR-0013 P5):\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_the_stage_branch_guard_still_bites() -> None:
    """Sensitivity: importing the enum counts, importing something else does not."""
    reads = ast.parse("from app.services.inbox_authority import resolve_stage\n")
    assert _reads_the_stage(reads)

    unrelated = ast.parse(
        "from app.services.inbox_authority import ActivateInboxAuthority\n"
    )
    assert not _reads_the_stage(unrelated)


def test_the_activation_gate_requires_an_empty_writer_baseline() -> None:
    """P5 cannot be activated while another module still writes a projected column.

    Not a style point. At MODULE stage the reconciler rebuilds the projection
    from `mod_inbox`, so any remaining Sub writer's value is silently discarded
    on the next reconcile. The baseline is therefore part of the activation
    gate, and this test is what makes "the gate is satisfied" checkable rather
    than remembered.
    """
    remaining = sorted(_baseline())
    if not remaining:
        return
    assert remaining == [
        "app/services/communication_intents.py",
        "app/services/team_inbox_field_job.py",
        "app/services/team_inbox_operations.py",
        "app/services/team_inbox_outbound.py",
        "app/tasks/notifications.py",
    ], (
        "the remaining direct inbox writers changed. Every one of them is "
        "blocked on one of the four module gaps recorded in ADR-0013 § 6a; a "
        f"different set means that analysis is stale:\n  {remaining}"
    )


# --------------------------------------------------------------------------
# 3. the shrinking list
# --------------------------------------------------------------------------


def _baseline() -> set[str]:
    return {
        line.strip()
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def test_no_new_module_writes_a_projected_inbox_column() -> None:
    added = sorted(set(projection_writer_files()) - _baseline())
    assert not added, (
        "these modules write a column `dotmac-inbox` owns after the ADR-0013 P5 "
        "switch, and the baseline does not list them. Route the write through "
        f"`app/services/inbox_module/`:\n  {chr(10).join(added)}\n\n{REGENERATE}"
    )


def test_a_retired_writer_is_removed_from_the_baseline() -> None:
    """The other direction. A stale line makes the remaining work look larger."""
    removed = sorted(_baseline() - set(projection_writer_files()))
    assert not removed, (
        "these modules are baselined and no longer write a projected column. "
        f"Delete their lines in the same change:\n  {chr(10).join(removed)}\n\n"
        f"{REGENERATE}"
    )


def test_the_projection_ratchet_still_bites() -> None:
    """Sensitivity: a resolved receiver counts, an unresolved one does not."""
    resolved = ast.parse(
        "def f(conversation: InboxConversation) -> None:\n"
        "    conversation.status = 'resolved'\n"
    )
    counter = _ProjectionWriteCounter()
    counter.visit(resolved)
    assert counter.count == 1
    assert counter.columns == {"InboxConversation.status"}

    sub_owned = ast.parse(
        "def f(conversation: InboxConversation) -> None:\n"
        "    conversation.priority = 10\n"
    )
    counter = _ProjectionWriteCounter()
    counter.visit(sub_owned)
    assert counter.count == 0, "`priority` is Sub's own column, not a projected one"

    unknown_receiver = ast.parse("def f(thing):\n    thing.status = 'resolved'\n")
    counter = _ProjectionWriteCounter()
    counter.visit(unknown_receiver)
    assert counter.count == 0


def test_the_census_watches_exactly_what_the_reconciler_writes() -> None:
    """The two lists are one decision; a disagreement makes both untrustworthy.

    `sent_at` and `received_at` are in the census and not in `MESSAGE_PROJECTION`
    because the reconciler unfolds the module's single `occurred_at` onto them by
    direction (`_message_targets`). They are still projected columns, so the
    census must watch them.
    """
    assert set(CONVERSATION_PROJECTION.values()) == CONVERSATION_PROJECTED_COLUMNS
    assert set(MESSAGE_PROJECTION.values()) | {"sent_at", "received_at"} == (
        MESSAGE_PROJECTED_COLUMNS
    )


# --------------------------------------------------------------------------
# vocabulary completeness
# --------------------------------------------------------------------------


def test_every_sub_channel_is_declared_to_the_module() -> None:
    """An undeclared channel raises `UnknownChannelError` at the first message.

    The module ships an empty registry on purpose, so this is not a formality:
    a channel added to `InboxChannelType` and not to `SUB_CHANNELS` fails at
    runtime, on an inbound webhook, in production.
    """
    declared = {spec.code for spec in SUB_CHANNELS}
    missing = sorted(
        member.value for member in InboxChannelType if member.value not in declared
    )
    assert not missing, (
        "these channels exist in `InboxChannelType` and are not declared in "
        "`app/services/inbox_channels.py`, so threading them raises at runtime:\n"
        f"  {chr(10).join(missing)}"
    )


def test_the_presence_map_is_exhaustive() -> None:
    """Sub has four presence states and the module three. All four must map.

    ADR-0013 § 6 records the `on_break` -> `AWAY` narrowing as knowing. A FIFTH
    Sub state added without a decision would otherwise raise on the first
    presence write.
    """
    from app.models.team_inbox import InboxAgentPresenceStatus

    unmapped = sorted(
        member.value
        for member in InboxAgentPresenceStatus
        if member not in PRESENCE_STATE_BY_SUB_STATUS
    )
    assert not unmapped, (
        "these Sub presence states have no module state. Map them in "
        "`app/services/inbox_module/references.py` and say in ADR-0013 § 6 what "
        f"the narrowing costs:\n  {chr(10).join(unmapped)}"
    )
    missing_reasons = sorted(
        member.value
        for member in InboxAgentPresenceStatus
        if member not in PRESENCE_AWAY_REASON_BY_SUB_STATUS
    )
    assert not missing_reasons, (
        "these Sub presence states have no product-owned roster reason mapping: "
        + ", ".join(missing_reasons)
    )
    for member in InboxAgentPresenceStatus:
        assert (
            sub_presence_status(presence_state(member), presence_away_reason(member))
            is member
        )


@pytest.mark.parametrize(
    "column",
    sorted(CONVERSATION_PROJECTED_COLUMNS | {"subscriber_id", "priority", "is_muted"}),
)
def test_every_watched_conversation_column_exists(column: str) -> None:
    """A census watching a column that does not exist reports a confident zero."""
    from app.models.team_inbox import InboxConversation

    assert hasattr(InboxConversation, column), (
        f"`{column}` is not a column on `InboxConversation`. The census and the "
        "reconciler both name it, and both would silently find nothing."
    )


def test_presence_capacity_column_is_nullable_as_the_backfill_assumes() -> None:
    """The backfill substitutes Sub's settings-backed default for a null capacity.

    If the column ever becomes NOT NULL the substitution is dead code, and
    leaving it in place would hide that the default stopped being consulted.
    """
    column = InboxAgentPresence.__table__.c.max_concurrent_conversations
    assert column.nullable, (
        "`max_concurrent_conversations` is no longer nullable. The backfill's "
        "fallback to `resolve_default_max_concurrent_conversations` is now "
        "unreachable — remove it, or explain why it stays."
    )
