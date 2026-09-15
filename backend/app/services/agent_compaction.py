"""
Agent conversation compaction helpers.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.models import AIConversation, AIConversationSummary
from app.services import llm_gateway

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 1800
MAX_STRUCTURED_ITEMS = 8
MAX_ITEM_CHARS = 200
MAX_REFERENCED_ENTRIES = 12
COMPACTION_METHOD = "structured_extractive_v1"
LLM_COMPACTION_METHOD = "llm_structured_v1"

_LLM_SUMMARY_SYSTEM_PROMPT = """你是一个会话摘要助手。请根据提供的对话历史生成结构化 JSON 摘要。

要求：
- 只输出一个 JSON 对象，不要使用 markdown 代码块，不要输出任何解释文字
- JSON 字段固定为：summary, open_questions, decisions, user_preferences, referenced_entries, pending_actions
- summary：简洁概括会话要点，面向用户可读
- 必须保留：用户目标、已确认结论、用户纠正、尚未解决的问题、对话约束
- 不得复制大段来源正文、OCR 全文，不得写入 reference_key、chunk_id、storage_path、文件路径
- open_questions、decisions、user_preferences、pending_actions：字符串数组
- referenced_entries：只能从用户消息末尾提供的「允许引用的记录列表」中选择，不得编造 entry_id 或 source_id
- 不要输出 API key、token、密码或其他敏感凭证
- 不要在 summary 中暴露 chunk_id、source_type、reference_key 等内部字段名
"""

_QUESTION_RE = re.compile(
    r"[^。！？\n]*[？?]|[^。！？\n]*(?:什么|怎么|为什么|如何|是否|能不能|可不可以|要不要|有没有|吗)[^。！？\n]*[。！？]?",
    re.IGNORECASE,
)
_DECISION_MARKERS = (
    "决定", "确定", "结论", "最终", "就按", "因此", "总结下来", "我们将会",
    "已选择", "定为", "采用", "方案是", "结果是",
)
_PREFERENCE_RE = re.compile(
    r"[^。！？\n]*(?:不要|别|默认|以后|优先|偏好|习惯|希望|总是|从来|务必|一定|尽量)[^。！？\n]*[。！？]?",
)
_PENDING_ACTION_RE = re.compile(
    r"[^。！？\n]*(?:"
    r"帮我.*?(?:创建|加|记|保存|写入)|"
    r"请.*?(?:创建|加|记|保存|写入)|"
    r"(?:创建|添加|保存|写入|记录).*?(?:待办|todo|记忆|总结|记录)|"
    r"(?:待办|todo|记得|回头).*?(?:做|完成|处理)|"
    r"记一下|帮我记|保存记忆|保存总结"
    r")[^。！？\n]*[。！？]?",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


def load_conversation_summary(db: Session, user_id: int, session_id: str) -> Optional[Dict]:
    row = (
        db.query(AIConversationSummary)
        .filter(
            AIConversationSummary.user_id == user_id,
            AIConversationSummary.session_id == session_id,
        )
        .first()
    )
    if not row:
        return None
    return {
        "summary": row.summary,
        "structured_json": row.structured_json or {},
        "message_count": int(row.message_count or 0),
        "last_message_at": row.last_message_at.isoformat() if row.last_message_at else None,
    }


def _normalize_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", (text or "").strip())


def _truncate(text: str, max_chars: int) -> str:
    cleaned = _normalize_text(text)
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def _split_sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[。！？!?])", text or "")
    return [_normalize_text(part) for part in parts if _normalize_text(part)]


def _dedupe_items(items: Sequence[str], *, max_items: int, max_chars: int) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for raw in items:
        item = _truncate(raw, max_chars)
        if not item:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= max_items:
            break
    return result


def _extractive_fallback(messages: Sequence[AIConversation], max_messages: int) -> str:
    parts: List[str] = []
    for msg in messages[-max_messages:]:
        role = "用户" if msg.role == "user" else "助手"
        parts.append(f"{role}: {msg.content[:220]}")
    return "\n".join(parts)[-4000:]


def _build_concise_summary(messages: Sequence[AIConversation], max_messages: int) -> str:
    highlights: List[str] = []
    for msg in messages[-max_messages:]:
        content = _normalize_text(msg.content)
        if not content:
            continue
        role = "用户" if msg.role == "user" else "助手"
        sentences = _split_sentences(content)
        snippet = sentences[0] if sentences else content
        highlights.append(f"{role}：{_truncate(snippet, 140)}")

    if not highlights:
        return _extractive_fallback(messages, max_messages)

    if len(highlights) > 6:
        summary = "；".join(highlights[:3] + ["…"] + highlights[-2:])
    else:
        summary = "；".join(highlights)

    return _truncate(summary, MAX_SUMMARY_CHARS)


def _extract_open_questions(messages: Sequence[AIConversation]) -> List[str]:
    candidates: List[str] = []
    for msg in messages:
        if msg.role != "user":
            continue
        for match in _QUESTION_RE.findall(msg.content or ""):
            cleaned = _normalize_text(match)
            if cleaned and ("?" in cleaned or "？" in cleaned or any(k in cleaned for k in ("什么", "怎么", "为什么", "如何", "是否", "吗"))):
                candidates.append(cleaned)
    return _dedupe_items(candidates, max_items=MAX_STRUCTURED_ITEMS, max_chars=MAX_ITEM_CHARS)


def _extract_decisions(messages: Sequence[AIConversation]) -> List[str]:
    candidates: List[str] = []
    for msg in messages:
        for sentence in _split_sentences(msg.content or ""):
            if any(marker in sentence for marker in _DECISION_MARKERS) and len(sentence) >= 6:
                candidates.append(sentence)
    return _dedupe_items(candidates, max_items=MAX_STRUCTURED_ITEMS, max_chars=MAX_ITEM_CHARS)


def _extract_user_preferences(messages: Sequence[AIConversation]) -> List[str]:
    candidates: List[str] = []
    for msg in messages:
        if msg.role != "user":
            continue
        for match in _PREFERENCE_RE.findall(msg.content or ""):
            cleaned = _normalize_text(match)
            if cleaned:
                candidates.append(cleaned)
    return _dedupe_items(candidates, max_items=MAX_STRUCTURED_ITEMS, max_chars=MAX_ITEM_CHARS)


def _extract_referenced_entries(messages: Sequence[AIConversation]) -> List[Dict]:
    seen: set[tuple] = set()
    entries: List[Dict] = []
    for msg in messages:
        refs = msg.references
        if not refs or not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            entry_id = ref.get("entry_id")
            source_id = ref.get("source_id")
            source_type = ref.get("source_type") or "entry"
            if entry_id is None and source_type == "entry" and source_id is not None:
                entry_id = source_id
            if entry_id is None and source_id is None:
                continue
            key = (entry_id, source_id, source_type)
            if key in seen:
                continue
            seen.add(key)
            item: Dict = {
                "entry_id": int(entry_id) if entry_id is not None else None,
                "source_id": int(source_id) if source_id is not None else None,
                "source_type": str(source_type),
            }
            title = ref.get("title")
            if title:
                item["title"] = _truncate(str(title), 120)
            entries.append(item)
            if len(entries) >= MAX_REFERENCED_ENTRIES:
                return entries
    return entries


def _extract_pending_actions(messages: Sequence[AIConversation]) -> List[str]:
    candidates: List[str] = []
    for msg in messages:
        for match in _PENDING_ACTION_RE.findall(msg.content or ""):
            cleaned = _normalize_text(match)
            if cleaned:
                candidates.append(cleaned)
    return _dedupe_items(candidates, max_items=MAX_STRUCTURED_ITEMS, max_chars=MAX_ITEM_CHARS)


def build_structured_summary(
    messages: Sequence[AIConversation],
    *,
    max_messages: int = 30,
) -> Dict:
    """Rule-based structured compaction output."""
    window = list(messages[-max_messages:])
    summary = _build_concise_summary(window, max_messages)
    if not summary:
        summary = _extractive_fallback(window, max_messages)

    return {
        "summary": summary,
        "open_questions": _extract_open_questions(window),
        "decisions": _extract_decisions(window),
        "user_preferences": _extract_user_preferences(window),
        "referenced_entries": _extract_referenced_entries(window),
        "pending_actions": _extract_pending_actions(window),
        "compaction_method": COMPACTION_METHOD,
    }


def _load_session_messages(
    db: Session,
    user_id: int,
    session_id: str,
) -> List[AIConversation]:
    return (
        db.query(AIConversation)
        .filter(AIConversation.user_id == user_id, AIConversation.session_id == session_id)
        .order_by(AIConversation.created_at.asc())
        .all()
    )


def _normalize_id(value: object) -> Optional[object]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def _entry_key(entry: Dict) -> tuple:
    return (
        _normalize_id(entry.get("entry_id")),
        _normalize_id(entry.get("source_id")),
        str(entry.get("source_type") or "entry"),
    )


def _parse_llm_json(raw_response: str) -> Optional[Dict]:
    text = (raw_response or "").strip()
    if not text:
        return None
    try:
        if "```json" in text:
            start = text.find("```json") + 7
            end = text.find("```", start)
            if end > start:
                text = text[start:end].strip()
        elif "```" in text:
            start = text.find("```") + 3
            end = text.find("```", start)
            if end > start:
                text = text[start:end].strip()
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _string_list(value: object) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _filter_referenced_entries(
    llm_entries: object,
    allowed_entries: Sequence[Dict],
) -> List[Dict]:
    allowed_map = {_entry_key(entry): dict(entry) for entry in allowed_entries}
    if not allowed_map:
        return []
    seen: set[tuple] = set()
    result: List[Dict] = []
    if not isinstance(llm_entries, list):
        return result
    for raw in llm_entries:
        if not isinstance(raw, dict):
            continue
        key = _entry_key(raw)
        if key not in allowed_map or key in seen:
            continue
        seen.add(key)
        result.append(dict(allowed_map[key]))
        if len(result) >= MAX_REFERENCED_ENTRIES:
            break
    return result


def _sanitize_llm_structured(raw: Dict, rule_summary: Dict) -> Dict:
    allowed_refs = rule_summary.get("referenced_entries") or []
    summary_text = str(raw.get("summary") or rule_summary.get("summary") or "")
    return {
        "summary": _truncate(summary_text, MAX_SUMMARY_CHARS),
        "open_questions": _dedupe_items(
            _string_list(raw.get("open_questions")),
            max_items=MAX_STRUCTURED_ITEMS,
            max_chars=MAX_ITEM_CHARS,
        ),
        "decisions": _dedupe_items(
            _string_list(raw.get("decisions")),
            max_items=MAX_STRUCTURED_ITEMS,
            max_chars=MAX_ITEM_CHARS,
        ),
        "user_preferences": _dedupe_items(
            _string_list(raw.get("user_preferences")),
            max_items=MAX_STRUCTURED_ITEMS,
            max_chars=MAX_ITEM_CHARS,
        ),
        "referenced_entries": _filter_referenced_entries(raw.get("referenced_entries"), allowed_refs),
        "pending_actions": _dedupe_items(
            _string_list(raw.get("pending_actions")),
            max_items=MAX_STRUCTURED_ITEMS,
            max_chars=MAX_ITEM_CHARS,
        ),
        "compaction_method": LLM_COMPACTION_METHOD,
        "fallback_compaction_method": COMPACTION_METHOD,
    }


def _persist_conversation_summary(
    db: Session,
    user_id: int,
    session_id: str,
    messages: Sequence[AIConversation],
    structured_json: Dict,
) -> Dict:
    summary = structured_json["summary"]
    row = (
        db.query(AIConversationSummary)
        .filter(
            AIConversationSummary.user_id == user_id,
            AIConversationSummary.session_id == session_id,
        )
        .first()
    )
    if row:
        row.summary = summary
        row.structured_json = structured_json
        row.message_count = len(messages)
        row.last_message_at = messages[-1].created_at
    else:
        db.add(AIConversationSummary(
            user_id=user_id,
            session_id=session_id,
            summary=summary,
            structured_json=structured_json,
            message_count=len(messages),
            last_message_at=messages[-1].created_at,
        ))
    db.commit()
    return load_conversation_summary(db, user_id, session_id)


async def generate_llm_structured_summary(
    db: Session,
    user_id: int,
    session_id: str,
    *,
    max_messages: int = 30,
) -> Optional[Dict]:
    """Optional LLM-based structured compaction; returns None on empty/failure."""
    try:
        messages = _load_session_messages(db, user_id, session_id)
        if not messages:
            return None

        window = messages[-max_messages:]
        rule_summary = build_structured_summary(messages, max_messages=max_messages)

        conv_lines: List[str] = []
        for msg in window:
            role = "user" if msg.role == "user" else "assistant"
            conv_lines.append(f"{role}: {(msg.content or '')[:800]}")

        allowed_refs_json = json.dumps(
            rule_summary.get("referenced_entries") or [],
            ensure_ascii=False,
        )
        user_content = (
            "对话历史：\n"
            + "\n".join(conv_lines)
            + "\n\n允许引用的记录列表（referenced_entries 只能从中选择）：\n"
            + allowed_refs_json
            + "\n\n规则抽取参考摘要（可改进表达但勿违背事实）：\n"
            + str(rule_summary.get("summary") or "")
        )

        result = await llm_gateway.generate_chat_completion(
            model_key=None,
            system=_LLM_SUMMARY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            mode="query",
            max_tokens=1200,
            temperature=0.1,
            user_id=user_id,
        )

        parsed = _parse_llm_json(result.content)
        if not parsed:
            logger.warning(
                "LLM structured summary JSON parse failed user_id=%s session_id=%s",
                user_id,
                session_id,
            )
            return None

        return _sanitize_llm_structured(parsed, rule_summary)
    except Exception as exc:
        logger.warning(
            "LLM structured summary failed user_id=%s session_id=%s error=%s",
            user_id,
            session_id,
            exc,
        )
        return None


def compact_conversation_summary(
    db: Session,
    user_id: int,
    session_id: str,
    *,
    min_messages: int = 1,
    max_messages: int = 30,
) -> Optional[Dict]:
    messages = _load_session_messages(db, user_id, session_id)
    if len(messages) < min_messages:
        return None

    structured_json = build_structured_summary(messages, max_messages=max_messages)
    return _persist_conversation_summary(db, user_id, session_id, messages, structured_json)


async def compact_conversation_summary_llm(
    db: Session,
    user_id: int,
    session_id: str,
    *,
    min_messages: int = 1,
    max_messages: int = 30,
) -> Optional[Dict]:
    messages = _load_session_messages(db, user_id, session_id)
    if len(messages) < min_messages:
        return None

    structured_json = await generate_llm_structured_summary(
        db,
        user_id,
        session_id,
        max_messages=max_messages,
    )
    if structured_json is None:
        return compact_conversation_summary(
            db,
            user_id,
            session_id,
            min_messages=min_messages,
            max_messages=max_messages,
        )

    return _persist_conversation_summary(db, user_id, session_id, messages, structured_json)


def refresh_conversation_summary(db: Session, user_id: int, session_id: str, threshold: int = 12) -> None:
    compact_conversation_summary(
        db,
        user_id,
        session_id,
        min_messages=threshold,
        max_messages=threshold,
    )
