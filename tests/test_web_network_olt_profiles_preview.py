from __future__ import annotations

from types import SimpleNamespace


def test_command_preview_context_masks_generated_credentials(monkeypatch) -> None:
    from app.models.network import OntProvisioningProfile
    from app.services import web_network_olt_profiles
    from app.services.network.olt_command_gen import OltCommandSet

    class FakeDb:
        def get(self, model, _id):
            if model is OntProvisioningProfile:
                return SimpleNamespace(name="Profile")
            return None

    monkeypatch.setattr(
        web_network_olt_profiles,
        "_resolve_ont_olt_context",
        lambda *_args: (
            SimpleNamespace(assignments=[]),
            SimpleNamespace(name="OLT"),
            "0/1/1",
            7,
        ),
    )
    monkeypatch.setattr(
        web_network_olt_profiles,
        "build_spec_from_profile",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        web_network_olt_profiles.HuaweiCommandGenerator,
        "generate_full_provisioning",
        lambda *_args: [
            OltCommandSet(
                step="WAN",
                commands=[
                    "ont wan pppoe password cipher ppp-secret",
                    "snmp-agent community read cipher public123",
                ],
            )
        ],
    )

    context = web_network_olt_profiles.command_preview_context(
        FakeDb(),
        "ont-id",
        "profile-id",
    )

    commands = context["command_sets"][0].commands
    assert "ppp-secret" not in "\n".join(commands)
    assert "public123" not in "\n".join(commands)
    assert commands == [
        "ont wan pppoe password cipher ********",
        "snmp-agent community read cipher ********",
    ]

