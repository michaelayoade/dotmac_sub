from app.models.webhook import WebhookEndpoint

# The SoT refactor moved the endpoint delivery-control helpers out of the
# task module and into the webhook_deliveries service (dropping the leading
# underscore as they became part of the service's public surface).
from app.services.webhook_deliveries import (
    endpoint_max_retries,
    endpoint_retry_delay,
    endpoint_timeout_seconds,
)


def test_webhook_task_delivery_defaults_match_legacy_behavior():
    endpoint = WebhookEndpoint(name="Default", url="https://example.com")

    assert endpoint_timeout_seconds(endpoint) == 30.0
    assert endpoint_max_retries(endpoint) == 10
    assert endpoint_retry_delay(endpoint, 1) == 60
    assert endpoint_retry_delay(endpoint, 10) == 28800


def test_webhook_task_uses_endpoint_delivery_controls():
    endpoint = WebhookEndpoint(
        name="Configured",
        url="https://example.com",
        delivery_timeout_seconds=5,
        max_retries=3,
        retry_backoff_seconds=2,
    )

    assert endpoint_timeout_seconds(endpoint) == 5.0
    assert endpoint_max_retries(endpoint) == 3
    assert endpoint_retry_delay(endpoint, 1) == 2
    assert endpoint_retry_delay(endpoint, 3) == 8
