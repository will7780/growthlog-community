"""
Agent pending action execution.

Only a small whitelist of user-confirmed writes is supported. The Agent itself
must create pending actions; business tables are modified only after confirm.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, Optional

from sqlalchemy.orm import Session

from app.models import AgentMemory, AgentPendingAction, Entry, EntryLabel, Todo
from app.services.agent_memory_quality import analyze_memory_candidate, normalize_memory_content
from app.timeutil import now_local

EXECUTABLE_ACTION_TYPES = {"create_todo", "create_memory", "update_memory", "save_summary_entry"}
MEMORY_TYPES = {"goal", "preference", "project", "profile", "insight"}


class PendingActionError(ValueError):
    """Raised when a pending action is invalid or cannot be executed."""


def create_pending_action(
    db: Session,
    *,
    user_id: int,
    action_type: str,
    payload_json: Dict,
    session_id: Optional[str] = None,
    created_by_message_id: Optional[int] = None,
) -> AgentPendingAction:
    action = AgentPendingAction(
        user_id=user_id,
        session_id=session_id,
        action_type=action_type,
        payload_json=payload_json,
        status="pending",
        created_by_message_id=created_by_message_id,
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


def propose_pending_actions_from_turn(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    user_message: str,
    answer: str,
    assistant_message_id: Optional[int] = None,
) -> list[AgentPendingAction]:
    """Create conservative pending actions when the user explicitly asks for one."""
    message = (user_message or "").strip()
    answer_text = (answer or "").strip()
    if not message or not answer_text:
        return []

    proposals: list[tuple[str, Dict]] = []
    if _wants_todo(message):
        proposals.append(("create_todo", {"content": _compact_text(answer_text, 500)}))
    if _wants_memory(message):
        proposals.append((
            "create_memory",
            {
                "memory_type": _infer_memory_type(message),
                "content": _compact_text(answer_text, 2000),
                "source": "agent_proposal",
                "confidence": 0.7,
            },
        ))
    if _wants_summary_entry(message):
        proposals.append((
            "save_summary_entry",
            {
                "title": "AI 总结",
                "content": _compact_text(answer_text, 10000),
                "label_code": _extract_label_code(message),
            },
        ))

    created: list[AgentPendingAction] = []
    for action_type, payload in proposals[:3]:
        created.append(create_pending_action(
            db,
            user_id=user_id,
            session_id=session_id,
            action_type=action_type,
            payload_json=payload,
            created_by_message_id=assistant_message_id,
        ))
    return created


def reject_pending_action(db: Session, action: AgentPendingAction) -> AgentPendingAction:
    if action.status != "pending":
        raise PendingActionError("只有 pending 状态的动作可以拒绝")
    action.status = "rejected"
    db.commit()
    db.refresh(action)
    return action


def confirm_pending_action(db: Session, action: AgentPendingAction) -> AgentPendingAction:
    if action.status != "pending":
        raise PendingActionError("只有 pending 状态的动作可以确认")
    if action.action_type not in EXECUTABLE_ACTION_TYPES:
        raise PendingActionError(f"动作 {action.action_type} 当前阶段不能执行")

    action.status = "confirmed"
    action.confirmed_at = now_local()
    try:
        result = _execute_action(db, action)
        action.status = "executed"
        action.executed_at = now_local()
        action.result_json = result
        action.error_message = None
        # Business write commits first so embedding network I/O never holds
        # the pending-action / AgentMemory transaction lock.
        db.commit()
    except Exception as exc:
        db.rollback()
        fresh = db.query(AgentPendingAction).filter(AgentPendingAction.id == action.id).first()
        if fresh:
            fresh.status = "failed"
            fresh.error_message = str(exc)[:1000]
            db.commit()
            db.refresh(fresh)
            return fresh
        raise

    db.refresh(action)
    # Post-commit embedding sync: provider failure must not flip executed→failed.
    if action.status == "executed" and action.action_type in {"create_memory", "update_memory"}:
        _sync_personalization_embedding_after_pending(db, action)
    elif action.status == "executed" and action.action_type == "create_todo":
        _sync_todo_knowledge_after_pending(db, action)
    return action


def _sync_todo_knowledge_after_pending(db: Session, action: AgentPendingAction) -> None:
    """Mirror a user-confirmed Todo after the business transaction commits."""
    result = action.result_json or {}
    todo_id = result.get("todo_id")
    if not todo_id:
        return
    from app.services.todo_knowledge import best_effort_sync_todo_ids

    best_effort_sync_todo_ids(
        db, user_id=int(action.user_id), todo_ids=[int(todo_id)]
    )


def _sync_personalization_embedding_after_pending(
    db: Session, action: AgentPendingAction
) -> None:
    """Reuse after_agent_memory_mutation; never raise into confirm_pending_action."""
    result = action.result_json or {}
    memory_id = result.get("memory_id")
    if not memory_id:
        return
    memory = (
        db.query(AgentMemory)
        .filter(
            AgentMemory.id == int(memory_id),
            AgentMemory.user_id == int(action.user_id),
        )
        .first()
    )
    if memory is None:
        return
    try:
        from app.services.agent_memory_embeddings import after_agent_memory_mutation

        after_agent_memory_mutation(
            db, user_id=int(action.user_id), memory=memory
        )
    except Exception:  # noqa: BLE001
        # Business action already executed; search may degrade to recent-memory.
        return


def _execute_action(db: Session, action: AgentPendingAction) -> Dict:
    if action.action_type == "create_todo":
        return _create_todo(db, action.user_id, action.payload_json or {})
    if action.action_type == "create_memory":
        return _create_memory(db, action.user_id, action.payload_json or {})
    if action.action_type == "update_memory":
        return _update_memory(db, action.user_id, action.payload_json or {})
    if action.action_type == "save_summary_entry":
        return _save_summary_entry(db, action.user_id, action.payload_json or {})
    raise PendingActionError(f"不支持的动作类型: {action.action_type}")


def _wants_todo(message: str) -> bool:
    return any(token in message for token in ["创建待办", "生成待办", "加到待办", "加入待办", "创建小要事", "加到小要事"])


def _wants_memory(message: str) -> bool:
    # Keep legacy trigger phrases; also accept「个性设置」wording.
    return any(
        token in message
        for token in [
            "保存为记忆",
            "写入记忆",
            "加入记忆",
            "保存长期记忆",
            "记住这个",
            "保存为个性设置",
            "写入个性设置",
            "加入个性设置",
            "保存个性设置",
        ]
    )


def _wants_summary_entry(message: str) -> bool:
    return any(token in message for token in ["保存为记录", "保存成记录", "保存总结", "保存复盘", "生成记录"])


def _infer_memory_type(message: str) -> str:
    if "偏好" in message:
        return "preference"
    if "项目" in message:
        return "project"
    if "画像" in message or "个人" in message:
        return "profile"
    if "洞察" in message or "结论" in message:
        return "insight"
    return "goal"


def _extract_label_code(message: str) -> Optional[str]:
    marker = "标签"
    if marker not in message:
        return None
    after = message.split(marker, 1)[1].strip(" ：:，,。")
    candidate = after.split()[0].strip(" ：:，,。") if after else ""
    return candidate[:32] or None


def _compact_text(text: str, max_len: int) -> str:
    return text.strip()[:max_len]


def _parse_due_date(value) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise PendingActionError("due_date 必须是 YYYY-MM-DD 格式") from exc
    raise PendingActionError("due_date 必须是 YYYY-MM-DD 格式")


def _create_todo(db: Session, user_id: int, payload: Dict) -> Dict:
    content = str(payload.get("content") or "").strip()
    if not content:
        raise PendingActionError("create_todo.content 不能为空")
    if len(content) > 500:
        raise PendingActionError("create_todo.content 不能超过 500 字")

    todo = Todo(
        user_id=user_id,
        content=content,
        due_date=_parse_due_date(payload.get("due_date")),
        is_done=False,
    )
    db.add(todo)
    db.flush()
    return {"todo_id": int(todo.id), "action": "created"}


def _create_memory(db: Session, user_id: int, payload: Dict) -> Dict:
    memory_type = str(payload.get("memory_type") or "").strip()
    content = normalize_memory_content(str(payload.get("content") or ""))
    if memory_type not in MEMORY_TYPES:
        raise PendingActionError("create_memory.memory_type 不合法")
    if not content:
        raise PendingActionError("create_memory.content 不能为空")
    quality = analyze_memory_candidate(
        db,
        user_id=user_id,
        memory_type=memory_type,
        content=content,
    )

    memory = AgentMemory(
        user_id=user_id,
        memory_type=memory_type,
        content=quality.normalized_content,
        source=str(payload.get("source") or "agent_pending")[:100],
        confidence=float(payload.get("confidence", 0.8)),
        is_active=True,
    )
    db.add(memory)
    db.flush()
    return {
        "memory_id": int(memory.id),
        "action": "created",
        "quality_warnings": quality.warnings,
    }


def _update_memory(db: Session, user_id: int, payload: Dict) -> Dict:
    memory_id = payload.get("memory_id")
    if not memory_id:
        raise PendingActionError("update_memory.memory_id 不能为空")
    memory = (
        db.query(AgentMemory)
        .filter(AgentMemory.id == int(memory_id), AgentMemory.user_id == user_id)
        .first()
    )
    if not memory:
        raise PendingActionError("个性设置不存在或不属于当前用户")

    if payload.get("memory_type") is not None:
        memory_type = str(payload["memory_type"]).strip()
        if memory_type not in MEMORY_TYPES:
            raise PendingActionError("update_memory.memory_type 不合法")
        memory.memory_type = memory_type
    next_memory_type = memory.memory_type
    quality_warnings = []
    if payload.get("content") is not None:
        content = normalize_memory_content(str(payload["content"]))
        if not content:
            raise PendingActionError("update_memory.content 不能为空")
        quality = analyze_memory_candidate(
            db,
            user_id=user_id,
            memory_type=next_memory_type,
            content=content,
            exclude_memory_id=int(memory.id),
        )
        quality_warnings = quality.warnings
        memory.content = quality.normalized_content
    if payload.get("confidence") is not None:
        memory.confidence = max(0.0, min(1.0, float(payload["confidence"])))
    if payload.get("is_active") is not None:
        memory.is_active = bool(payload["is_active"])

    db.flush()
    return {
        "memory_id": int(memory.id),
        "action": "updated",
        "quality_warnings": quality_warnings,
    }


def _resolve_label_code(db: Session, user_id: int, requested: str | None) -> str:
    candidates = []
    if requested:
        candidates.append(requested)
    candidates.extend(["review", "insight", "work"])

    for code in candidates:
        label = (
            db.query(EntryLabel)
            .filter(
                EntryLabel.code == str(code).strip(),
                EntryLabel.is_active.is_(True),
                (EntryLabel.user_id.is_(None)) | (EntryLabel.user_id == user_id),
            )
            .first()
        )
        if label:
            return label.code

    fallback = (
        db.query(EntryLabel)
        .filter(
            EntryLabel.is_active.is_(True),
            (EntryLabel.user_id.is_(None)) | (EntryLabel.user_id == user_id),
        )
        .order_by(EntryLabel.sort_order.asc(), EntryLabel.created_at.asc())
        .first()
    )
    if not fallback:
        raise PendingActionError("没有可用标签，无法保存总结记录")
    return fallback.code


def _save_summary_entry(db: Session, user_id: int, payload: Dict) -> Dict:
    content = str(payload.get("content") or payload.get("summary") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not content:
        raise PendingActionError("save_summary_entry.content 不能为空")
    if title and not content.startswith(title):
        content = f"{title}\n{content}"
    if len(content) > 10000:
        content = content[:10000]

    label_code = _resolve_label_code(db, user_id, payload.get("label_code"))
    parent_id = payload.get("parent_id")
    if parent_id is not None:
        parent = (
            db.query(Entry)
            .filter(Entry.id == int(parent_id), Entry.user_id == user_id)
            .first()
        )
        if not parent:
            raise PendingActionError("父记录不存在或不属于当前用户")
        if parent.label_code != label_code:
            raise PendingActionError("父记录与总结记录标签不一致")

    entry = Entry(
        user_id=user_id,
        label_code=label_code,
        content=content,
        parent_id=int(parent_id) if parent_id is not None else None,
    )
    db.add(entry)
    db.flush()
    return {"entry_id": int(entry.id), "label_code": label_code, "action": "created"}
