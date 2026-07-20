"""Schemas for the native team-inbox live-chat broker endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class ChatSessionResponse(BaseModel):
    """Everything a client needs to drive one native team-inbox chat session."""

    session_id: str
    visitor_token: str
    conversation_id: str | None = None
    # Native widget endpoints the client calls with X-Visitor-Token.
    ws_url: str
    api_base: str
