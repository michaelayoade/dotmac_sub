from app.services.dual_stack_canary import _radius_pd_values


def test_radius_pd_values_uses_only_available_sources():
    rows = [
        {
            "available": True,
            "radreply": [
                {
                    "attribute": "Delegated-IPv6-Prefix",
                    "value": "2001:db8:1::/56",
                }
            ],
        },
        {
            "available": False,
            "radreply": [
                {
                    "attribute": "Delegated-IPv6-Prefix",
                    "value": "2001:db8:bad::/56",
                }
            ],
        },
    ]

    assert _radius_pd_values(rows) == {"2001:db8:1::/56"}
