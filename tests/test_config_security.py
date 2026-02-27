import logging

from app.config import Settings, warn_insecure_service_urls


def test_warn_insecure_service_urls_logs_non_loopback_http(caplog):
    config = Settings(s3_endpoint_url="http://minio:9000")

    with caplog.at_level(logging.WARNING):
        warn_insecure_service_urls(config)

    assert "Insecure http:// URL configured for s3_endpoint_url" in caplog.text


def test_warn_insecure_service_urls_skips_loopback_http(caplog):
    config = Settings(
        s3_endpoint_url="http://127.0.0.1:9000",
        meta_graph_base_url="http://localhost:8080/meta",
    )

    with caplog.at_level(logging.WARNING):
        warn_insecure_service_urls(config)

    assert "Insecure http:// URL configured" not in caplog.text
