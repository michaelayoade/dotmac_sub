"""Static guards on the Integrator port's boundary.

These are the checks that keep the port an adapter after the people who wrote it
have moved on. Each states an enforceable premise (ADR-0018): if the premise
stops holding, the test fails rather than quietly passing over an empty set.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PORT = PROJECT_ROOT / "app/api/integrator_observations.py"
ENVELOPE = PROJECT_ROOT / "app/services/team_inbox_integrator_envelope.py"
MIRROR = PROJECT_ROOT / "app/services/team_inbox_integrator_mirror.py"
DESCRIPTOR = PROJECT_ROOT / "app/services/integrations/product_port_descriptor.py"
LEGACY_WHATSAPP = PROJECT_ROOT / "app/api/inbox_webhooks.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(_source(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_the_port_never_reaches_past_its_owners():
    """The port records and delegates. Every Inbox decision belongs elsewhere."""

    forbidden = {
        "app.services.team_inbox_channel_receive",
        "app.services.team_inbox_receive",
        "app.services.team_inbox_routing",
        "app.services.team_inbox_threads",
        "app.services.team_inbox_contact_resolution",
        "app.services.conversation_ticket_handoff",
        "app.services.team_inbox_delivery_receipts",
    }
    assert not (_imported_modules(PORT) & forbidden)


def test_the_port_issues_no_direct_database_statement():
    source = _source(PORT)
    for pattern in ("db.query(", "db.execute(", "db.add(", "select(", "db.commit("):
        assert pattern not in source, f"the port issued {pattern!r} itself"


def test_the_port_does_not_verify_a_provider_signature():
    """The sensitivity proof for the whole authentication design.

    Sub is not talking to the provider here, so the provider's signature does
    not cover the bytes that arrive. A future reader "restoring" an HMAC check
    would be verifying a signature over a body that is not the signed body —
    a control that looks like security and is not.
    """

    source = _source(PORT)
    for pattern in ("hmac", "compare_digest", "X-Hub-Signature", "sha256="):
        assert pattern not in source, (
            f"the port grew a provider signature check ({pattern!r}); "
            "it authenticates the Integrator, not the provider"
        )


def test_the_normalizer_touches_no_session_and_no_network():
    source = _source(ENVELOPE)
    for pattern in ("Session", "requests", "httpx", "urllib", "db."):
        assert pattern not in source, f"the normalizer reached for {pattern!r}"


def test_product_port_descriptor_is_read_only_and_cannot_accept_provider_routing():
    from app.services.integrations.product_port_descriptor import (
        product_port_descriptor,
        product_port_descriptor_v3,
        settlement_product_port_descriptor_v3,
    )

    assert list(inspect.signature(product_port_descriptor).parameters) == [
        "db",
        "destination_binding_id",
    ]
    assert list(inspect.signature(product_port_descriptor_v3).parameters) == [
        "db",
        "destination_binding_id",
    ]
    assert list(
        inspect.signature(settlement_product_port_descriptor_v3).parameters
    ) == ["db", "destination_binding_id"]
    source = _source(DESCRIPTOR)
    for pattern in ("db.add(", "db.delete(", "db.execute(", "db.commit("):
        assert pattern not in source


def test_the_mirror_writes_nothing():
    source = _source(MIRROR)
    for pattern in ("db.add(", "db.commit(", "db.flush(", "db.delete(", "insert("):
        assert pattern not in source, f"the mirror wrote something ({pattern!r})"


def test_the_mirror_uses_the_observation_owners_published_fingerprint():
    """A second definition of "is this the same fact?" would drift from the first."""

    source = _source(MIRROR)
    assert "observation_fingerprint" in source
    assert "hashlib" not in source, (
        "the mirror recomputed a fingerprint of its own instead of asking the "
        "observation owner"
    )


def test_no_integrator_member_was_added_to_the_provider_enum():
    """A transport must not masquerade as a provider in the domain identity.

    Recording Integrator-delivered WhatsApp under a distinct provider would give
    one upstream event two identities either side of a cutover, and every
    message in flight would be recorded twice. See
    ``docs/INTEGRATOR_MESSAGING_RECEIVE_CUTOVER.md`` section 2.2 — this is a
    deliberate deviation from the specification and the test is what keeps it
    deliberate.
    """

    from app.services.team_inbox_observations import InboxProvider

    assert {item.value for item in InboxProvider} == {
        "smtp",
        "meta_cloud_api",
        "meta_social",
        "chat_widget",
        "fiber_website",
    }


def test_the_port_uses_subs_own_provider_event_identity_convention():
    """The overlap window is only safe while both producers agree on identity."""

    from app.models.team_inbox import InboxObservationKind
    from app.services.team_inbox_integrator_envelope import scoped_provider_event_id

    assert (
        scoped_provider_event_id(
            kind=InboxObservationKind.message, provider_event_id="wamid.X"
        )
        == "message:wamid.X"
    )
    assert (
        scoped_provider_event_id(
            kind=InboxObservationKind.delivery_receipt, provider_event_id="wamid.X"
        )
        == "receipt:wamid.X"
    )
    # And that convention is the one the legacy receiver actually uses, read
    # from its source rather than asserted from memory. If Sub's own receiver
    # ever changes prefix, this fails here instead of duplicating live traffic.
    legacy = _source(PROJECT_ROOT / "app/services/team_inbox_channel_receive.py")
    assert 'f"message:{external_message_id}"' in legacy


@pytest.mark.parametrize(
    "channel", ["note", "field_job", "website_fiber", "email", "chat_widget"]
)
def test_no_provider_may_claim_a_channel_outside_its_own_family(channel):
    from app.models.team_inbox import InboxChannelType
    from app.services.team_inbox_integrator_envelope import PROVIDER_CHANNELS
    from app.services.team_inbox_observations import InboxProvider

    allowed = PROVIDER_CHANNELS[InboxProvider.meta_cloud_api]
    assert InboxChannelType(channel) not in allowed


def test_the_channel_allowlist_still_bites():
    """A guard over an empty set passes for the wrong reason."""

    from app.models.team_inbox import InboxChannelType
    from app.services.team_inbox_integrator_envelope import PROVIDER_CHANNELS

    covered = {channel for row in PROVIDER_CHANNELS.values() for channel in row}
    uncovered = set(InboxChannelType) - covered
    # There must always be at least one channel no provider can claim, or the
    # allowlist has quietly become an allow-everything.
    assert uncovered, "every channel is now claimable; the allowlist is inert"
    assert InboxChannelType.note in uncovered
    assert InboxChannelType.field_job in uncovered


def test_the_legacy_receivers_defects_are_still_present_and_still_owed():
    """A two-directional ratchet on the debt this migration exists to retire.

    Both defects are recorded in
    ``docs/INTEGRATOR_MESSAGING_RECEIVE_CUTOVER.md`` section 7.4 as things
    retired WITH the old receiver. This test fails when either disappears
    without that document being updated — a fix landing quietly would mean the
    cutover plan now describes work that no longer exists, and a reader would
    trust a stale retirement list.
    """

    legacy = _source(LEGACY_WHATSAPP)
    meta = _source(PROJECT_ROOT / "app/api/meta_inbox_webhooks.py")
    plan = _source(PROJECT_ROOT / "docs/INTEGRATOR_MESSAGING_RECEIVE_CUTOVER.md")

    digest_identity = 'f"meta:{hashlib.sha256(raw_body).hexdigest()}"' in legacy
    borrowed_secret = "_whatsapp_signature_fallback_secret" in meta

    assert digest_identity == ("meta:{sha256(raw_body)}" in plan), (
        "the request-digest identity defect and the cutover plan's retirement "
        "list disagree; update section 7 in the same change"
    )
    assert borrowed_secret == ("_whatsapp_signature_fallback_secret" in plan), (
        "the borrowed-secret defect and the cutover plan's retirement list "
        "disagree; update section 7 in the same change"
    )


def test_no_untrusted_exception_text_is_persisted_to_error_detail():
    """`error_detail` is a durable operator-facing column, not a log line.

    An arbitrary exception's message is untrusted text of unbounded shape — it
    can carry a payload fragment, a connection string, or a provider's own
    error body. The generic handler must persist the exception CLASS, never
    `str(exc)`. `dotmac-integration` 0.1.0a4 fixed this exact defect class on
    the Integrator's side of the wire; this keeps Sub's side from acquiring it.

    The DomainError branch may persist `exc.message` because that class
    contracts its message as operator-safe, so the check is specific to the
    catch-all rather than banning the column outright.
    """

    source = _source(PORT)
    assert "error_detail=type(exc).__name__" in source, (
        "the catch-all failure path no longer records the exception class"
    )
    for leaked in (
        "error_detail=str(exc)",
        'error_detail=f"{exc}',
        "error_detail=repr(exc)",
    ):
        assert leaked not in source, (
            f"untrusted exception text reaches a durable column: {leaked!r}"
        )


def test_the_shadow_route_and_the_write_route_do_not_share_a_scope():
    from app.api.integrator_observations import (
        INTEGRATOR_MIRROR_SCOPE,
        INTEGRATOR_OBSERVATION_SCOPE,
    )

    assert INTEGRATOR_MIRROR_SCOPE != INTEGRATOR_OBSERVATION_SCOPE


def test_both_scopes_are_seeded():
    """A route permission that is not seeded is a dark surface with green CI."""

    from app.api.integrator_observations import (
        INTEGRATOR_MIRROR_SCOPE,
        INTEGRATOR_OBSERVATION_SCOPE,
    )
    from scripts.seed.seed_rbac import DEFAULT_PERMISSIONS

    seeded = {key for key, _description in DEFAULT_PERMISSIONS}
    assert INTEGRATOR_OBSERVATION_SCOPE in seeded
    assert INTEGRATOR_MIRROR_SCOPE in seeded


MAIN = PROJECT_ROOT / "app/main.py"
PORT_MODULE = "app.api.integrator_observations"


def _spec_lists(name: str) -> list[tuple[str, ...]]:
    """The router specs assigned to `name` in app/main.py, read statically."""

    tree = ast.parse(_source(MAIN))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if name not in targets or not isinstance(node.value, ast.List):
            continue
        specs: list[tuple[str, ...]] = []
        for element in node.value.elts:
            if isinstance(element, ast.Tuple):
                specs.append(
                    tuple(
                        part.value
                        for part in element.elts
                        if isinstance(part, ast.Constant)
                    )
                )
        return specs
    raise AssertionError(f"app/main.py no longer assigns {name}")


def _literal_prefix_of_joined_string(module_path: Path, variable: str) -> str:
    """The constant leading text of an f-string assigned to `variable`."""

    tree = ast.parse(_source(module_path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(t.id == variable for t in node.targets if isinstance(t, ast.Name)):
            continue
        value = node.value
        if isinstance(value, ast.JoinedStr) and value.values:
            head = value.values[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                return head.value
    raise AssertionError(f"{module_path.name} no longer builds {variable} literally")


def test_the_receiver_is_a_core_router_so_a_mount_failure_cannot_be_swallowed():
    """`enabled` must be impossible while the receiver is not actually mounted.

    The descriptor's `activation_state` is derived from two database columns. It
    cannot see whether the route it advertises is being served, so what stops it
    claiming `enabled` at a dead path is WHERE the router is mounted.

    `_include_core_routers` calls `_apply_router_spec` with no exception
    handling: a core router that fails to import or mount aborts the boot. The
    deferred loader deliberately does the opposite — it logs and continues, so
    the rest of the surface survives one bad module. That is right for a
    deferred router and fatal for this one: a swallowed failure would leave Sub
    answering the descriptor from an earlier worker while the delivery path
    404s, which is precisely the dishonest `enabled` the activation gate exists
    to prevent.

    Moving this spec into the deferred list would be a one-line change that
    looks like a startup-latency improvement and silently removes the guarantee.
    """

    core = {spec[0] for spec in _spec_lists("_CORE_ROUTER_SPECS")}
    deferred = {spec[0] for spec in _spec_lists("_DEFERRED_API_ROUTER_SPECS")}

    assert PORT_MODULE in core, (
        f"{PORT_MODULE} must stay in _CORE_ROUTER_SPECS: only the core loader "
        "is fail-fast, so only there does a mount failure stop the boot instead "
        "of leaving the descriptor advertising a path nothing serves"
    )
    assert PORT_MODULE not in deferred
    # The premise is only enforceable while the two loaders still differ.
    assert deferred, "the deferred list is empty; this guard's premise is gone"


def test_the_descriptor_and_the_receiver_are_served_by_the_same_router():
    """Mounting is all-or-nothing, so activation cannot outlive the receiver.

    Both routes are declared on one `APIRouter` in one module. That is what
    makes "the descriptor answered, therefore the receiver is mounted" true by
    construction rather than by coincidence — a reader who split the descriptor
    into its own module would create exactly the state this port must never
    reach: a readable descriptor claiming `enabled` for a receiver that was
    never mounted.
    """

    tree = ast.parse(_source(PORT))
    routes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            if call is None or not isinstance(call.func, ast.Attribute):
                continue
            router = call.func.value
            if isinstance(router, ast.Name):
                routes[node.name] = router.id

    descriptor_router = routes.get("read_product_port_descriptor")
    descriptor_v3_router = routes.get("read_product_port_descriptor_v3")
    receiver_router = routes.get("receive_integrator_observation")

    assert descriptor_router == "router"
    assert descriptor_v3_router == "router"
    assert receiver_router == "router"
    assert descriptor_router == receiver_router
    assert descriptor_v3_router == receiver_router


def test_the_advertised_delivery_path_is_built_from_the_mounted_prefixes():
    """What the descriptor publishes must be where the receiver actually is.

    `delivery_path` is a literal in the descriptor service, while the real path
    is `_mount_router`'s api prefix plus the port's own `APIRouter(prefix=...)`.
    Three independent strings that have to agree, and nothing today makes them:
    renaming the router prefix leaves the descriptor advertising the old path,
    and the reconciler would faithfully bind a destination that 404s.
    """

    advertised = _literal_prefix_of_joined_string(DESCRIPTOR, "delivery_path")

    main_tree = ast.parse(_source(MAIN))
    api_prefixes = {
        keyword.value.value
        for node in ast.walk(main_tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "prefix"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
        and keyword.value.value.startswith("/api")
    }
    assert api_prefixes, "app/main.py no longer mounts an /api prefix literally"

    port_tree = ast.parse(_source(PORT))
    router_prefixes = {
        keyword.value.value
        for node in ast.walk(port_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "APIRouter"
        for keyword in node.keywords
        if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant)
    }
    assert len(router_prefixes) == 1, "the port declares no single router prefix"
    router_prefix = router_prefixes.pop()

    expected = {f"{api}{router_prefix}/" for api in api_prefixes}
    assert advertised in expected, (
        f"the descriptor advertises {advertised!r}, but the receiver is mounted "
        f"at one of {sorted(expected)!r} — a descriptor that names a path "
        "nothing serves is a binding to a dead destination"
    )
