"""Pydantic schemas for notification settings (no secrets/UID/endpoint)."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class NotificationSettingsResponse(BaseModel):
    state: str
    reminders_enabled: bool
    today_plan_time: Optional[str] = None
    unfinished_time: Optional[str] = None
    urgent_overdue_enabled: bool = True
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    privacy_note: str
    web_push_ready: bool = False
    active_device_count: int = 0
    sound_note: Optional[str] = None


class NotificationSettingsPatch(BaseModel):
    reminders_enabled: Optional[bool] = None
    today_plan_time: Optional[str] = Field(default=None, description="HH:MM")
    unfinished_time: Optional[str] = Field(default=None, description="HH:MM")
    urgent_overdue_enabled: Optional[bool] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None


class TestSendRequest(BaseModel):
    endpoint: str = Field(..., min_length=12, max_length=2048)


class TestSendResponse(BaseModel):
    ok: bool
    message_id_present: bool = False
    cooldown_seconds: int = 60


class PreflightResponse(BaseModel):
    public_base_url_configured: bool
    worker_enabled: bool
    web_push_enabled: bool = False
    web_push_vapid_configured: bool = False
    web_push_crypto_configured: bool = False
    web_push_ready: bool = False


class WebPushPublicKeyResponse(BaseModel):
    public_key: str
    ready: bool
    enabled: bool = False


class WebPushKeys(BaseModel):
    p256dh: str = Field(..., min_length=8, max_length=512)
    auth: str = Field(..., min_length=8, max_length=512)


class WebPushSubscriptionRequest(BaseModel):
    endpoint: str = Field(..., min_length=12, max_length=2048)
    expirationTime: Optional[float] = None
    keys: WebPushKeys

    @field_validator("expirationTime", mode="before")
    @classmethod
    def _expiration_time_strict(cls, value: Any) -> Any:
        if value is None:
            return None
        if type(value) is bool or type(value) not in (int, float):
            raise ValueError("expirationTime must be a number or null")
        return float(value)


class WebPushSubscriptionDeleteRequest(BaseModel):
    endpoint: str = Field(..., min_length=12, max_length=2048)


class WebPushStatusRequest(BaseModel):
    endpoint: str = Field(..., min_length=12, max_length=2048)


class WebPushStatusResponse(BaseModel):
    current_device_active: bool
    active_device_count: int = 0


class WebPushOkResponse(BaseModel):
    ok: bool
    active_device_count: int = 0
