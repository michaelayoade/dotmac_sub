"""Billing approval is an admission/lifecycle command, never a loose flag."""

import ast
from pathlib import Path

from app.services.scheduler import PERMANENT_LIFECYCLE_TASKS
from app.services.task_reliability import (
    TASK_RELIABILITY_CONTRACTS,
    FailureVisibility,
    Idempotency,
    RetryPolicy,
)

ROOT = Path(__file__).resolve().parents[2]

# The exact, closed set of functions in account_lifecycle.py allowed to call
# `_require_billing_approval`. This is an inventory, not a count: a 7th
# enclosing function, or any of these six missing, fails the test below.
EXPECTED_BILLING_APPROVAL_CALL_SITES = frozenset(
    {
        "apply_requested_account_status",
        "restore_subscription_detailed",
        "activate_subscription",
        "enable_subscription",
        "transition_account_status",
        "unsuspend_account_override",
    }
)


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _billing_approval_call_sites(source: str) -> dict[str, list[ast.Call]]:
    """Map each enclosing top-level function name to its calls to
    ``_require_billing_approval``, found anywhere in its body (including
    nested blocks). Calls outside any function are keyed under ``"<module>"``.
    """
    tree = ast.parse(source)
    sites: dict[str, list[ast.Call]] = {}

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id == (
                "_require_billing_approval"
            ):
                enclosing = self.stack[-1] if self.stack else "<module>"
                sites.setdefault(enclosing, []).append(node)
            self.generic_visit(node)

    _Visitor().visit(tree)
    return sites


def _typed_intent_member(call: ast.Call) -> str | None:
    """Return the ``ActivationIntent`` member name passed as ``intent=``.

    Returns ``None`` if there is no ``intent`` keyword at all, and the
    sentinel ``"<non-typed>"`` if one exists but is not a literal
    ``ActivationIntent.MEMBER`` attribute access (e.g. a bare string, a
    variable, or any other value that isn't traceably the closed enum).
    """
    for kw in call.keywords:
        if kw.arg != "intent":
            continue
        value = kw.value
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "ActivationIntent"
        ):
            return value.attr
        return "<non-typed>"
    return None


#: `restore_subscription_detailed` is the one call site that FORWARDS its own
#: `intent` parameter rather than hardcoding a literal member, because it is
#: the sole function serving two distinct external callers (ordinary
#: restoration and the registered deletion-recovery participant). Its
#: explicitness is proven differently: the function's own signature must
#: require `intent: ActivationIntent` with no default (checked separately
#: below), so a caller can never omit it or pass an untyped value.
_FORWARDS_ITS_OWN_INTENT_PARAMETER = "restore_subscription_detailed"


def _requires_typed_intent_parameter(source: str, function_name: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            args = node.args
            kwonly = args.kwonlyargs
            defaults = args.kw_defaults
            for arg, default in zip(kwonly, defaults, strict=True):
                if arg.arg != "intent":
                    continue
                if default is not None:
                    return False  # has a default: not required
                annotation = arg.annotation
                return (
                    isinstance(annotation, ast.Name)
                    and annotation.id == "ActivationIntent"
                )
    return False


def test_exactly_six_call_sites_each_pass_a_typed_activation_intent() -> None:
    """Exact enclosing-function inventory, not a count.

    A missing expected call site, an added 7th, or a call missing/misusing
    the typed ``intent=`` keyword must all fail this test — proven by the
    sensitivity tests below, which exercise the same checker function
    against synthetic source rather than mutating the shipped module.
    """
    source = _source("app/services/account_lifecycle.py")
    sites = _billing_approval_call_sites(source)
    found = set(sites)
    assert found == EXPECTED_BILLING_APPROVAL_CALL_SITES, (
        f"missing={EXPECTED_BILLING_APPROVAL_CALL_SITES - found} "
        f"unexpected={found - EXPECTED_BILLING_APPROVAL_CALL_SITES}"
    )
    for name, calls in sites.items():
        for call in calls:
            member = _typed_intent_member(call)
            assert member is not None, (
                f"{name} calls _require_billing_approval with no explicit intent="
            )
            if name == _FORWARDS_ITS_OWN_INTENT_PARAMETER:
                assert _requires_typed_intent_parameter(source, name), (
                    f"{name} must require intent: ActivationIntent with no "
                    "default on its own signature, since it forwards rather "
                    "than hardcodes the value"
                )
            else:
                assert member != "<non-typed>", (
                    f"{name} calls _require_billing_approval with an intent= "
                    "that is not a literal ActivationIntent member"
                )


_SYNTHETIC_SIX = """
def apply_requested_account_status():
    _require_billing_approval(db, subscriber_id=x, intent=ActivationIntent.ACCOUNT_STATUS_REQUEST)

def restore_subscription_detailed():
    _require_billing_approval(db, subscriber_id=x, intent=intent)

def activate_subscription():
    _require_billing_approval(db, subscriber_id=x, intent=ActivationIntent.SUBSCRIPTION_ACTIVATION_FROM_PENDING)

def enable_subscription():
    _require_billing_approval(db, subscriber_id=x, intent=ActivationIntent.SUBSCRIPTION_REACTIVATION_FROM_DISABLED)

def transition_account_status():
    _require_billing_approval(db, subscriber_id=x, intent=ActivationIntent.ACCOUNT_STATUS_TRANSITION)

def unsuspend_account_override():
    _require_billing_approval(db, subscriber_id=x, intent=ActivationIntent.ACCOUNT_UNSUSPEND)
"""


def test_sensitivity_checker_accepts_the_clean_six() -> None:
    """Baseline: the checker passes over a correctly-shaped synthetic tree.

    A check that only ever passes over a clean tree proves nothing about
    itself — the two tests below plant a missing site and an unexpected
    7th site respectively and show each is named.
    """
    sites = _billing_approval_call_sites(_SYNTHETIC_SIX)
    assert set(sites) == EXPECTED_BILLING_APPROVAL_CALL_SITES
    # `restore_subscription_detailed` forwards its own `intent` parameter
    # rather than a literal `ActivationIntent.MEMBER` — in BOTH this
    # synthetic snippet and the real module. `<non-typed>` here is the
    # correct shape for that one call site (see
    # `_FORWARDS_ITS_OWN_INTENT_PARAMETER`), not a false negative.
    assert _typed_intent_member(sites["restore_subscription_detailed"][0]) == (
        "<non-typed>"
    )


def test_sensitivity_a_missing_expected_call_site_is_detected() -> None:
    mutated = _SYNTHETIC_SIX.replace(
        "def unsuspend_account_override():\n"
        "    _require_billing_approval(db, subscriber_id=x, "
        "intent=ActivationIntent.ACCOUNT_UNSUSPEND)\n",
        "",
    )
    sites = _billing_approval_call_sites(mutated)
    assert set(sites) != EXPECTED_BILLING_APPROVAL_CALL_SITES
    assert "unsuspend_account_override" in (
        EXPECTED_BILLING_APPROVAL_CALL_SITES - set(sites)
    )


def test_sensitivity_an_unexpected_seventh_call_site_is_detected() -> None:
    mutated = _SYNTHETIC_SIX + (
        "\ndef some_new_function():\n"
        "    _require_billing_approval(db, subscriber_id=x, "
        "intent=ActivationIntent.ACCOUNT_STATUS_REQUEST)\n"
    )
    sites = _billing_approval_call_sites(mutated)
    assert set(sites) != EXPECTED_BILLING_APPROVAL_CALL_SITES
    assert "some_new_function" in (set(sites) - EXPECTED_BILLING_APPROVAL_CALL_SITES)


def test_sensitivity_an_untyped_or_wrong_intent_call_is_detected() -> None:
    no_intent = _SYNTHETIC_SIX.replace(
        "intent=ActivationIntent.ACCOUNT_STATUS_REQUEST", ""
    ).replace("intent=x, )", ")")
    call = _billing_approval_call_sites(no_intent)["apply_requested_account_status"][0]
    assert _typed_intent_member(call) is None

    wrong_type = _SYNTHETIC_SIX.replace(
        "intent=ActivationIntent.ACCOUNT_STATUS_REQUEST", 'intent="active"'
    )
    call = _billing_approval_call_sites(wrong_type)["apply_requested_account_status"][0]
    assert _typed_intent_member(call) == "<non-typed>"


def test_profile_and_bulk_adapters_do_not_write_billing_approval() -> None:
    source = _source("app/services/web_customer_actions.py")
    assert "subscriber.billing_enabled =" not in source
    assert "change_account_billing_approval(" in source


def test_generic_subscriber_update_rejects_billing_approval_mutation() -> None:
    source = _source("app/services/subscriber.py")
    assert source.count('data.pop("billing_enabled", None)') >= 2
    assert "Billing approval is a lifecycle command" in source


def test_drift_reconciler_is_permanent_and_not_flag_gated() -> None:
    task_name = "app.tasks.enforcement.reconcile_billing_approval_drift"
    assert task_name in PERMANENT_LIFECYCLE_TASKS
    scheduler = _source("app/services/scheduler_config.py")
    task_index = scheduler.index(f'task_name="{task_name}"')
    block = scheduler[task_index - 200 : task_index + 200]
    assert "enabled=True" in block


def test_drift_reconciler_retries_by_guarded_per_item_beat_pass() -> None:
    contract = TASK_RELIABILITY_CONTRACTS[
        "app.tasks.enforcement.reconcile_billing_approval_drift"
    ]

    assert contract.retry_policy is RetryPolicy.BEAT_RERUN
    assert contract.idempotency is Idempotency.PER_ITEM_GUARDED
    assert contract.failure_visibility is FailureVisibility.LOG_ONLY


def test_billing_approval_owner_is_the_only_explicit_runtime_field_writer() -> None:
    owner = _source("app/services/account_billing_approval.py")
    assert "account.billing_enabled = False" in owner
    assert "account.billing_enabled = True" in owner
    for relative in (
        "app/services/web_customer_actions.py",
        "app/services/subscriber.py",
        "app/services/account_lifecycle.py",
    ):
        source = _source(relative)
        assert ".billing_enabled = False" not in source
        assert ".billing_enabled = True" not in source


def test_retired_global_billing_switch_is_not_a_presentation_fallback() -> None:
    for relative in (
        "app/services/web_customer_details.py",
        "app/services/web_catalog_subscriptions.py",
    ):
        source = _source(relative)
        defaults = source[source.index("def _billing_global_defaults") :]
        defaults = defaults[: defaults.index("\n\ndef ", 1)]
        assert '"billing_enabled"' not in defaults
