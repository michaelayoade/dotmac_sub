from __future__ import annotations

import warnings

from app.dependency_warning_filters import install_celery_dependency_warning_filters


def _emit_warning(message: str, *, module: str) -> None:
    warnings.warn_explicit(
        message,
        UserWarning,
        filename="dependency.py",
        lineno=18,
        module=module,
    )


def test_langgraph_allowed_objects_filter_is_narrowly_scoped():
    message = (
        "The default value of `allowed_objects` will change in a future version. "
        "Pass it explicitly."
    )

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        install_celery_dependency_warning_filters()

        _emit_warning(message, module="langgraph.checkpoint.base")
        _emit_warning(message, module="another.dependency")
        _emit_warning(
            "A different LangGraph warning.",
            module="langgraph.checkpoint.base",
        )

    assert [str(item.message) for item in captured] == [
        message,
        "A different LangGraph warning.",
    ]
