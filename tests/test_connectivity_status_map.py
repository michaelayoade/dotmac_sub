"""Single canonical status→connectivity classification (review task #13).

The blocked/terminated status sets had been copied (and had drifted) across
radius_access_state, radius_reject and radius_reconciliation. They now share
one source of truth, and the suspension audit targets blocked + terminated so
`disabled`/`canceled`/`expired` leaks are no longer under-reported.
"""

from __future__ import annotations

from app.models.catalog import SubscriptionStatus
from app.services import radius_access_state as ras


def test_classification_is_exhaustive_and_disjoint():
    sets = [
        ras.ACTIVE_STATUSES,
        ras.BLOCKED_STATUSES,
        ras.TERMINATED_STATUSES,
        ras.UNPROVISIONED_STATUSES,
    ]
    union = set().union(*sets)
    assert union == set(SubscriptionStatus)  # exhaustive
    total = sum(len(s) for s in sets)
    assert total == len(union)  # disjoint (no status double-classified)


def test_no_access_set_includes_stopped_and_disabled():
    """The drift that let stopped/disabled keep access: both must now be in the
    canonical no-access set."""
    assert SubscriptionStatus.stopped in ras.NO_ACCESS_STATUSES
    assert SubscriptionStatus.disabled in ras.NO_ACCESS_STATUSES
    assert ras.NO_ACCESS_STATUSES == (ras.BLOCKED_STATUSES | ras.TERMINATED_STATUSES)


def test_dependent_modules_reference_canonical_set():
    from app.services import radius_reject
    from app.services.radius_reconciliation import _NO_ACCESS_STATUSES

    assert _NO_ACCESS_STATUSES == ras.NO_ACCESS_STATUSES
    assert radius_reject._STATUS_BLOCKED == ras.NO_ACCESS_STATUSES
