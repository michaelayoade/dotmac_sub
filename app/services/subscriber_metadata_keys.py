"""The closed set of keys `subscribers.metadata` may hold, and who owns each.

`subscribers.metadata` is a JSONB column with no schema. Until 2026-08-22 an
admin form field accepted arbitrary JSON and wrote it wholesale, so any caller
could invent any key on any subscriber. This module is what closed that: the
column's key space is now **enumerated**, and a key absent from it is refused
rather than accepted, ignored or stripped.

## Refused, not sanitised

Silently dropping an unknown key would be worse than accepting it. The caller
believes the write succeeded, the value is gone, and the failure surfaces later
as absent data with no error attached to the moment it was lost. A refusal
names the key and the fact that nothing owns it.

## This registry is a ratchet, not a schema

Every entry here is a fact stored in an unowned JSONB column, and the target is
an empty registry. Each key's `owner` is where that fact is going, not a claim
that it has arrived — most still live in this column today. Retiring a key means
deleting its entry in the same change that moves it, which is what makes the
progress countable.

Adding an entry is therefore not a routine act. A new fact belongs in a typed
column owned by a service; this registry exists to let the EXISTING ones be
refused-by-default while they are moved, not to make the column extensible
again with extra steps.

The full classification — authoritative state, derived projection, observation,
integration payload, obsolete — is in
`docs/SUBSCRIBER_METADATA_OWNERSHIP.md`. It is deliberately not duplicated here:
this module answers "may this key be written", and that document answers "why
does this key exist and where is it going".
"""

from __future__ import annotations

from typing import Final

#: Every key the column may hold, mapped to the service that owns the fact.
#: Measured by `scripts/architecture/subscriber_metadata_census.py`, which the
#: architecture guard keeps in step with this registry in both directions.
DECLARED_METADATA_KEYS: Final[dict[str, str]] = {
    # --- account deletion and recovery -----------------------------------
    # Two lineages recording the same event, with no rule about which wins.
    # Both move to `customer.account_lifecycle`; see that extraction's
    # inventory at docs/designs/SUBSCRIBER_ACCOUNT_LIFECYCLE_SOURCES.md.
    "account_deletion_requested_at": "customer.account_lifecycle",
    "account_deletion_reason": "customer.account_lifecycle",
    "recovery_deleted_at": "customer.account_lifecycle",
    "recovery_deleted_by": "customer.account_lifecycle",
    "recovery_purge_due_at": "customer.account_lifecycle",
    "recovery_purged_at": "customer.account_lifecycle",
    "recovery_last_restored_at": "customer.account_lifecycle",
    "recovery_last_restored_by": "customer.account_lifecycle",
    # A serialised copy of subscriptions, service orders and CPE devices, on
    # the row whose deletion it describes. It is not re-homed to another JSON
    # column — it is replaced by typed references and versions.
    "recovery_snapshot": "customer.account_lifecycle",
    # --- service restriction ---------------------------------------------
    "restricted_since": "access.subscription_lifecycle",
    "restricted_status": "access.subscription_lifecycle",
    "last_restricted_status": "access.subscription_lifecycle",
    "last_restricted_ended_at": "access.subscription_lifecycle",
    # --- NIN verification ------------------------------------------------
    # A projection of `subscriber_nin_verifications`. Read-only for display:
    # `web_customer_actions` currently DECIDES from it, refusing an edit to
    # the authoritative `nin` column, which is the coupling to remove.
    "nin_verified": "customer.nin_verification",
    "nin_last_checked_at": "customer.nin_verification",
    # --- portal notifications --------------------------------------------
    # An unbounded, unindexed list rewritten in full on every read receipt.
    "portal_read_notification_keys": "customer.portal_notifications",
    # --- notification preferences ----------------------------------------
    "account_notifications": "customer.notification_policy",
    "billing_notifications": "customer.notification_policy",
    "general_notifications": "customer.notification_policy",
    "push_notifications": "customer.notification_policy",
    "service_notifications": "customer.notification_policy",
    "usage_notifications": "customer.notification_policy",
    "sms_updates": "customer.notification_policy",
    # Written beside `billing_notifications` by the same module: two keys,
    # one preference.
    "send_billing_notifications": "customer.notification_policy",
    # --- observations ------------------------------------------------------
    "geocode_attempted_at": "gis.spatial_sync",
    "crm_customer_name_remediation_digest": "customer.name_remediation",
    # --- integration provenance -------------------------------------------
    # Someone else's data, frozen. Nothing writes these; they are read for
    # provenance only and carry no decision a migration must reproduce.
    "splynx_date_add": "integration.splynx_import",
    "splynx_last_update": "integration.splynx_import",
    "splynx_deleted": "integration.splynx_import",
    "splynx_status": "integration.splynx_import",
    "crm_person_id": "party.registry",
    # --- obsolete: duplicates of facts that already have a typed home ------
    # A JSON copy of `Subscriber.category`, a real column on the same row,
    # read by nine modules.
    "subscriber_category": "OBSOLETE: read Subscriber.category",
    # A fourth place an address coordinate lives, after Address.latitude,
    # Address.longitude, Address.geom and GeoLocation.
    "latitude": "OBSOLETE: gis.spatial_sync owns coordinates",
    "longitude": "OBSOLETE: gis.spatial_sync owns coordinates",
}


class UndeclaredMetadataKeyError(ValueError):
    """A caller tried to write a `subscribers.metadata` key nothing owns."""

    def __init__(self, keys: frozenset[str]) -> None:
        self.keys = keys
        listed = ", ".join(sorted(keys))
        super().__init__(
            f"`subscribers.metadata` has no owner for {listed}. The column's "
            "key space is closed: every key is declared in "
            "app/services/subscriber_metadata_keys.py with the service that "
            "owns the fact. A new fact belongs in a typed column owned by a "
            "service, not in this column — see "
            "docs/SUBSCRIBER_METADATA_OWNERSHIP.md."
        )


def undeclared_keys(metadata: object) -> frozenset[str]:
    """Keys in a proposed metadata value that no owner declares."""

    if not isinstance(metadata, dict):
        return frozenset()
    return frozenset(
        str(key) for key in metadata if str(key) not in DECLARED_METADATA_KEYS
    )


def reject_undeclared_keys(metadata: object) -> None:
    """Raise if a proposed metadata value carries a key nothing owns.

    Called by `customer.accounts` — the column's declared owner — on every
    write path that accepts a whole metadata value, so the refusal happens once
    at the owner rather than once per caller.
    """

    offending = undeclared_keys(metadata)
    if offending:
        raise UndeclaredMetadataKeyError(offending)
