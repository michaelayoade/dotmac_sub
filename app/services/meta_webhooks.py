"""Meta webhook processing service.

Handles incoming webhooks from Facebook (Messenger) and Instagram (DMs).
Processes webhook payloads and creates messages in the CRM system.

Environment Variables:
    META_APP_SECRET: Required for webhook signature verification
    META_WEBHOOK_VERIFY_TOKEN: Token for webhook verification challenge
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.logging import get_logger
from app.config import settings
from app.services.settings_spec import resolve_value
from app.models.domain_settings import SettingDomain
from app.models.connector import ConnectorConfig, ConnectorType
from app.models.crm.enums import (
    ChannelType,
    ConversationStatus,
    MessageDirection,
    MessageStatus,
)
from app.models.crm.conversation import Message
from app.models.subscriber import ChannelType as SubscriberChannelType, Subscriber, SubscriberChannel
from app.models.crm.comments import SocialCommentPlatform
from app.models.integration import IntegrationTarget, IntegrationTargetType
from app.models.oauth_token import OAuthToken
from app.schemas.crm.conversation import ConversationCreate, MessageCreate
from app.schemas.crm.inbox import (
    FacebookCommentPayload,
    FacebookMessengerWebhookPayload,
    InstagramCommentPayload,
    InstagramDMWebhookPayload,
    MetaWebhookPayload,
    _attachments_have_story_mention,
)
from app.services.common import coerce_uuid
from app.services.crm import contact as contact_service
from app.services.crm import conversation as conversation_service
from app.services.crm import comments as comments_service
from app.services.crm import inbox as inbox_service

logger = get_logger(__name__)

_META_IDENTITY_KEYS = {
    "account_id",
    "subscriber_id",
    "account_number",
    "subscriber_number",
    "customer_id",
    "email",
    "phone",
}


def _get_meta_graph_base_url(db: Session | None) -> str:
    if db:
        version = resolve_value(db, SettingDomain.comms, "meta_graph_api_version")
    else:
        version = None
    if not version:
        version = settings.meta_graph_api_version
    return f"https://graph.facebook.com/{version}"


def _normalize_external_id(raw_id: str | None) -> tuple[str | None, str | None]:
    if not raw_id:
        return None, None
    if len(raw_id) <= 120:
        return raw_id, None
    digest = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()
    return digest, raw_id


def _normalize_external_ref(raw_id: str | None) -> str | None:
    if not raw_id:
        return None
    return raw_id if len(raw_id) <= 255 else None


def _normalize_phone_address(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits or None


def _fetch_profile_name(
    access_token: str | None,
    user_id: str,
    fields: str,
    base_url: str,
) -> str | None:
    if not access_token:
        return None
    try:
        with httpx.Client(timeout=5) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/{user_id}",
                params={"fields": fields, "access_token": access_token},
            )
        if response.status_code >= 400:
            if response.status_code in (401, 403):
                logger.warning(
                    "meta_profile_lookup_auth_failed user_id=%s status=%s body=%s",
                    user_id,
                    response.status_code,
                    response.text,
                )
            else:
                logger.debug(
                    "meta_profile_lookup_failed user_id=%s status=%s body=%s",
                    user_id,
                    response.status_code,
                    response.text,
                )
            return None
        data = response.json()
        return data.get("username") or data.get("name")
    except Exception as exc:
        logger.warning("meta_profile_lookup_exception user_id=%s error=%s", user_id, exc)
        return None


def _coerce_identity_dict(value: object) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _extract_identity_metadata(*values: object) -> dict:
    identity: dict = {}
    for value in values:
        data = _coerce_identity_dict(value)
        if not data:
            continue
        for key in _META_IDENTITY_KEYS:
            candidate = data.get(key)
            if candidate is None:
                continue
            if isinstance(candidate, str):
                candidate = candidate.strip()
            if candidate:
                identity[key] = candidate
    return identity


def _find_subscriber_by_email_or_phone(db: Session, metadata: dict | None) -> Subscriber | None:
    if not metadata or not isinstance(metadata, dict):
        return None
    email = metadata.get("email")
    if isinstance(email, str):
        email_value = email.strip().lower()
    else:
        email_value = None
    if email_value:
        channel = (
            db.query(SubscriberChannel)
            .filter(SubscriberChannel.channel_type == SubscriberChannelType.email)
            .filter(func.lower(SubscriberChannel.address) == email_value)
            .first()
        )
        if channel:
            return channel.subscriber
        subscriber = db.query(Subscriber).filter(func.lower(Subscriber.email) == email_value).first()
        if subscriber:
            return subscriber

    phone = metadata.get("phone")
    if isinstance(phone, str):
        phone_value = _normalize_phone_address(phone)
        raw_phone = phone.strip()
    else:
        phone_value = None
        raw_phone = None
    if phone_value:
        channel = (
            db.query(SubscriberChannel)
            .filter(
                SubscriberChannel.channel_type.in_(
                    {
                        SubscriberChannelType.phone,
                        SubscriberChannelType.sms,
                        SubscriberChannelType.whatsapp,
                    }
                )
            )
            .filter(
                or_(
                    SubscriberChannel.address == phone_value,
                    SubscriberChannel.address == raw_phone,
                )
            )
            .first()
        )
        if channel:
            return channel.subscriber
        subscriber = db.query(Subscriber).filter(
            or_(
                Subscriber.phone == phone_value,
                Subscriber.phone == raw_phone,
            )
        ).first()
        if subscriber:
            return subscriber

    return None


def _resolve_meta_subscriber_and_channel(
    db: Session,
    channel_type: ChannelType,
    sender_id: str,
    contact_name: str | None,
    metadata: dict | None,
):
    account, _ = inbox_service._resolve_account_from_identifiers(
        db,
        None,
        channel_type,
        metadata,
    )
    subscriber = None
    if account and account.subscriber and account.subscriber.subscriber_id:
        subscriber = db.get(Subscriber, account.subscriber.subscriber_id)
    if not subscriber:
        subscriber = _find_subscriber_by_email_or_phone(db, metadata)
    if subscriber:
        channel = inbox_service._ensure_subscriber_channel(db, subscriber, channel_type, sender_id)
        if contact_name and not subscriber.display_name:
            subscriber.display_name = contact_name
            db.commit()
            db.refresh(subscriber)
        if account:
            inbox_service._apply_account_to_subscriber(db, subscriber, account)
        return subscriber, channel
    subscriber, channel = contact_service.get_or_create_contact_by_channel(
        db,
        channel_type,
        sender_id,
        contact_name,
    )
    if account:
        inbox_service._apply_account_to_subscriber(db, subscriber, account)
    return subscriber, channel


def _apply_meta_read_receipt(
    db: Session,
    channel_type: ChannelType,
    contact_id: str | None,
    watermark: int | float | None,
) -> None:
    if not contact_id or watermark is None:
        return
    timestamp = float(watermark)
    if timestamp > 1_000_000_000_000:
        timestamp = timestamp / 1000
    read_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    subscriber_channel_type = SubscriberChannelType(channel_type.value)
    channel = (
        db.query(SubscriberChannel)
        .filter(SubscriberChannel.channel_type == subscriber_channel_type)
        .filter(SubscriberChannel.address == contact_id)
        .first()
    )
    if not channel:
        return
    db.query(Message).filter(
        Message.subscriber_channel_id == channel.id,
        Message.channel_type == channel_type,
        Message.direction == MessageDirection.inbound,
        Message.status == MessageStatus.received,
        Message.read_at.is_(None),
        func.coalesce(Message.received_at, Message.created_at) <= read_at,
    ).update({"read_at": read_at})
    db.commit()


def verify_webhook_signature(
    payload_body: bytes,
    signature_header: str | None,
    app_secret: str,
) -> bool:
    """Verify Meta webhook signature (X-Hub-Signature-256).

    Meta signs all webhook payloads with the app secret. This function
    verifies the signature to ensure the webhook is authentic.

    Args:
        payload_body: Raw request body bytes
        signature_header: Value of X-Hub-Signature-256 header
        app_secret: Facebook App Secret

    Returns:
        True if signature is valid, False otherwise
    """
    if not signature_header or not signature_header.startswith("sha256="):
        logger.warning("webhook_signature_missing_or_invalid")
        return False

    expected_signature = signature_header[7:]  # Remove "sha256=" prefix
    computed_signature = hmac.new(
        app_secret.encode(),
        payload_body,
        hashlib.sha256,
    ).hexdigest()

    is_valid = hmac.compare_digest(expected_signature, computed_signature)
    if not is_valid:
        logger.warning("webhook_signature_mismatch")
    return is_valid


def _resolve_meta_connector(
    db: Session,
    connector_type: ConnectorType,
) -> tuple[IntegrationTarget | None, ConnectorConfig | None]:
    """Find active Meta connector and integration target.

    Args:
        db: Database session
        connector_type: ConnectorType.facebook or ConnectorType.instagram

    Returns:
        Tuple of (IntegrationTarget, ConnectorConfig) or (None, None)
    """
    target = (
        db.query(IntegrationTarget)
        .join(ConnectorConfig, ConnectorConfig.id == IntegrationTarget.connector_config_id)
        .filter(IntegrationTarget.target_type == IntegrationTargetType.crm)
        .filter(IntegrationTarget.is_active.is_(True))
        .filter(ConnectorConfig.connector_type == connector_type)
        .filter(ConnectorConfig.is_active.is_(True))
        .order_by(IntegrationTarget.created_at.desc())
        .first()
    )
    if not target:
        return None, None
    return target, target.connector_config


def _find_token_for_account(
    db: Session,
    connector_config_id,
    account_type: str,
    account_id: str,
) -> OAuthToken | None:
    """Find OAuth token for a specific account.

    Args:
        db: Database session
        connector_config_id: UUID of ConnectorConfig
        account_type: "page" or "instagram_business"
        account_id: External account ID

    Returns:
        OAuthToken or None if not found
    """
    return (
        db.query(OAuthToken)
        .filter(OAuthToken.connector_config_id == connector_config_id)
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == account_type)
        .filter(OAuthToken.external_account_id == account_id)
        .filter(OAuthToken.is_active.is_(True))
        .first()
    )


def process_messenger_webhook(
    db: Session,
    payload: MetaWebhookPayload,
) -> list[dict]:
    """Process Facebook Messenger webhook payload.

    Args:
        db: Database session
        payload: Validated MetaWebhookPayload

    Returns:
        List of result dicts with message_id and status
    """
    results = []

    target, config = _resolve_meta_connector(db, ConnectorType.facebook)
    base_url = _get_meta_graph_base_url(db)

    for entry in payload.entry:
        page_id = entry.id
        page_token = None
        if config:
            token = _find_token_for_account(db, config.id, "page", page_id)
            page_token = token.access_token if token else None

        if entry.changes:
            results.extend(_process_facebook_comment_changes(db, entry))

        if not entry.messaging:
            continue

        for messaging_event in entry.messaging:
            if messaging_event.postback and not messaging_event.message:
                logger.info(
                    "messenger_webhook_postback_ignored page_id=%s sender_id=%s",
                    page_id,
                    (messaging_event.sender or {}).get("id"),
                )
                continue
            if messaging_event.delivery and not messaging_event.message:
                logger.info(
                    "messenger_webhook_delivery_ignored page_id=%s sender_id=%s",
                    page_id,
                    (messaging_event.sender or {}).get("id"),
                )
                continue
            if messaging_event.read and not messaging_event.message:
                sender_id = (messaging_event.sender or {}).get("id")
                recipient_id = (messaging_event.recipient or {}).get("id")
                contact_id = sender_id
                if sender_id == page_id:
                    contact_id = recipient_id
                _apply_meta_read_receipt(
                    db,
                    ChannelType.facebook_messenger,
                    contact_id,
                    (messaging_event.read or {}).get("watermark"),
                )
                continue
            # Skip non-message events (typing indicators, etc.)
            if not messaging_event.message:
                continue

            message = messaging_event.message
            sender = messaging_event.sender or {}

            # Skip echo messages (messages sent by the page)
            if message.get("is_echo"):
                continue

            sender_id = sender.get("id")
            if not sender_id:
                logger.warning("messenger_webhook_missing_sender page_id=%s", page_id)
                continue
            if sender_id == page_id:
                logger.info(
                    "messenger_webhook_skip_self page_id=%s sender_id=%s",
                    page_id,
                    sender_id,
                )
                continue

            attachments = message.get("attachments", [])
            if attachments:
                logger.info(
                    "instagram_webhook_attachments message_id=%s attachments=%s",
                    message.get("mid"),
                    attachments,
                )
            # Get message text
            text = message.get("text")
            if not text:
                if attachments:
                    text = "(attachment)"
                else:
                    continue

            # Parse timestamp
            received_at = None
            if messaging_event.timestamp:
                received_at = datetime.fromtimestamp(
                    messaging_event.timestamp / 1000,
                    tz=timezone.utc,
                )

            external_id, external_ref = _normalize_external_id(message.get("mid"))
            metadata = {
                "attachments": message.get("attachments"),
                "reply_to": message.get("reply_to"),
            }
            identity_metadata = _extract_identity_metadata(
                message.get("metadata"),
                message.get("referral"),
                (message.get("referral") or {}).get("ref") if isinstance(message.get("referral"), dict) else None,
                messaging_event.postback.get("payload") if messaging_event.postback else None,
            )
            if identity_metadata:
                metadata.update(identity_metadata)
            if external_ref:
                metadata["provider_message_id"] = external_ref
            contact_name = (
                sender.get("name")
                or (message.get("from", {}) if isinstance(message.get("from"), dict) else {}).get("name")
                or _fetch_profile_name(page_token, sender_id, "name", base_url)
                or f"Facebook User {sender_id}"
            )
            parsed = FacebookMessengerWebhookPayload(
                contact_address=sender_id,
                contact_name=contact_name,
                message_id=external_id,
                page_id=page_id,
                body=text,
                received_at=received_at,
                metadata=metadata,
            )

            try:
                result_msg = receive_facebook_message(db, parsed)
                results.append({
                    "message_id": str(result_msg.id),
                    "status": "received",
                })
            except Exception as exc:
                logger.exception(
                    "messenger_webhook_processing_failed page_id=%s error=%s",
                    page_id,
                    exc,
                )
                results.append({
                    "message_id": None,
                    "status": "failed",
                    "error": str(exc),
                })

    return results


def process_instagram_webhook(
    db: Session,
    payload: MetaWebhookPayload,
) -> list[dict]:
    """Process Instagram webhook payload.

    Args:
        db: Database session
        payload: Validated MetaWebhookPayload

    Returns:
        List of result dicts with message_id and status
    """
    results = []

    target, config = _resolve_meta_connector(db, ConnectorType.facebook)
    base_url = _get_meta_graph_base_url(db)

    for entry in payload.entry:
        ig_account_id = entry.id
        ig_token = None
        if config:
            token = _find_token_for_account(db, config.id, "instagram_business", ig_account_id)
            ig_token = token.access_token if token else None

        if entry.changes:
            results.extend(_process_instagram_comment_changes(db, entry))

        if not entry.messaging:
            continue

        for messaging_event in entry.messaging:
            if messaging_event.postback and not messaging_event.message:
                logger.info(
                    "instagram_webhook_postback_ignored ig_account_id=%s sender_id=%s",
                    ig_account_id,
                    (messaging_event.sender or {}).get("id"),
                )
                continue
            if messaging_event.delivery and not messaging_event.message:
                logger.info(
                    "instagram_webhook_delivery_ignored ig_account_id=%s sender_id=%s",
                    ig_account_id,
                    (messaging_event.sender or {}).get("id"),
                )
                continue
            if messaging_event.read and not messaging_event.message:
                sender_id = (messaging_event.sender or {}).get("id")
                recipient_id = (messaging_event.recipient or {}).get("id")
                contact_id = sender_id
                if sender_id == ig_account_id:
                    contact_id = recipient_id
                _apply_meta_read_receipt(
                    db,
                    ChannelType.instagram_dm,
                    contact_id,
                    (messaging_event.read or {}).get("watermark"),
                )
                continue
            if not messaging_event.message:
                continue

            message = messaging_event.message
            sender = messaging_event.sender or {}

            attachments = message.get("attachments", [])
            logger.info(
                "instagram_webhook_message_keys message_id=%s keys=%s attachments_count=%s",
                message.get("mid"),
                list(message.keys()),
                len(attachments),
            )
            if attachments:
                logger.info(
                    "instagram_webhook_attachments message_id=%s attachments=%s",
                    message.get("mid"),
                    attachments,
                )

            # Skip echo messages
            if message.get("is_echo"):
                continue

            sender_id = sender.get("id")
            if not sender_id:
                logger.warning(
                    "instagram_webhook_missing_sender ig_account_id=%s",
                    ig_account_id,
                )
                continue
            if sender_id == ig_account_id:
                logger.info(
                    "instagram_webhook_skip_self ig_account_id=%s sender_id=%s",
                    ig_account_id,
                    sender_id,
                )
                continue
            logger.info(
                "instagram_webhook_ids ig_account_id=%s sender_id=%s",
                ig_account_id,
                sender_id,
            )

            text = message.get("text")
            is_story_mention = _attachments_have_story_mention(attachments)
            if not text:
                if attachments and not is_story_mention:
                    text = "(attachment)"
                elif not is_story_mention:
                    continue

            received_at = None
            if messaging_event.timestamp:
                received_at = datetime.fromtimestamp(
                    messaging_event.timestamp / 1000,
                    tz=timezone.utc,
                )

            external_id, external_ref = _normalize_external_id(message.get("mid"))
            metadata = {
                "attachments": message.get("attachments"),
            }
            identity_metadata = _extract_identity_metadata(
                message.get("metadata"),
                message.get("referral"),
                (message.get("referral") or {}).get("ref") if isinstance(message.get("referral"), dict) else None,
                messaging_event.postback.get("payload") if messaging_event.postback else None,
            )
            if identity_metadata:
                metadata.update(identity_metadata)
            if external_ref:
                metadata["provider_message_id"] = external_ref
            contact_name = (
                sender.get("username")
                or sender.get("name")
                or (message.get("from", {}) if isinstance(message.get("from"), dict) else {}).get("username")
                or _fetch_profile_name(ig_token, sender_id, "username,name", base_url)
                or f"Instagram User {sender_id}"
            )
            parsed = InstagramDMWebhookPayload(
                contact_address=sender_id,
                contact_name=contact_name,
                message_id=external_id,
                instagram_account_id=ig_account_id,
                body=text,
                received_at=received_at,
                metadata=metadata,
            )

            try:
                result_msg = receive_instagram_message(db, parsed)
                results.append({
                    "message_id": str(result_msg.id),
                    "status": "received",
                })
            except Exception as exc:
                logger.exception(
                    "instagram_webhook_processing_failed ig_account_id=%s error=%s",
                    ig_account_id,
                    exc,
                )
                results.append({
                    "message_id": None,
                    "status": "failed",
                    "error": str(exc),
                })

    return results


def _parse_webhook_timestamp(value) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str):
            candidate = value.strip()
            if candidate.endswith("Z"):
                candidate = candidate.replace("Z", "+00:00")
            if candidate.endswith("+0000"):
                candidate = candidate[:-5] + "+00:00"
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return None


def _process_facebook_comment_changes(
    db: Session,
    entry,
) -> list[dict]:
    results = []
    for change in entry.changes or []:
        value = change.get("value") or {}
        if change.get("field") != "feed":
            continue
        if value.get("item") != "comment":
            continue
        sender_id = value.get("sender_id") or value.get("from_id")
        if sender_id and sender_id == entry.id:
            logger.info(
                "facebook_comment_skip_self page_id=%s sender_id=%s",
                entry.id,
                sender_id,
            )
            continue
        try:
            payload = FacebookCommentPayload(
                post_id=value.get("post_id") or "",
                comment_id=value.get("comment_id") or "",
                parent_id=value.get("parent_id"),
                from_id=value.get("sender_id") or "",
                from_name=value.get("sender_name"),
                message=value.get("message") or "",
                created_time=_parse_webhook_timestamp(value.get("created_time"))
                or datetime.now(timezone.utc),
                page_id=entry.id,
            )
        except Exception as exc:
            logger.info("facebook_comment_payload_invalid %s", exc)
            continue

        try:
            if payload.parent_id and payload.parent_id != payload.post_id:
                reply = comments_service.upsert_social_comment_reply(
                    db=db,
                    platform=SocialCommentPlatform.facebook,
                    parent_external_id=payload.parent_id,
                    external_id=payload.comment_id,
                    message=payload.message,
                    created_time=payload.created_time,
                    raw_payload=value,
                )
                status = "stored" if reply else "skipped"
                results.append({"comment_id": payload.comment_id, "status": status})
            else:
                comments_service.upsert_social_comment(
                    db=db,
                    platform=SocialCommentPlatform.facebook,
                    external_id=payload.comment_id,
                    external_post_id=payload.post_id,
                    source_account_id=payload.page_id,
                    author_id=payload.from_id,
                    author_name=payload.from_name,
                    message=payload.message,
                    created_time=payload.created_time,
                    permalink_url=None,
                    raw_payload=value,
                )
                results.append({"comment_id": payload.comment_id, "status": "stored"})
        except Exception as exc:
            logger.info("facebook_comment_store_failed %s", exc)
            results.append({"comment_id": payload.comment_id, "status": "failed"})
    return results


def _process_instagram_comment_changes(
    db: Session,
    entry,
) -> list[dict]:
    results = []
    for change in entry.changes or []:
        value = change.get("value") or {}
        if change.get("field") != "comments":
            continue
        sender_id = (value.get("from") or {}).get("id")
        if sender_id and sender_id == entry.id:
            logger.info(
                "instagram_comment_skip_self ig_account_id=%s sender_id=%s",
                entry.id,
                sender_id,
            )
            continue
        try:
            payload = InstagramCommentPayload(
                media_id=value.get("media_id") or "",
                comment_id=value.get("comment_id") or value.get("id") or "",
                from_id=(value.get("from") or {}).get("id") or "",
                from_username=(value.get("from") or {}).get("username"),
                text=value.get("text") or "",
                timestamp=_parse_webhook_timestamp(value.get("timestamp"))
                or datetime.now(timezone.utc),
                instagram_account_id=entry.id,
            )
        except Exception as exc:
            logger.info("instagram_comment_payload_invalid %s", exc)
            continue

        try:
            parent_id = value.get("parent_id")
            if parent_id:
                reply = comments_service.upsert_social_comment_reply(
                    db=db,
                    platform=SocialCommentPlatform.instagram,
                    parent_external_id=parent_id,
                    external_id=payload.comment_id,
                    message=payload.text,
                    created_time=payload.timestamp,
                    raw_payload=value,
                )
                status = "stored" if reply else "skipped"
                results.append({"comment_id": payload.comment_id, "status": status})
            else:
                comments_service.upsert_social_comment(
                    db=db,
                    platform=SocialCommentPlatform.instagram,
                    external_id=payload.comment_id,
                    external_post_id=payload.media_id,
                    source_account_id=payload.instagram_account_id,
                    author_id=payload.from_id,
                    author_name=payload.from_username,
                    message=payload.text,
                    created_time=payload.timestamp,
                    permalink_url=None,
                    raw_payload=value,
                )
                results.append({"comment_id": payload.comment_id, "status": "stored"})
        except Exception as exc:
            logger.info("instagram_comment_store_failed %s", exc)
            results.append({"comment_id": payload.comment_id, "status": "failed"})
    return results


def receive_facebook_message(
    db: Session,
    payload: FacebookMessengerWebhookPayload,
):
    """Process an inbound Facebook Messenger message.

    Creates or updates contact, conversation, and message records.

    Args:
        db: Database session
        payload: Parsed Facebook Messenger webhook payload

    Returns:
        Message record
    """
    received_at = payload.received_at or datetime.now(timezone.utc)

    # Find Meta connector/target
    target, config = _resolve_meta_connector(db, ConnectorType.facebook)

    # Create/get contact with Facebook Messenger channel
    contact, channel = _resolve_meta_subscriber_and_channel(
        db,
        ChannelType.facebook_messenger,
        payload.contact_address,
        payload.contact_name,
        payload.metadata,
    )

    external_id = payload.message_id
    if not external_id:
        external_id = inbox_service._build_inbound_dedupe_id(
            ChannelType.facebook_messenger,
            payload.contact_address,
            None,
            payload.body,
            payload.received_at,
            source_id=payload.page_id,
        )

    # Check for duplicate message
    existing = inbox_service._find_duplicate_inbound_message(
        db,
        ChannelType.facebook_messenger,
        channel.id,
        target.id if target else None,
        external_id,
        None,  # No subject for Messenger
        payload.body,
        received_at,
        dedupe_across_targets=True,
    )
    if existing:
        logger.debug(
            "duplicate_messenger_message message_id=%s",
            external_id,
        )
        return existing

    # Resolve or create conversation
    conversation = conversation_service.resolve_open_conversation_for_channel(
        db,
        str(contact.id),
        ChannelType.facebook_messenger,
    )
    if not conversation:
        conversation = conversation_service.Conversations.create(
            db,
            ConversationCreate(
                subscriber_id=contact.id,
                is_active=True,
            ),
        )
    elif not conversation.is_active:
        conversation.is_active = True
        conversation.status = ConversationStatus.open
        db.commit()
        db.refresh(conversation)

    metadata = dict(payload.metadata or {})
    metadata["page_id"] = payload.page_id
    external_ref = _normalize_external_ref(metadata.get("provider_message_id"))

    # Create message
    message = conversation_service.Messages.create(
        db,
            MessageCreate(
            conversation_id=conversation.id,
            subscriber_channel_id=channel.id,
            channel_target_id=target.id if target else None,
            channel_type=ChannelType.facebook_messenger,
            direction=MessageDirection.inbound,
            status=MessageStatus.received,
            body=payload.body,
            external_id=external_id,
            external_ref=external_ref,
            received_at=received_at,
            metadata_=metadata,
        ),
    )

    logger.info(
        "received_facebook_message contact_id=%s message_id=%s page_id=%s",
        contact.id,
        message.id,
        payload.page_id,
    )

    return message


def receive_instagram_message(
    db: Session,
    payload: InstagramDMWebhookPayload,
):
    """Process an inbound Instagram DM.

    Creates or updates contact, conversation, and message records.

    Args:
        db: Database session
        payload: Parsed Instagram DM webhook payload

    Returns:
        Message record
    """
    received_at = payload.received_at or datetime.now(timezone.utc)
    body = payload.body if payload.body is not None else "(story mention)"

    # Find Meta connector/target (Instagram uses same connector as Facebook)
    target, config = _resolve_meta_connector(db, ConnectorType.facebook)

    # Create/get contact with Instagram DM channel
    contact, channel = _resolve_meta_subscriber_and_channel(
        db,
        ChannelType.instagram_dm,
        payload.contact_address,
        payload.contact_name,
        payload.metadata,
    )

    external_id = payload.message_id
    if not external_id:
        external_id = inbox_service._build_inbound_dedupe_id(
            ChannelType.instagram_dm,
            payload.contact_address,
            None,
            body,
            payload.received_at,
            source_id=payload.instagram_account_id,
        )

    # Check for duplicate message
    existing = inbox_service._find_duplicate_inbound_message(
        db,
        ChannelType.instagram_dm,
        channel.id,
        target.id if target else None,
        external_id,
        None,
        body,
        received_at,
        dedupe_across_targets=True,
    )
    if existing:
        logger.debug(
            "duplicate_instagram_message message_id=%s",
            external_id,
        )
        return existing

    # Resolve or create conversation
    conversation = conversation_service.resolve_open_conversation_for_channel(
        db,
        str(contact.id),
        ChannelType.instagram_dm,
    )
    if not conversation:
        conversation = conversation_service.Conversations.create(
            db,
            ConversationCreate(
                subscriber_id=contact.id,
                is_active=True,
            ),
        )
    elif not conversation.is_active:
        conversation.is_active = True
        conversation.status = ConversationStatus.open
        db.commit()
        db.refresh(conversation)

    metadata = dict(payload.metadata or {})
    metadata["instagram_account_id"] = payload.instagram_account_id
    external_ref = _normalize_external_ref(metadata.get("provider_message_id"))

    # Create message
    message = conversation_service.Messages.create(
        db,
            MessageCreate(
            conversation_id=conversation.id,
            subscriber_channel_id=channel.id,
            channel_target_id=target.id if target else None,
            channel_type=ChannelType.instagram_dm,
            direction=MessageDirection.inbound,
            status=MessageStatus.received,
            body=body,
            external_id=external_id,
            external_ref=external_ref,
            received_at=received_at,
            metadata_=metadata,
        ),
    )

    logger.info(
        "received_instagram_message contact_id=%s message_id=%s ig_account_id=%s",
        contact.id,
        message.id,
        payload.instagram_account_id,
    )

    return message
