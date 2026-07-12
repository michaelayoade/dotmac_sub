"""UFiber router-mode ONU -> subscriber link reconciler.

Covers the auth-safe MAC match (ONU own-MAC == active subscription MAC),
ambiguity/no-match skips, the already-linked and Huawei (non-UISP) exclusions,
idempotency, duplicate-subscription ambiguity, and — critically — that the
pass NEVER writes ``subscriptions.mac_address``.
"""

from __future__ import annotations

import uuid

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.models.subscriber import Subscriber
from app.services.topology.ufiber_onu_link import (
    PROVENANCE,
    link_ufiber_onus_to_subscribers,
)

# UISP reports MACs colon-separated and uppercase; the sub row stores whatever
# RADIUS/import left. Deliberately different casing/separators to exercise
# normalization (both normalize to the same 12 nibbles).
ROUTER_MAC_UISP = "F0:9F:C2:AA:BB:01"
ROUTER_MAC_SUB = "f09fc2-aabb01"


def _olt(db_session, name="UF-OLT-1", vendor="ubiquiti"):
    olt = OLTDevice(name=name, vendor=vendor)
    db_session.add(olt)
    db_session.flush()
    return olt


def _onu(
    db_session,
    olt,
    *,
    mac,
    uisp_device_id="uisp-onu-1",
    serial="UFONU0001",
    name=None,
):
    onu = OntUnit(
        serial_number=serial,
        olt_device=olt,
        uisp_device_id=uisp_device_id,
        mac_address=mac,
        name=name,
        is_active=True,
    )
    db_session.add(onu)
    db_session.flush()
    return onu


def _extra_subscriber(db_session, first="Other", last="Customer"):
    sub = Subscriber(
        first_name=first,
        last_name=last,
        email=f"other-{uuid.uuid4().hex}@example.com",
    )
    db_session.add(sub)
    db_session.flush()
    return sub


def _active_subscription(db_session, subscriber, catalog_offer, mac):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        mac_address=mac,
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def _active_assignments(db_session, ont_unit_id):
    return (
        db_session.query(OntAssignment)
        .filter(
            OntAssignment.ont_unit_id == ont_unit_id,
            OntAssignment.active.is_(True),
        )
        .all()
    )


def test_router_mode_mac_match_creates_assignment(
    db_session, subscriber, catalog_offer
):
    olt = _olt(db_session)
    onu = _onu(db_session, olt, mac=ROUTER_MAC_UISP)
    sub = _active_subscription(db_session, subscriber, catalog_offer, ROUTER_MAC_SUB)
    db_session.commit()

    result = link_ufiber_onus_to_subscribers(db_session)

    assert result["candidates"] == 1
    assert result["matched_linked"] == 1
    assert result["ambiguous"] == 0
    assert result["no_match"] == 0

    assignments = _active_assignments(db_session, onu.id)
    assert len(assignments) == 1
    assignment = assignments[0]
    assert assignment.subscriber_id == subscriber.id
    assert assignment.subscription_id == sub.id
    assert assignment.service_address_id == sub.service_address_id
    assert assignment.notes == PROVENANCE
    assert assignment.active is True

    # AUTH-SAFETY: the subscription MAC (RADIUS calling-station-id) is untouched.
    db_session.refresh(sub)
    assert sub.mac_address == ROUTER_MAC_SUB


def test_pon_port_id_copied_from_onu_when_present(
    db_session, subscriber, catalog_offer
):
    from app.models.network import PonPort

    olt = _olt(db_session)
    pon = PonPort(olt=olt, name="0/1/1", port_number=1, is_active=True)
    db_session.add(pon)
    db_session.flush()
    onu = _onu(db_session, olt, mac=ROUTER_MAC_UISP)
    onu.pon_port_id = pon.id
    _active_subscription(db_session, subscriber, catalog_offer, ROUTER_MAC_SUB)
    db_session.commit()

    link_ufiber_onus_to_subscribers(db_session)

    assignment = _active_assignments(db_session, onu.id)[0]
    assert assignment.pon_port_id == pon.id


def test_ambiguous_mac_two_subscribers_creates_nothing(
    db_session, subscriber, catalog_offer
):
    olt = _olt(db_session)
    onu = _onu(db_session, olt, mac=ROUTER_MAC_UISP)
    _active_subscription(db_session, subscriber, catalog_offer, ROUTER_MAC_SUB)
    other = _extra_subscriber(db_session)
    # Same MAC, a DIFFERENT active subscriber -> genuinely ambiguous.
    _active_subscription(db_session, other, catalog_offer, ROUTER_MAC_UISP)
    db_session.commit()

    result = link_ufiber_onus_to_subscribers(db_session)

    assert result["candidates"] == 1
    assert result["ambiguous"] == 1
    assert result["matched_linked"] == 0
    assert _active_assignments(db_session, onu.id) == []


def test_ambiguous_mac_resolved_by_name_tiebreak(db_session, subscriber, catalog_offer):
    # subscriber fixture is "Test User". A second active subscriber carries the
    # SAME MAC (a stale duplicate) -> 2 distinct subscribers = ambiguous by MAC
    # alone. The ONU's UISP name resembles ONLY the real subscriber, so the
    # name tiebreak (mirrors uisp_sync's IP+name arm) resolves it.
    olt = _olt(db_session)
    onu = _onu(db_session, olt, mac=ROUTER_MAC_UISP, name="Test User")
    _active_subscription(db_session, subscriber, catalog_offer, ROUTER_MAC_SUB)
    other = _extra_subscriber(db_session, first="Zed", last="Zzyzx")
    _active_subscription(db_session, other, catalog_offer, ROUTER_MAC_UISP)
    db_session.commit()

    result = link_ufiber_onus_to_subscribers(db_session)

    assert result["candidates"] == 1
    assert result["ambiguous"] == 0
    assert result["matched_by_name_tiebreak"] == 1
    assert result["matched_linked"] == 0
    # The losing duplicate subscription is flagged for ops (never modified).
    assert result["duplicate_active_mac"] == 1

    assignments = _active_assignments(db_session, onu.id)
    assert len(assignments) == 1
    assert assignments[0].subscriber_id == subscriber.id
    assert "name tiebreak" in assignments[0].notes


def test_two_similar_names_stays_ambiguous(db_session, subscriber, catalog_offer):
    # Both candidate subscribers share the ONU's name, so the runner-up also
    # clears the similarity threshold -> the tiebreak refuses to guess and the
    # item stays ambiguous (nothing linked).
    olt = _olt(db_session)
    onu = _onu(db_session, olt, mac=ROUTER_MAC_UISP, name="Acme Networks")
    twin = _extra_subscriber(db_session, first="Acme", last="Networks")
    other = _extra_subscriber(db_session, first="Acme", last="Networks")
    _active_subscription(db_session, twin, catalog_offer, ROUTER_MAC_SUB)
    _active_subscription(db_session, other, catalog_offer, ROUTER_MAC_UISP)
    db_session.commit()

    result = link_ufiber_onus_to_subscribers(db_session)

    assert result["candidates"] == 1
    assert result["ambiguous"] == 1
    assert result["matched_by_name_tiebreak"] == 0
    assert result["matched_linked"] == 0
    assert result["duplicate_active_mac"] == 0
    assert _active_assignments(db_session, onu.id) == []


def test_duplicate_active_services_one_subscriber_stays_ambiguous(
    db_session, subscriber, catalog_offer
):
    olt = _olt(db_session)
    onu = _onu(db_session, olt, mac=ROUTER_MAC_UISP)
    # Customer identity is not service identity. Two active services carrying
    # the same MAC cannot be assigned safely without another service-level
    # signal, even when both belong to the same subscriber.
    _active_subscription(db_session, subscriber, catalog_offer, ROUTER_MAC_SUB)
    _active_subscription(db_session, subscriber, catalog_offer, ROUTER_MAC_UISP)
    db_session.commit()

    result = link_ufiber_onus_to_subscribers(db_session)

    assert result["ambiguous"] == 1
    assert result["matched_linked"] == 0
    assert _active_assignments(db_session, onu.id) == []


def test_onu_with_existing_active_assignment_skipped(
    db_session, subscriber, catalog_offer
):
    olt = _olt(db_session)
    onu = _onu(db_session, olt, mac=ROUTER_MAC_UISP)
    _active_subscription(db_session, subscriber, catalog_offer, ROUTER_MAC_SUB)
    # Pre-existing active assignment (fill-null-only: never overwrite it).
    existing = OntAssignment(
        ont_unit_id=onu.id, subscriber_id=subscriber.id, active=True
    )
    db_session.add(existing)
    db_session.commit()

    result = link_ufiber_onus_to_subscribers(db_session)

    # Excluded from candidates entirely — nothing to link.
    assert result["candidates"] == 0
    assert result["matched_linked"] == 0
    assert len(_active_assignments(db_session, onu.id)) == 1


def test_huawei_non_uisp_ont_never_touched(db_session, subscriber, catalog_offer):
    olt = _olt(db_session, name="Huawei-OLT", vendor="Huawei")
    # Huawei ONT: uisp_device_id is NULL -> out of scope, even with a MAC match.
    onu = _onu(db_session, olt, mac=ROUTER_MAC_UISP, uisp_device_id=None)
    _active_subscription(db_session, subscriber, catalog_offer, ROUTER_MAC_SUB)
    db_session.commit()

    result = link_ufiber_onus_to_subscribers(db_session)

    assert result["candidates"] == 0
    assert result["matched_linked"] == 0
    assert _active_assignments(db_session, onu.id) == []


def test_no_active_subscription_match_is_no_match(
    db_session, subscriber, catalog_offer
):
    olt = _olt(db_session)
    onu = _onu(db_session, olt, mac=ROUTER_MAC_UISP)
    # Bridge-mode UF-Nano analogue: the ONU MAC matches no subscription MAC.
    _active_subscription(db_session, subscriber, catalog_offer, "00:11:22:33:44:55")
    db_session.commit()

    result = link_ufiber_onus_to_subscribers(db_session)

    assert result["candidates"] == 1
    assert result["no_match"] == 1
    assert result["matched_linked"] == 0
    assert _active_assignments(db_session, onu.id) == []


def test_inactive_subscription_mac_does_not_match(
    db_session, subscriber, catalog_offer
):
    olt = _olt(db_session)
    onu = _onu(db_session, olt, mac=ROUTER_MAC_UISP)
    sub = _active_subscription(db_session, subscriber, catalog_offer, ROUTER_MAC_SUB)
    sub.status = SubscriptionStatus.blocked
    db_session.commit()

    result = link_ufiber_onus_to_subscribers(db_session)

    assert result["no_match"] == 1
    assert result["matched_linked"] == 0


def test_idempotent_rerun_creates_no_duplicate(db_session, subscriber, catalog_offer):
    olt = _olt(db_session)
    onu = _onu(db_session, olt, mac=ROUTER_MAC_UISP)
    _active_subscription(db_session, subscriber, catalog_offer, ROUTER_MAC_SUB)
    db_session.commit()

    first = link_ufiber_onus_to_subscribers(db_session)
    db_session.commit()
    second = link_ufiber_onus_to_subscribers(db_session)
    db_session.commit()

    assert first["matched_linked"] == 1
    # Second pass: the ONU now has an active assignment, so it is no longer a
    # candidate -> nothing created.
    assert second["candidates"] == 0
    assert second["matched_linked"] == 0
    assert len(_active_assignments(db_session, onu.id)) == 1
