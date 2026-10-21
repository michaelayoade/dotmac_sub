"""Kernel conflict-savepoint participation in Sub owner commands.

Starter kernel services run expected-conflict mutations inside
``dotmac_kernel.transactions.conflict_savepoint``. Its RELEASE is a
savepoint commit, which fires the owner boundary's ``before_commit``
guard, and a recovered expected-conflict rollback fires its
``after_soft_rollback`` recorder. This module registers the kernel's
exact claim/classification contract with the (kernel-free) owner
boundary: the claim consumes permission for one exact RELEASE against
the command's stored root, and the classifier names only rollbacks of
the kernel savepoint itself (or its failed-flush child). A failed
kernel recovery latches the kernel's own failed-root instead and
still fails closed at completion.

First required by the gateway prepaid lane, where Starter money
services execute inside ``execute_owner_savepoint`` (purchase
staging); the gap was latent for every Starter lane under an owner
command.
"""

from __future__ import annotations

from dotmac_kernel.transactions import (
    claim_conflict_savepoint_commit,
    current_conflict_savepoint,
)
from sqlalchemy.orm import Session, SessionTransaction

from app.services.owner_commands import register_savepoint_participant_hooks


def _claim_release(session: Session, root: SessionTransaction) -> bool:
    return claim_conflict_savepoint_commit(session, expected_root=root)


def _is_recovered_rollback(
    session: Session, previous_transaction: SessionTransaction
) -> bool:
    state = current_conflict_savepoint(session)
    if state is None:
        return False
    return previous_transaction is state.savepoint or (
        previous_transaction.parent is state.savepoint
        and not previous_transaction.nested
    )


def install() -> None:
    """Register the kernel claim/classifiers with the owner boundary."""
    register_savepoint_participant_hooks(
        claim_release=_claim_release,
        is_recovered_rollback=_is_recovered_rollback,
    )
