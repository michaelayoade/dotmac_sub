from __future__ import annotations

import pickle

from app.services.domain_errors import DomainError


class ExampleDomainError(DomainError):
    pass


def test_domain_error_round_trips_through_pickle() -> None:
    original = ExampleDomainError(
        code="inventory.changed",
        message="Inventory changed during collection.",
        details={"device_count": 3},
        retryable=False,
    )

    restored = pickle.loads(pickle.dumps(original))  # noqa: S301

    assert isinstance(restored, ExampleDomainError)
    assert restored.code == original.code
    assert restored.message == original.message
    assert restored.details == original.details
    assert restored.retryable is False
    assert str(restored) == original.message
