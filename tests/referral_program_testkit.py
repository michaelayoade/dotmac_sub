"""Typed referral-program command helpers for focused tests."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.referral_native import Referral, ReferralCode
from app.models.subscriber import Subscriber
from app.services import referrals as referral_program
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def _context(reason: str, *, idempotency_key: str | None = None) -> CommandContext:
    return CommandContext.system(
        actor="test_referral_program",
        scope=referral_program.REFERRAL_PROGRAM_SCOPE,
        reason=reason,
        idempotency_key=idempotency_key,
    )


def ensure_code(db: Session, subscriber_id: str | UUID) -> ReferralCode:
    resolved = UUID(str(subscriber_id))
    db_session_adapter.release_read_transaction(db)
    result = referral_program.ensure_referral_code(
        db,
        referral_program.EnsureReferralCodeCommand(
            context=_context(
                "Test requested a referral code",
                idempotency_key=f"referral-code:{resolved}",
            ),
            subscriber_id=resolved,
        ),
    )
    return referral_program.referrals.get_code(db, result.referral_code_id)


def capture(db: Session, **values: object) -> Referral:
    db_session_adapter.release_read_transaction(db)
    result = referral_program.capture_referral(
        db,
        referral_program.CaptureReferralCommand(
            context=_context("Test submitted a referral capture"),
            **values,
        ),
    )
    return referral_program.referrals.get(db, result.referral_id)


def qualify_for_subscriber(
    db: Session, subscriber: Subscriber | UUID | str
) -> Referral | None:
    subscriber_id = (
        subscriber.id if isinstance(subscriber, Subscriber) else UUID(str(subscriber))
    )
    db_session_adapter.release_read_transaction(db)
    result = referral_program.qualify_referral_for_subscriber(
        db,
        referral_program.QualifyReferralForSubscriberCommand(
            context=_context("Test observed Subscriber activation"),
            subscriber_id=subscriber_id,
        ),
    )
    if result.referral_id is None or result.outcome in {
        "not_applicable",
        "already_qualified",
    }:
        return None
    return referral_program.referrals.get(db, result.referral_id)


def qualify_override(db: Session, referral_id: str | UUID) -> Referral:
    resolved = UUID(str(referral_id))
    db_session_adapter.release_read_transaction(db)
    referral_program.qualify_referral_override(
        db,
        referral_program.QualifyReferralOverrideCommand(
            context=_context(
                "Test operator overrode referral qualification",
                idempotency_key=f"referral-qualify-override:{resolved}",
            ),
            referral_id=resolved,
        ),
    )
    return referral_program.referrals.get(db, resolved)


def issue_reward(db: Session, referral_id: str | UUID) -> Referral:
    resolved = UUID(str(referral_id))
    db_session_adapter.release_read_transaction(db)
    referral_program.issue_referral_reward(
        db,
        referral_program.IssueReferralRewardCommand(
            context=_context(
                "Test requested referral reward issuance",
                idempotency_key=f"referral-reward:{resolved}",
            ),
            referral_id=resolved,
        ),
    )
    return referral_program.referrals.get(db, resolved)


def reject(db: Session, referral_id: str | UUID, reason: str) -> Referral:
    resolved = UUID(str(referral_id))
    db_session_adapter.release_read_transaction(db)
    referral_program.reject_referral(
        db,
        referral_program.RejectReferralCommand(
            context=_context(
                "Test rejected the referral",
                idempotency_key=f"referral-reject:{resolved}",
            ),
            referral_id=resolved,
            reason=reason,
        ),
    )
    return referral_program.referrals.get(db, resolved)


def refer_friend(
    db: Session,
    subscriber_id: str | UUID,
    **values: object,
) -> dict[str, str]:
    resolved = UUID(str(subscriber_id))
    db_session_adapter.release_read_transaction(db)
    result = referral_program.refer_friend(
        db,
        referral_program.ReferFriendCommand(
            context=_context("Test submitted a Refer & Earn prospect"),
            referrer_subscriber_id=resolved,
            **values,
        ),
    )
    return {
        "id": str(result.referral_id),
        "status": result.status,
        "message": "Referral submitted",
    }
