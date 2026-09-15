"""Normalize authorized Notion page text. Never include secrets, emails, or media bodies."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

PAGE_TEXT_LIMIT_BYTES = 2 * 1024 * 1024
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
SIGNED_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

ALLOWED_BLOCK_TYPES = {
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "toggle",
    "quote",
    "callout",
    "code",
    "equation",
    "table_row",
    "child_page",
    "child_database",
    "bookmark",
    "file",
    "image",
    "pdf",
    "video",
    "audio",
    "embed",
    "link_preview",
}

MEDIA_NAME_TYPES = {"file", "image", "pdf", "video", "audio", "bookmark", "embed", "link_preview"}
ALLOWED_PROPERTY_TYPES = {
    "title",
    "rich_text",
    "number",
    "checkbox",
    "status",
    "select",
    "multi_select",
    "date",
    "url",
    "formula",
    "rollup",
}


def _plain_rich_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = str(item.get("plain_text") or item.get("text", {}).get("content") or "")
            if text:
                parts.append(text)
    return "".join(parts)


def sanitize_visible_text(text: str) -> str:
    cleaned = EMAIL_RE.sub("", text or "")
    cleaned = UUID_RE.sub("", cleaned)
    cleaned = SIGNED_URL_RE.sub("", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def extract_title(page: dict[str, Any]) -> str:
    props = page.get("properties") if isinstance(page.get("properties"), dict) else {}
    for value in props.values():
        if isinstance(value, dict) and value.get("type") == "title":
            title = sanitize_visible_text(_plain_rich_text(value.get("title")))
            if title:
                return title[:512]
    if page.get("child_page") and isinstance(page["child_page"], dict):
        return sanitize_visible_text(str(page["child_page"].get("title") or ""))[:512]
    return "未命名页面"


def extract_data_source_title(data_source: dict[str, Any]) -> str:
    title = sanitize_visible_text(_plain_rich_text(data_source.get("title")))
    if title:
        return title[:512]
    return "未命名数据库"


def extract_property_text(page: dict[str, Any]) -> list[str]:
    props = page.get("properties") if isinstance(page.get("properties"), dict) else {}
    lines: list[str] = []
    for name, value in props.items():
        if not isinstance(value, dict):
            continue
        kind = str(value.get("type") or "")
        if kind not in ALLOWED_PROPERTY_TYPES:
            continue
        text = ""
        if kind in {"title", "rich_text"}:
            text = _plain_rich_text(value.get(kind))
        elif kind == "number" and value.get("number") is not None:
            text = str(value.get("number"))
        elif kind == "checkbox":
            text = "是" if value.get("checkbox") else "否"
        elif kind in {"status", "select"}:
            selected = value.get(kind) if isinstance(value.get(kind), dict) else {}
            text = str(selected.get("name") or "")
        elif kind == "multi_select":
            text = "、".join(
                str(item.get("name") or "")
                for item in (value.get("multi_select") or [])
                if isinstance(item, dict)
            )
        elif kind == "date":
            date_v = value.get("date") if isinstance(value.get("date"), dict) else {}
            text = str(date_v.get("start") or "")
        elif kind == "url":
            raw_url = str(value.get("url") or "")
            if raw_url.startswith("https://"):
                text = raw_url
        elif kind in {"formula", "rollup"}:
            nested = value.get(kind) if isinstance(value.get(kind), dict) else {}
            if nested.get("type") == "string":
                text = str(nested.get("string") or "")
            elif nested.get("type") == "number" and nested.get("number") is not None:
                text = str(nested.get("number"))
        text = sanitize_visible_text(text)
        if text:
            lines.append(f"{sanitize_visible_text(str(name))}：{text}")
    return lines

def _block_line(block: dict[str, Any]) -> tuple[str, bool]:
    kind = str(block.get("type") or "")
    if kind not in ALLOWED_BLOCK_TYPES:
        return "", True
    payload = block.get(kind) if isinstance(block.get(kind), dict) else {}
    if kind in MEDIA_NAME_TYPES:
        name = str(payload.get("name") or payload.get("caption") or "")
        caption = _plain_rich_text(payload.get("caption"))
        text = sanitize_visible_text(" ".join(part for part in [name, caption] if part))
        return text, False
    if kind == "equation":
        expr = payload.get("expression") if isinstance(payload.get("expression"), str) else _plain_rich_text(payload.get("rich_text"))
        return sanitize_visible_text(str(expr or "")), False
    if kind == "table_row":
        cells = payload.get("cells") if isinstance(payload.get("cells"), list) else []
        cell_text = " | ".join(sanitize_visible_text(_plain_rich_text(cell)) for cell in cells)
        return cell_text, False
    if kind == "to_do":
        checked = "已完成" if payload.get("checked") else "未完成"
        return sanitize_visible_text(f"{checked} {_plain_rich_text(payload.get('rich_text'))}"), False
    if kind == "child_page":
        return "", False
    text = _plain_rich_text(payload.get("rich_text"))
    return sanitize_visible_text(text), False


def normalize_page(
    *,
    title: str,
    breadcrumb: str,
    properties: list[str],
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    lines = [sanitize_visible_text(title)]
    if breadcrumb:
        lines.append(sanitize_visible_text(breadcrumb))
    lines.extend(properties)
    unsupported = 0
    for block in blocks:
        text, skipped = _block_line(block)
        if skipped:
            unsupported += 1
            continue
        if text:
            lines.append(text)
    body = "\n".join(line for line in lines if line)
    raw = body.encode("utf-8")
    truncated = False
    if len(raw) > PAGE_TEXT_LIMIT_BYTES:
        raw = raw[:PAGE_TEXT_LIMIT_BYTES]
        while raw and (raw[-1] & 0xC0) == 0x80:
            raw = raw[:-1]
        body = raw.decode("utf-8", errors="ignore")
        truncated = True
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return {
        "normalized_text": body,
        "observed_content_hash": digest,
        "unsupported_block_count": unsupported,
        "truncated": truncated,
        "partial": truncated or unsupported > 0,
    }


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def parent_uuid(page: dict[str, Any]) -> str | None:
    parent = page.get("parent") if isinstance(page.get("parent"), dict) else {}
    kind = str(parent.get("type") or "")
    if kind == "page_id":
        return str(parent.get("page_id") or "") or None
    if kind in {"data_source_id", "database_id"}:
        return str(parent.get(kind) or "") or None
    return None

def safe_page_url(page: dict[str, Any]) -> str | None:
    from app.services.notion_reference import resolve_notion_page_url

    return resolve_notion_page_url(str(page.get("url") or ""), str(page.get("id") or ""))


def dump_canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
