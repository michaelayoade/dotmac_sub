"""Concrete ``PppoeCredentialProvider`` backed by ``AccessCredential``.

This bridge lives OUTSIDE ``app.services.network`` so it is free to
import from the subscription/catalog domain without violating the
import-linter contract. Network services depend only on the
:class:`~app.services.network._credentials.PppoeCredentialProvider`
Protocol; this adapter wires the catalog ORM model up to that Protocol
at the composition root.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.catalog import AccessCredential
from app.services.network._credentials import (
    PppoeCredential,
    PppoeCredentialProvider,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _to_dto(row: AccessCredential) -> PppoeCredential:
    return PppoeCredential(
        subscriber_id=row.subscriber_id,
        username=row.username,
        secret_hash=row.secret_hash,
        is_active=row.is_active,
    )


class AccessCredentialAdapter(PppoeCredentialProvider):
    """``AccessCredential``-backed implementation of the provider protocol."""

    def __init__(self, db: Session):
        self._db = db

    def get_by_username(self, username: str) -> PppoeCredential | None:
        stmt = select(AccessCredential).where(
            AccessCredential.username == username,
            AccessCredential.is_active.is_(True),
        )
        row = self._db.scalars(stmt).first()
        return _to_dto(row) if row is not None else None

    def get_by_subscriber_id(self, subscriber_id: UUID) -> PppoeCredential | None:
        stmt = select(AccessCredential).where(
            AccessCredential.subscriber_id == subscriber_id,
            AccessCredential.is_active.is_(True),
        )
        row = self._db.scalars(stmt).first()
        return _to_dto(row) if row is not None else None

    def get_active_by_subscriber_ids(
        self, subscriber_ids: Iterable[UUID]
    ) -> dict[UUID, PppoeCredential]:
        ids = list(subscriber_ids)
        if not ids:
            return {}
        stmt = select(AccessCredential).where(
            AccessCredential.subscriber_id.in_(ids),
            AccessCredential.is_active.is_(True),
        )
        result: dict[UUID, PppoeCredential] = {}
        for row in self._db.scalars(stmt):
            # If a subscriber has multiple active credentials, keep the
            # first one encountered — matches the behaviour of the
            # previous SQL join (which non-deterministically picked one
            # via outerjoin before aggregation).
            if row.subscriber_id not in result:
                result[row.subscriber_id] = _to_dto(row)
        return result
