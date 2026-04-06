from app.websocket import manager as websocket_manager


def test_mask_redis_url_masks_password() -> None:
    url = "redis://:secret-password@redis:6379/0"

    masked = websocket_manager._mask_redis_url(url)

    assert masked == "redis://:***@redis:6379/0"


def test_mask_redis_url_leaves_passwordless_url_unchanged() -> None:
    url = "redis://redis:6379/0"

    masked = websocket_manager._mask_redis_url(url)

    assert masked == url
