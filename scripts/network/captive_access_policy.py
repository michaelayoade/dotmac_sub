"""Operator adapter for the composable captive access policy.

Adapter for ``access.captive_access_policy`` (list) and
``access.captive_access_policy_change`` (preview/apply/drain). It owns no
decision: it parses arguments into typed changes, calls the owners, and maps
domain errors to exit codes. Nothing here contacts a router.

    # list rules and customer sets
    python -m scripts.network.captive_access_policy rules [--include-disabled]

    # preview a change (read-only); prints the exact preview fingerprint
    python -m scripts.network.captive_access_policy preview --change add-rule \\
        --scope plan_family --plan-family home_flex --effect allow \\
        --category residential --reseller-condition house \\
        --change-reason "Pilot captive access for Home Flex"

    # apply exactly that preview for at most N subscriptions
    python -m scripts.network.captive_access_policy apply --change add-rule ... \\
        --expected-preview-fingerprint <sha256> --actor-system-user-id <uuid> \\
        --reason "approved in CHG-123" --idempotency-key CHG-123-1 \\
        [--max-subscriptions 200] --confirm

    # customer-set cohorts
    ... preview --change create-set --name "Lekki pilot" --change-reason "..."
    ... preview --change add-members --customer-set-id <uuid> \\
        --member <uuid> [--member <uuid> ...] [--members-file ids.txt] \\
        --change-reason "..."
    ... preview --change remove-members --customer-set-id <uuid> --member <uuid> ...

    # re-evaluate existing locks in bounded batches until nothing remains
    python -m scripts.network.captive_access_policy drain \\
        --actor-system-user-id <uuid> --reason "..." --idempotency-key CHG-123 \\
        [--max-subscriptions 200] [--max-batches 20] --confirm

``--actor-system-user-id`` is the operator's claimed staff identity; the owner
re-verifies the ``network:radius:write`` grant inside its own transaction.
Shell access to run this script is the authentication boundary, as for the
other operator scripts.

Exit codes: 0 success; 1 refused by the owner (domain error); 2 invalid
arguments.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

from app.models.captive_access_policy import (
    CaptiveAccessRuleEffect,
    CaptiveAccessRuleScope,
    CaptiveResellerCondition,
)
from app.models.subscriber import SubscriberCategory
from app.services.captive_access_policy import (
    AddCaptiveAccessRule,
    AddCaptiveCustomerSetMembers,
    CaptiveAccessRuleListQuery,
    CaptiveAccessRuleView,
    CaptivePolicyChange,
    CaptiveRuleConditions,
    CaptiveRuleSpec,
    CreateCaptiveCustomerSet,
    DisableCaptiveAccessRule,
    ReevaluateCaptivePolicy,
    RemoveCaptiveCustomerSetMembers,
    list_captive_access_rules,
)
from app.services.captive_access_policy_change import (
    DEFAULT_MAX_SUBSCRIPTIONS,
    ApplyCaptivePolicyChangeCommand,
    CaptiveAccessMove,
    CaptivePolicyChangeOutcome,
    CaptivePolicyChangePreview,
    PreviewCaptivePolicyChangeQuery,
    apply_captive_policy_change,
    preview_captive_policy_change,
    principal_label,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

_CHANGES = (
    "reevaluate",
    "add-rule",
    "disable-rule",
    "create-set",
    "add-members",
    "remove-members",
)


class UsageError(Exception):
    pass


def _add_change_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--change", choices=_CHANGES, default="reevaluate")
    parser.add_argument(
        "--scope", choices=[item.value for item in CaptiveAccessRuleScope]
    )
    parser.add_argument(
        "--effect", choices=[item.value for item in CaptiveAccessRuleEffect]
    )
    parser.add_argument("--subscriber-id", type=UUID)
    parser.add_argument("--customer-set-id", type=UUID)
    parser.add_argument("--plan-family")
    parser.add_argument("--offer-id", type=UUID, action="append", default=[])
    parser.add_argument(
        "--category",
        choices=[item.value for item in SubscriberCategory],
        action="append",
        default=[],
    )
    parser.add_argument(
        "--reseller-condition",
        choices=[item.value for item in CaptiveResellerCondition],
        default=CaptiveResellerCondition.any.value,
    )
    parser.add_argument("--reseller-id", type=UUID, action="append", default=[])
    parser.add_argument("--rule-id", type=UUID)
    parser.add_argument("--name")
    parser.add_argument("--description")
    parser.add_argument("--member", type=UUID, action="append", default=[])
    parser.add_argument("--members-file", type=Path)
    parser.add_argument(
        "--change-reason", help="Evidence recorded on the rule/cohort change."
    )


def _add_apply_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor-system-user-id", type=UUID, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument(
        "--max-subscriptions", type=int, default=DEFAULT_MAX_SUBSCRIPTIONS
    )
    parser.add_argument("--confirm", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    rules = commands.add_parser("rules", help="list rules and customer sets")
    rules.add_argument("--include-disabled", action="store_true")
    preview = commands.add_parser("preview", help="preview a change (read-only)")
    _add_change_arguments(preview)
    preview.add_argument("--show", type=int, default=50, help="moves to list")
    apply = commands.add_parser("apply", help="apply one previewed change")
    _add_change_arguments(apply)
    _add_apply_arguments(apply)
    apply.add_argument("--expected-preview-fingerprint", required=True)
    drain = commands.add_parser(
        "drain", help="re-evaluate existing locks in bounded batches"
    )
    _add_apply_arguments(drain)
    drain.add_argument("--max-batches", type=int, default=20)
    return parser


def _members(args: argparse.Namespace) -> frozenset[UUID]:
    values = set(args.member)
    if args.members_file is not None:
        for line in args.members_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    values.add(UUID(line))
                except ValueError as exc:
                    raise UsageError(f"invalid account id in file: {line}") from exc
    return frozenset(values)


def _require(value: str | None, flag: str) -> str:
    if not value:
        raise UsageError(f"{flag} is required for this change")
    return value


def build_change(args: argparse.Namespace) -> CaptivePolicyChange:
    """Translate CLI arguments into one typed change (no decisions)."""

    if args.change == "reevaluate":
        return ReevaluateCaptivePolicy()
    reason = _require(args.change_reason, "--change-reason")
    if args.change == "add-rule":
        scope = CaptiveAccessRuleScope(_require(args.scope, "--scope"))
        return AddCaptiveAccessRule(
            rule=CaptiveRuleSpec(
                scope=scope,
                effect=CaptiveAccessRuleEffect(_require(args.effect, "--effect")),
                reason=reason,
                subscriber_id=args.subscriber_id,
                customer_set_id=args.customer_set_id,
                plan_family=args.plan_family,
                offer_ids=frozenset(args.offer_id),
                conditions=CaptiveRuleConditions(
                    subscriber_categories=(
                        frozenset(SubscriberCategory(item) for item in args.category)
                        if args.category
                        else None
                    ),
                    reseller_condition=CaptiveResellerCondition(
                        args.reseller_condition
                    ),
                    reseller_ids=frozenset(args.reseller_id),
                ),
            )
        )
    if args.change == "disable-rule":
        if args.rule_id is None:
            raise UsageError("--rule-id is required for disable-rule")
        return DisableCaptiveAccessRule(rule_id=args.rule_id, reason=reason)
    if args.change == "create-set":
        return CreateCaptiveCustomerSet(
            name=_require(args.name, "--name"),
            description=args.description,
            reason=reason,
        )
    if args.customer_set_id is None:
        raise UsageError("--customer-set-id is required for membership changes")
    members = _members(args)
    if args.change == "add-members":
        return AddCaptiveCustomerSetMembers(
            customer_set_id=args.customer_set_id,
            subscriber_ids=members,
            reason=reason,
        )
    return RemoveCaptiveCustomerSetMembers(
        customer_set_id=args.customer_set_id,
        subscriber_ids=members,
        reason=reason,
    )


def _rule_row(rule: CaptiveAccessRuleView) -> dict[str, object]:
    categories = rule.conditions.subscriber_categories
    return {
        "id": str(rule.id),
        "scope": rule.scope.value,
        "effect": rule.effect.value,
        "enabled": rule.enabled,
        "subscriber_id": str(rule.subscriber_id) if rule.subscriber_id else None,
        "customer_set_id": str(rule.customer_set_id) if rule.customer_set_id else None,
        "plan_family": rule.plan_family,
        "offer_ids": sorted(str(item) for item in rule.offer_ids),
        "categories": (
            sorted(item.value for item in categories)
            if categories is not None
            else None
        ),
        "reseller_condition": rule.conditions.reseller_condition.value,
        "reseller_ids": sorted(str(item) for item in rule.conditions.reseller_ids),
        "created_by": rule.created_by,
        "created_at": rule.created_at.isoformat(),
        "reason": rule.reason,
    }


def _move_row(move: CaptiveAccessMove) -> dict[str, object]:
    return {
        "subscription_id": str(move.subscription_id),
        "direction": move.direction.value,
        "plan_family": move.plan_family,
        "routers": list(move.router_names),
        "reason": move.reason,
    }


def _preview_dict(
    preview: CaptivePolicyChangePreview, *, show: int
) -> dict[str, object]:
    return {
        "change_kind": preview.change_kind,
        "preview_fingerprint": preview.preview_fingerprint,
        "evaluated_at": preview.evaluated_at.isoformat(),
        "subscriptions_evaluated": preview.subscriptions_evaluated,
        "lock_updates": len(preview.lock_updates),
        "subscriptions_with_lock_updates": len(preview.subscriptions_with_lock_updates),
        "to_captive": len(preview.to_captive),
        "to_hard_reject": len(preview.to_hard_reject),
        "by_router": [
            {"direction": item.direction.value, "router": item.key, "count": item.count}
            for item in preview.by_router
        ],
        "by_plan_family": [
            {
                "direction": item.direction.value,
                "plan_family": item.key,
                "count": item.count,
            }
            for item in preview.by_plan_family
        ],
        "moves": [_move_row(item) for item in preview.moves[: max(show, 0)]],
    }


def _outcome_dict(outcome: CaptivePolicyChangeOutcome) -> dict[str, object]:
    return {
        "change_id": str(outcome.change_id),
        "change_kind": outcome.change_kind,
        "replayed": outcome.replayed,
        "rule_id": str(outcome.rule_id) if outcome.rule_id else None,
        "customer_set_id": (
            str(outcome.customer_set_id) if outcome.customer_set_id else None
        ),
        "members_added": outcome.members_added,
        "members_removed": outcome.members_removed,
        "members_unchanged": outcome.members_unchanged,
        "lock_updates_applied": outcome.lock_updates_applied,
        "subscriptions_applied": len(outcome.subscriptions_applied),
        "moves_applied": [_move_row(item) for item in outcome.moves_applied],
        "remaining_subscriptions": outcome.remaining_subscriptions,
    }


def _preview(change: CaptivePolicyChange, actor: str) -> CaptivePolicyChangePreview:
    with db_session_adapter.read_session() as db:
        return preview_captive_policy_change(
            db, query=PreviewCaptivePolicyChangeQuery(change=change, actor=actor)
        )


def _apply(
    args: argparse.Namespace,
    *,
    change: CaptivePolicyChange,
    fingerprint: str,
    idempotency_key: str,
) -> CaptivePolicyChangeOutcome:
    command_id = uuid4()
    with db_session_adapter.owner_command_session() as db:
        return apply_captive_policy_change(
            db,
            ApplyCaptivePolicyChangeCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=principal_label(args.actor_system_user_id),
                    scope="access:captive_access_policy:write",
                    reason=args.reason,
                    idempotency_key=idempotency_key,
                ),
                change=change,
                expected_preview_fingerprint=fingerprint,
                authorized_system_user_id=args.actor_system_user_id,
                max_subscriptions=args.max_subscriptions,
            ),
        )


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "rules":
            with db_session_adapter.read_session() as db:
                listing = list_captive_access_rules(
                    db,
                    query=CaptiveAccessRuleListQuery(
                        include_disabled=args.include_disabled
                    ),
                )
            _emit(
                {
                    "rules": [_rule_row(item) for item in listing.rules],
                    "customer_sets": [
                        {
                            "id": str(item.id),
                            "name": item.name,
                            "is_active": item.is_active,
                            "members": len(item.member_ids),
                        }
                        for item in listing.customer_sets
                    ],
                }
            )
            return EXIT_OK
        if args.command == "preview":
            change = build_change(args)
            _emit(_preview_dict(_preview(change, "operator:preview"), show=args.show))
            return EXIT_OK
        if not args.confirm:
            raise UsageError("--confirm is required to apply")
        actor = principal_label(args.actor_system_user_id)
        if args.command == "apply":
            change = build_change(args)
            outcome = _apply(
                args,
                change=change,
                fingerprint=args.expected_preview_fingerprint,
                idempotency_key=args.idempotency_key,
            )
            _emit(_outcome_dict(outcome))
            return EXIT_OK
        # drain: preview + apply re-evaluation batches until nothing remains.
        batches: list[dict[str, object]] = []
        for index in range(max(args.max_batches, 0)):
            preview = _preview(ReevaluateCaptivePolicy(), actor)
            if not preview.lock_updates:
                break
            outcome = _apply(
                args,
                change=ReevaluateCaptivePolicy(),
                fingerprint=preview.preview_fingerprint,
                idempotency_key=f"{args.idempotency_key}:batch:{index}",
            )
            batches.append(_outcome_dict(outcome))
            if outcome.remaining_subscriptions == 0:
                break
        _emit({"batches": batches})
        return EXIT_OK
    except UsageError as exc:
        print(f"usage: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except DomainError as exc:
        _emit({"refused": exc.code, "message": exc.message})
        return EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
