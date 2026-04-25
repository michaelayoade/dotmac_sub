"""Stub for removed legacy ONT profile linking.

After migration 064 dropped the provisioning_profile_id column from OntUnit,
this helper is no longer functional. Tests that called it will no-op.

TODO: Remove this file and update tests to use OntBundleAssignment instead.
"""
from __future__ import annotations


def seed_legacy_profile_link(ont: object, profile: object) -> None:
    """No-op stub - legacy column has been dropped."""
    pass
