from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote_plus, urlparse

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import MutableHeaders, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.unit_of_work import ConcurrencyConflict
from app.web.auth.dependencies import AuthenticationRequired

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")
_TEMPLATES_ROOT = Path("templates")

_FRIENDLY_DEFAULT_BAD_REQUEST = "Some required information is missing or invalid. Please check the form and try again."
_REDIRECT_ERROR_TOKEN_TO_STATUS = {
    "forbidden": 403,
    "access_denied": 403,
    "unauthorized": 403,
    "not_found": 404,
    "missing": 404,
    "does_not_exist": 404,
    "invalid": 400,
    "bad_request": 400,
}


def _error_payload(code: str, message: str, details: object, request_id: str | None):
    return {
        "code": code,
        "message": message,
        "details": details,
        "request_id": request_id,
    }


def _is_html_request(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    content_type = (request.headers.get("content-type") or "").lower()
    if request.headers.get("HX-Request", "").lower() == "true":
        return False
    if request.url.path.startswith("/api/"):
        return False
    if "application/json" in content_type:
        return False
    if "application/json" in accept and "text/html" not in accept:
        return False
    return "text/html" in accept or not request.url.path.startswith("/api/")


def _request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    return str(rid) if rid else "unknown"


def _friendly_bad_request_message(detail: object) -> str:
    if isinstance(detail, str):
        msg = detail.strip()
        if not msg:
            return _FRIENDLY_DEFAULT_BAD_REQUEST
        lowered = msg.lower()
        if any(
            token in lowered
            for token in (
                "validation error",
                "type_error",
                "value_error",
                "traceback",
                "{",
                "[",
            )
        ):
            return _FRIENDLY_DEFAULT_BAD_REQUEST
        return msg
    if isinstance(detail, dict):
        for key in ("message", "detail", "error"):
            val = detail.get(key)
            if isinstance(val, str) and val.strip():
                lowered = val.lower()
                if any(
                    token in lowered
                    for token in ("type_error", "value_error", "traceback")
                ):
                    return _FRIENDLY_DEFAULT_BAD_REQUEST
                return val.strip()
    return _FRIENDLY_DEFAULT_BAD_REQUEST


def _login_redirect_for_path(request: Request) -> str:
    next_url = str(request.url.path)
    if request.url.query:
        next_url += f"?{request.url.query}"
    quoted_next = quote(next_url, safe="")
    path = request.url.path
    if path.startswith("/portal/"):
        return f"/portal/auth/login?next={quoted_next}"
    if path.startswith("/reseller/"):
        return f"/reseller/auth/login?next={quoted_next}"
    if path.startswith("/vendor/"):
        return f"/vendor/auth/login?next={quoted_next}"
    return f"/auth/login?next={quoted_next}"


def _friendly_redirect_error_message(error_value: str, status_code: int) -> str:
    token = error_value.strip().lower().replace(" ", "_")
    if token in {"forbidden", "access_denied", "unauthorized"}:
        return "You do not have permission to access this page."
    if token in {"not_found", "missing", "does_not_exist"}:
        return "The requested item could not be found."
    if status_code == 400:
        return _FRIENDLY_DEFAULT_BAD_REQUEST
    return error_value.strip()


def _template_response(request: Request, status_code: int, message: str):
    template_name = f"errors/{status_code}.html"
    if request.url.path.startswith("/admin"):
        admin_template = _TEMPLATES_ROOT / "admin" / "errors" / f"{status_code}.html"
        if admin_template.exists():
            template_name = f"admin/errors/{status_code}.html"
    return templates.TemplateResponse(
        request,
        template_name,
        {
            "request": request,
            "message": message,
            "request_id": _request_id(request),
        },
        status_code=status_code,
    )


class RedirectErrorTemplateMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        captured: list[Message] = []

        async def send_wrapper(asgi_message: Message) -> None:
            captured.append(dict(asgi_message))

        await self.app(scope, receive, send_wrapper)

        start_message = next(
            (
                message
                for message in captured
                if message["type"] == "http.response.start"
            ),
            None,
        )
        if start_message is None:
            return

        headers = MutableHeaders(scope=start_message)
        if request.headers.get("HX-Request", "").lower() == "true":
            for asgi_message in captured:
                await send(asgi_message)
            return

        status_code = start_message["status"]
        location = headers.get("location")
        if (
            status_code not in {301, 302, 303, 307, 308}
            or not location
            or "error=" not in location
        ):
            for asgi_message in captured:
                await send(asgi_message)
            return

        parsed = urlparse(location)
        params = parse_qs(parsed.query or "")
        raw = params.get("error", [None])[0]
        if raw is None:
            for asgi_message in captured:
                await send(asgi_message)
            return

        raw_message = unquote_plus(str(raw)).strip()
        token = raw_message.lower().replace(" ", "_")
        mapped_status = _REDIRECT_ERROR_TOKEN_TO_STATUS.get(token)
        if mapped_status is None:
            for asgi_message in captured:
                await send(asgi_message)
            return

        friendly_message = _friendly_redirect_error_message(raw_message, mapped_status)
        response = _template_response(
            request,
            status_code=mapped_status,
            message=friendly_message,
        )
        await response(scope, receive, send)


def register_error_handlers(app) -> None:
    app.add_middleware(RedirectErrorTemplateMiddleware)

    @app.exception_handler(ConcurrencyConflict)
    async def concurrency_conflict_handler(request: Request, exc: ConcurrencyConflict):
        """Handle concurrency conflicts (optimistic locking failures, etc.)."""
        if _is_html_request(request):
            return _template_response(
                request,
                status_code=409,
                message=exc.message,
            )
        return JSONResponse(
            status_code=409,
            content=_error_payload(
                "concurrency_conflict",
                exc.message,
                None,
                _request_id(request),
            ),
        )

    @app.exception_handler(AuthenticationRequired)
    async def auth_required_handler(request: Request, exc: AuthenticationRequired):
        """Redirect to login page when authentication is required."""
        return RedirectResponse(url=exc.redirect_url, status_code=303)

    async def _handle_http_exception(
        request: Request, status_code: int, detail: object
    ):
        if _is_html_request(request):
            if status_code == 401:
                return RedirectResponse(
                    url=_login_redirect_for_path(request), status_code=303
                )
            if status_code == 400:
                return _template_response(
                    request,
                    status_code=400,
                    message=_friendly_bad_request_message(detail),
                )
            if status_code == 403:
                message = (
                    detail
                    if isinstance(detail, str)
                    else (
                        "You do not have permission to view this page. "
                        "If you believe this is a mistake, please contact your administrator."
                    )
                )
                return _template_response(request, status_code=403, message=message)
            if status_code == 404:
                message = (
                    detail
                    if isinstance(detail, str) and detail.strip()
                    else "Page not found"
                )
                return _template_response(request, status_code=404, message=message)
            if status_code == 409:
                message = (
                    detail
                    if isinstance(detail, str) and detail.strip()
                    else "Request conflict"
                )
                return _template_response(request, status_code=409, message=message)

        detail_payload = detail
        code = f"http_{status_code}"
        message = "Request failed"
        details = None
        if isinstance(detail_payload, dict):
            code = detail_payload.get("code", code)
            message = detail_payload.get("message", message)
            details = detail_payload.get("details")
        elif isinstance(detail_payload, str):
            message = detail_payload
        else:
            details = detail_payload
        return JSONResponse(
            status_code=status_code,
            content=_error_payload(code, message, details, _request_id(request)),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return await _handle_http_exception(request, exc.status_code, exc.detail)

    @app.exception_handler(StarletteHTTPException)
    async def starlette_http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ):
        detail = (
            exc.detail if getattr(exc, "detail", None) is not None else "Request failed"
        )
        return await _handle_http_exception(request, exc.status_code, detail)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        if _is_html_request(request):
            return _template_response(
                request,
                status_code=400,
                message=_FRIENDLY_DEFAULT_BAD_REQUEST,
            )

        # Convert errors to JSON-serializable format.
        def _sanitize_input(value):
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            if isinstance(value, UploadFile):
                return value.filename or "upload"
            if isinstance(value, dict):
                return {key: _sanitize_input(val) for key, val in value.items()}
            if isinstance(value, (list, tuple, set)):
                return [_sanitize_input(item) for item in value]
            if isinstance(value, (str, int, float, bool)) or value is None:
                return value
            return str(value)

        errors = []
        for error in exc.errors():
            error_copy = dict(error)
            if "input" in error_copy:
                error_copy["input"] = _sanitize_input(error_copy.get("input"))
            errors.append(error_copy)
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                "validation_error", "Validation error", errors, _request_id(request)
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception(
            "Unhandled exception on %s %s",
            request.method,
            request.url.path,
            extra={"request_id": _request_id(request)},
        )
        if _is_html_request(request):
            return _template_response(
                request,
                status_code=500,
                message="Oops! Something went wrong on our end. Please try again later.",
            )
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                "internal_error", "Internal server error", None, _request_id(request)
            ),
        )
