from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.datastructures import UploadFile

from app.web.auth.dependencies import AuthenticationRequired


def _error_payload(code: str, message: str, details):
    return {"code": code, "message": message, "details": details}


def register_error_handlers(app) -> None:
    @app.exception_handler(AuthenticationRequired)
    async def auth_required_handler(request: Request, exc: AuthenticationRequired):
        """Redirect to login page when authentication is required."""
        return RedirectResponse(url=exc.redirect_url, status_code=303)
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        code = f"http_{exc.status_code}"
        message = "Request failed"
        details = None
        if isinstance(detail, dict):
            code = detail.get("code", code)
            message = detail.get("message", message)
            details = detail.get("details")
        elif isinstance(detail, str):
            message = detail
        else:
            details = detail
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(code, message, details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
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
                "validation_error", "Validation error", errors
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                "internal_error", "Internal server error", None
            ),
        )
