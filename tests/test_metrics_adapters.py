from app.services.network.metrics_adapters import ZabbixMetricsAdapter


class _FakeZabbixClient:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def get_items(self, **kwargs):
        self.calls.append(kwargs)
        return self.items


def test_zabbix_signal_item_lookup_does_not_default_to_interface_filter():
    client = _FakeZabbixClient(
        [
            {"itemid": "101", "key_": "ont.signal.onu_rx[abc]"},
            {"itemid": "102", "key_": "gpon.olt.rx.power[abc]"},
            {"itemid": "999", "key_": "net.if.in[eth0]"},
        ]
    )
    adapter = ZabbixMetricsAdapter(api_url="http://zabbix.example/api", api_token="t")
    adapter._client = client

    items = adapter._get_signal_items("host-1")

    assert client.calls == [{"host_ids": ["host-1"], "metric": "", "limit": 10000}]
    assert items == {"onu_rx": "101", "olt_rx": "102"}
