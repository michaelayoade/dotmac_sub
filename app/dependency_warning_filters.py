"""Narrow warning filters for pinned third-party dependencies."""

from __future__ import annotations

import warnings


def install_celery_dependency_warning_filters() -> None:
    """Suppress known import-time warnings that application code cannot configure."""

    # LangGraph 0.6.x emits this while importing its checkpoint base module. The
    # application does not construct the serializer that owns ``allowed_objects``,
    # so there is no application-level value to pass explicitly. Keep the filter
    # scoped to that module and message until the pinned dependency is upgraded.
    warnings.filterwarnings(
        "ignore",
        message=(
            r"The default value of `allowed_objects` will change in a future "
            r"version\..*"
        ),
        module=r"langgraph\.checkpoint\.base",
    )
