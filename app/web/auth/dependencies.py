"""Web authentication dependencies for cookie-based auth with redirects."""

from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.auth import Session as AuthSession, SessionStatus
from app.models.person import Person
from app.services.auth_flow import decode_access_token


class AuthenticationRequired(Exception):
    """Raised when authentication is required but not provided."""

    def __init__(self, redirect_url: str = "/auth/login"):
        self.redirect_url = redirect_url
        super().__init__("Authentication required")


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_session_token(request: Request) -> str | None:
    """Extract session token from cookie or Authorization header."""
    # First check for cookie-based token
    cookie_token = request.cookies.get("session_token")
    if cookie_token:
        return cookie_token

    # Fall back to Bearer token from Authorization header (for API calls)
    auth_header = request.headers.get("authorization")
    if auth_header:
        parts = auth_header.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()

    return None


def validate_session_token(
    request: Request,
    db: Session = Depends(_get_db),
) -> dict | None:
    """Validate session token and return user info if valid.

    Returns None if not authenticated (doesn't raise).
    """
    token = get_session_token(request)
    if not token:
        return None

    try:
        payload = decode_access_token(db, token)
    except Exception:
        return None

    person_id = payload.get("sub")
    session_id = payload.get("session_id")
    if not person_id or not session_id:
        return None

    now = datetime.now(timezone.utc)
    session = (
        db.query(AuthSession)
        .filter(AuthSession.id == session_id)
        .filter(AuthSession.person_id == person_id)
        .filter(AuthSession.status == SessionStatus.active)
        .filter(AuthSession.revoked_at.is_(None))
        .filter(AuthSession.expires_at > now)
        .first()
    )
    if not session:
        return None

    # Get person details
    person = db.get(Person, person_id)
    if not person:
        return None

    roles = payload.get("roles", [])
    scopes = payload.get("scopes", [])

    return {
        "person_id": str(person_id),
        "session_id": str(session_id),
        "roles": roles if isinstance(roles, list) else [],
        "scopes": scopes if isinstance(scopes, list) else [],
        "person": person,
    }


def require_web_auth(
    request: Request,
    db: Session = Depends(_get_db),
) -> dict:
    """Require authentication for web routes.

    Raises AuthenticationRequired if not authenticated.
    The exception handler should redirect to login with next URL.
    """
    auth_info = validate_session_token(request, db)
    if not auth_info:
        # Build redirect URL with next parameter
        next_url = str(request.url.path)
        if request.url.query:
            next_url += f"?{request.url.query}"
        redirect_url = f"/auth/refresh?next={quote(next_url)}"
        raise AuthenticationRequired(redirect_url)

    # Store auth info in request state for use by templates
    request.state.auth = auth_info
    request.state.user = auth_info["person"]
    request.state.actor_id = auth_info["person_id"]
    request.state.actor_type = "user"

    return auth_info


def get_current_user_from_auth(auth: dict = Depends(require_web_auth)) -> dict:
    """Get current user info formatted for templates."""
    person = auth.get("person")
    if not person:
        return {
            "id": "",
            "initials": "??",
            "name": "Unknown User",
            "email": "",
        }

    name = f"{person.first_name} {person.last_name}".strip()
    initials = _get_initials(name)

    return {
        "id": str(person.id),
        "initials": initials,
        "name": name,
        "email": person.email or "",
        "roles": auth.get("roles", []),
    }


def _get_initials(name: str) -> str:
    """Get initials from a name."""
    if not name:
        return "??"
    parts = name.split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return name[0:2].upper()
