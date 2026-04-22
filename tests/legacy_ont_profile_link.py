from __future__ import annotations


def seed_legacy_profile_link(ont: object, profile: object) -> None:
    """Mark a test ONT as legacy-linked to a provisioning profile.

    This helper exists to keep deliberate pre-migration coverage explicit while
    the runtime system has already cut over to bundle assignments.
    """
    setattr(ont, "provisioning_profile_id", getattr(profile, "id", profile))
