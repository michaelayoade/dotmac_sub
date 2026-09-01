"""Committed maintenance and repair commands for Team Inbox projections."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.integration_platform import IntegrationInbox
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxMediaAsset,
    InboxMessage,
)
from app.services import (
    team_inbox_media,
    team_inbox_observations,
    team_inbox_operations,
    team_inbox_outbound,
    team_inbox_realtime,
    team_inbox_routing,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)

OWNER = "communications.team_inbox_maintenance"
_MAINTENANCE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="scheduled Inbox projection maintenance and repair",
    name="execute_team_inbox_maintenance_command",
)


@dataclass(frozen=True, slots=True)
class RetryFailedOutboundCommand:
    context: CommandContext
    limit: int = 50
    max_retry_count: int = 5


@dataclass(frozen=True, slots=True)
class PromoteMediaAssetsCommand:
    context: CommandContext
    limit: int = 200


@dataclass(frozen=True, slots=True)
class AutoResolveStaleCommand:
    context: CommandContext
    stale_hours: int = 72
    limit: int = 200


@dataclass(frozen=True, slots=True)
class MaintenanceOutcome:
    changed: int
    skipped: int = 0


@dataclass(frozen=True, slots=True)
class MetaProfileRepairCandidate:
    conversation_id: UUID
    channel_type: str
    contact_address: str


@dataclass(frozen=True, slots=True)
class MetaProfileRepairPreview:
    candidates: tuple[MetaProfileRepairCandidate, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class ApplyMetaProfileObservationCommand:
    context: CommandContext
    conversation_id: UUID
    expected_channel_type: str
    expected_contact_address: str
    display_name: str
    username: str | None
    profile_pic: str | None


@dataclass(frozen=True, slots=True)
class FailedMetaDeliveryCandidate:
    message_id: UUID
    channel_type: str
    retry_count: int


@dataclass(frozen=True, slots=True)
class FailedMetaDeliveryPreview:
    candidates: tuple[FailedMetaDeliveryCandidate, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class RetryFailedMetaDeliveriesCommand:
    context: CommandContext
    message_ids: tuple[UUID, ...]
    max_retry_count: int = 5


class TeamInboxMaintenanceError(DomainError):
    """A bounded Inbox repair command cannot be executed safely."""


def preview_meta_profile_repairs(
    db: Session, *, limit: int = 500
) -> MetaProfileRepairPreview:
    rows = (
        db.query(InboxConversation)
        .filter(
            InboxConversation.channel_type.in_(("facebook_messenger", "instagram_dm")),
            InboxConversation.contact_address.isnot(None),
            InboxConversation.is_active.is_(True),
        )
        .order_by(InboxConversation.id)
        .limit(5000)
        .all()
    )
    candidates = tuple(
        MetaProfileRepairCandidate(
            conversation_id=row.id,
            channel_type=row.channel_type,
            contact_address=str(row.contact_address),
        )
        for row in rows
        if not str((row.metadata_ or {}).get("contact_name") or "").strip()
    )[: max(1, min(int(limit), 5000))]
    digest = hashlib.sha256(
        "\n".join(
            f"{row.conversation_id}:{row.channel_type}:{row.contact_address}"
            for row in candidates
        ).encode()
    ).hexdigest()
    return MetaProfileRepairPreview(candidates=candidates, digest=digest)


def apply_meta_profile_observation(
    db: Session, command: ApplyMetaProfileObservationCommand
) -> MaintenanceOutcome:
    def operation() -> MaintenanceOutcome:
        conversation = (
            db.query(InboxConversation)
            .filter(InboxConversation.id == command.conversation_id)
            .with_for_update()
            .one_or_none()
        )
        if conversation is None:
            raise TeamInboxMaintenanceError(
                code="communications.team_inbox_maintenance.conversation_not_found",
                message="Conversation was not found.",
            )
        if (
            conversation.channel_type != command.expected_channel_type
            or conversation.contact_address != command.expected_contact_address
        ):
            raise TeamInboxMaintenanceError(
                code="communications.team_inbox_maintenance.profile_target_changed",
                message="Conversation identity changed after preview.",
            )
        display_name = command.display_name.strip()
        if not display_name:
            raise TeamInboxMaintenanceError(
                code="communications.team_inbox_maintenance.profile_name_missing",
                message="Meta did not return a usable contact name.",
            )
        metadata = dict(conversation.metadata_ or {})
        metadata["contact_name"] = display_name[:200]
        metadata["contact_name_source"] = "provider_observation"
        metadata["contact_profile"] = {
            "display_name": display_name[:255],
            "username": (command.username or "")[:255] or None,
            "profile_pic": (command.profile_pic or "")[:1000] or None,
        }
        conversation.metadata_ = metadata
        db.flush()
        return MaintenanceOutcome(changed=1)

    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=operation,
    )


def preview_failed_meta_deliveries(
    db: Session, *, limit: int = 100
) -> FailedMetaDeliveryPreview:
    rows = (
        db.query(InboxMessage)
        .filter(
            InboxMessage.direction == "outbound",
            InboxMessage.channel_type.in_(("facebook_messenger", "instagram_dm")),
            InboxMessage.metadata_["delivery_status"].as_string() == "failed",
        )
        .order_by(InboxMessage.id)
        .limit(max(1, min(int(limit), 1000)))
        .all()
    )
    candidates = tuple(
        FailedMetaDeliveryCandidate(
            message_id=row.id,
            channel_type=row.channel_type,
            retry_count=int((row.metadata_ or {}).get("retry_count") or 0),
        )
        for row in rows
    )
    digest = hashlib.sha256(
        "\n".join(
            f"{row.message_id}:{row.channel_type}:{row.retry_count}"
            for row in candidates
        ).encode()
    ).hexdigest()
    return FailedMetaDeliveryPreview(candidates=candidates, digest=digest)


def retry_failed_meta_deliveries(
    db: Session, command: RetryFailedMetaDeliveriesCommand
) -> MaintenanceOutcome:
    def operation() -> MaintenanceOutcome:
        changed = 0
        skipped = 0
        for message_id in command.message_ids:
            message = (
                db.query(InboxMessage)
                .filter(InboxMessage.id == message_id)
                .with_for_update()
                .one_or_none()
            )
            if message is None or message.channel_type not in {
                "facebook_messenger",
                "instagram_dm",
            }:
                skipped += 1
                continue
            metadata = dict(message.metadata_ or {})
            if (
                metadata.get("delivery_status") != "failed"
                or int(metadata.get("retry_count") or 0) >= command.max_retry_count
            ):
                skipped += 1
                continue
            result = team_inbox_outbound.retry_outbound_message(db, message=message)
            if result.kind in {"sent", "queued"}:
                changed += 1
            else:
                skipped += 1
        return MaintenanceOutcome(changed=changed, skipped=skipped)

    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=operation,
    )


@dataclass(frozen=True, slots=True)
class RepairWhatsAppLocationsCommand:
    context: CommandContext
    conversation_ids: tuple[UUID, ...]
    receipt_limit: int = 5000


@dataclass(frozen=True, slots=True)
class RepairWhatsAppLocationsOutcome:
    repaired: int
    already_complete: int
    missing_evidence: int
    receipts_scanned: int


@dataclass(frozen=True, slots=True)
class _RecoveredLocationEvidence:
    provider_message_id: str
    receipt_id: UUID
    location: team_inbox_observations.InboundLocationObservation


def _receipt_message_ids(receipt: IntegrationInbox) -> frozenset[UUID]:
    consequence = receipt.consequence_json or {}
    raw_items = consequence.get("items")
    if not isinstance(raw_items, list):
        return frozenset()
    message_ids: set[UUID] = set()
    for item in raw_items:
        if not isinstance(item, dict) or not item.get("message_id"):
            continue
        try:
            message_ids.add(UUID(str(item["message_id"])))
        except ValueError:
            continue
    return frozenset(message_ids)


def _receipt_location_evidence(
    receipt: IntegrationInbox,
    *,
    provider_message_id: str,
) -> _RecoveredLocationEvidence | None:
    payload = receipt.payload_json or {}
    raw_entries = payload.get("entry")
    if not isinstance(raw_entries, list):
        return None
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        raw_changes = entry.get("changes")
        if not isinstance(raw_changes, list):
            continue
        for change in raw_changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            raw_messages = value.get("messages")
            if not isinstance(raw_messages, list):
                continue
            for raw_message in raw_messages:
                if (
                    not isinstance(raw_message, dict)
                    or str(raw_message.get("id") or "") != provider_message_id
                    or str(raw_message.get("type") or "") != "location"
                ):
                    continue
                raw_location = raw_message.get("location")
                if not isinstance(raw_location, dict):
                    return None
                try:
                    location = team_inbox_observations.inbound_location_observation(
                        latitude=raw_location.get("latitude"),
                        longitude=raw_location.get("longitude"),
                        name=raw_location.get("name"),
                        address=raw_location.get("address"),
                    )
                except team_inbox_observations.TeamInboxObservationError:
                    return None
                if location is None:
                    return None
                return _RecoveredLocationEvidence(
                    provider_message_id=provider_message_id,
                    receipt_id=receipt.id,
                    location=location,
                )
    return None


def _apply_location_repair(
    *,
    message: InboxMessage,
    asset: InboxMediaAsset,
    evidence: _RecoveredLocationEvidence,
) -> None:
    location_metadata = evidence.location.to_metadata()
    message_metadata = dict(message.metadata_ or {})
    raw_attachments = message_metadata.get("attachments")
    attachments = list(raw_attachments) if isinstance(raw_attachments, list) else []
    updated_attachments: list[object] = []
    repaired_attachment = False
    for item in attachments:
        if (
            not repaired_attachment
            and isinstance(item, dict)
            and item.get("type") == "location"
        ):
            updated_item = dict(item)
            updated_item["location"] = location_metadata
            updated_attachments.append(updated_item)
            repaired_attachment = True
        else:
            updated_attachments.append(item)
    if not repaired_attachment:
        updated_attachments.append({"type": "location", "location": location_metadata})
    message_metadata["attachments"] = updated_attachments
    message_metadata["location_repair"] = {
        "source": "verified_integration_inbox_receipt",
        "receipt_id": str(evidence.receipt_id),
    }
    message.metadata_ = message_metadata

    asset_metadata = dict(asset.metadata_ or {})
    asset_metadata["location"] = location_metadata
    asset_metadata["location_repair"] = {
        "source": "verified_integration_inbox_receipt",
        "receipt_id": str(evidence.receipt_id),
    }
    asset.metadata_ = asset_metadata


def repair_whatsapp_locations(
    db: Session,
    command: RepairWhatsAppLocationsCommand,
) -> RepairWhatsAppLocationsOutcome:
    """Restore dropped WhatsApp coordinates from verified raw receipts.

    The caller must name the exact conversations. The repair locks their
    location assets and messages, accepts only receipts whose recorded
    consequence names the same message, and is idempotent once coordinates are
    present.
    """

    def operation() -> RepairWhatsAppLocationsOutcome:
        conversation_ids = tuple(dict.fromkeys(command.conversation_ids))
        if not conversation_ids or len(conversation_ids) > 100:
            raise TeamInboxMaintenanceError(
                code="communications.team_inbox_maintenance.invalid_location_repair_scope",
                message="Location repair requires between 1 and 100 conversation IDs.",
            )
        receipt_limit = max(1, min(command.receipt_limit, 10000))
        assets = (
            db.query(InboxMediaAsset)
            .filter(InboxMediaAsset.conversation_id.in_(conversation_ids))
            .filter(InboxMediaAsset.asset_type == "location")
            .order_by(InboxMediaAsset.created_at.asc(), InboxMediaAsset.id.asc())
            .with_for_update()
            .all()
        )
        message_ids = tuple(
            dict.fromkeys(asset.message_id for asset in assets if asset.message_id)
        )
        messages = (
            db.query(InboxMessage)
            .filter(InboxMessage.id.in_(message_ids))
            .with_for_update()
            .all()
            if message_ids
            else []
        )
        messages_by_id = {message.id: message for message in messages}
        incomplete: dict[UUID, InboxMediaAsset] = {}
        already_complete = 0
        missing_without_message = 0
        for asset in assets:
            if isinstance((asset.metadata_ or {}).get("location"), dict):
                already_complete += 1
            elif asset.message_id is None:
                missing_without_message += 1
            else:
                incomplete[asset.message_id] = asset
        if not incomplete:
            return RepairWhatsAppLocationsOutcome(
                repaired=0,
                already_complete=already_complete,
                missing_evidence=missing_without_message,
                receipts_scanned=0,
            )

        receipts = (
            db.query(IntegrationInbox)
            .filter(IntegrationInbox.event_type == "whatsapp.meta.webhook.v1")
            .filter(IntegrationInbox.state == "processed")
            .order_by(IntegrationInbox.received_at.desc())
            .limit(receipt_limit)
            .all()
        )
        evidence_by_message_id: dict[UUID, _RecoveredLocationEvidence] = {}
        target_ids = frozenset(incomplete)
        for receipt in receipts:
            matched_ids = _receipt_message_ids(receipt) & target_ids
            for message_id in matched_ids:
                message = messages_by_id.get(message_id)
                if message is None or not message.external_message_id:
                    continue
                evidence = _receipt_location_evidence(
                    receipt,
                    provider_message_id=message.external_message_id,
                )
                if evidence is not None:
                    evidence_by_message_id[message_id] = evidence

        repaired = 0
        for message_id, asset in incomplete.items():
            message = messages_by_id.get(message_id)
            evidence = evidence_by_message_id.get(message_id)
            if message is None or evidence is None:
                continue
            _apply_location_repair(message=message, asset=asset, evidence=evidence)
            repaired += 1
            logger.info(
                "team inbox WhatsApp location repaired",
                extra={
                    "event": "team_inbox_whatsapp_location_repaired",
                    "conversation_id": str(message.conversation_id),
                    "message_id": str(message.id),
                    "receipt_id": str(evidence.receipt_id),
                },
            )
        db.flush()
        return RepairWhatsAppLocationsOutcome(
            repaired=repaired,
            already_complete=already_complete,
            missing_evidence=missing_without_message + len(incomplete) - repaired,
            receipts_scanned=len(receipts),
        )

    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=operation,
    )


@dataclass(frozen=True, slots=True)
class RecoverStaleAiIntakeCommand:
    context: CommandContext
    now: datetime | None = None
    limit: int = 200


def _parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def recover_stale_ai_intake(
    db: Session, command: RecoverStaleAiIntakeCommand
) -> MaintenanceOutcome:
    """Move expired intake waits to the normal fallback team path.

    This reconciler never creates messages, queue entries, or assignments. It
    only repairs destination-team state for unowned conversations.
    """

    def operation() -> MaintenanceOutcome:
        now = (command.now or datetime.now(UTC)).astimezone(UTC)
        conversations = (
            db.query(InboxConversation)
            .filter(InboxConversation.is_active.is_(True))
            .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
            .filter(
                InboxConversation.metadata_["ai_intake"]["status"]
                .as_string()
                .in_(("classifying", "awaiting_follow_up"))
            )
            .order_by(InboxConversation.updated_at.asc())
            .limit(max(1, min(command.limit, 1000)))
            .with_for_update(skip_locked=True)
            .all()
        )
        changed = 0
        skipped = 0
        for conversation in conversations:
            metadata = dict(conversation.metadata_ or {})
            state_value = metadata.get("ai_intake")
            if not isinstance(state_value, dict):
                continue
            state = dict(state_value)
            if state.get("status") not in {"classifying", "awaiting_follow_up"}:
                continue
            due_at = _parse_instant(state.get("ai_intake_fallback_due_at"))
            if due_at is None:
                updated_at = _parse_instant(state.get("updated_at"))
                baseline = updated_at or conversation.updated_at or now
                if baseline.tzinfo is None:
                    baseline = baseline.replace(tzinfo=UTC)
                due_at = baseline.astimezone(UTC) + timedelta(minutes=5)
            if due_at > now:
                continue
            active_assignment = (
                db.query(InboxConversationAssignment.id)
                .filter(InboxConversationAssignment.conversation_id == conversation.id)
                .filter(InboxConversationAssignment.is_active.is_(True))
                .first()
            )
            if active_assignment is not None:
                state.update(
                    {
                        "status": "skipped",
                        "reason": "active_owner",
                        "updated_at": now.isoformat(),
                    }
                )
                metadata["ai_intake"] = state
                conversation.metadata_ = metadata
                skipped += 1
                continue
            decision = team_inbox_routing.resolve_channel_routing_decision(
                db,
                channel_type=conversation.channel_type,
                provider=str(state.get("provider") or "default"),
                account_scope=str(state.get("account_scope") or "default"),
                fallback_service_team_id=team_inbox_routing.default_service_team_id(db),
                metadata={**state, "ai_intake_status": "escalated"},
            )
            participants = [
                item
                for item in (
                    decision.primary_service_team_id,
                    decision.channel_service_team_id,
                )
                if item
            ]
            team_inbox_routing.apply_email_routing_plan(
                db,
                conversation=conversation,
                plan=team_inbox_routing.EmailTeamRoutingPlan(
                    primary_service_team_id=decision.primary_service_team_id,
                    participant_service_team_ids=list(dict.fromkeys(participants)),
                    matches=[],
                    unmatched_recipients=[],
                ),
            )
            state.update(
                {
                    "status": "escalated",
                    "reason": "fallback_timeout",
                    "destination_team_id": decision.primary_service_team_id,
                    "routing_reason": decision.reason,
                    "updated_at": now.isoformat(),
                }
            )
            metadata["ai_intake"] = state
            conversation.metadata_ = metadata
            logger.info(
                "stale AI intake routed to fallback",
                extra={
                    "event": "ai_intake_fallback_selected",
                    "conversation_id": str(conversation.id),
                    "destination_team_id": decision.primary_service_team_id,
                    "reason": "fallback_timeout",
                },
            )
            team_inbox_realtime.publish_queue_event(
                db, conversation_id=str(conversation.id), created=False
            )
            changed += 1
        return MaintenanceOutcome(changed=changed, skipped=skipped)

    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=operation,
    )


def retry_failed_outbound(
    db: Session, command: RetryFailedOutboundCommand
) -> MaintenanceOutcome:
    def operation() -> MaintenanceOutcome:
        result = team_inbox_operations.retry_failed_outbound_batch(
            db,
            limit=max(1, command.limit),
            max_retry_count=max(1, command.max_retry_count),
        )
        retried = result.get("retried")
        skipped = result.get("skipped")
        return MaintenanceOutcome(
            changed=len(retried) if isinstance(retried, list) else 0,
            skipped=len(skipped) if isinstance(skipped, list) else 0,
        )

    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=operation,
    )


def promote_media_assets(
    db: Session, command: PromoteMediaAssetsCommand
) -> MaintenanceOutcome:
    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=lambda: MaintenanceOutcome(
            changed=team_inbox_media.promote_unmaterialized_assets(
                db, limit=max(1, command.limit)
            )
        ),
    )


def auto_resolve_stale(
    db: Session, command: AutoResolveStaleCommand
) -> MaintenanceOutcome:
    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=lambda: MaintenanceOutcome(
            changed=team_inbox_operations.auto_resolve_stale_conversations(
                db,
                stale_hours=max(1, command.stale_hours),
                limit=max(1, command.limit),
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class BackfillParticipantsCommand:
    context: CommandContext
    limit: int = 200


def backfill_participants(
    db: Session, command: BackfillParticipantsCommand
) -> MaintenanceOutcome:
    """Project participants for conversations that have none yet.

    Walks oldest-first over unprojected conversations, so repeated runs move
    forward rather than re-treading the same head. Idempotent: it admits only
    missing endpoints, so a completed conversation is untouched and a partial
    one is finished.
    """
    from app.services import team_inbox_participants

    def operation() -> MaintenanceOutcome:
        result = team_inbox_participants.backfill_conversations(
            db, limit=max(1, command.limit)
        )
        return MaintenanceOutcome(
            changed=int(result.get("participants") or 0),
            skipped=int(result.get("conversations") or 0),
        )

    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=operation,
    )


@dataclass(frozen=True, slots=True)
class WakeDueSnoozedCommand:
    context: CommandContext
    limit: int = 200


def wake_due_snoozed(db: Session, command: WakeDueSnoozedCommand) -> MaintenanceOutcome:
    """Return conversations whose chosen wake time has passed to the open queue."""

    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=lambda: MaintenanceOutcome(
            changed=team_inbox_operations.wake_due_snoozed_conversations(
                db, limit=max(1, command.limit)
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class ReleaseScheduledRepliesCommand:
    context: CommandContext
    limit: int = 50


def release_scheduled_replies(
    db: Session, command: ReleaseScheduledRepliesCommand
) -> MaintenanceOutcome:
    """Send scheduled replies whose time has come.

    Each send is independent: one failure marks that message failed and the
    batch continues, so a single bad recipient cannot hold up everyone else's
    scheduled mail.
    """

    def operation() -> MaintenanceOutcome:
        from app.services import team_inbox_outbound

        due = team_inbox_outbound.due_scheduled_replies(db, limit=max(1, command.limit))
        sent = 0
        skipped = 0
        for message in due:
            result = team_inbox_outbound.send_scheduled_reply(db, message=message)
            if result.kind in {"sent", "queued"}:
                sent += 1
            else:
                skipped += 1
        return MaintenanceOutcome(changed=sent, skipped=skipped)

    return execute_owner_command(
        db,
        definition=_MAINTENANCE_COMMAND,
        context=command.context,
        operation=operation,
    )
