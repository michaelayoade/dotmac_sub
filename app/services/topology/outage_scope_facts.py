"""Pure immutable audience interpretation shared by lifecycle and evidence owners."""

from app.models.network_monitoring import OutageScopeRevision


def revision_audience_subscription_ids(revision: OutageScopeRevision) -> set[str]:
    return {
        str(member.subscription_id)
        for member in revision.members
        if member.membership in ("entered", "retained")
    }
