"""
Keyword retrieval for GrowthLog records.

Phase 1 uses a conservative LIKE-based fallback so it works on the current
MySQL schema. The public function shape is intentionally provider-neutral and
can later be backed by MySQL FULLTEXT/ngram or a dedicated chunk_terms table.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime

from app.timeutil import now_local
from typing import Dict, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Entry, EntryLabel
from app.services.query_normalize import normalize_query


def extract_query_terms(query: str, max_terms: int = 12) -> List[str]:
    """Extract stable keyword terms with CJK/Latin boundary + synonym expansion."""
    nq = normalize_query(query)
    terms: "OrderedDict[str, None]" = OrderedDict()
    for token in nq.expanded_terms:
        tok = (token or "").strip().lower()
        if not tok:
            continue
        if len(tok) >= 2 or ("\u4e00" <= tok[0] <= "\u9fff"):
            terms[tok[:40]] = None
        cjk_chars = [ch for ch in tok if "\u4e00" <= ch <= "\u9fff"]
        if len(cjk_chars) >= 2:
            text = "".join(cjk_chars)
            for size in (2, 3, 4):
                for idx in range(0, max(0, len(text) - size + 1)):
                    terms[text[idx: idx + size]] = None
                    if len(terms) >= max_terms:
                        return list(terms.keys())
        if len(terms) >= max_terms:
            return list(terms.keys())

    if not terms and (query or "").strip():
        terms[(query or "").strip()[:40].lower()] = None
    return list(terms.keys())[:max_terms]


def _title_and_snippet(content: str, max_len: int = 260) -> tuple[str, str]:
    title = (content or "").split("\n")[0].strip()
    body = (content or "").replace(title, "", 1).strip() if title else (content or "").strip()
    return title[:120], body[:max_len]


def _keyword_score(content: str, label_name: str, terms: List[str]) -> float:
    haystack = f"{content or ''}\n{label_name or ''}".lower()
    score = 0.0
    for term in terms:
        count = haystack.count(term.lower())
        if count:
            score += 1.0 + min(count, 5) * 0.15
    return score


def search_keyword_entries(
    db: Session,
    user_id: int,
    query: str,
    top_k: int = 30,
    label_code: Optional[str] = None,
) -> List[Dict]:
    terms = extract_query_terms(query)
    if not terms:
        return []

    filters = []
    for term in terms:
        pattern = f"%{term}%"
        filters.append(Entry.content.ilike(pattern))
        filters.append(Entry.label_code.ilike(pattern))
        filters.append(EntryLabel.name.ilike(pattern))

    rows = (
        db.query(Entry, EntryLabel.name)
        .outerjoin(EntryLabel, Entry.label_code == EntryLabel.code)
        .filter(Entry.user_id == user_id)
    )
    if label_code:
        rows = rows.filter(Entry.label_code == label_code)
    rows = rows.filter(or_(*filters)).order_by(Entry.created_at.desc()).limit(max(top_k * 3, top_k)).all()

    scored: List[tuple[float, Entry, str]] = []
    now_ts = now_local().timestamp()
    for entry, label_name in rows:
        base = _keyword_score(entry.content or "", label_name or entry.label_code, terms)
        if base <= 0:
            continue
        created_ts = entry.created_at.timestamp() if entry.created_at else now_ts
        age_days = max(0.0, (now_ts - created_ts) / 86400)
        recency_bonus = max(0.0, 0.2 - min(age_days, 365) / 365 * 0.2)
        scored.append((base + recency_bonus, entry, label_name or entry.label_code))

    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return []

    max_score = scored[0][0] or 1.0
    results: List[Dict] = []
    for score, entry, label_name in scored[:top_k]:
        title, snippet = _title_and_snippet(entry.content)
        normalized_score = max(0.0, min(1.0, score / max_score))
        results.append({
            "source_type": "entry",
            "source_id": int(entry.id),
            "entry_id": int(entry.id),
            "chunk_id": None,
            "title": title or f"记录 #{entry.id}",
            "label_name": label_name,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "snippet": snippet,
            "content": entry.content,
            "relevance_score": float(normalized_score),
            "retrieval_method": "keyword",
            "source_reason": "keyword_match",
            "metadata": {
                "matched_terms": terms,
                "keyword_score": score,
            },
        })
    return results
