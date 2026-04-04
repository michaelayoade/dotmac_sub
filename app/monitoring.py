"""
Centralized monitoring setup for Loki logging and GlitchTip error tracking.

Usage:
    from app.monitoring import setup_monitoring
    setup_monitoring(app_name="dotmac-sub", server="prod-1")

Environment variables:
    LOKI_URL: Loki push endpoint (default: http://160.119.127.195:3100/loki/api/v1/push)
    GLITCHTIP_DSN: Sentry-compatible DSN for GlitchTip (optional)
    ENVIRONMENT: Environment name (default: production)
    MONITORING_ENABLED: Set to "false" to disable external monitoring (default: true)
"""

import logging
import os

logger = logging.getLogger(__name__)

# Default monitoring server
DEFAULT_MONITORING_HOST = "160.119.127.195"
DEFAULT_LOKI_URL = f"http://{DEFAULT_MONITORING_HOST}:3100/loki/api/v1/push"


def _setup_loki(
    app_name: str,
    server: str,
    environment: str,
    loki_url: str,
) -> bool:
    """Configure Loki log handler.

    Returns:
        True if Loki handler was configured, False otherwise.
    """
    try:
        import logging_loki  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "python-logging-loki not installed, Loki logging disabled. "
            "Install with: pip install python-logging-loki"
        )
        return False

    try:
        handler = logging_loki.LokiHandler(
            url=loki_url,
            tags={
                "app": app_name,
                "server": server,
                "environment": environment,
            },
            version="1",
        )
        # Add to root logger so all logs are pushed
        root_logger = logging.getLogger()
        handler.setLevel(logging.INFO)
        root_logger.addHandler(handler)
        logger.info(
            f"Loki logging enabled: app={app_name}, server={server}, "
            f"environment={environment}, url={loki_url}"
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to configure Loki handler: {e}")
        return False


def _setup_sentry(
    app_name: str,
    server: str,
    environment: str,
    dsn: str,
    traces_sample_rate: float = 0.1,
) -> bool:
    """Configure Sentry SDK for GlitchTip error tracking.

    Returns:
        True if Sentry was configured, False otherwise.
    """
    try:
        import sentry_sdk
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
    except ImportError:
        logger.warning(
            "sentry-sdk not installed, error tracking disabled. "
            "Install with: pip install sentry-sdk[fastapi]"
        )
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            traces_sample_rate=traces_sample_rate,
            release=f"{app_name}@{os.getenv('APP_VERSION', 'unknown')}",
            server_name=server,
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                SqlalchemyIntegration(),
                CeleryIntegration(),
            ],
            # Don't send PII by default
            send_default_pii=False,
            # Attach request data for context
            request_bodies="medium",
        )
        logger.info(
            f"GlitchTip error tracking enabled: app={app_name}, server={server}, "
            f"environment={environment}"
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to configure Sentry/GlitchTip: {e}")
        return False


def setup_monitoring(
    app_name: str = "dotmac-sub",
    server: str = "default",
    environment: str | None = None,
    loki_url: str | None = None,
    glitchtip_dsn: str | None = None,
    traces_sample_rate: float = 0.1,
) -> dict[str, bool]:
    """
    Initialize monitoring with Loki logging and GlitchTip error tracking.

    Args:
        app_name: Application name for labeling (default: "dotmac-sub")
        server: Server/host identifier for labeling (default: "default")
        environment: Environment name (default: from ENVIRONMENT env var or "production")
        loki_url: Loki push URL (default: from LOKI_URL env var)
        glitchtip_dsn: GlitchTip/Sentry DSN (default: from GLITCHTIP_DSN env var)
        traces_sample_rate: Percentage of requests to trace (default: 0.1 = 10%)

    Returns:
        Dict with status of each component: {"loki": bool, "sentry": bool}

    Example:
        # Basic usage with defaults
        setup_monitoring(app_name="my-app", server="prod-1")

        # With custom configuration
        setup_monitoring(
            app_name="my-app",
            server="prod-1",
            environment="staging",
            traces_sample_rate=0.5,
        )
    """
    # Check if monitoring is enabled
    if os.getenv("MONITORING_ENABLED", "true").lower() in ("false", "0", "no"):
        logger.info("External monitoring disabled via MONITORING_ENABLED=false")
        return {"loki": False, "sentry": False}

    # Resolve configuration from environment
    resolved_env = environment or os.getenv("ENVIRONMENT") or "production"
    resolved_loki_url = loki_url or os.getenv("LOKI_URL") or DEFAULT_LOKI_URL
    resolved_dsn = glitchtip_dsn or os.getenv("GLITCHTIP_DSN") or ""

    result = {"loki": False, "sentry": False}

    # Setup Loki logging
    result["loki"] = _setup_loki(app_name, server, resolved_env, resolved_loki_url)

    # Setup GlitchTip/Sentry error tracking (only if DSN is configured)
    if resolved_dsn:
        result["sentry"] = _setup_sentry(
            app_name, server, resolved_env, resolved_dsn, traces_sample_rate
        )
    else:
        logger.info(
            "GLITCHTIP_DSN not configured, error tracking disabled. "
            "Set GLITCHTIP_DSN to enable GlitchTip integration."
        )

    return result
