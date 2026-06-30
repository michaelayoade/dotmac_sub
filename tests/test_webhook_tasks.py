from app.models.webhook import WebhookEndpoint
from app.tasks import webhooks


def test_webhook_task_delivery_defaults_match_legacy_behavior():
    endpoint = WebhookEndpoint(name="Default", url="https://example.com")

    assert webhooks._endpoint_timeout_seconds(endpoint) == 30.0
    assert webhooks._endpoint_max_retries(endpoint) == 10
    assert webhooks._endpoint_retry_delay(endpoint, 1) == 60
    assert webhooks._endpoint_retry_delay(endpoint, 10) == 28800


def test_webhook_task_uses_endpoint_delivery_controls():
    endpoint = WebhookEndpoint(
        name="Configured",
        url="https://example.com",
        delivery_timeout_seconds=5,
        max_retries=3,
        retry_backoff_seconds=2,
    )

    assert webhooks._endpoint_timeout_seconds(endpoint) == 5.0
    assert webhooks._endpoint_max_retries(endpoint) == 3
    assert webhooks._endpoint_retry_delay(endpoint, 1) == 2
    assert webhooks._endpoint_retry_delay(endpoint, 3) == 8
