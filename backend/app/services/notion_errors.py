"""Stable Notion error codes. Public messages never include tokens, UUIDs, or URLs."""
from __future__ import annotations


class NotionServiceError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


NOTION_DISABLED = "NOTION_DISABLED"
NOTION_NOT_CONFIGURED = "NOTION_NOT_CONFIGURED"
NOTION_OAUTH_STATE_INVALID = "NOTION_OAUTH_STATE_INVALID"
NOTION_OAUTH_STATE_EXPIRED = "NOTION_OAUTH_STATE_EXPIRED"
NOTION_OAUTH_EXCHANGE_FAILED = "NOTION_OAUTH_EXCHANGE_FAILED"
NOTION_TOKEN_EXCHANGE_FAILED = "NOTION_TOKEN_EXCHANGE_FAILED"
NOTION_CONNECTION_CONFLICT = "NOTION_CONNECTION_CONFLICT"
NOTION_CONNECTION_REQUIRED = "NOTION_CONNECTION_REQUIRED"
NOTION_REAUTH_REQUIRED = "NOTION_REAUTH_REQUIRED"
NOTION_SYNC_IN_PROGRESS = "NOTION_SYNC_IN_PROGRESS"
NOTION_RATE_LIMITED = "NOTION_RATE_LIMITED"
NOTION_SYNC_FAILED = "NOTION_SYNC_FAILED"
NOTION_WEBHOOK_INVALID = "NOTION_WEBHOOK_INVALID"
NOTION_PAGE_NOT_FOUND = "NOTION_PAGE_NOT_FOUND"
NOTION_PAGE_UNAVAILABLE = "NOTION_PAGE_UNAVAILABLE"
NOTION_SOURCE_SYNC_PENDING = "NOTION_SOURCE_SYNC_PENDING"
NOTION_INTERNAL_ERROR = "NOTION_INTERNAL_ERROR"
NOTION_PERMISSION_LOST = "NOTION_PERMISSION_LOST"
NOTION_REMOTE_DELETED = "NOTION_REMOTE_DELETED"
PAGE_TEXT_LIMIT = "PAGE_TEXT_LIMIT"

SAFE_MESSAGES = {
    NOTION_DISABLED: "Notion 接入未启用。",
    NOTION_NOT_CONFIGURED: "Notion 接入尚未完成配置。",
    NOTION_OAUTH_STATE_INVALID: "授权状态无效或已被使用。",
    NOTION_OAUTH_STATE_EXPIRED: "授权已过期，请重新连接。",
    NOTION_OAUTH_EXCHANGE_FAILED: "Notion 授权交换失败，请重试。",
    NOTION_TOKEN_EXCHANGE_FAILED: "Notion 授权交换失败，请重试。",
    NOTION_CONNECTION_CONFLICT: "当前连接正在更新，请稍后重试。",
    NOTION_CONNECTION_REQUIRED: "尚未连接 Notion。",
    NOTION_REAUTH_REQUIRED: "需要重新授权 Notion。",
    NOTION_SYNC_IN_PROGRESS: "正在同步 Notion，请稍后再试。",
    NOTION_RATE_LIMITED: "Notion 请求过于频繁，请稍后重试。",
    NOTION_SYNC_FAILED: "Notion 同步失败，请稍后重试。",
    NOTION_WEBHOOK_INVALID: "Webhook 校验失败。",
    NOTION_PAGE_NOT_FOUND: "页面不存在或无权访问。",
    NOTION_PAGE_UNAVAILABLE: "页面当前不可用。",
    NOTION_SOURCE_SYNC_PENDING: "该 Notion 页面正在同步，请稍后再试。",
    NOTION_INTERNAL_ERROR: "Notion 服务暂时不可用。",
}


def public_message(code: str) -> str:
    return SAFE_MESSAGES.get(code, SAFE_MESSAGES[NOTION_INTERNAL_ERROR])


def raise_notion(code: str, *, status_code: int = 400, retry_after_seconds: int | None = None) -> None:
    raise NotionServiceError(
        code,
        public_message(code),
        status_code=status_code,
        retry_after_seconds=retry_after_seconds,
    )
