"""Manifest for staffed inbox operations.

The `permissions` block is the point of this module's authorization surface.
Sub currently gates inbox actions on `support:ticket:update` — a permission
about tickets, held by everyone who edits one, which happens to be reachable
from the inbox screen. That makes "may reassign another agent's conversation"
and "may edit a ticket" the same decision, so a deployment cannot separate them
without a code change.

These codes separate them. `presence.self` and `presence.manage` split changing
your OWN availability from changing somebody else's; transfer, cross-team
transfer, escalation and supervisor override are four decisions a deployment
may bind to four different roles. Per ADR-0008 the vocabulary is declared here
and may only be REFERENCED elsewhere: a product's route calls
`require_permission("inbox_operations.conversation.transfer")` and the boot
fails on a typo rather than the request 403-ing months later.

Default roles are deliberately narrow. `presence.self` is the only code an
ordinary agent holds by default; everything that touches another person's work
starts with supervisors and admins, because widening a grant is a deliberate
act and narrowing one after the fact is an incident.
"""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.permissions import PermissionSpec
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_inbox_operations.models import TENANT_TABLES

module = ModuleManifest(
    code="inbox_operations",
    version="0.1.0a5",
    core=False,
    short_code="inbox_ops",
    migration_prefix="io",
    migration_branch="inbox_operations",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
    permissions=(
        PermissionSpec(
            code="inbox_operations.presence.self",
            description=(
                "Choose your own availability and send your own heartbeat. The "
                "one code an ordinary agent needs, and the reason it exists "
                "separately: an agent managing their own break must not imply "
                "they can change anyone else's."
            ),
            default_roles=("agent", "supervisor", "admin"),
        ),
        PermissionSpec(
            code="inbox_operations.presence.manage",
            description=(
                "Change another agent's availability state or assignment "
                "capacity. The module refuses the write without an actor and a "
                "reason, so holding this code always leaves a trail."
            ),
            default_roles=("supervisor", "admin"),
        ),
        PermissionSpec(
            code="inbox_operations.conversation.claim",
            description=(
                "Pull queued work in a queue you are eligible for. Separate "
                "from transfer because taking unowned work and taking work "
                "away from a colleague are not the same act."
            ),
            default_roles=("agent", "supervisor", "admin"),
        ),
        PermissionSpec(
            code="inbox_operations.conversation.transfer",
            description=(
                "Move a conversation to another agent in the same queue, cold "
                "or warm, and requeue work you hold."
            ),
            default_roles=("agent", "supervisor", "admin"),
        ),
        PermissionSpec(
            code="inbox_operations.conversation.transfer_cross_team",
            description=(
                "Target an agent in a different queue. Held separately because "
                "a cross-team move lands work on people whose backlog and SLA "
                "the sender does not carry."
            ),
            default_roles=("supervisor", "admin"),
        ),
        PermissionSpec(
            code="inbox_operations.conversation.escalate",
            description=(
                "Ask for an escalation on a conversation and record the ask on "
                "its timeline. Never moves ownership — the record has no "
                "target-agent column. Whether the escalation should exist, at "
                "what level and who answers it belongs to "
                "dotmac-operational-escalations, not to this permission."
            ),
            default_roles=("agent", "supervisor", "admin"),
        ),
        PermissionSpec(
            code="inbox_operations.supervisor.override",
            description=(
                "Force a transfer past an offline, full or cross-queue target. "
                "The exception is recorded as an exception, with its own "
                "reason, rather than looking like a routine move afterwards."
            ),
            default_roles=("supervisor", "admin"),
        ),
    ),
    # A `notify_reference` that only sits in a column is a note about who
    # someone ought to tell. These are the alerts the commands actually send,
    # enqueued in the same transaction as the state change; Messaging and
    # Integrator own delivery and its outcomes.
    outbox_event_types=(
        "inbox_operations.transfer_requested.v1",
        "inbox_operations.conversation_transferred.v1",
        "inbox_operations.escalation_requested.v1",
    ),
    audit_actions=(
        "inbox_operations.presence.overridden",
        "inbox_operations.conversation.claimed",
        "inbox_operations.conversation.transferred",
        "inbox_operations.conversation.requeued",
        "inbox_operations.conversation.escalated",
        "inbox_operations.session.ended",
    ),
)

__all__ = ["module"]
