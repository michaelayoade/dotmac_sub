from app.services.network.ont_config_snapshots import redact_snapshot_secrets


def test_snapshot_redaction_is_recursive_and_preserves_non_secrets():
    payload = {
        "SSID": "CustomerNet",
        "Password": "plaintext",
        "nested": {"KeyPassphrase": "another-secret", "Channel": 6},
        "items": [{"pre_shared_key": "psk"}],
    }

    assert redact_snapshot_secrets(payload) == {
        "SSID": "CustomerNet",
        "Password": "[redacted]",
        "nested": {"KeyPassphrase": "[redacted]", "Channel": 6},
        "items": [{"pre_shared_key": "[redacted]"}],
    }
