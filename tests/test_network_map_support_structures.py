"""Network-map fiber support-structure canvas layer."""

from app.services.network_map import build_network_map_context


def test_map_context_includes_support_structures_stat(db_session):
    ctx = build_network_map_context(db_session)
    # the fiber support-structure layer contributes a stat + a feature type
    assert "support_structures" in ctx["stats"]
    assert ctx["stats"]["support_structures"] == 0
    types = {f["properties"]["type"] for f in ctx["map_data"]["features"]}
    assert "support_structure" not in types  # none seeded


def test_map_context_stats_has_all_fiber_layers(db_session):
    stats = build_network_map_context(db_session)["stats"]
    for key in (
        "fdh_cabinets",
        "splice_closures",
        "access_points",
        "support_structures",
    ):
        assert key in stats
