"""Name-pattern classifier for the plan_family backfill."""

from scripts.one_off.backfill_plan_families import classify


def test_classify_families():
    assert classify("Unlimited Platinum Plus") == "unlimited"
    assert classify("Homeflex Elite") == "home_flex"
    assert classify("Home Flex Basic") == "home_flex"
    assert classify("45 Mbps Dedicated") == "dedicated"
    assert classify("675 Mbps Dedicated") == "dedicated"
    assert classify("Random Promo") is None
    assert classify("") is None
