"""Notion integration API contracts. No tokens, UUIDs, or provider IDs."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class NotionPreflight(BaseModel):
    enabled: bool
    client_configured: bool
    secret_configured: bool
    redirect_configured: bool
    token_key_configured: bool
    webhook_token_configured: bool
    api_version_configured: bool
    sync_enabled: bool


class NotionStatusResponse(BaseModel):
    enabled: bool
    configured: bool
    connected: bool = False
    ui_state: str
    workspace_name: Optional[str] = None
    sync_status: Optional[str] = None
    connection_status: Optional[str] = None
    page_count: Optional[int] = None
    discovered_count: Optional[int] = None
    indexed_count: Optional[int] = None
    partial_count: Optional[int] = None
    pending_count: Optional[int] = None
    last_sync_completed_at: Optional[str] = None
    last_error_code: Optional[str] = None
    preflight: NotionPreflight


class NotionOAuthStartResponse(BaseModel):
    authorization_url: str


class NotionSyncResponse(BaseModel):
    accepted: bool
    idempotent: bool = False


class NotionPageListItem(BaseModel):
    title: str
    breadcrumb: str = ""
    sync_status: str
    unsupported_block_count: int = 0


class NotionPageListResponse(BaseModel):
    items: List[NotionPageListItem] = Field(default_factory=list)
    next_cursor: Optional[str] = None
