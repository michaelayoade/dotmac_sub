"""Fast-unit profile owner session lifecycle; no deployed-schema acceptance.

The fixture's Connection supplies test isolation. A fresh Session gives the
canonical command its own boundary without releasing or rolling back the caller.
The Subscriber fixture is the authenticating principal in these self-care tests.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.customer_portal_profile_commands import PORTAL_PROFILE_WRITE_SCOPE
from app.services.owner_commands import CommandContext


@contextmanager
def profile_owner_session(caller: Session) -> Iterator[Session]:
    with Session(
        bind=caller.get_bind(),
        autoflush=False,
        join_transaction_mode="create_savepoint",
    ) as owner:
        yield owner


def profile_command_context(*, actor_id: UUID) -> CommandContext:
    return CommandContext.system(
        actor=str(actor_id),
        scope=PORTAL_PROFILE_WRITE_SCOPE,
        reason="Authenticated self-care profile unit test",
    )
