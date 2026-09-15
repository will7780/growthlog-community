"""
Review scope resolver.

This helper is read-only and user-scoped. It is safe to keep enabled while the
LLM review generation feature remains gated.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from sqlalchemy.orm import Session

from app.models import Entry
from app.timeutil import now_local


def _parse_date(value: Optional[str]):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10])
    except ValueError:
        return None


def resolve_review_scope_entries(
    db: Session,
    user_id: int,
    scope_type: str = "recent_7d",
    entry_ids: Optional[Iterable[int]] = None,
    tag: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50,
) -> List[Entry]:
    query = db.query(Entry).filter(Entry.user_id == user_id)
    normalized_scope = (scope_type or "recent_7d").strip()

    if normalized_scope == "entries" and entry_ids:
        ids = sorted({int(item) for item in entry_ids if item})
        return (
            query.filter(Entry.id.in_(ids))
            .order_by(Entry.created_at.desc())
            .limit(limit)
            .all()
        )

    if normalized_scope == "tag" and tag:
        query = query.filter(Entry.label_code == tag)
    elif normalized_scope == "range":
        start = _parse_date(start_date)
        end = _parse_date(end_date)
        if start:
            query = query.filter(Entry.created_at >= start)
        if end:
            query = query.filter(Entry.created_at < end + timedelta(days=1))
    elif normalized_scope == "recent_30d":
        query = query.filter(Entry.created_at >= now_local() - timedelta(days=30))
    elif normalized_scope in {"recent_7d", "recent"}:
        query = query.filter(Entry.created_at >= now_local() - timedelta(days=7))

    return query.order_by(Entry.created_at.desc()).limit(limit).all()
