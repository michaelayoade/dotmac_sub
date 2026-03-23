from __future__ import annotations

import warnings
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from starlette.templating import Jinja2Templates, _TemplateResponse

APP_TIMEZONE_NAME = "Africa/Lagos"
APP_TIMEZONE = ZoneInfo(APP_TIMEZONE_NAME)


def localize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(APP_TIMEZONE)


def format_in_app_timezone(
    value: datetime | None,
    fmt: str = "%Y-%m-%d %H:%M",
    empty: str = "",
) -> str:
    localized = localize_datetime(value)
    if localized is None:
        return empty
    return localized.strftime(fmt)


class _DisplayProxyBase:
    __slots__ = ()


class DisplayMapping(_DisplayProxyBase):
    __slots__ = ("_value",)

    def __init__(self, value: Mapping[Any, Any]) -> None:
        self._value = value

    def __getitem__(self, key: Any) -> Any:
        return localize_for_display(self._value[key])

    def __iter__(self):
        return iter(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __contains__(self, item: object) -> bool:
        return item in self._value

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._value, name)
        if callable(attr):
            return attr
        return localize_for_display(attr)

    def get(self, key: Any, default: Any = None) -> Any:
        return localize_for_display(self._value.get(key, default))

    def items(self):
        for key, value in self._value.items():
            yield key, localize_for_display(value)

    def keys(self):
        return self._value.keys()

    def values(self):
        for value in self._value.values():
            yield localize_for_display(value)

    def __str__(self) -> str:
        return str(self._value)

    def __repr__(self) -> str:
        return repr(self._value)


class DisplayObject(_DisplayProxyBase):
    __slots__ = ("_value",)

    def __init__(self, value: Any) -> None:
        self._value = value

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._value, name)
        if callable(attr):
            return attr
        return localize_for_display(attr)

    def __getitem__(self, key: Any) -> Any:
        return localize_for_display(self._value[key])

    def __iter__(self):
        for item in self._value:
            yield localize_for_display(item)

    def __len__(self) -> int:
        return len(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __contains__(self, item: object) -> bool:
        return item in self._value

    def __str__(self) -> str:
        return str(self._value)

    def __repr__(self) -> str:
        return repr(self._value)

    def __html__(self) -> str:
        html = getattr(self._value, "__html__", None)
        if callable(html):
            return html()
        return str(self._value)


def localize_for_display(value: Any) -> Any:
    if isinstance(value, datetime):
        return localize_datetime(value)
    if value is None or isinstance(value, (str, bytes, int, float, bool, date)):
        return value
    if isinstance(value, _DisplayProxyBase):
        return value
    if isinstance(value, Mapping):
        return DisplayMapping(value)
    if isinstance(value, list):
        return [localize_for_display(item) for item in value]
    if isinstance(value, tuple):
        return tuple(localize_for_display(item) for item in value)
    if isinstance(value, set):
        return {localize_for_display(item) for item in value}
    module_name = getattr(value.__class__, "__module__", "")
    if module_name.startswith(("starlette.", "fastapi.")):
        return value
    return DisplayObject(value)


def localize_template_context(context: Mapping[str, Any]) -> dict[str, Any]:
    localized: dict[str, Any] = {}
    for key, value in context.items():
        if key == "request":
            localized[key] = value
        else:
            localized[key] = localize_for_display(value)
    return localized


def install_template_timezone_localization() -> None:
    if getattr(Jinja2Templates.TemplateResponse, "_dotmac_timezone_patched", False):
        return

    def _patched_template_response(
        self: Jinja2Templates,
        *args: Any,
        **kwargs: Any,
    ) -> _TemplateResponse:
        if args:
            if isinstance(args[0], str):
                warnings.warn(
                    "The `name` is not the first parameter anymore. "
                    "The first parameter should be the `Request` instance.\n"
                    'Replace `TemplateResponse(name, {"request": request})` by '
                    "`TemplateResponse(request, name)`.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                name = args[0]
                context = args[1] if len(args) > 1 else kwargs.get("context", {})
                status_code = (
                    args[2] if len(args) > 2 else kwargs.get("status_code", 200)
                )
                headers = args[3] if len(args) > 3 else kwargs.get("headers")
                media_type = args[4] if len(args) > 4 else kwargs.get("media_type")
                background = args[5] if len(args) > 5 else kwargs.get("background")
                if "request" not in context:
                    raise ValueError('context must include a "request" key')
                request = context["request"]
            else:
                request = args[0]
                name = args[1] if len(args) > 1 else kwargs["name"]
                context = args[2] if len(args) > 2 else kwargs.get("context", {})
                status_code = (
                    args[3] if len(args) > 3 else kwargs.get("status_code", 200)
                )
                headers = args[4] if len(args) > 4 else kwargs.get("headers")
                media_type = args[5] if len(args) > 5 else kwargs.get("media_type")
                background = args[6] if len(args) > 6 else kwargs.get("background")
        else:
            if "request" not in kwargs:
                warnings.warn(
                    "The `TemplateResponse` now requires the `request` argument.\n"
                    'Replace `TemplateResponse(name, {"context": context})` by '
                    "`TemplateResponse(request, name)`.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                if "request" not in kwargs.get("context", {}):
                    raise ValueError('context must include a "request" key')
            context = kwargs.get("context", {})
            request = kwargs.get("request", context.get("request"))
            name = cast(str, kwargs["name"])
            status_code = kwargs.get("status_code", 200)
            headers = kwargs.get("headers")
            media_type = kwargs.get("media_type")
            background = kwargs.get("background")

        context.setdefault("request", request)
        for context_processor in self.context_processors:
            context.update(context_processor(request))

        template = self.get_template(name)
        return _TemplateResponse(
            template,
            localize_template_context(context),
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )

    patched = cast(Any, _patched_template_response)
    patched._dotmac_timezone_patched = True
    templates_cls = cast(Any, Jinja2Templates)
    templates_cls.TemplateResponse = cast(
        Callable[..., _TemplateResponse], _patched_template_response
    )
