"""Safe Notion URL and public reference helpers."""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse
from uuid import UUID

ALLOWED_NOTION_HOSTS = ("notion.so",)


def sanitize_notion_url(raw: Optional[str]) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = urlparse(text)
    except Exception:
        return None
    if parsed.scheme != "https":
        return None
    if parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return None
    if host == "notion.so" or host.endswith(".notion.so"):
        return parsed.geturl()
    return None


def resolve_notion_page_url(raw: Optional[str], page_uuid: Optional[str]) -> Optional[str]:
    """Recover canonical links for mirrors whose provider omitted the URL."""
    safe = sanitize_notion_url(raw)
    if safe:
        return safe
    try:
        identifier = UUID(str(page_uuid or ""))
    except (ValueError, AttributeError):
        return None
    return f"https://www.notion.so/{identifier.hex}"


def page_is_searchable(page: Any) -> bool:
    status = str(getattr(page, "sync_status", "") or "")
    observed = str(getattr(page, "observed_content_hash", "") or "")
    indexed = str(getattr(page, "indexed_content_hash", "") or "")
    if status not in {"indexed", "partial"}:
        return False
    if not observed or not indexed or observed != indexed:
        return False
    return True


def infer_notion_object_kind(page: Any, known_page_uuids: set[str]) -> str:
    """Distinguish database rows without mislabeling an orphaned child page."""
    parent = str(getattr(page, "parent_notion_page_uuid", "") or "")
    breadcrumb = str(getattr(page, "breadcrumb", "") or "")
    parts = [part for part in breadcrumb.split(" / ") if part.strip()]
    if parent and parent not in known_page_uuids and len(parts) >= 2:
        return "database_row"
    return "page"


def notion_page_reference(
    page: Any,
    *,
    method: str,
    score: float,
    snippet: Optional[str] = None,
    object_kind: str = "page",
) -> dict[str, Any]:
    page_id = int(page.id)
    title = str(page.title or "").strip()[:120] or "Notion 页面"
    text = str(page.normalized_text or "")
    synced = page.last_sync_completed_at if hasattr(page, "last_sync_completed_at") else None
    if synced is None:
        synced = getattr(page, "updated_at", None)
    return {
        "source_type": "notion_page",
        "source_id": page_id,
        "notion_page_id": page_id,
        "entry_id": None,
        "chunk_id": None,
        "reference_key": f"notion_page:source:{page_id}",
        "title": title,
        "label_name": "Notion",
        "created_at": page.created_at.isoformat() if getattr(page, "created_at", None) else None,
        "snippet": (snippet or text)[:420],
        "content": text,
        "relevance_score": float(score),
        "retrieval_method": method,
        "source_reason": "notion_page_match",
        "metadata": {
            "origin_type": "notion_page",
            "origin_id": page_id,
            "breadcrumb": str(page.breadcrumb or ""),
            "sync_status": str(page.sync_status or ""),
            "object_kind": "database_row" if object_kind == "database_row" else "page",
            "synced_at": synced.isoformat() if synced is not None and hasattr(synced, "isoformat") else None,
            "content_hash": str(page.indexed_content_hash or ""),
            "has_open_url": bool(resolve_notion_page_url(getattr(page, "notion_url", None), getattr(page, "notion_page_uuid", None))),
        },
    }
