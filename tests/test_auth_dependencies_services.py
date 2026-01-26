"""Tests for auth dependencies services."""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services import auth_dependencies


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestExtractBearerToken:
    """Tests for _extract_bearer_token function."""

    def test_none_authorization(self):
        """Test with None authorization header."""
        result = auth_dependencies._extract_bearer_token(None)
        assert result is None

    def test_empty_authorization(self):
        """Test with empty authorization header."""
        result = auth_dependencies._extract_bearer_token("")
        assert result is None

    def test_valid_bearer_token(self):
        """Test with valid Bearer token."""
        result = auth_dependencies._extract_bearer_token("Bearer my-token-123")
        assert result == "my-token-123"

    def test_bearer_lowercase(self):
        """Test with lowercase bearer."""
        result = auth_dependencies._extract_bearer_token("bearer my-token")
        assert result == "my-token"

    def test_bearer_mixed_case(self):
        """Test with mixed case bearer."""
        result = auth_dependencies._extract_bearer_token("BEARER my-token")
        assert result == "my-token"

    def test_no_bearer_prefix(self):
        """Test without Bearer prefix."""
        result = auth_dependencies._extract_bearer_token("Basic abc123")
        assert result is None

    def test_token_with_spaces(self):
        """Test token is trimmed."""
        result = auth_dependencies._extract_bearer_token("Bearer   token-with-spaces  ")
        assert result == "token-with-spaces"

    def test_only_bearer(self):
        """Test with only Bearer word."""
        result = auth_dependencies._extract_bearer_token("Bearer")
        assert result is None


class TestAsUtc:
    """Tests for _as_utc function."""

    def test_none_value(self):
        """Test with None value."""
        result = auth_dependencies._as_utc(None)
        assert result is None

    def test_naive_datetime(self):
        """Test with naive datetime."""
        naive = datetime(2024, 1, 15, 12, 0, 0)
        result = auth_dependencies._as_utc(naive)
        assert result.tzinfo == timezone.utc
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15

    def test_aware_datetime(self):
        """Test with already aware datetime."""
        aware = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = auth_dependencies._as_utc(aware)
        assert result is aware


class TestIsJwt:
    """Tests for _is_jwt function."""

    def test_valid_jwt_format(self):
        """Test with valid JWT format (two dots)."""
        result = auth_dependencies._is_jwt("header.payload.signature")
        assert result is True

    def test_jwt_with_complex_parts(self):
        """Test with realistic JWT."""
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        result = auth_dependencies._is_jwt(jwt)
        assert result is True

    def test_not_jwt_no_dots(self):
        """Test with token without dots."""
        result = auth_dependencies._is_jwt("simple-session-token")
        assert result is False

    def test_not_jwt_one_dot(self):
        """Test with one dot."""
        result = auth_dependencies._is_jwt("part1.part2")
        assert result is False

    def test_not_jwt_three_dots(self):
        """Test with three dots."""
        result = auth_dependencies._is_jwt("a.b.c.d")
        assert result is False


class TestHasAuditScope:
    """Tests for _has_audit_scope function."""

    def test_empty_payload(self):
        """Test with empty payload."""
        result = auth_dependencies._has_audit_scope({})
        assert result is False

    def test_audit_read_in_scope_string(self):
        """Test with audit:read in scope string."""
        result = auth_dependencies._has_audit_scope({"scope": "read audit:read write"})
        assert result is True

    def test_audit_star_in_scope_string(self):
        """Test with audit:* in scope string."""
        result = auth_dependencies._has_audit_scope({"scope": "audit:*"})
        assert result is True

    def test_audit_read_in_scopes_list(self):
        """Test with audit:read in scopes list."""
        result = auth_dependencies._has_audit_scope({"scopes": ["read", "audit:read"]})
        assert result is True

    def test_admin_role_string(self):
        """Test with admin role as string."""
        result = auth_dependencies._has_audit_scope({"role": "admin"})
        assert result is True

    def test_auditor_role_in_roles_list(self):
        """Test with auditor in roles list."""
        result = auth_dependencies._has_audit_scope({"roles": ["user", "auditor"]})
        assert result is True

    def test_no_audit_scope_or_role(self):
        """Test without audit scope or role."""
        result = auth_dependencies._has_audit_scope({
            "scope": "read write",
            "scopes": ["basic"],
            "role": "user",
            "roles": ["viewer"],
        })
        assert result is False

    def test_mixed_scope_and_role(self):
        """Test with both scope and role fields."""
        result = auth_dependencies._has_audit_scope({
            "scope": "basic",
            "roles": ["admin"],  # admin role grants access
        })
        assert result is True


# =============================================================================
# Require Audit Auth Tests
# =============================================================================


class TestRequireAuditAuth:
    """Tests for require_audit_auth dependency."""

    def test_no_credentials(self, db_session):
        """Test with no credentials provided."""
        with pytest.raises(HTTPException) as exc_info:
            auth_dependencies.require_audit_auth(
                authorization=None,
                x_session_token=None,
                x_api_key=None,
                request=None,
                db=db_session,
            )
        assert exc_info.value.status_code == 401
        assert "Unauthorized" in exc_info.value.detail

    def test_jwt_without_audit_scope(self, db_session, person):
        """Test with JWT that lacks audit scope."""
        from app.models.auth import Session as AuthSession, SessionStatus

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        token_hash = hashlib.sha256(b"jwt-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=now_naive + timedelta(hours=24),
        )
        db_session.add(auth_session)
        db_session.commit()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                "scope": "read write",  # No audit scope
            }

            with pytest.raises(HTTPException) as exc_info:
                auth_dependencies.require_audit_auth(
                    authorization="Bearer header.payload.signature",
                    x_session_token=None,
                    x_api_key=None,
                    request=None,
                    db=db_session,
                )

            assert exc_info.value.status_code == 403
            assert "Insufficient scope" in exc_info.value.detail

    def test_jwt_with_audit_scope_success(self, db_session, person):
        """Test with valid JWT that has audit scope."""
        from app.models.auth import Session as AuthSession, SessionStatus

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        token_hash = hashlib.sha256(b"jwt-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=now_naive + timedelta(hours=24),
        )
        db_session.add(auth_session)
        db_session.commit()

        mock_request = MagicMock()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                "scope": "audit:read",
            }

            result = auth_dependencies.require_audit_auth(
                authorization="Bearer header.payload.signature",
                x_session_token=None,
                x_api_key=None,
                request=mock_request,
                db=db_session,
            )

            assert result["actor_type"] == "user"
            assert result["actor_id"] == str(person.id)
            assert mock_request.state.actor_id == str(person.id)

    def test_jwt_session_not_found(self, db_session, person):
        """Test with JWT where session doesn't exist."""
        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(uuid.uuid4()),  # Non-existent
                "roles": ["admin"],
            }

            with pytest.raises(HTTPException) as exc_info:
                auth_dependencies.require_audit_auth(
                    authorization="Bearer header.payload.signature",
                    x_session_token=None,
                    x_api_key=None,
                    request=None,
                    db=db_session,
                )

            assert exc_info.value.status_code == 401
            assert "Invalid session" in exc_info.value.detail

    def test_jwt_session_revoked(self, db_session, person):
        """Test with revoked session."""
        from app.models.auth import Session as AuthSession, SessionStatus

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        token_hash = hashlib.sha256(b"jwt-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.revoked,  # Revoked
            token_hash=token_hash,
            expires_at=now_naive + timedelta(hours=24),
        )
        db_session.add(auth_session)
        db_session.commit()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                "roles": ["admin"],
            }

            with pytest.raises(HTTPException) as exc_info:
                auth_dependencies.require_audit_auth(
                    authorization="Bearer header.payload.signature",
                    x_session_token=None,
                    x_api_key=None,
                    request=None,
                    db=db_session,
                )

            assert exc_info.value.status_code == 401

    def test_jwt_session_expired(self, db_session, person):
        """Test with expired session."""
        from app.models.auth import Session as AuthSession, SessionStatus

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        expired_at = now_naive - timedelta(hours=1)  # Already expired
        token_hash = hashlib.sha256(b"jwt-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=expired_at,
        )
        db_session.add(auth_session)
        db_session.commit()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                "roles": ["admin"],
            }

            with pytest.raises(HTTPException) as exc_info:
                auth_dependencies.require_audit_auth(
                    authorization="Bearer header.payload.signature",
                    x_session_token=None,
                    x_api_key=None,
                    request=None,
                    db=db_session,
                )

            assert exc_info.value.status_code == 401
            assert "Session expired" in exc_info.value.detail

    def test_jwt_no_session_id_in_payload(self, db_session, person):
        """Test with JWT that has no session_id (e.g., service token)."""
        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "roles": ["admin"],  # Has audit access
                # No session_id - skips session validation
            }

            result = auth_dependencies.require_audit_auth(
                authorization="Bearer header.payload.signature",
                x_session_token=None,
                x_api_key=None,
                request=None,
                db=db_session,
            )

            assert result["actor_type"] == "user"
            assert result["actor_id"] == str(person.id)

    def test_session_token_valid(self, db_session, person):
        """Test with valid session token (non-JWT)."""
        from app.models.auth import Session as AuthSession, SessionStatus

        session_token = "plain-session-token"
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)

        with patch.object(auth_dependencies, "hash_session_token") as mock_hash:
            mock_hash.return_value = "hashed-token"

            auth_session = AuthSession(
                person_id=person.id,
                status=SessionStatus.active,
                token_hash="hashed-token",
                expires_at=now_naive + timedelta(hours=24),
            )
            db_session.add(auth_session)
            db_session.commit()

            mock_request = MagicMock()

            result = auth_dependencies.require_audit_auth(
                authorization=None,
                x_session_token=session_token,
                x_api_key=None,
                request=mock_request,
                db=db_session,
            )

            assert result["actor_type"] == "user"
            assert result["actor_id"] == str(person.id)

    def test_session_token_expired(self, db_session, person):
        """Test with expired session token."""
        from app.models.auth import Session as AuthSession, SessionStatus

        session_token = "plain-session-token"
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        expired_at = now_naive - timedelta(hours=1)

        with patch.object(auth_dependencies, "hash_session_token") as mock_hash:
            mock_hash.return_value = "hashed-token"

            auth_session = AuthSession(
                person_id=person.id,
                status=SessionStatus.active,
                token_hash="hashed-token",
                expires_at=expired_at,  # Expired
            )
            db_session.add(auth_session)
            db_session.commit()

            # Should fall through to API key check and fail
            with pytest.raises(HTTPException) as exc_info:
                auth_dependencies.require_audit_auth(
                    authorization=None,
                    x_session_token=session_token,
                    x_api_key=None,
                    request=None,
                    db=db_session,
                )

            assert exc_info.value.status_code == 401

    def test_api_key_valid(self, db_session):
        """Test with valid API key."""
        from app.models.auth import ApiKey

        api_key_value = "test-api-key-123"
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)

        with patch.object(auth_dependencies, "hash_api_key") as mock_hash:
            mock_hash.return_value = "hashed-api-key"

            api_key = ApiKey(
                label="Test API Key",
                key_hash="hashed-api-key",
                is_active=True,
                expires_at=now_naive + timedelta(days=30),
            )
            db_session.add(api_key)
            db_session.commit()

            mock_request = MagicMock()

            result = auth_dependencies.require_audit_auth(
                authorization=None,
                x_session_token=None,
                x_api_key=api_key_value,
                request=mock_request,
                db=db_session,
            )

            assert result["actor_type"] == "api_key"
            assert result["actor_id"] == str(api_key.id)

    def test_api_key_no_expiry(self, db_session):
        """Test with API key that has no expiry."""
        from app.models.auth import ApiKey

        api_key_value = "test-api-key-123"

        with patch.object(auth_dependencies, "hash_api_key") as mock_hash:
            mock_hash.return_value = "hashed-api-key"

            api_key = ApiKey(
                label="Test API Key",
                key_hash="hashed-api-key",
                is_active=True,
                expires_at=None,  # No expiry
            )
            db_session.add(api_key)
            db_session.commit()

            result = auth_dependencies.require_audit_auth(
                authorization=None,
                x_session_token=None,
                x_api_key=api_key_value,
                request=None,
                db=db_session,
            )

            assert result["actor_type"] == "api_key"

    def test_api_key_inactive(self, db_session):
        """Test with inactive API key."""
        from app.models.auth import ApiKey

        api_key_value = "test-api-key-123"

        with patch.object(auth_dependencies, "hash_api_key") as mock_hash:
            mock_hash.return_value = "hashed-api-key"

            api_key = ApiKey(
                label="Test API Key",
                key_hash="hashed-api-key",
                is_active=False,  # Inactive
            )
            db_session.add(api_key)
            db_session.commit()

            with pytest.raises(HTTPException) as exc_info:
                auth_dependencies.require_audit_auth(
                    authorization=None,
                    x_session_token=None,
                    x_api_key=api_key_value,
                    request=None,
                    db=db_session,
                )

            assert exc_info.value.status_code == 401

    def test_api_key_revoked(self, db_session):
        """Test with revoked API key."""
        from app.models.auth import ApiKey

        api_key_value = "test-api-key-123"
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)

        with patch.object(auth_dependencies, "hash_api_key") as mock_hash:
            mock_hash.return_value = "hashed-api-key"

            api_key = ApiKey(
                label="Test API Key",
                key_hash="hashed-api-key",
                is_active=True,
                revoked_at=now_naive - timedelta(hours=1),  # Revoked
            )
            db_session.add(api_key)
            db_session.commit()

            with pytest.raises(HTTPException) as exc_info:
                auth_dependencies.require_audit_auth(
                    authorization=None,
                    x_session_token=None,
                    x_api_key=api_key_value,
                    request=None,
                    db=db_session,
                )

            assert exc_info.value.status_code == 401


# =============================================================================
# Require User Auth Tests
# =============================================================================


class TestRequireUserAuth:
    """Tests for require_user_auth dependency."""

    def test_no_authorization(self, db_session):
        """Test with no authorization header."""
        with pytest.raises(HTTPException) as exc_info:
            auth_dependencies.require_user_auth(
                authorization=None,
                request=None,
                db=db_session,
            )
        assert exc_info.value.status_code == 401

    def test_invalid_authorization_format(self, db_session):
        """Test with invalid authorization format."""
        with pytest.raises(HTTPException) as exc_info:
            auth_dependencies.require_user_auth(
                authorization="Basic abc123",  # Not Bearer
                request=None,
                db=db_session,
            )
        assert exc_info.value.status_code == 401

    def test_missing_person_id_in_payload(self, db_session):
        """Test with JWT missing person_id."""
        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {"session_id": str(uuid.uuid4())}  # No sub

            with pytest.raises(HTTPException) as exc_info:
                auth_dependencies.require_user_auth(
                    authorization="Bearer header.payload.signature",
                    request=None,
                    db=db_session,
                )

            assert exc_info.value.status_code == 401

    def test_missing_session_id_in_payload(self, db_session, person):
        """Test with JWT missing session_id."""
        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {"sub": str(person.id)}  # No session_id

            with pytest.raises(HTTPException) as exc_info:
                auth_dependencies.require_user_auth(
                    authorization="Bearer header.payload.signature",
                    request=None,
                    db=db_session,
                )

            assert exc_info.value.status_code == 401

    def test_session_not_found(self, db_session, person):
        """Test with non-existent session."""
        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(uuid.uuid4()),  # Non-existent
            }

            with pytest.raises(HTTPException) as exc_info:
                auth_dependencies.require_user_auth(
                    authorization="Bearer header.payload.signature",
                    request=None,
                    db=db_session,
                )

            assert exc_info.value.status_code == 401

    def test_session_expired(self, db_session, person):
        """Test with expired session."""
        from app.models.auth import Session as AuthSession, SessionStatus

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        expired_at = now_naive - timedelta(hours=1)
        token_hash = hashlib.sha256(b"test-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=expired_at,  # Expired
        )
        db_session.add(auth_session)
        db_session.commit()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
            }

            with pytest.raises(HTTPException) as exc_info:
                auth_dependencies.require_user_auth(
                    authorization="Bearer header.payload.signature",
                    request=None,
                    db=db_session,
                )

            assert exc_info.value.status_code == 401

    def test_success_with_roles_and_scopes(self, db_session, person):
        """Test successful auth with roles and scopes."""
        from app.models.auth import Session as AuthSession, SessionStatus

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        token_hash = hashlib.sha256(b"test-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=now_naive + timedelta(hours=24),
        )
        db_session.add(auth_session)
        db_session.commit()

        mock_request = MagicMock()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                "roles": ["admin", "user"],
                "scopes": ["read", "write"],
            }

            result = auth_dependencies.require_user_auth(
                authorization="Bearer header.payload.signature",
                request=mock_request,
                db=db_session,
            )

            assert result["person_id"] == str(person.id)
            assert result["session_id"] == str(auth_session.id)
            assert result["roles"] == ["admin", "user"]
            assert result["scopes"] == ["read", "write"]
            assert mock_request.state.actor_id == str(person.id)

    def test_success_empty_roles_and_scopes(self, db_session, person):
        """Test successful auth with no roles/scopes."""
        from app.models.auth import Session as AuthSession, SessionStatus

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        token_hash = hashlib.sha256(b"test-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=now_naive + timedelta(hours=24),
        )
        db_session.add(auth_session)
        db_session.commit()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                # No roles or scopes
            }

            result = auth_dependencies.require_user_auth(
                authorization="Bearer header.payload.signature",
                request=None,
                db=db_session,
            )

            assert result["roles"] == []
            assert result["scopes"] == []


# =============================================================================
# Require Role Tests
# =============================================================================


class TestRequireRole:
    """Tests for require_role dependency factory."""

    def test_role_in_jwt_payload(self, db_session, person):
        """Test when role is already in JWT payload."""
        from app.models.auth import Session as AuthSession, SessionStatus

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        token_hash = hashlib.sha256(b"test-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=now_naive + timedelta(hours=24),
        )
        db_session.add(auth_session)
        db_session.commit()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                "roles": ["admin"],  # Has the required role
            }

            # Create the dependency
            require_admin = auth_dependencies.require_role("admin")

            # First call require_user_auth to get auth dict
            auth = auth_dependencies.require_user_auth(
                authorization="Bearer header.payload.signature",
                request=None,
                db=db_session,
            )

            # Call the role dependency
            result = require_admin(auth=auth, db=db_session)

            assert result["person_id"] == str(person.id)

    def test_role_in_database(self, db_session, person):
        """Test when role is assigned via database."""
        from app.models.auth import Session as AuthSession, SessionStatus
        from app.models.rbac import Role, PersonRole

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        token_hash = hashlib.sha256(b"test-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=now_naive + timedelta(hours=24),
        )
        db_session.add(auth_session)

        # Create role in database
        role = Role(name="editor", is_active=True)
        db_session.add(role)
        db_session.commit()

        # Link person to role
        person_role = PersonRole(person_id=person.id, role_id=role.id)
        db_session.add(person_role)
        db_session.commit()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                "roles": [],  # Not in JWT
            }

            auth = auth_dependencies.require_user_auth(
                authorization="Bearer header.payload.signature",
                request=None,
                db=db_session,
            )

            require_editor = auth_dependencies.require_role("editor")
            result = require_editor(auth=auth, db=db_session)

            assert result["person_id"] == str(person.id)

    def test_role_not_found(self, db_session, person):
        """Test when role doesn't exist in database."""
        from app.models.auth import Session as AuthSession, SessionStatus

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        token_hash = hashlib.sha256(b"test-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=now_naive + timedelta(hours=24),
        )
        db_session.add(auth_session)
        db_session.commit()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                "roles": [],
            }

            auth = auth_dependencies.require_user_auth(
                authorization="Bearer header.payload.signature",
                request=None,
                db=db_session,
            )

            require_nonexistent = auth_dependencies.require_role("nonexistent_role")

            with pytest.raises(HTTPException) as exc_info:
                require_nonexistent(auth=auth, db=db_session)

            assert exc_info.value.status_code == 403
            assert "Role not found" in exc_info.value.detail

    def test_user_lacks_role(self, db_session, person):
        """Test when user doesn't have required role."""
        from app.models.auth import Session as AuthSession, SessionStatus
        from app.models.rbac import Role

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        token_hash = hashlib.sha256(b"test-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=now_naive + timedelta(hours=24),
        )
        db_session.add(auth_session)

        # Create role but don't assign to user
        role = Role(name="super_admin", is_active=True)
        db_session.add(role)
        db_session.commit()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                "roles": [],
            }

            auth = auth_dependencies.require_user_auth(
                authorization="Bearer header.payload.signature",
                request=None,
                db=db_session,
            )

            require_super_admin = auth_dependencies.require_role("super_admin")

            with pytest.raises(HTTPException) as exc_info:
                require_super_admin(auth=auth, db=db_session)

            assert exc_info.value.status_code == 403
            assert "Forbidden" in exc_info.value.detail


# =============================================================================
# Require Permission Tests
# =============================================================================


class TestRequirePermission:
    """Tests for require_permission dependency factory."""

    def test_admin_role_grants_any_permission(self, db_session, person):
        """Test admin role grants any permission."""
        from app.models.auth import Session as AuthSession, SessionStatus

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        token_hash = hashlib.sha256(b"test-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=now_naive + timedelta(hours=24),
        )
        db_session.add(auth_session)
        db_session.commit()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                "roles": ["admin"],
            }

            auth = auth_dependencies.require_user_auth(
                authorization="Bearer header.payload.signature",
                request=None,
                db=db_session,
            )

            require_any = auth_dependencies.require_permission("any:permission")
            result = require_any(auth=auth, db=db_session)

            assert result["person_id"] == str(person.id)

    def test_permission_in_scopes(self, db_session, person):
        """Test permission granted via scopes."""
        from app.models.auth import Session as AuthSession, SessionStatus

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        token_hash = hashlib.sha256(b"test-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=now_naive + timedelta(hours=24),
        )
        db_session.add(auth_session)
        db_session.commit()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                "scopes": ["tickets:read", "tickets:write"],
            }

            auth = auth_dependencies.require_user_auth(
                authorization="Bearer header.payload.signature",
                request=None,
                db=db_session,
            )

            require_tickets_read = auth_dependencies.require_permission("tickets:read")
            result = require_tickets_read(auth=auth, db=db_session)

            assert result["person_id"] == str(person.id)

    def test_permission_via_role_in_database(self, db_session, person):
        """Test permission granted via role-permission link in database."""
        from app.models.auth import Session as AuthSession, SessionStatus
        from app.models.rbac import Role, Permission, PersonRole, RolePermission

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        token_hash = hashlib.sha256(b"test-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=now_naive + timedelta(hours=24),
        )
        db_session.add(auth_session)

        # Create role, permission, and links
        role = Role(name="support", is_active=True)
        db_session.add(role)

        permission = Permission(
            key="tickets:manage",
            description="Manage Tickets",
            is_active=True,
        )
        db_session.add(permission)
        db_session.commit()

        person_role = PersonRole(person_id=person.id, role_id=role.id)
        db_session.add(person_role)

        role_permission = RolePermission(role_id=role.id, permission_id=permission.id)
        db_session.add(role_permission)
        db_session.commit()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                "roles": [],
                "scopes": [],
            }

            auth = auth_dependencies.require_user_auth(
                authorization="Bearer header.payload.signature",
                request=None,
                db=db_session,
            )

            require_tickets_manage = auth_dependencies.require_permission("tickets:manage")
            result = require_tickets_manage(auth=auth, db=db_session)

            assert result["person_id"] == str(person.id)

    def test_permission_not_found(self, db_session, person):
        """Test when permission doesn't exist."""
        from app.models.auth import Session as AuthSession, SessionStatus

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        token_hash = hashlib.sha256(b"test-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=now_naive + timedelta(hours=24),
        )
        db_session.add(auth_session)
        db_session.commit()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                "roles": [],
                "scopes": [],
            }

            auth = auth_dependencies.require_user_auth(
                authorization="Bearer header.payload.signature",
                request=None,
                db=db_session,
            )

            require_nonexistent = auth_dependencies.require_permission("nonexistent:permission")

            with pytest.raises(HTTPException) as exc_info:
                require_nonexistent(auth=auth, db=db_session)

            assert exc_info.value.status_code == 403
            assert "Permission not found" in exc_info.value.detail

    def test_user_lacks_permission(self, db_session, person):
        """Test when user doesn't have required permission."""
        from app.models.auth import Session as AuthSession, SessionStatus
        from app.models.rbac import Permission

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        token_hash = hashlib.sha256(b"test-token").hexdigest()

        auth_session = AuthSession(
            person_id=person.id,
            status=SessionStatus.active,
            token_hash=token_hash,
            expires_at=now_naive + timedelta(hours=24),
        )
        db_session.add(auth_session)

        # Create permission but don't assign to user
        permission = Permission(key="billing:admin", description="Billing Admin", is_active=True)
        db_session.add(permission)
        db_session.commit()

        with patch.object(auth_dependencies, "decode_access_token") as mock_decode:
            mock_decode.return_value = {
                "sub": str(person.id),
                "session_id": str(auth_session.id),
                "roles": [],
                "scopes": [],
            }

            auth = auth_dependencies.require_user_auth(
                authorization="Bearer header.payload.signature",
                request=None,
                db=db_session,
            )

            require_billing_admin = auth_dependencies.require_permission("billing:admin")

            with pytest.raises(HTTPException) as exc_info:
                require_billing_admin(auth=auth, db=db_session)

            assert exc_info.value.status_code == 403
            assert "Forbidden" in exc_info.value.detail
