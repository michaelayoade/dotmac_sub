"""Authenticated typed command fixture for subscriber enrollment tests."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.auth import Session as AuthSession
from app.services import password_authentication as owner
from app.services.owner_commands import CommandContext


@dataclass(frozen=True, slots=True)
class SubscriberMfaEnrollment:
    db: Session = field(repr=False)
    subscriber_id: UUID
    proof: owner.SubscriberMfaSessionProof

    def _context(self) -> CommandContext:
        return CommandContext.system(
            actor=f"subscriber:{self.subscriber_id}",
            scope=owner.SUBSCRIBER_MFA_SCOPE,
            reason="Synthetic authenticated enrollment test",
        )

    def setup(self, *, label: str = "device") -> owner.SubscriberMfaSetupOutcome:
        owner.release_read_transaction(self.db)
        return owner.setup_subscriber_mfa(
            self.db,
            command=owner.SetupSubscriberMfaCommand(
                context=self._context(),
                subscriber_id=self.subscriber_id,
                label=label,
                provenance=owner.SubscriberMfaProvenance.AUTH_API,
                session=self.proof,
            ),
        )

    def confirm(
        self, *, method_id: UUID, code: str, subscriber_id: UUID | None = None
    ) -> owner.SubscriberMfaMethodView:
        owner.release_read_transaction(self.db)
        return owner.confirm_subscriber_mfa(
            self.db,
            command=owner.ConfirmSubscriberMfaCommand(
                context=self._context(),
                subscriber_id=subscriber_id or self.subscriber_id,
                method_id=method_id,
                code=code,
                provenance=owner.SubscriberMfaProvenance.AUTH_API,
                session=self.proof,
                recovery_delivery=owner.RecoveryCodeDelivery.NONE,
            ),
        ).method


def authenticated_subscriber_enrollment(
    db: Session, *, subscriber_id: UUID
) -> SubscriberMfaEnrollment:
    session = AuthSession(
        subscriber_id=subscriber_id,
        token_hash=uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(session)
    db.commit()
    proof = owner.SubscriberMfaSessionProof(session.id, subscriber_id, False)
    owner.release_read_transaction(db)
    return SubscriberMfaEnrollment(db, subscriber_id, proof)
