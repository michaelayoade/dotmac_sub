"""Schemas for the live-chat broker endpoints.

The sub never exposes the CRM's conversation internals; it hands the client an
opaque visitor token plus the URLs needed to talk to the CRM chat_widget
channel directly (REST for send/history, WebSocket for real-time).
"""

from __future__ import annotations

from pydantic import BaseModel


class ChatSessionResponse(BaseModel):
    """Everything a client needs to drive the CRM chat widget for one session."""

    session_id: str
    visitor_token: str
    conversation_id: str | None = None
    # CRM endpoints the client talks to directly with X-Visitor-Token.
    ws_url: str
    api_base: str
