"""The package profile is projected onto the subscription; credentials are not touched.

``apply_offer_radius_profile`` is the one writer of ``Subscription.radius_profile_id``
and it mirrors the package link. It used to guess from the previous offer whether
each credential "inherited" the old profile and rewrite every credential of the
subscriber, so a plan change on one service rewrote another service's credential
and a stale imported profile stayed forever. It now writes the subscription only.
"""

from __future__ import annotations

from app.services import access_rate_profile as owner
from app.services.catalog.subscriptions import apply_offer_radius_profile
from tests.test_access_rate_profile import _account, _offer, _profile, _service


def test_sets_the_package_profile_for_a_subscription_without_one(db_session):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer)

    resolved = apply_offer_radius_profile(db_session, sub)

    assert resolved == ten.id
    assert sub.radius_profile_id == ten.id
    assert credential.radius_profile_id is None


def test_a_plan_change_projects_the_new_package_and_rewrites_no_credential(db_session):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    twenty = _profile(db_session, "Twenty", rate="20M/20M")
    fifteen = _profile(db_session, "Fifteen", rate="15M/15M")
    offer_a = _offer(db_session, "A", profile=ten)
    offer_b = _offer(db_session, "B", profile=twenty)
    # A stale imported profile in the slot: the old heuristic left it forever.
    sub, credential = _service(db_session, offer_a, sub_profile=ten, slot=fifteen)

    sub.offer_id = offer_b.id
    apply_offer_radius_profile(db_session, sub)
    db_session.commit()

    assert sub.radius_profile_id == twenty.id
    assert credential.radius_profile_id == fifteen.id  # untouched
    assert owner.resolve_effective_rate(db_session, sub, credential).rate_limit == (
        "20M/20M"
    )


def test_a_plan_change_on_one_service_leaves_the_other_service_alone(db_session):
    """Two services on one account: changing service 1 must not touch service 2."""
    ten = _profile(db_session, "Ten", rate="10M/10M")
    twenty = _profile(db_session, "Twenty", rate="20M/20M")
    offer_a = _offer(db_session, "A", profile=ten)
    offer_b = _offer(db_session, "B", profile=twenty)
    account = _account(db_session)
    first, first_cred = _service(db_session, offer_a, sub_profile=ten, account=account)
    second, second_cred = _service(
        db_session, offer_a, sub_profile=ten, slot=ten, account=account
    )

    first.offer_id = offer_b.id
    apply_offer_radius_profile(db_session, first)
    db_session.commit()

    assert first.radius_profile_id == twenty.id
    assert second.radius_profile_id == ten.id
    assert second_cred.radius_profile_id == ten.id  # not rewritten
    assert first_cred.radius_profile_id is None
    assert owner.resolve_effective_rate(db_session, second, second_cred).rate_limit == (
        "10M/10M"
    )
    assert owner.resolve_effective_rate(db_session, first, first_cred).rate_limit == (
        "20M/20M"
    )


def test_an_ambiguous_package_uses_the_requested_link_when_it_is_one(db_session):
    from app.models.catalog import OfferRadiusProfile

    ten = _profile(db_session, "Ten", rate="10M/10M")
    twelve = _profile(db_session, "Twelve", rate="12M/12M")
    offer = _offer(db_session, "A", profile=ten)
    db_session.add(OfferRadiusProfile(offer_id=offer.id, profile_id=twelve.id))
    db_session.commit()
    sub, _ = _service(db_session, offer)

    assert apply_offer_radius_profile(db_session, sub, target_profile_id=twelve.id) == (
        twelve.id
    )
    # A value that is not one of the links is ignored, never stored.
    other = _profile(db_session, "Other", rate="3M/3M")
    resolved = apply_offer_radius_profile(db_session, sub, target_profile_id=other.id)
    assert resolved in {ten.id, twelve.id}


def test_no_package_link_clears_the_projection(db_session):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    offer = _offer(db_session, "Unlinked")
    sub, _ = _service(db_session, offer, sub_profile=ten)

    assert apply_offer_radius_profile(db_session, sub) is None
    assert sub.radius_profile_id is None
