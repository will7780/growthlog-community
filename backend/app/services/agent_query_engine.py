"""
Agent QueryEngine for GrowthLog.

This layer keeps tools registered, user-scoped, permission-checked and audited.
The first production version still runs only read tools; write tools can be added
later behind explicit confirmation without changing the route contract.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import func, or_, text

from app.models import AgentMemory, AgentToolAudit, AIConversation, Entry, EntryLabel, Todo
from app.timeutil import now_local
from app.services.agent_context_builder import build_tool_use_context
from app.services.agent_context import ToolUseContext
from app.services.agent_permissions import PermissionGate
from app.services.agent_tool_registry import AgentTool
from app.services.agent_tools import (
    generate_agent_answer,
    get_database_catalog,
    get_recent_entries,
    get_todo_snapshot,
)
from app.services import llm_gateway
from app.services.rag_pipeline import retrieve_rag_context
from app.services.rank_fusion import result_key
from app.services.ai_session_embeddings import search_ai_conversations_vector
from app.services.ai_session_summary_embeddings import search_ai_conversation_summaries_vector

logger = logging.getLogger(__name__)


def _title_and_snippet(content: str, max_len: int = 220) -> tuple[str, str]:
    title = (content or "").split("\n")[0].strip()
    body = (content or "").replace(title, "", 1).strip() if title else (content or "").strip()
    return (title[:120] or "记录"), body[:max_len]


def _entry_evidence(
    entry: Entry,
    label_name: str,
    *,
    score: float = 0.5,
    reason: str = "",
) -> Dict[str, Any]:
    title, snippet = _title_and_snippet(entry.content)
    return {
        "source_type": "entry",
        "entry_id": int(entry.id),
        "title": title,
        "snippet": snippet,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "label_code": entry.label_code,
        "label_name": label_name or entry.label_code,
        "relevance_score": float(score),
        "reason": reason[:120] if reason else "",
    }


def _fetch_entry_evidence(
    db: Session,
    user_id: int,
    *,
    since: Optional[datetime] = None,
    label_code: Optional[str] = None,
    limit: int = 8,
    score: float = 0.5,
    reason: str = "",
) -> List[Dict[str, Any]]:
    query = (
        db.query(Entry, EntryLabel.name)
        .outerjoin(EntryLabel, Entry.label_code == EntryLabel.code)
        .filter(Entry.user_id == user_id)
    )
    if since is not None:
        query = query.filter(Entry.created_at >= since)
    if label_code:
        query = query.filter(Entry.label_code == label_code)
    rows = query.order_by(Entry.created_at.desc()).limit(limit).all()
    return [
        _entry_evidence(entry, label_name or entry.label_code, score=score, reason=reason)
        for entry, label_name in rows
    ]


def _collect_analysis_entry_references(observations: Dict[str, Any]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for tool_name in (
        "timeline_analysis",
        "weekly_review",
        "monthly_review",
        "goal_progress_analysis",
        "emotion_pattern_analysis",
        "knowledge_cluster_analysis",
    ):
        result = observations.get(tool_name) or {}
        for item in result.get("evidence_entries") or []:
            if item.get("entry_id") is not None:
                refs.append(item)
    return refs


def _audit_tool(
    db: Session,
    user_id: int,
    session_id: str,
    tool: AgentTool,
    status: str,
    input_json: Optional[Dict] = None,
    output_summary: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    try:
        db.add(AgentToolAudit(
            user_id=user_id,
            session_id=session_id,
            tool_name=tool.name,
            permission=tool.permission,
            status=status,
            input_json=input_json,
            output_summary=output_summary[:500] if output_summary else None,
            error_message=error_message[:1000] if error_message else None,
        ))
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("failed to write agent audit: %s", exc)


def _run_database_catalog(ctx: ToolUseContext) -> Dict:
    return get_database_catalog(ctx.db, ctx.user_id)


def _run_recent_entries(ctx: ToolUseContext) -> List[Dict]:
    return get_recent_entries(ctx.db, ctx.user_id)


def _run_hybrid_rag_search(ctx: ToolUseContext) -> Dict:
    return retrieve_rag_context(
        db=ctx.db,
        user_id=ctx.user_id,
        query=ctx.query,
        top_k=12,
        dense_top_k=30,
        keyword_top_k=30,
        attachment_top_k=12,
        include_attachments=True,
    )


def _run_todo_snapshot(ctx: ToolUseContext) -> Dict:
    return get_todo_snapshot(ctx.db, ctx.user_id, ctx.query)


def _run_memory_context(ctx: ToolUseContext) -> Dict:
    return {
        "memories": ctx.memories,
        "conversation_summary": ctx.conversation_summary,
    }


def _entry_stats_since(db: Session, user_id: int, days: int) -> Dict:
    since = now_local() - timedelta(days=days)
    rows = (
        db.query(Entry.label_code, EntryLabel.name, func.count(Entry.id))
        .outerjoin(EntryLabel, Entry.label_code == EntryLabel.code)
        .filter(Entry.user_id == user_id, Entry.created_at >= since)
        .group_by(Entry.label_code, EntryLabel.name)
        .all()
    )
    total = sum(int(count or 0) for _, _, count in rows)
    return {
        "days": days,
        "entry_count": total,
        "labels": [
            {"label_code": code, "label_name": name or code, "count": int(count or 0)}
            for code, name, count in rows
        ],
    }


def _run_timeline_analysis(ctx: ToolUseContext) -> Dict:
    since = now_local() - timedelta(days=30)
    rows = (
        ctx.db.query(func.date(Entry.created_at), func.count(Entry.id))
        .filter(Entry.user_id == ctx.user_id, Entry.created_at >= since)
        .group_by(func.date(Entry.created_at))
        .order_by(func.date(Entry.created_at).asc())
        .all()
    )
    return {
        "window_days": 30,
        "daily_counts": [{"date": str(day), "count": int(count or 0)} for day, count in rows],
        "label_stats": _entry_stats_since(ctx.db, ctx.user_id, 30)["labels"],
        "evidence_entries": _fetch_entry_evidence(
            ctx.db,
            ctx.user_id,
            since=since,
            limit=8,
            score=0.45,
            reason="timeline_recent_entries",
        ),
    }


def _run_weekly_review(ctx: ToolUseContext) -> Dict:
    stats = _entry_stats_since(ctx.db, ctx.user_id, 7)
    since = now_local() - timedelta(days=7)
    todos_open = ctx.db.query(Todo).filter(Todo.user_id == ctx.user_id, Todo.parent_id.is_(None), Todo.is_done.is_(False)).count()
    todos_done = (
        ctx.db.query(Todo)
        .filter(Todo.user_id == ctx.user_id, Todo.parent_id.is_(None), Todo.is_done.is_(True), Todo.completed_at >= since)
        .count()
    )
    return {
        **stats,
        "open_todos": int(todos_open or 0),
        "completed_todos": int(todos_done or 0),
        "evidence_entries": _fetch_entry_evidence(
            ctx.db,
            ctx.user_id,
            since=since,
            limit=8,
            score=0.48,
            reason="weekly_review_recent_entries",
        ),
    }


def _run_monthly_review(ctx: ToolUseContext) -> Dict:
    stats = _entry_stats_since(ctx.db, ctx.user_id, 30)
    since = now_local() - timedelta(days=30)
    todo_total = ctx.db.query(Todo).filter(Todo.user_id == ctx.user_id, Todo.parent_id.is_(None)).count()
    todo_open = ctx.db.query(Todo).filter(Todo.user_id == ctx.user_id, Todo.parent_id.is_(None), Todo.is_done.is_(False)).count()
    return {
        **stats,
        "todo_total": int(todo_total or 0),
        "todo_open": int(todo_open or 0),
        "evidence_entries": _fetch_entry_evidence(
            ctx.db,
            ctx.user_id,
            since=since,
            limit=8,
            score=0.48,
            reason="monthly_review_recent_entries",
        ),
    }


def _run_goal_progress_analysis(ctx: ToolUseContext) -> Dict:
    goals = (
        ctx.db.query(AgentMemory)
        .filter(
            AgentMemory.user_id == ctx.user_id,
            AgentMemory.is_active.is_(True),
            AgentMemory.memory_type == "goal",
        )
        .order_by(AgentMemory.updated_at.desc(), AgentMemory.created_at.desc())
        .limit(5)
        .all()
    )
    open_todos = (
        ctx.db.query(Todo)
        .filter(Todo.user_id == ctx.user_id, Todo.parent_id.is_(None), Todo.is_done.is_(False))
        .order_by(Todo.created_at.desc())
        .limit(8)
        .all()
    )
    evidence_items: List[Dict[str, Any]] = [
        {
            "source_type": "memory",
            "memory_id": int(goal.id),
            "title": (goal.content or "")[:120],
            "snippet": (goal.content or "")[:220],
            "reason": "active_goal",
        }
        for goal in goals
    ]
    evidence_items.extend([
        {
            "source_type": "todo",
            "todo_id": int(todo.id),
            "title": (todo.content or "")[:120],
            "snippet": (todo.content or "")[:220],
            "created_at": todo.created_at.isoformat() if todo.created_at else None,
            "reason": "open_todo",
        }
        for todo in open_todos
    ])

    evidence_entries = _fetch_entry_evidence(
        ctx.db,
        ctx.user_id,
        limit=6,
        score=0.42,
        reason="goal_progress_context_entries",
    )
    if goals:
        terms = _query_terms(goals[0].content)[:3]
        if terms:
            filters = [Entry.content.ilike(f"%{term}%") for term in terms]
            related_rows = (
                ctx.db.query(Entry, EntryLabel.name)
                .outerjoin(EntryLabel, Entry.label_code == EntryLabel.code)
                .filter(Entry.user_id == ctx.user_id, or_(*filters))
                .order_by(Entry.created_at.desc())
                .limit(4)
                .all()
            )
            seen = {item["entry_id"] for item in evidence_entries}
            for entry, label_name in related_rows:
                entry_id = int(entry.id)
                if entry_id in seen:
                    continue
                seen.add(entry_id)
                evidence_entries.append(
                    _entry_evidence(
                        entry,
                        label_name or entry.label_code,
                        score=0.55,
                        reason="goal_related_entry",
                    )
                )
            evidence_entries = evidence_entries[:8]

    return {
        "goals": [{"id": int(goal.id), "content": goal.content[:300]} for goal in goals],
        "open_todos": [
            {
                "todo_id": int(todo.id),
                "content": todo.content,
                "due_date": todo.due_date.isoformat() if todo.due_date else None,
            }
            for todo in open_todos
        ],
        "evidence_items": evidence_items[:12],
        "evidence_entries": evidence_entries,
    }


def _run_emotion_pattern_analysis(ctx: ToolUseContext) -> Dict:
    terms = ["开心", "焦虑", "压力", "疲惫", "兴奋", "难受", "沮丧", "满足", "卡住", "担心"]
    since = now_local() - timedelta(days=30)
    rows = (
        ctx.db.query(Entry, EntryLabel.name)
        .outerjoin(EntryLabel, Entry.label_code == EntryLabel.code)
        .filter(
            Entry.user_id == ctx.user_id,
            Entry.created_at >= since,
            or_(*[Entry.content.ilike(f"%{term}%") for term in terms]),
        )
        .order_by(Entry.created_at.desc())
        .limit(10)
        .all()
    )
    matched_entries = [
        {
            "entry_id": int(entry.id),
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "snippet": (entry.content or "")[:220],
            "matched_terms": [term for term in terms if term in (entry.content or "")],
        }
        for entry, _label_name in rows
    ]
    evidence_entries = [
        _entry_evidence(
            entry,
            label_name or entry.label_code,
            score=0.6,
            reason=f"emotion_match:{','.join([t for t in terms if t in (entry.content or '')][:3])}",
        )
        for entry, label_name in rows
    ]
    return {
        "window_days": 30,
        "matched_entries": matched_entries,
        "evidence_entries": evidence_entries,
    }


def _run_knowledge_cluster_analysis(ctx: ToolUseContext) -> Dict:
    rows = (
        ctx.db.query(Entry.label_code, EntryLabel.name, func.count(Entry.id))
        .outerjoin(EntryLabel, Entry.label_code == EntryLabel.code)
        .filter(Entry.user_id == ctx.user_id)
        .group_by(Entry.label_code, EntryLabel.name)
        .order_by(func.count(Entry.id).desc())
        .limit(12)
        .all()
    )
    evidence_entries: List[Dict[str, Any]] = []
    for code, name, _count in rows[:4]:
        evidence_entries.extend(
            _fetch_entry_evidence(
                ctx.db,
                ctx.user_id,
                label_code=code,
                limit=2,
                score=0.46,
                reason=f"cluster:{name or code}",
            )
        )
    return {
        "clusters": [
            {"label_code": code, "label_name": name or code, "entry_count": int(count or 0)}
            for code, name, count in rows
        ],
        "evidence_entries": evidence_entries[:8],
    }


def _query_terms(query: str) -> List[str]:
    normalized = "".join(ch if ch.isalnum() else " " for ch in query.lower())
    terms = [term for term in normalized.split() if len(term) >= 2]
    if not terms and query.strip():
        terms = [query.strip()[:24]]
    return terms[:6]


AI_SESSION_SEARCH_LIMIT = 8


def _format_session_search_hit(
    *,
    message_id: int,
    session_id: str,
    role: str,
    content: str,
    created_at,
    retrieval_method: str,
    relevance_score: float,
) -> Dict:
    return {
        "message_id": int(message_id),
        "session_id": session_id,
        "role": role,
        "snippet": (content or "")[:360],
        "created_at": created_at.isoformat() if created_at else None,
        "retrieval_method": retrieval_method,
        "relevance_score": float(relevance_score),
    }


def _like_relevance_score(content: str, terms: List[str]) -> float:
    haystack = (content or "").lower()
    if not haystack or not terms:
        return 0.0
    hits = sum(1 for term in terms if term.lower() in haystack)
    return hits / len(terms)


def _run_ai_session_search_fulltext(ctx: ToolUseContext) -> Optional[List[Dict]]:
    query_text = (ctx.query or "").strip()
    if not query_text:
        return []

    sql = text(
        """
        SELECT id, session_id, role, content, created_at,
               MATCH(content) AGAINST(:query IN NATURAL LANGUAGE MODE) AS relevance_score
        FROM ai_conversations
        WHERE user_id = :user_id
          AND session_id != :session_id
          AND MATCH(content) AGAINST(:query IN NATURAL LANGUAGE MODE)
        ORDER BY relevance_score DESC, created_at DESC
        LIMIT :limit
        """
    )
    try:
        rows = ctx.db.execute(
            sql,
            {
                "query": query_text,
                "user_id": ctx.user_id,
                "session_id": ctx.session_id,
                "limit": AI_SESSION_SEARCH_LIMIT,
            },
        ).fetchall()
    except Exception as exc:
        logger.warning(
            "ai_session_search FULLTEXT unavailable, falling back to LIKE user_id=%s session_id=%s error=%s",
            ctx.user_id,
            ctx.session_id,
            exc,
        )
        try:
            ctx.db.rollback()
        except Exception:
            logger.debug("ai_session_search rollback after FULLTEXT failure skipped", exc_info=True)
        return None

    return [
        _format_session_search_hit(
            message_id=int(row.id),
            session_id=row.session_id,
            role=row.role,
            content=row.content,
            created_at=row.created_at,
            retrieval_method="fulltext",
            relevance_score=max(0.0, float(row.relevance_score or 0.0)),
        )
        for row in rows
    ]


def _run_ai_session_search_like(ctx: ToolUseContext, terms: List[str]) -> List[Dict]:
    filters = [AIConversation.content.ilike(f"%{term}%") for term in terms]
    rows = (
        ctx.db.query(AIConversation)
        .filter(
            AIConversation.user_id == ctx.user_id,
            AIConversation.session_id != ctx.session_id,
            or_(*filters),
        )
        .order_by(AIConversation.created_at.desc())
        .limit(AI_SESSION_SEARCH_LIMIT)
        .all()
    )
    scored_rows = [
        (
            _like_relevance_score(row.content or "", terms),
            row,
        )
        for row in rows
    ]
    scored_rows.sort(key=lambda item: (item[0], item[1].created_at or datetime.min), reverse=True)
    return [
        _format_session_search_hit(
            message_id=int(row.id),
            session_id=row.session_id,
            role=row.role,
            content=row.content,
            created_at=row.created_at,
            retrieval_method="like_fallback",
            relevance_score=score,
        )
        for score, row in scored_rows
    ]


def _run_ai_session_search(ctx: ToolUseContext) -> List[Dict]:
    query_text = (ctx.query or "").strip()
    if not query_text:
        return []

    summary_hits = search_ai_conversation_summaries_vector(
        ctx.db,
        ctx.user_id,
        ctx.session_id,
        query_text,
        top_k=AI_SESSION_SEARCH_LIMIT,
    )
    if summary_hits:
        return summary_hits

    vector_hits = search_ai_conversations_vector(
        ctx.db,
        ctx.user_id,
        ctx.session_id,
        query_text,
        top_k=AI_SESSION_SEARCH_LIMIT,
    )
    if vector_hits:
        return vector_hits

    terms = _query_terms(ctx.query)
    if not terms:
        return []

    fulltext_hits = _run_ai_session_search_fulltext(ctx)
    if fulltext_hits is not None:
        return fulltext_hits

    return _run_ai_session_search_like(ctx, terms)


def _query_matches(query: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in query for keyword in keywords)


SELECTION_STRATEGY = "rule_based_v1"
LLM_SELECTION_STRATEGY = "llm_planner_v1"
ALWAYS_SELECTED_TOOLS = ("memory_context", "hybrid_rag_search")
ADVANCED_ANALYSIS_TOOLS = {
    "timeline_analysis",
    "weekly_review",
    "monthly_review",
    "goal_progress_analysis",
    "emotion_pattern_analysis",
    "knowledge_cluster_analysis",
    "ai_session_search",
}

_LLM_PLANNER_SYSTEM_PROMPT = """你是一个 Agent 工具选择助手。根据用户问题和简短会话摘要，从可用只读工具列表中选择最合适的工具。

要求：
- 只输出一个 JSON 对象，不要使用 markdown 代码块，不要输出任何解释文字
- JSON 格式固定为：{"selected_tools":["tool_name",...],"reason":"..."}
- selected_tools 只能从提供的工具 name 中选择
- 必须包含 memory_context 和 hybrid_rag_search
- 最多选择 6 个工具
- 不要输出 API key、token、密码或其他敏感凭证
"""

_LLM_FOLLOWUP_PLANNER_SYSTEM_PROMPT = """你是 Agent follow-up 工具选择助手。第一轮只读工具已执行，请根据用户问题和第一轮结果摘要，判断是否需要补充 1-2 个尚未执行过的只读工具。

要求：
- 只输出一个 JSON 对象，不要使用 markdown 代码块，不要输出任何解释文字
- JSON 格式固定为：{"selected_tools":["tool_name",...],"reason":"..."}
- selected_tools 只能从剩余可用工具 name 中选择，不得重复已执行工具
- 若无需补充，返回 {"selected_tools":[],"reason":"..."}
- 最多选择 2 个工具
- 不要输出 API key、token、密码或其他敏感凭证
"""


def normalize_agent_planner(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower()
    if normalized == "llm":
        return "llm"
    return "rule"


def normalize_agent_tool_rounds(value: Optional[int]) -> int:
    if value is None:
        return 1
    try:
        rounds = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(2, rounds))


MAX_AGENT_TOOLS_TOTAL = 8
ROUND1_MAX_TOOLS = 6
ROUND2_MAX_TOOLS = 2


def _tool_catalog_for_planner() -> List[Dict[str, str]]:
    catalog: List[Dict[str, str]] = []
    for tool in TOOL_REGISTRY:
        if not tool.default_enabled:
            continue
        if tool.permission != "read":
            continue
        catalog.append({
            "name": tool.name,
            "title": tool.title,
            "description": tool.description or tool.title,
            "toolset": tool.toolset,
            "permission": tool.permission,
        })
    return catalog


def _parse_llm_planner_json(raw_response: str) -> Optional[Dict]:
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


def _validate_llm_selected_tools(raw_tools: List[str]) -> Optional[str]:
    if not raw_tools:
        return "empty_tool_list"
    for name in raw_tools:
        tool = TOOL_REGISTRY_BY_NAME.get(name)
        if tool is None:
            return "unknown_tool"
        if tool.permission != "read":
            return "non_read_tool"
    return None


def _finalize_selected_tools(selected: List[str], max_tools: int) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []

    def add(name: str) -> None:
        if name in seen or name not in TOOL_REGISTRY_BY_NAME:
            return
        seen.add(name)
        ordered.append(name)

    for name in ALWAYS_SELECTED_TOOLS:
        add(name)
    for name in selected:
        add(name)

    if len(ordered) > max_tools:
        core = list(ALWAYS_SELECTED_TOOLS)
        rest = [name for name in ordered if name not in core]
        ordered = core + rest[: max(0, max_tools - len(core))]
    return ordered


def _build_tool_selection_payload(
    selected_tool_names: List[str],
    *,
    selection_strategy: str,
    planner_fallback_reason: Optional[str] = None,
) -> Dict[str, Any]:
    skipped_tool_names = [
        tool.name for tool in TOOL_REGISTRY if tool.name not in selected_tool_names
    ]
    payload: Dict[str, Any] = {
        "selected_tools": selected_tool_names,
        "selection_strategy": selection_strategy,
        "skipped_tools": skipped_tool_names,
    }
    if planner_fallback_reason:
        payload["planner_fallback_reason"] = planner_fallback_reason
    return payload


def resolve_rule_tool_selection(ctx: ToolUseContext, max_tools: int = 6) -> Dict[str, Any]:
    selected_tool_names = plan_agent_tools(ctx, max_tools=max_tools)
    return _build_tool_selection_payload(
        selected_tool_names,
        selection_strategy=SELECTION_STRATEGY,
    )


async def plan_agent_tools_llm(
    ctx: ToolUseContext,
    max_tools: int = 6,
) -> Tuple[List[str], str, Optional[str]]:
    """
    Try LLM planner; on failure return rule planner selection and fallback reason.
    Returns (selected_tools, selection_strategy, planner_fallback_reason).
    """
    try:
        summary_text = ""
        if ctx.conversation_summary and isinstance(ctx.conversation_summary, dict):
            summary_text = str(ctx.conversation_summary.get("summary") or "")[:400]

        user_content = (
            "用户问题：\n"
            + (ctx.query or "").strip()
            + "\n\n会话摘要：\n"
            + (summary_text or "无")
            + "\n\n可用工具列表：\n"
            + json.dumps(_tool_catalog_for_planner(), ensure_ascii=False)
        )

        result = await llm_gateway.generate_chat_completion(
            model_key=None,
            system=_LLM_PLANNER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            mode="query",
            max_tokens=600,
            temperature=0.1,
            user_id=ctx.user_id,
        )

        parsed = _parse_llm_planner_json(result.content)
        if not parsed:
            rule_selected = plan_agent_tools(ctx, max_tools=max_tools)
            return rule_selected, SELECTION_STRATEGY, "planner_json_parse_failed"

        raw_tools = parsed.get("selected_tools") or []
        if not isinstance(raw_tools, list):
            rule_selected = plan_agent_tools(ctx, max_tools=max_tools)
            return rule_selected, SELECTION_STRATEGY, "planner_invalid_selected_tools"

        normalized_tools = [str(name).strip() for name in raw_tools if str(name).strip()]
        validation_error = _validate_llm_selected_tools(normalized_tools)
        if validation_error:
            rule_selected = plan_agent_tools(ctx, max_tools=max_tools)
            return rule_selected, SELECTION_STRATEGY, validation_error

        selected = _finalize_selected_tools(normalized_tools, max_tools)
        return selected, LLM_SELECTION_STRATEGY, None
    except Exception as exc:
        logger.warning(
            "LLM tool planner failed user_id=%s session_id=%s error=%s",
            ctx.user_id,
            ctx.session_id,
            exc,
        )
        rule_selected = plan_agent_tools(ctx, max_tools=max_tools)
        return rule_selected, SELECTION_STRATEGY, "planner_llm_call_failed"


async def resolve_agent_tool_selection(
    ctx: ToolUseContext,
    *,
    planner: str = "rule",
    max_tools: int = 6,
) -> Dict[str, Any]:
    if normalize_agent_planner(planner) == "llm":
        selected, strategy, fallback_reason = await plan_agent_tools_llm(ctx, max_tools=max_tools)
        return _build_tool_selection_payload(
            selected,
            selection_strategy=strategy,
            planner_fallback_reason=fallback_reason,
        )
    return resolve_rule_tool_selection(ctx, max_tools=max_tools)


async def plan_agent_followup_tools_llm(
    ctx: ToolUseContext,
    tool_steps: List[Dict[str, Any]],
    executed_tool_names: List[str],
    *,
    max_tools: int = ROUND2_MAX_TOOLS,
) -> Tuple[List[str], Optional[str]]:
    """
    Follow-up LLM planner for a second read-only tool round.
    Returns (selected_tools, fallback_reason). Never raises to caller.
    """
    executed_set = set(executed_tool_names)
    try:
        remaining_catalog = [
            item for item in _tool_catalog_for_planner()
            if item["name"] not in executed_set
        ]
        if not remaining_catalog:
            return [], "no_remaining_tools"

        summary_text = ""
        if ctx.conversation_summary and isinstance(ctx.conversation_summary, dict):
            summary_text = str(ctx.conversation_summary.get("summary") or "")[:400]

        round1_steps = [
            {
                "tool": step.get("tool"),
                "summary": step.get("summary"),
                "status": step.get("status"),
            }
            for step in tool_steps
            if step.get("round", 1) == 1
        ]

        user_content = (
            "用户问题：\n"
            + (ctx.query or "").strip()
            + "\n\n会话摘要：\n"
            + (summary_text or "无")
            + "\n\n第一轮已执行工具：\n"
            + json.dumps(sorted(executed_set), ensure_ascii=False)
            + "\n\n第一轮 tool_steps 摘要：\n"
            + json.dumps(round1_steps, ensure_ascii=False)
            + "\n\n剩余可用工具列表：\n"
            + json.dumps(remaining_catalog, ensure_ascii=False)
        )

        result = await llm_gateway.generate_chat_completion(
            model_key=None,
            system=_LLM_FOLLOWUP_PLANNER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            mode="query",
            max_tokens=400,
            temperature=0.1,
            user_id=ctx.user_id,
        )

        parsed = _parse_llm_planner_json(result.content)
        if not parsed:
            return [], "followup_json_parse_failed"

        raw_tools = parsed.get("selected_tools") or []
        if not isinstance(raw_tools, list):
            return [], "followup_invalid_selected_tools"

        normalized_tools = [str(name).strip() for name in raw_tools if str(name).strip()]
        if not normalized_tools:
            return [], "empty_tool_list"

        selected: List[str] = []
        seen: set[str] = set()
        for name in normalized_tools:
            if name in executed_set:
                return [], "already_executed_tool"
            if name in seen:
                continue
            tool = TOOL_REGISTRY_BY_NAME.get(name)
            if tool is None:
                return [], "unknown_tool"
            if tool.permission != "read":
                return [], "non_read_tool"
            seen.add(name)
            selected.append(name)

        if not selected:
            return [], "no_new_tools"

        return selected[:max_tools], None
    except Exception as exc:
        logger.warning(
            "LLM follow-up tool planner failed user_id=%s session_id=%s error=%s",
            ctx.user_id,
            ctx.session_id,
            exc,
        )
        return [], "followup_llm_call_failed"


def plan_agent_tools(ctx: ToolUseContext, max_tools: int = 6) -> List[str]:
    """Select read tools for the current query using deterministic rules."""
    query = (ctx.query or "").strip()
    selected: List[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        selected.append(name)

    for name in ALWAYS_SELECTED_TOOLS:
        add(name)

    if _query_matches(query, ("之前", "上次", "聊过", "历史会话", "决策", "计划")):
        add("ai_session_search")
    if _query_matches(query, ("待办", "todo", "小要事", "任务", "完成")):
        add("todo_snapshot")
    if _query_matches(query, ("最近", "刚才", "近期", "记录")):
        add("recent_entries")
    if _query_matches(query, ("周", "本周", "周复盘")):
        add("weekly_review")
    if _query_matches(query, ("月", "本月", "月度")):
        add("monthly_review")
    if _query_matches(query, ("时间线", "趋势", "频率", "连续")):
        add("timeline_analysis")
    if _query_matches(query, ("目标", "进度", "推进")):
        add("goal_progress_analysis")
    if _query_matches(query, ("情绪", "压力", "焦虑", "开心", "状态")):
        add("emotion_pattern_analysis")
    if _query_matches(query, ("标签", "分类", "知识", "主题", "分布")):
        add("knowledge_cluster_analysis")

    has_rule_advanced = any(name in seen for name in ADVANCED_ANALYSIS_TOOLS)
    if not has_rule_advanced:
        add("database_catalog")
        if len(selected) < max_tools and "recent_entries" not in seen:
            add("recent_entries")
    elif "database_catalog" not in seen and len(selected) < max_tools:
        add("database_catalog")

    if len(selected) > max_tools:
        core = list(ALWAYS_SELECTED_TOOLS)
        rest = [name for name in selected if name not in core]
        selected = core + rest[: max(0, max_tools - len(core))]

    return selected


TOOL_REGISTRY: List[AgentTool] = [
    AgentTool("database_catalog", "读取 GrowthLog 数据目录", "read", _run_database_catalog),
    AgentTool("memory_context", "读取个性设置与会话摘要", "read", _run_memory_context),
    AgentTool("ai_session_search", "检索历史 AI 会话", "read", _run_ai_session_search),
    AgentTool("hybrid_rag_search", "混合检索记录与附件", "read", _run_hybrid_rag_search),
    AgentTool("recent_entries", "读取近期记录", "read", _run_recent_entries),
    AgentTool("todo_snapshot", "读取小要事状态", "read", _run_todo_snapshot),
    AgentTool("timeline_analysis", "分析记录时间线", "read", _run_timeline_analysis),
    AgentTool("weekly_review", "读取周复盘统计", "read", _run_weekly_review),
    AgentTool("monthly_review", "读取月度复盘统计", "read", _run_monthly_review),
    AgentTool("goal_progress_analysis", "分析目标推进状态", "read", _run_goal_progress_analysis),
    AgentTool("emotion_pattern_analysis", "分析情绪线索", "read", _run_emotion_pattern_analysis),
    AgentTool("knowledge_cluster_analysis", "分析知识标签分布", "read", _run_knowledge_cluster_analysis),
]

TOOL_REGISTRY_BY_NAME = {tool.name: tool for tool in TOOL_REGISTRY}


def _summarize_tool_result(tool_name: str, result: Any) -> str:
    if tool_name == "database_catalog":
        counts = result.get("counts", {})
        return f"entries={counts.get('entries', 0)}, todos={counts.get('todos', 0)}, embeddings={counts.get('embeddings', 0)}"
    if tool_name == "todo_snapshot":
        return f"未完成 {len(result.get('open', []))} 条，关键词相关 {len(result.get('matched', []))} 条"
    if tool_name == "memory_context":
        return f"记忆 {len(result.get('memories', []))} 条，会话摘要={'有' if result.get('conversation_summary') else '无'}"
    if tool_name == "ai_session_search":
        return f"命中历史会话消息 {len(result)} 条"
    if tool_name == "timeline_analysis":
        return (
            f"近 30 天记录 {sum(item.get('count', 0) for item in result.get('daily_counts', []))} 条，"
            f"证据 {len(result.get('evidence_entries', []))} 条"
        )
    if tool_name in {"weekly_review", "monthly_review"}:
        return (
            f"记录 {result.get('entry_count', 0)} 条，标签 {len(result.get('labels', []))} 类，"
            f"证据 {len(result.get('evidence_entries', []))} 条"
        )
    if tool_name == "goal_progress_analysis":
        return (
            f"目标 {len(result.get('goals', []))} 条，未完成待办 {len(result.get('open_todos', []))} 条，"
            f"证据项 {len(result.get('evidence_items', []))} 条"
        )
    if tool_name == "emotion_pattern_analysis":
        return f"命中情绪相关记录 {len(result.get('evidence_entries', result.get('matched_entries', [])))} 条"
    if tool_name == "knowledge_cluster_analysis":
        return (
            f"标签簇 {len(result.get('clusters', []))} 类，"
            f"证据 {len(result.get('evidence_entries', []))} 条"
        )
    if tool_name == "hybrid_rag_search":
        debug = result.get("retrieval_debug", {}) if isinstance(result, dict) else {}
        return (
            f"融合命中 {len(result.get('references', []))} 条，"
            f"dense={debug.get('dense_count', 0)}, "
            f"keyword={debug.get('keyword_count', 0)}, "
            f"attachment={debug.get('attachment_count', 0)}"
        )
    if isinstance(result, list):
        return f"命中 {len(result)} 条"
    return "已读取"


def _execute_agent_tools_round(
    db: Session,
    user_id: int,
    session_id: str,
    query: str,
    ctx: ToolUseContext,
    tool_names: List[str],
    *,
    round_num: int,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    observations: Dict[str, Any] = {}
    tool_steps: List[Dict[str, Any]] = []

    for tool_name in tool_names:
        tool = TOOL_REGISTRY_BY_NAME.get(tool_name)
        if tool is None:
            continue
        try:
            PermissionGate.assert_allowed(tool)
            result = tool.runner(ctx)
            observations[tool.name] = result
            summary = _summarize_tool_result(tool.name, result)
            tool_steps.append({
                "tool": tool.name,
                "title": tool.title,
                "summary": summary,
                "status": "allowed",
                "permission": tool.permission,
                "round": round_num,
            })
            _audit_tool(db, user_id, session_id, tool, "allowed", {"query": query[:500]}, summary)
        except PermissionError as exc:
            observations[tool.name] = None
            tool_steps.append({
                "tool": tool.name,
                "title": tool.title,
                "summary": str(exc),
                "status": "denied",
                "permission": tool.permission,
                "round": round_num,
            })
            _audit_tool(db, user_id, session_id, tool, "denied", {"query": query[:500]}, None, str(exc))
        except Exception as exc:
            observations[tool.name] = None
            tool_steps.append({
                "tool": tool.name,
                "title": tool.title,
                "summary": f"工具执行失败: {exc}",
                "status": "failed",
                "permission": tool.permission,
                "round": round_num,
            })
            _audit_tool(db, user_id, session_id, tool, "failed", {"query": query[:500]}, None, str(exc))

    return observations, tool_steps


def _assemble_agent_observations(
    observations: Dict[str, Any],
    tool_selection: Dict[str, Any],
    tool_steps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    rag_context = observations.get("hybrid_rag_search") or {}
    hybrid_references = rag_context.get("references") or []
    references_by_key: Dict[str, Dict] = {}
    for ref in [
        *hybrid_references,
        *(observations.get("recent_entries") or []),
        *_collect_analysis_entry_references(observations),
    ]:
        key = result_key(ref)
        if key not in references_by_key or ref.get("relevance_score", 0.0) > references_by_key[key].get("relevance_score", 0.0):
            references_by_key[key] = ref

    return {
        "catalog": observations.get("database_catalog") or {},
        "memory_context": observations.get("memory_context") or {"memories": [], "conversation_summary": None},
        "session_search": observations.get("ai_session_search") or [],
        "analysis": {
            "timeline": observations.get("timeline_analysis") or {},
            "weekly_review": observations.get("weekly_review") or {},
            "monthly_review": observations.get("monthly_review") or {},
            "goal_progress": observations.get("goal_progress_analysis") or {},
            "emotion_pattern": observations.get("emotion_pattern_analysis") or {},
            "knowledge_clusters": observations.get("knowledge_cluster_analysis") or {},
        },
        "hybrid_rag": rag_context,
        "hybrid_references": hybrid_references,
        "retrieval_debug": rag_context.get("retrieval_debug", {}),
        "semantic_entries": rag_context.get("dense_results") or [],
        "semantic_attachments": rag_context.get("attachment_results") or [],
        "keyword_entries": rag_context.get("keyword_results") or [],
        "recent_entries": observations.get("recent_entries") or [],
        "todos": observations.get("todo_snapshot") or {"open": [], "recent_done": [], "matched": []},
        "references": list(references_by_key.values())[:12],
        "tool_selection": tool_selection,
        "tool_steps": tool_steps,
    }


def _build_agent_observations_with_ctx(
    db: Session,
    user_id: int,
    session_id: str,
    query: str,
    ctx: ToolUseContext,
    tool_selection: Dict[str, Any],
) -> Dict:
    selected_tool_names = tool_selection.get("selected_tools") or []
    observations, tool_steps = _execute_agent_tools_round(
        db,
        user_id,
        session_id,
        query,
        ctx,
        selected_tool_names,
        round_num=1,
    )
    return _assemble_agent_observations(observations, tool_selection, tool_steps)


def build_agent_observations_v2(
    db: Session,
    user_id: int,
    session_id: str,
    query: str,
    conversation_history: Optional[List[Dict]] = None,
    *,
    planner: str = "rule",
    max_tools: int = 6,
) -> Dict:
    ctx = build_tool_use_context(
        db,
        user_id=user_id,
        session_id=session_id,
        query=query,
        conversation_history=conversation_history,
    )
    tool_selection = resolve_rule_tool_selection(ctx, max_tools=max_tools)
    if normalize_agent_planner(planner) == "llm":
        logger.debug("build_agent_observations_v2 called with llm planner; using rule planner in sync path")
    return _build_agent_observations_with_ctx(
        db,
        user_id,
        session_id,
        query,
        ctx,
        tool_selection,
    )


async def build_agent_observations_v2_async(
    db: Session,
    user_id: int,
    session_id: str,
    query: str,
    conversation_history: Optional[List[Dict]] = None,
    *,
    planner: str = "rule",
    max_tools: int = ROUND1_MAX_TOOLS,
    max_rounds: int = 1,
) -> Dict:
    normalized_rounds = normalize_agent_tool_rounds(max_rounds)
    ctx = build_tool_use_context(
        db,
        user_id=user_id,
        session_id=session_id,
        query=query,
        conversation_history=conversation_history,
    )
    tool_selection = await resolve_agent_tool_selection(ctx, planner=planner, max_tools=max_tools)
    tool_selection["rounds"] = 1
    tool_selection["max_rounds"] = normalized_rounds
    tool_selection["followup_selected_tools"] = []

    observations, tool_steps = _execute_agent_tools_round(
        db,
        user_id,
        session_id,
        query,
        ctx,
        tool_selection.get("selected_tools") or [],
        round_num=1,
    )
    executed_tools = list(tool_selection.get("selected_tools") or [])

    if normalize_agent_planner(planner) == "llm" and normalized_rounds > 1:
        followup_tools, followup_reason = await plan_agent_followup_tools_llm(
            ctx,
            tool_steps,
            executed_tools,
            max_tools=ROUND2_MAX_TOOLS,
        )
        tool_selection["followup_selected_tools"] = followup_tools
        if followup_reason:
            tool_selection["followup_fallback_reason"] = followup_reason

        remaining_slots = max(0, MAX_AGENT_TOOLS_TOTAL - len(executed_tools))
        followup_tools = [name for name in followup_tools if name not in set(executed_tools)][:remaining_slots]
        if followup_tools:
            round2_observations, round2_steps = _execute_agent_tools_round(
                db,
                user_id,
                session_id,
                query,
                ctx,
                followup_tools,
                round_num=2,
            )
            observations.update(round2_observations)
            tool_steps.extend(round2_steps)
            tool_selection["rounds"] = 2
            tool_selection["followup_selected_tools"] = followup_tools

    return _assemble_agent_observations(observations, tool_selection, tool_steps)


async def run_agent_query(
    db: Session,
    user_id: int,
    session_id: str,
    query: str,
    conversation_history: Optional[List[Dict]] = None,
    *,
    planner: str = "rule",
) -> Dict:
    observations = await build_agent_observations_v2_async(
        db,
        user_id,
        session_id,
        query,
        conversation_history,
        planner=planner,
    )
    answer_result = await generate_agent_answer(query, observations, conversation_history)
    answer = answer_result[0] if isinstance(answer_result, tuple) else answer_result
    return {
        "answer": answer,
        "observations": observations,
    }
