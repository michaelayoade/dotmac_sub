from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class NextcloudTalkAuth(BaseModel):
    connector_config_id: UUID | None = None
    base_url: str | None = Field(default=None, max_length=500)
    username: str | None = Field(default=None, max_length=150)
    app_password: str | None = Field(default=None, max_length=255)
    timeout_sec: int | None = Field(default=None, ge=1, le=120)

    @model_validator(mode="after")
    def _validate_auth(self) -> NextcloudTalkAuth:
        if self.connector_config_id is None:
            if not self.base_url or not self.username or not self.app_password:
                raise ValueError(
                    "Provide base_url, username, and app_password when connector_config_id is not set."
                )
        return self


class NextcloudTalkRoomListRequest(NextcloudTalkAuth):
    pass


class NextcloudTalkRoomCreateRequest(NextcloudTalkAuth):
    room_name: str = Field(min_length=1, max_length=200)
    room_type: str | int = Field(default="public")
    options: dict | None = None


class NextcloudTalkMessageRequest(NextcloudTalkAuth):
    message: str = Field(min_length=1)
    options: dict | None = None
