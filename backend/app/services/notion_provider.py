"""Notion HTTPS provider boundary. Routes and sync never call Notion HTTP directly."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from app.config import settings
from app.services.notion_errors import (
    NOTION_INTERNAL_ERROR,
    NOTION_OAUTH_EXCHANGE_FAILED,
    NOTION_PERMISSION_LOST,
    NOTION_RATE_LIMITED,
    NOTION_REAUTH_REQUIRED,
    NOTION_REMOTE_DELETED,
    NotionServiceError,
    public_message,
)

logger = logging.getLogger(__name__)

NOTION_API_HOST = "api.notion.com"
NOTION_OAUTH_AUTHORIZE_PATH = "/v1/oauth/authorize"
USER_AGENT = f"GrowthLog/{settings.growthlog_version}"
MAX_RETRIES = 6
MAX_AVG_RPS = 3.0
DEFAULT_TIMEOUT = 20.0


class NotionProviderError(NotionServiceError):
    def __init__(self, code: str, *, status_code: int = 400, retry_after_seconds: int | None = None):
        super().__init__(code, public_message(code), status_code=status_code, retry_after_seconds=retry_after_seconds)


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: Optional[str]
    bot_id: str
    workspace_id: str
    workspace_name: str
    owner_notion_user_id: Optional[str]
    expires_at_epoch: Optional[int]


class RateLimiter:
    def __init__(self, rate: float = MAX_AVG_RPS) -> None:
        self.rate = rate
        self._tokens = rate
        self._updated = time.monotonic()

    def acquire(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.rate, self._tokens + (now - self._updated) * self.rate)
        self._updated = now
        if self._tokens < 1.0:
            time.sleep((1.0 - self._tokens) / self.rate)
            self._tokens = 0.0
            self._updated = time.monotonic()
        else:
            self._tokens -= 1.0


def _fixed_api_base() -> str:
    configured = (settings.notion_api_host or "https://api.notion.com").strip()
    parsed = urlparse(configured)
    if parsed.scheme != "https" or parsed.hostname != NOTION_API_HOST:
        return "https://api.notion.com"
    return "https://api.notion.com"


def _safe_status(status: Optional[int]) -> None:
    logger.warning("notion_provider_http status=%s", status)


class NotionHttpProvider:
    def __init__(self) -> None:
        self._limiters: dict[str, RateLimiter] = {}

    def _limiter(self, connection_key: str) -> RateLimiter:
        bucket = self._limiters.get(connection_key)
        if bucket is None:
            bucket = RateLimiter()
            self._limiters[connection_key] = bucket
        return bucket

    def _headers(self, access_token: Optional[str] = None) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Notion-Version": (settings.notion_api_version or "2026-03-11").strip(),
            "Accept": "application/json",
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: Optional[str] = None,
        json_body: Optional[dict[str, Any]] = None,
        connection_key: str = "default",
        allow_refresh: bool = False,
        refresh_cb: Optional[Any] = None,
    ) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        self._limiter(connection_key).acquire()
        url = f"{_fixed_api_base()}{path}"
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != NOTION_API_HOST:
            raise NotionProviderError(NOTION_INTERNAL_ERROR, status_code=500)
        last_error: Optional[NotionProviderError] = None
        refreshed = False
        for attempt in range(MAX_RETRIES):
            body = None if json_body is None else json.dumps(json_body).encode("utf-8")
            headers = self._headers(access_token)
            if body is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                    raw = resp.read()
                    if not raw:
                        return {}
                    parsed_json = json.loads(raw.decode("utf-8"))
                    if not isinstance(parsed_json, dict):
                        raise NotionProviderError(NOTION_INTERNAL_ERROR, status_code=502)
                    return parsed_json
            except urllib.error.HTTPError as exc:
                status = int(getattr(exc, "code", 0) or 0)
                retry_after = None
                try:
                    retry_after_raw = exc.headers.get("Retry-After") if exc.headers else None
                    if retry_after_raw:
                        retry_after = max(1, int(float(retry_after_raw)))
                except Exception:
                    retry_after = None
                _safe_status(status)
                if status == 401 and allow_refresh and not refreshed and refresh_cb is not None:
                    access_token = refresh_cb()
                    refreshed = True
                    continue
                if status == 401:
                    raise NotionProviderError(NOTION_REAUTH_REQUIRED, status_code=401) from exc
                if status == 403:
                    raise NotionProviderError(NOTION_PERMISSION_LOST, status_code=403) from exc
                if status == 404:
                    raise NotionProviderError(NOTION_REMOTE_DELETED, status_code=404) from exc
                if status == 429:
                    last_error = NotionProviderError(
                        NOTION_RATE_LIMITED,
                        status_code=429,
                        retry_after_seconds=retry_after or min(30, 2 ** attempt),
                    )
                    time.sleep(float(last_error.retry_after_seconds or 1))
                    continue
                if status >= 500:
                    last_error = NotionProviderError(NOTION_INTERNAL_ERROR, status_code=502)
                    time.sleep(min(30, 2 ** attempt))
                    continue
                raise NotionProviderError(NOTION_INTERNAL_ERROR, status_code=502) from exc
            except TimeoutError as exc:
                last_error = NotionProviderError(NOTION_INTERNAL_ERROR, status_code=504)
                time.sleep(min(30, 2 ** attempt))
                if attempt + 1 >= MAX_RETRIES:
                    raise last_error from exc
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__.lower()
                if "timeout" in name:
                    last_error = NotionProviderError(NOTION_INTERNAL_ERROR, status_code=504)
                    time.sleep(min(30, 2 ** attempt))
                    continue
                logger.warning("notion_provider_network code=%s", type(exc).__name__)
                last_error = NotionProviderError(NOTION_INTERNAL_ERROR, status_code=502)
                time.sleep(min(30, 2 ** attempt))
        raise last_error or NotionProviderError(NOTION_INTERNAL_ERROR, status_code=502)

    def exchange_code(self, code: str, redirect_uri: str) -> TokenBundle:
        import base64
        import urllib.error
        import urllib.request

        client_id = (settings.notion_oauth_client_id or "").strip()
        client_secret = (settings.notion_oauth_client_secret or "").strip()
        if not client_id or not client_secret:
            raise NotionProviderError(NOTION_OAUTH_EXCHANGE_FAILED, status_code=503)
        payload = json.dumps(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        ).encode("utf-8")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        req = urllib.request.Request(
            f"{_fixed_api_base()}/v1/oauth/token",
            data=payload,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Notion-Version": (settings.notion_api_version or "2026-03-11").strip(),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("notion_oauth_exchange_failed")
            raise NotionProviderError(NOTION_OAUTH_EXCHANGE_FAILED, status_code=400) from exc
        return _token_bundle_from_payload(data)

    def refresh_token(self, refresh_token: str) -> TokenBundle:
        import base64
        import urllib.request

        client_id = (settings.notion_oauth_client_id or "").strip()
        client_secret = (settings.notion_oauth_client_secret or "").strip()
        payload = json.dumps({"grant_type": "refresh_token", "refresh_token": refresh_token}).encode("utf-8")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        req = urllib.request.Request(
            f"{_fixed_api_base()}/v1/oauth/token",
            data=payload,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Notion-Version": (settings.notion_api_version or "2026-03-11").strip(),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("notion_oauth_refresh_failed")
            raise NotionProviderError(NOTION_REAUTH_REQUIRED, status_code=401) from exc
        return _token_bundle_from_payload(data)

    def search_pages(self, access_token: str, *, connection_key: str, start_cursor: Optional[str] = None) -> dict[str, Any]:
        body: dict[str, Any] = {"page_size": 100, "filter": {"value": "page", "property": "object"}}
        if start_cursor:
            body["start_cursor"] = start_cursor
        return self._request("POST", "/v1/search", access_token=access_token, json_body=body, connection_key=connection_key)

    def search_data_sources(self, access_token: str, *, connection_key: str, start_cursor: Optional[str] = None) -> dict[str, Any]:
        body: dict[str, Any] = {"page_size": 100, "filter": {"value": "data_source", "property": "object"}}
        if start_cursor:
            body["start_cursor"] = start_cursor
        return self._request("POST", "/v1/search", access_token=access_token, json_body=body, connection_key=connection_key)

    def get_page(self, access_token: str, page_id: str, *, connection_key: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/pages/{page_id}", access_token=access_token, connection_key=connection_key)

    def list_block_children(
        self,
        access_token: str,
        block_id: str,
        *,
        connection_key: str,
        start_cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        path = f"/v1/blocks/{block_id}/children?page_size=100"
        if start_cursor:
            path += f"&start_cursor={start_cursor}"
        return self._request("GET", path, access_token=access_token, connection_key=connection_key)

    def query_data_source(
        self,
        access_token: str,
        data_source_id: str,
        *,
        connection_key: str,
        start_cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor
        return self._request(
            "POST",
            f"/v1/data_sources/{data_source_id}/query",
            access_token=access_token,
            json_body=body,
            connection_key=connection_key,
        )


def _token_bundle_from_payload(data: dict[str, Any]) -> TokenBundle:
    if not isinstance(data, dict) or not data.get("access_token"):
        raise NotionProviderError(NOTION_OAUTH_EXCHANGE_FAILED, status_code=400)
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    owner_user = owner.get("user") if isinstance(owner.get("user"), dict) else {}
    return TokenBundle(
        access_token=str(data.get("access_token") or ""),
        refresh_token=str(data["refresh_token"]) if data.get("refresh_token") else None,
        bot_id=str(data.get("bot_id") or data.get("id") or ""),
        workspace_id=str(data.get("workspace_id") or workspace.get("id") or ""),
        workspace_name=str(data.get("workspace_name") or workspace.get("name") or ""),
        owner_notion_user_id=str(owner_user.get("id") or "") or None,
        expires_at_epoch=int(data["expires_at"]) if data.get("expires_at") else None,
    )


class FakeNotionProvider:
    def __init__(self, state_path: Optional[Path] = None) -> None:
        self._path = state_path or _fake_state_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write(_default_fake_state())

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return _default_fake_state()

    def _write(self, data: dict[str, Any]) -> None:
        self._path.write_text(json.dumps(data), encoding="utf-8")

    def configure(self, **updates: Any) -> None:
        data = self._read()
        data.update(updates)
        self._write(data)

    def exchange_code(self, code: str, redirect_uri: str) -> TokenBundle:
        data = self._read()
        fail = data.get("fail_exchange")
        if fail:
            data["fail_exchange"] = False
            self._write(data)
            raise NotionProviderError(NOTION_OAUTH_EXCHANGE_FAILED, status_code=400)
        workspace = data.get("workspace") or {}
        return TokenBundle(
            access_token=str(data.get("access_token") or "fake-access"),
            refresh_token=str(data.get("refresh_token") or "fake-refresh"),
            bot_id=str(workspace.get("bot_id") or "fake-bot"),
            workspace_id=str(workspace.get("id") or "fake-workspace"),
            workspace_name=str(workspace.get("name") or "Fake Workspace"),
            owner_notion_user_id="fake-owner",
            expires_at_epoch=None,
        )

    def refresh_token(self, refresh_token: str) -> TokenBundle:
        data = self._read()
        data["refresh_calls"] = int(data.get("refresh_calls") or 0) + 1
        if data.get("fail_refresh"):
            self._write(data)
            raise NotionProviderError(NOTION_REAUTH_REQUIRED, status_code=401)
        data["access_token"] = "fake-access-refreshed"
        self._write(data)
        return self.exchange_code("unused", "unused")

    def _maybe_fault(self, kind: str) -> None:
        data = self._read()
        if data.get("force_401"):
            if not data.get("consumed_401"):
                data["consumed_401"] = True
                self._write(data)
                raise NotionProviderError(NOTION_REAUTH_REQUIRED, status_code=401)
        if data.get("force_429"):
            data["force_429"] = False
            self._write(data)
            raise NotionProviderError(NOTION_RATE_LIMITED, status_code=429, retry_after_seconds=1)
        lost = set(data.get("permission_lost_ids") or [])
        deleted = set(data.get("deleted_ids") or [])
        if kind in lost:
            raise NotionProviderError(NOTION_PERMISSION_LOST, status_code=403)
        if kind in deleted:
            raise NotionProviderError(NOTION_REMOTE_DELETED, status_code=404)

    def search_pages(self, access_token: str, *, connection_key: str, start_cursor: Optional[str] = None) -> dict[str, Any]:
        self._maybe_fault("search")
        pages = list((self._read().get("pages") or []))
        return _paginate(pages, start_cursor)

    def search_data_sources(self, access_token: str, *, connection_key: str, start_cursor: Optional[str] = None) -> dict[str, Any]:
        sources = list((self._read().get("data_sources") or []))
        return _paginate(sources, start_cursor)

    def get_page(self, access_token: str, page_id: str, *, connection_key: str) -> dict[str, Any]:
        self._maybe_fault(page_id)
        for page in self._read().get("pages") or []:
            if page.get("id") == page_id:
                return page
        raise NotionProviderError(NOTION_REMOTE_DELETED, status_code=404)

    def list_block_children(
        self,
        access_token: str,
        block_id: str,
        *,
        connection_key: str,
        start_cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        self._maybe_fault(block_id)
        tree = self._read().get("blocks") or {}
        blocks = list(tree.get(block_id) or [])
        return _paginate(blocks, start_cursor)

    def query_data_source(
        self,
        access_token: str,
        data_source_id: str,
        *,
        connection_key: str,
        start_cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        rows = list((self._read().get("data_source_rows") or {}).get(data_source_id) or [])
        return _paginate(rows, start_cursor)


def _paginate(items: list[dict[str, Any]], start_cursor: Optional[str], page_size: int = 2) -> dict[str, Any]:
    start = 0
    if start_cursor:
        try:
            start = int(start_cursor)
        except ValueError:
            start = 0
    chunk = items[start: start + page_size]
    nxt = start + page_size
    has_more = nxt < len(items)
    return {
        "results": chunk,
        "has_more": has_more,
        "next_cursor": str(nxt) if has_more else None,
    }


def _default_fake_state() -> dict[str, Any]:
    parent = "11111111-1111-1111-1111-111111111111"
    child_a = "22222222-2222-2222-2222-222222222222"
    child_b = "33333333-3333-3333-3333-333333333333"
    db_page = "44444444-4444-4444-4444-444444444444"
    return {
        "workspace": {"id": "ws-fake", "name": "Fake Workspace", "bot_id": "bot-fake"},
        "access_token": "fake-access",
        "refresh_token": "fake-refresh",
        "pages": [
            {
                "object": "page",
                "id": parent,
                "url": "https://www.notion.so/fake-parent",
                "last_edited_time": "2026-08-31T00:00:00.000Z",
                "properties": {"title": {"type": "title", "title": [{"plain_text": "Parent Page"}]}},
                "parent": {"type": "workspace"},
            },
            {
                "object": "page",
                "id": child_a,
                "url": "https://www.notion.so/fake-child-a",
                "last_edited_time": "2026-08-31T00:00:00.000Z",
                "properties": {"title": {"type": "title", "title": [{"plain_text": "Child Notes"}]}},
                "parent": {"type": "page_id", "page_id": parent},
            },
            {
                "object": "page",
                "id": child_b,
                "url": "https://www.notion.so/fake-child-b",
                "last_edited_time": "2026-08-31T00:00:00.000Z",
                "properties": {"title": {"type": "title", "title": [{"plain_text": "Child Project"}]}},
                "parent": {"type": "page_id", "page_id": parent},
            },
            {
                "object": "page",
                "id": db_page,
                "url": "https://www.notion.so/fake-db-row",
                "last_edited_time": "2026-08-31T00:00:00.000Z",
                "properties": {"Name": {"type": "title", "title": [{"plain_text": "Database Row"}]}},
                "parent": {"type": "data_source_id", "data_source_id": "ds-fake"},
            },
        ],
        "data_sources": [{"object": "data_source", "id": "ds-fake", "title": [{"plain_text": "Fake DB"}], "parent": {"type": "page_id", "page_id": parent}}],
        "data_source_rows": {
            "ds-fake": [
                {"object": "page", "id": db_page, "url": "https://www.notion.so/fake-db-row"},
            ]
        },
        "blocks": {
            parent: [
                {
                    "type": "paragraph",
                    "has_children": False,
                    "paragraph": {"rich_text": [{"plain_text": "Parent body about retrieval safety."}]},
                },
                {"type": "child_page", "has_children": False, "id": child_a, "child_page": {"title": "Child Notes"}},
                {"type": "child_page", "has_children": False, "id": child_b, "child_page": {"title": "Child Project"}},
                {"type": "unsupported", "has_children": False},
            ],
            child_a: [
                {
                    "type": "heading_1",
                    "has_children": False,
                    "heading_1": {"rich_text": [{"plain_text": "Notes heading"}]},
                },
                {
                    "type": "bulleted_list_item",
                    "has_children": False,
                    "bulleted_list_item": {"rich_text": [{"plain_text": "Lexical unique token alphafox"}]},
                },
                {
                    "type": "toggle",
                    "has_children": True,
                    "id": "blk-toggle",
                    "toggle": {"rich_text": [{"plain_text": "Toggle title"}]},
                },
            ],
            "blk-toggle": [
                {
                    "type": "paragraph",
                    "has_children": False,
                    "paragraph": {"rich_text": [{"plain_text": "Nested toggle body betavector"}]},
                }
            ],
            child_b: [
                {
                    "type": "code",
                    "has_children": False,
                    "code": {"rich_text": [{"plain_text": "print('dense unique token gammatree')"}]},
                }
            ],
            db_page: [
                {
                    "type": "paragraph",
                    "has_children": False,
                    "paragraph": {"rich_text": [{"plain_text": "Database row body deltamirror"}]},
                }
            ],
        },
        "refresh_calls": 0,
    }


def _fake_state_path() -> Path:
    override = (os.environ.get("NOTION_FAKE_STATE_PATH") or "").strip()
    if override:
        path = Path(override)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.gettempdir()) / f"growthlog_fake_notion_{os.getpid()}.json"


_FAKE: Optional[FakeNotionProvider] = None
_HTTP = NotionHttpProvider()


def get_fake_notion_provider(state_path: Optional[Path] = None) -> FakeNotionProvider:
    global _FAKE
    if state_path is not None:
        _FAKE = FakeNotionProvider(state_path)
        return _FAKE
    if _FAKE is None:
        _FAKE = FakeNotionProvider()
    return _FAKE


def reset_fake_notion_provider(state_path: Optional[Path] = None) -> FakeNotionProvider:
    global _FAKE
    _FAKE = FakeNotionProvider(state_path)
    return _FAKE


def get_notion_provider() -> Any:
    if settings.notion_use_fake or (os.environ.get("NOTION_USE_FAKE") or "").lower() in {"1", "true", "yes"}:
        return get_fake_notion_provider()
    return _HTTP


def authorization_url(state: str) -> str:
    from urllib.parse import urlencode

    redirect = (settings.notion_oauth_redirect_uri or "").strip()
    client_id = (settings.notion_oauth_client_id or "").strip()
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "owner": "user",
            "redirect_uri": redirect,
            "state": state,
        }
    )
    return f"https://api.notion.com{NOTION_OAUTH_AUTHORIZE_PATH}?{query}"
