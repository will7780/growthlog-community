"""
High-recall candidate pool builder for lookup_all / lookup_one.

Must NOT reuse truncated generation_pool or context as "all results".
Must NOT import r5 eval gold / qrels / private manifests.

R5.2.2: candidate cap is an internal resource budget. Over-budget pools use
deterministic high-recall selection by topic-hit quality (not relevance).
Filtered results must not be described as exhaustive "全部".
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Entry, EntryLabel
from app.services.attachment_search import search_similar_attachment_chunks
from app.services.embedding import generate_embedding
from app.services.query_normalize import normalize_query, primary_topic_terms, retrieval_query_text
from app.services.rag_pipeline import _normalize_attachment, fetch_dense_channel_refs
from app.services.reference_identity import annotate_with_canonical_keys, dedupe_by_evidence_origin
from app.services.vector_search import initialize_user_index
from app.services.todo_knowledge import search_keyword_todos
from app.services.notion_knowledge import search_keyword_notion_pages

logger = logging.getLogger(__name__)

# R11.7: internal adaptive budget target ~80–120; never user-facing "too broad".
DEFAULT_CANDIDATE_CAP = 120
DEFAULT_DENSE_TOP_K = 80
DEFAULT_ATTACHMENT_TOP_K = 40
DEFAULT_LEXICAL_SCAN_BUDGET = 2000
HIT_WINDOW = 180
MAX_HIT_WINDOWS = 3
SHORT_FULL_BODY_CAP = 400
OPENING_LEN = 160
MIN_DENSE_CHANNEL_SEATS = 1
# R5.5D: bounded multi-segment evidence for dense / no-term long notes.
DEFAULT_EVIDENCE_BUDGET = 1400
MIN_EVIDENCE_BUDGET = 1200
MAX_EVIDENCE_BUDGET = 1600


def _evidence_budget() -> int:
    raw = (os.getenv("RAG_LOOKUP_EVIDENCE_BUDGET") or "").strip()
    if raw.isdigit():
        return max(MIN_EVIDENCE_BUDGET, min(MAX_EVIDENCE_BUDGET, int(raw)))
    return DEFAULT_EVIDENCE_BUDGET


def _is_dense_retrieval(ref: Mapping[str, Any]) -> bool:
    method = str(ref.get("retrieval_method") or "").lower()
    source = str(ref.get("source_type") or "").lower()
    if "dense" in method:
        return True
    if source in {"entry", "knowledge", "attachment"} and method in {
        "dense",
        "entry_dense",
        "knowledge_dense",
        "attachment_dense",
        "",
    }:
        # Prefer explicit channel tags when present.
        pass
    channels = ref.get("retrieval_sources") or ref.get("channels") or []
    if isinstance(channels, (list, tuple)):
        joined = " ".join(str(c).lower() for c in channels)
        if "dense" in joined:
            return True
    return "dense" in method


def _stratified_long_windows(
    text: str, *, budget: int
) -> tuple[str, List[str], bool, float]:
    """Deterministic opening + uniform mid segments + tail; never uses gold.

    Returns (window, sources, truncated, coverage_ratio).
    coverage_ratio = unique char span covered / len(text), clipped to [0, 1].
    """
    raw = text or ""
    if not raw:
        return "", [], False, 1.0
    if len(raw) <= budget:
        return raw, ["full_body"], False, 1.0
    sep = " … "
    # Five anchors (0 / 25% / 50% / 75% / 100%) share the budget after separators.
    n_segs = 5
    usable = max(400, budget - (n_segs - 1) * len(sep))
    seg = max(80, usable // n_segs)
    labels = ("opening", "mid_1", "mid_2", "mid_3", "tail")
    spans: List[tuple[int, int]] = []
    sources: List[str] = []
    max_start = max(0, len(raw) - seg)
    for i in range(n_segs):
        start = int(max_start * i / (n_segs - 1)) if n_segs > 1 else 0
        end = min(len(raw), start + seg)
        spans.append((start, end))
        sources.append(labels[i])
    # Merge overlapping spans for coverage accounting.
    merged: List[tuple[int, int]] = []
    for start, end in sorted(spans):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    covered = sum(end - start for start, end in merged)
    coverage = round(min(1.0, covered / float(len(raw))), 4)
    parts = [raw[s:e] for s, e in spans]
    window = sep.join(parts)
    truncated = len(window) > budget or len(raw) > budget
    return window[:budget], sources, truncated, coverage

# User-visible copy when internal budget selection was applied (not "全部").
FILTERED_LOOKUP_PREFACE = "为你筛选到以下相关记录"

_BUDGET_CHANNEL_ORDER = (
    "lexical_title",
    "lexical_body",
    "entry_dense",
    "knowledge_dense",
    "attachment_dense",
    "other",
)

_DENSE_CHANNELS = ("entry_dense", "knowledge_dense", "attachment_dense")

_EXHAUSTIVE_CLAIM_RES = (
    re.compile(r"全部相关(的)?(记录|结果|内容|条目)?", re.IGNORECASE),
    re.compile(r"所有相关(的)?(记录|结果|内容|条目)?", re.IGNORECASE),
    re.compile(r"完整列出", re.IGNORECASE),
    re.compile(r"完整罗列", re.IGNORECASE),
    re.compile(r"列出全部", re.IGNORECASE),
    re.compile(r"列出所有", re.IGNORECASE),
    re.compile(r"全部列出", re.IGNORECASE),
    re.compile(r"所有列出", re.IGNORECASE),
    re.compile(r"无一遗漏", re.IGNORECASE),
    re.compile(r"完整清单", re.IGNORECASE),
    re.compile(r"全部(的)?(记录|结果|提示词|prompt)", re.IGNORECASE),
    re.compile(r"所有(的)?(记录|结果|提示词|prompt)", re.IGNORECASE),
)


def _candidate_cap() -> int:
    raw = (os.getenv("RAG_LOOKUP_ALL_CANDIDATE_CAP") or "").strip()
    if raw.isdigit():
        return max(20, min(2000, int(raw)))
    return DEFAULT_CANDIDATE_CAP


def _dense_top_k() -> int:
    raw = (os.getenv("RAG_LOOKUP_ALL_DENSE_TOP_K") or "").strip()
    if raw.isdigit():
        return max(10, min(500, int(raw)))
    return DEFAULT_DENSE_TOP_K


def _lexical_scan_budget() -> int:
    raw = (os.getenv("RAG_LOOKUP_LEXICAL_SCAN_BUDGET") or "").strip()
    if raw.isdigit():
        return max(100, min(20000, int(raw)))
    return DEFAULT_LEXICAL_SCAN_BUDGET


def _title_and_body(content: str) -> tuple[str, str]:
    title = (content or "").split("\n")[0].strip()
    body = (content or "").replace(title, "", 1).strip() if title else (content or "").strip()
    return title[:120], body


def _public_title(title: Any) -> str:
    """Public-facing title: never emit '记录 #<db id>'; empty -> '记录'."""
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    if not text:
        return "记录"
    if re.fullmatch(r"记录\s*#\s*\d+", text):
        return "记录"
    text = re.sub(r"记录\s*#\s*\d+", "记录", text).strip()
    return (text or "记录")[:120]


def _unique_term_hits(text: str, terms: Sequence[str]) -> int:
    hay = (text or "").lower()
    seen: Set[str] = set()
    for term in terms:
        t = (term or "").strip().lower()
        if len(t) < 2:
            continue
        if t in hay:
            seen.add(t)
    return len(seen)


def _term_positions(text: str, terms: Sequence[str]) -> List[tuple[int, str]]:
    hay_l = (text or "").lower()
    hits: List[tuple[int, str]] = []
    for term in terms:
        t = (term or "").strip().lower()
        if len(t) < 2:
            continue
        start = 0
        while True:
            pos = hay_l.find(t, start)
            if pos < 0:
                break
            hits.append((pos, t))
            start = pos + max(1, len(t))
    hits.sort(key=lambda x: x[0])
    return hits


def _hit_window(text: str, terms: Sequence[str], *, radius: int = HIT_WINDOW) -> str:
    hay = text or ""
    positions = _term_positions(hay, terms)
    if not positions:
        return hay[: radius * 2]
    best = positions[0][0]
    start = max(0, best - radius)
    end = min(len(hay), best + radius)
    return hay[start:end]


def _multi_hit_windows(text: str, terms: Sequence[str], *, radius: int = HIT_WINDOW) -> List[str]:
    """Up to MAX_HIT_WINDOWS non-overlapping local windows around term hits."""
    hay = text or ""
    positions = _term_positions(hay, terms)
    if not positions:
        return [hay[: radius * 2]] if hay else []
    windows: List[str] = []
    used_ranges: List[tuple[int, int]] = []
    for pos, _term in positions:
        start = max(0, pos - radius)
        end = min(len(hay), pos + radius)
        if any(not (end <= a or start >= b) for a, b in used_ranges):
            continue
        windows.append(hay[start:end])
        used_ranges.append((start, end))
        if len(windows) >= MAX_HIT_WINDOWS:
            break
    return windows


def _lexical_quality_key(ref: Mapping[str, Any]) -> tuple:
    ev = ref.get("judge_evidence") if isinstance(ref.get("judge_evidence"), dict) else {}
    title_hits = int(ev.get("title_term_hit_count") or 0)
    body_hits = int(ev.get("body_term_hit_count") or 0)
    return (
        0 if title_hits > 0 else 1,
        -title_hits,
        -body_hits,
        int(ref.get("entry_id") or 0),
        str(ref.get("reference_key") or ""),
    )


def lexical_union_entries(
    db: Session,
    *,
    user_id: int,
    terms: Sequence[str],
    label_code: Optional[str] = None,
    scan_budget: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Union of entry rows matching ANY normalized/synonym term.

    Uses a configurable scan budget + topic-quality ordering — not a silent
    id-ordered hard truncate at 500.
    """
    clean = []
    seen: Set[str] = set()
    for t in terms:
        tok = (t or "").strip()
        if len(tok) < 2:
            continue
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append(tok[:40])
    empty_meta = {
        "pre_scan_count": 0,
        "scan_budget": scan_budget if scan_budget is not None else _lexical_scan_budget(),
        "scan_budget_applied": False,
    }
    if not clean:
        return [], empty_meta

    filters = []
    for term in clean:
        pattern = f"%{term}%"
        filters.append(Entry.content.ilike(pattern))
        filters.append(Entry.label_code.ilike(pattern))
        filters.append(EntryLabel.name.ilike(pattern))

    q = (
        db.query(Entry, EntryLabel.name)
        .outerjoin(EntryLabel, Entry.label_code == EntryLabel.code)
        .filter(Entry.user_id == user_id)
        .filter(or_(*filters))
    )
    if label_code:
        q = q.filter(Entry.label_code == label_code)

    budget = int(scan_budget) if scan_budget is not None else _lexical_scan_budget()
    budget = max(100, min(20000, budget))
    # R5.2.3: avoid expensive COUNT(*); probe budget+1 to detect truncation.
    rows = q.order_by(Entry.id.asc()).limit(budget + 1).all()
    scan_budget_applied = len(rows) > budget
    if scan_budget_applied:
        rows = rows[:budget]
    pre_scan_count = len(rows) + (1 if scan_budget_applied else 0)

    out: List[Dict[str, Any]] = []
    for entry, label_name in rows:
        content = entry.content or ""
        title, body = _title_and_body(content)
        title_hits = _unique_term_hits(title, clean)
        body_hits = _unique_term_hits(body, clean)
        window = _hit_window(content, clean)
        out.append(
            {
                "source_type": "entry",
                "source_id": int(entry.id),
                "entry_id": int(entry.id),
                "chunk_id": None,
                "title": _public_title(title),
                "label_name": label_name or entry.label_code or "",
                "created_at": entry.created_at.isoformat() if entry.created_at else None,
                "snippet": window[:420],
                "content": content,
                "relevance_score": 1.0,
                "retrieval_method": "lexical_union",
                "source_reason": "lookup_all_lexical",
                "metadata": {"label_code": entry.label_code},
                "judge_evidence": {
                    "title": _public_title(title),
                    "hit_window": window[:420],
                    "source_type": "entry",
                    "entry_id": int(entry.id),
                    "title_term_hit": title_hits > 0,
                    "body_term_hit": body_hits > 0,
                    "title_term_hit_count": title_hits,
                    "body_term_hit_count": body_hits,
                },
            }
        )

    out.sort(key=_lexical_quality_key)
    if pre_scan_count < 0:
        pre_scan_count = len(out)
    meta = {
        "pre_scan_count": pre_scan_count,
        "scan_budget": budget,
        "scan_budget_applied": scan_budget_applied,
    }
    return out, meta


def _budget_channel(ref: Mapping[str, Any]) -> str:
    """Channel bucket for budget selection — recall routing, not relevance."""
    method = str(ref.get("retrieval_method") or "")
    source = str(ref.get("source_type") or "")
    evidence = ref.get("judge_evidence") if isinstance(ref.get("judge_evidence"), dict) else {}
    if method in {"lexical_union", "lexical_todo", "lexical_notion"}:
        return "lexical_title" if evidence.get("title_term_hit") else "lexical_body"
    if source == "attachment_chunk" or method == "attachment_dense":
        return "attachment_dense"
    if source == "knowledge_source":
        return "knowledge_dense"
    if method == "dense" or source == "entry":
        return "entry_dense"
    return "other"


def _selection_sort_key(ref: Mapping[str, Any]) -> tuple:
    """Lower is better. reference_key is final tie-break only."""
    ev = ref.get("judge_evidence") if isinstance(ref.get("judge_evidence"), dict) else {}
    title_hits = int(ev.get("title_term_hit_count") or 0)
    if title_hits <= 0 and ev.get("title_term_hit"):
        title_hits = 1
    body_hits = int(ev.get("body_term_hit_count") or 0)
    if body_hits <= 0 and ev.get("body_term_hit"):
        body_hits = 1
    channel_rank = int(ref.get("_channel_rank") or 10**9)
    dense_rank = int(ref.get("_dense_rank") or channel_rank)
    return (
        0 if title_hits > 0 else 1,
        -title_hits,
        -body_hits,
        channel_rank,
        dense_rank,
        str(ref.get("reference_key") or ""),
    )


def select_candidates_for_budget(
    refs: Sequence[Dict[str, Any]],
    *,
    budget: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Deterministic high-recall cross-channel selection under an internal budget.

    Does NOT decide relevant / found. Strong lexical topic hits preferred;
    min seats reserved for present dense channels; reference_key is tie-break only.
    """
    cap = max(1, int(budget))
    items = [dict(r) for r in refs]
    if len(items) <= cap:
        return items, {
            "candidate_budget_applied": False,
            "result_scope": "exhaustive",
            "pre_budget_count": len(items),
            "selected_count": len(items),
            "budget": cap,
            "channel_selected": {},
            "selection_reason": "under_budget_all",
        }

    # Assign stable per-channel ranks. Prefer explicit retrieval ranks when present;
    # otherwise order by reference_key so selection is independent of merge order.
    buckets: Dict[str, List[Dict[str, Any]]] = {name: [] for name in _BUDGET_CHANNEL_ORDER}
    for ref in items:
        ch = _budget_channel(ref)
        ref["_sel_channel"] = ch
        buckets.setdefault(ch, []).append(ref)
    for ch, bucket in buckets.items():
        bucket.sort(
            key=lambda r: (
                int(
                    r.get("channel_rank")
                    if r.get("channel_rank") is not None
                    else (r.get("rank") if r.get("rank") is not None else 10**9)
                ),
                -float(r.get("relevance_score") or 0.0),
                str(r.get("reference_key") or ""),
            )
        )
        for i, ref in enumerate(bucket, start=1):
            explicit = ref.get("channel_rank")
            if explicit is None:
                explicit = ref.get("rank")
            ref["_channel_rank"] = int(explicit) if explicit is not None else i
            if ch in _DENSE_CHANNELS:
                dense_explicit = ref.get("dense_rank")
                ref["_dense_rank"] = (
                    int(dense_explicit) if dense_explicit is not None else ref["_channel_rank"]
                )
            else:
                ref["_dense_rank"] = 10**9

    ordered = sorted(items, key=_selection_sort_key)
    selected: List[Dict[str, Any]] = []
    selected_keys: Set[str] = set()
    channel_selected = {name: 0 for name in _BUDGET_CHANNEL_ORDER}
    present_channels = {str(r.get("_sel_channel")) for r in ordered}

    def _try_add(ref: Dict[str, Any]) -> bool:
        if len(selected) >= cap:
            return False
        key = str(ref.get("reference_key") or "")
        if key and key in selected_keys:
            return False
        selected.append(ref)
        if key:
            selected_keys.add(key)
        ch = str(ref.get("_sel_channel") or "other")
        channel_selected[ch] = channel_selected.get(ch, 0) + 1
        return True

    # Pre-reserve min seats for dense channels that actually exist (quality-best each).
    reserved: List[Dict[str, Any]] = []
    for ch in _DENSE_CHANNELS:
        if ch not in present_channels:
            continue
        taken = 0
        for ref in ordered:
            if ref.get("_sel_channel") != ch:
                continue
            reserved.append(ref)
            taken += 1
            if taken >= MIN_DENSE_CHANNEL_SEATS:
                break
    reserve_n = min(len(reserved), cap)

    # Fill remaining seats by topic-quality (strong lexical title hits first).
    quality_slots = max(0, cap - reserve_n)
    for ref in ordered:
        if len(selected) >= quality_slots:
            break
        _try_add(ref)

    # Ensure reserved dense seats are present.
    for ref in reserved:
        _try_add(ref)

    # Fill any leftover.
    for ref in ordered:
        _try_add(ref)

    # Stable output order = selection quality (not round-robin by key).
    selected.sort(key=_selection_sort_key)
    for ref in selected:
        ref.pop("_sel_channel", None)
        ref.pop("_channel_rank", None)
        ref.pop("_dense_rank", None)

    return selected, {
        "candidate_budget_applied": True,
        "result_scope": "budget_filtered",
        "pre_budget_count": len(items),
        "selected_count": len(selected),
        "budget": cap,
        "channel_selected": channel_selected,
        "selection_reason": "topic_quality_min_dense_seats",
        # Internal diagnostic alias — must never alone trigger user 503.
        "candidate_pool_truncated": True,
    }


def apply_lookup_answer_scope(
    answer: str,
    *,
    intent: str,
    result_scope: str,
) -> str:
    """Ensure filtered lookup_all answers do not falsely claim exhaustive listings."""
    text = (answer or "").strip()
    if intent != "lookup_all" or result_scope != "budget_filtered":
        return text
    for pat in _EXHAUSTIVE_CLAIM_RES:
        text = pat.sub("相关记录", text)
    text = re.sub(r"\s+", " ", text).strip(" ，,。")
    if FILTERED_LOOKUP_PREFACE in text:
        return text
    if not text:
        return f"{FILTERED_LOOKUP_PREFACE}。"
    return f"{FILTERED_LOOKUP_PREFACE}。\n{text}"


def _attach_judge_evidence(refs: Sequence[Dict[str, Any]], terms: Sequence[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    topic_terms = [t for t in terms if len((t or "").strip()) >= 2]
    budget = _evidence_budget()
    for ref in refs:
        item = dict(ref)
        text = str(item.get("content") or item.get("snippet") or "")
        title = _public_title(item.get("title") or "")
        item["title"] = title
        combined = f"{title}\n{text}" if title else text
        content_char_len = len(text)
        title_hits = _unique_term_hits(title, topic_terms)
        body_hits = _unique_term_hits(text, topic_terms)
        title_term_hit = title_hits > 0
        body_term_hit = body_hits > 0
        term_hit_count = len(_term_positions(combined, topic_terms))
        topic_term_absent = bool((not title_term_hit) and (not body_term_hit))
        dense_hit = _is_dense_retrieval(item)

        window_sources: List[str] = []
        truncated = False
        coverage = 1.0
        if content_char_len > 0 and content_char_len <= SHORT_FULL_BODY_CAP:
            window = text[:SHORT_FULL_BODY_CAP]
            evidence_mode = "short_full_body"
            window_sources = ["full_body"]
            coverage = 1.0
        elif topic_term_absent and dense_hit and content_char_len > SHORT_FULL_BODY_CAP:
            # R5.5D: dense long notes without surface terms — stratified segments.
            window, window_sources, truncated, coverage = _stratified_long_windows(
                text, budget=budget
            )
            evidence_mode = "dense_stratified_segments"
        else:
            opening = text[:OPENING_LEN].strip()
            windows = _multi_hit_windows(combined, topic_terms, radius=HIT_WINDOW)
            parts = []
            if opening:
                parts.append(opening)
                window_sources.append("opening")
            for idx, w in enumerate(windows):
                w = (w or "").strip()
                if w and w not in parts:
                    parts.append(w)
                    window_sources.append(f"hit_window_{idx + 1}")
                if len(parts) >= 1 + MAX_HIT_WINDOWS:
                    break
            joined = " … ".join(parts)
            truncated = len(joined) > budget or content_char_len > budget
            window = joined[:budget]
            evidence_mode = "title_opening_windows"
            covered = min(len(window), content_char_len) if content_char_len else 0
            coverage = round(covered / float(content_char_len), 4) if content_char_len else 1.0

        evidence = {
            "title": title,
            "hit_window": window[:budget],
            "window_sources": list(window_sources),
            "window_count": len(window_sources),
            "title_term_hit": bool(title_term_hit),
            "body_term_hit": bool(body_term_hit),
            "title_term_hit_count": int(title_hits),
            "body_term_hit_count": int(body_hits),
            "term_hit_count": int(term_hit_count),
            "content_char_len": int(content_char_len),
            "short_note": bool(content_char_len > 0 and content_char_len <= SHORT_FULL_BODY_CAP),
            "topic_term_absent": topic_term_absent,
            "evidence_mode": evidence_mode,
            "evidence_coverage": coverage,
            "truncated": bool(truncated),
            "source_type": item.get("source_type"),
            "entry_id": item.get("entry_id"),
            "attachment_id": item.get("attachment_id"),
            "chunk_id": item.get("chunk_id"),
            "modality": item.get("modality"),
            "retrieval_method": item.get("retrieval_method"),
        }
        from app.services.lookup_judge import compute_evidence_digest

        evidence["evidence_digest"] = compute_evidence_digest(evidence)
        item["judge_evidence"] = evidence
        if window:
            item["snippet"] = window[: min(520, budget)]
        out.append(item)
    return out


def build_lookup_candidate_pool(
    db: Session,
    *,
    user_id: int,
    query: str,
    label_code: Optional[str] = None,
    candidate_cap: Optional[int] = None,
    include_attachments: bool = True,
    query_vector: Optional[Sequence[float]] = None,
    generate_query_embedding: bool = True,
    topic_terms: Optional[Sequence[str]] = None,
    retrieval_query: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Independent high-recall pool for lookup intents.

    candidate_cap is an internal LLM-work budget. Over budget → deterministic
    topic-quality selection (result_scope=budget_filtered), never a user 503.

    When generate_query_embedding=False, never call generate_embedding (caller
    already attempted once for this request).

    R11.7-Fix: pass canonical topic_terms / retrieval_query so presentation
    wording in `query` does not affect lexical/dense channels.
    """
    if retrieval_query is not None and str(retrieval_query).strip():
        nq = normalize_query(str(retrieval_query).strip())
    else:
        nq = normalize_query(query)
    if topic_terms is not None:
        terms = [str(t).strip() for t in topic_terms if str(t).strip()]
    else:
        terms = primary_topic_terms(nq)
    if not terms:
        terms = list(nq.expanded_terms)
    retrieval_q = (
        str(retrieval_query).strip()
        if retrieval_query is not None and str(retrieval_query).strip()
        else retrieval_query_text(nq)
    )
    cap = candidate_cap if candidate_cap is not None else _candidate_cap()
    dense_k = _dense_top_k()

    channel_status: Dict[str, str] = {}
    lexical_refs: List[Dict[str, Any]] = []
    lexical_scan_meta: Dict[str, Any] = {
        "pre_scan_count": 0,
        "scan_budget": _lexical_scan_budget(),
        "scan_budget_applied": False,
    }
    knowledge_refs: List[Dict[str, Any]] = []
    entry_dense_refs: List[Dict[str, Any]] = []
    attachment_refs: List[Dict[str, Any]] = []
    todo_lexical_refs: List[Dict[str, Any]] = []
    notion_lexical_refs: List[Dict[str, Any]] = []
    reused_query_vector = query_vector is not None
    active_query_vector: Optional[List[float]] = (
        list(query_vector) if query_vector is not None else None
    )
    embedding_attempted = reused_query_vector or (not generate_query_embedding)
    # Desensitized stage timings (ms). No query/body content.
    stage_ms: Dict[str, float] = {
        "embedding": 0.0,
        "index_init": 0.0,
        "lexical": 0.0,
        "knowledge_dense": 0.0,
        "entry_dense": 0.0,
        "attachment": 0.0,
        "evidence": 0.0,
        "total": 0.0,
    }
    t_total0 = time.perf_counter()

    t0 = time.perf_counter()
    try:
        # FAISS load/rebuild from stored embeddings only — no document regen.
        initialize_user_index(user_id, db)
        channel_status["index_init"] = "ok"
    except Exception as exc:
        channel_status["index_init"] = f"degraded:{type(exc).__name__}"
        logger.warning("lookup pool index_init failed user=%s: %s", user_id, exc)
    stage_ms["index_init"] = round((time.perf_counter() - t0) * 1000.0, 2)

    t0 = time.perf_counter()
    try:
        lexical_refs, lexical_scan_meta = lexical_union_entries(
            db, user_id=user_id, terms=terms, label_code=label_code
        )
        channel_status["lexical_union"] = "ok"
    except Exception as exc:
        channel_status["lexical_union"] = f"degraded:{type(exc).__name__}"
        logger.warning("lexical_union failed user=%s: %s", user_id, exc)
    try:
        todo_lexical_refs = search_keyword_todos(
            db, user_id=user_id, terms=terms, top_k=max(80, cap)
        )
        channel_status["todo_lexical"] = "ok"
    except Exception as exc:
        channel_status["todo_lexical"] = f"degraded:{type(exc).__name__}"
        logger.warning("todo lexical failed user=%s: %s", user_id, exc)
    try:
        notion_lexical_refs = search_keyword_notion_pages(
            db, user_id=user_id, terms=terms, top_k=max(80, cap)
        )
        channel_status["notion_lexical"] = "ok"
    except Exception as exc:
        notion_lexical_refs = []
        channel_status["notion_lexical"] = f"degraded:{type(exc).__name__}"
        logger.warning("notion lexical failed user=%s: %s", user_id, exc)
    stage_ms["lexical"] = round((time.perf_counter() - t0) * 1000.0, 2)

    if active_query_vector is None and generate_query_embedding:
        embedding_attempted = True
        t0 = time.perf_counter()
        try:
            active_query_vector = generate_embedding(f"query: {retrieval_q}")
        except Exception as exc:
            channel_status["embedding"] = f"degraded:{type(exc).__name__}"
            logger.warning("lookup pool embedding failed user=%s: %s", user_id, exc)
        stage_ms["embedding"] = round((time.perf_counter() - t0) * 1000.0, 2)
    elif active_query_vector is None and not generate_query_embedding:
        channel_status["embedding"] = "skipped:caller_degraded"
        channel_status["entry_dense"] = "degraded:no_query_vector"
        channel_status["knowledge_dense"] = "degraded:no_query_vector"
        channel_status["attachment_dense"] = "degraded:no_query_vector"
    else:
        channel_status["embedding"] = "reused"

    if active_query_vector is not None:
        try:
            knowledge_refs, entry_dense_refs, dense_status, dense_timings = fetch_dense_channel_refs(
                db,
                user_id=user_id,
                query_vector=active_query_vector,
                top_k=dense_k,
                label_code=label_code,
            )
            channel_status.update(dense_status)
            stage_ms["knowledge_dense"] = float(dense_timings.get("knowledge_dense") or 0.0)
            stage_ms["entry_dense"] = float(dense_timings.get("entry_dense") or 0.0)
        except Exception as exc:
            channel_status["dense"] = f"degraded:{type(exc).__name__}"
        if include_attachments:
            t0 = time.perf_counter()
            try:
                attachment_refs = [
                    _normalize_attachment(item)
                    for item in search_similar_attachment_chunks(
                        db=db,
                        query_vector=active_query_vector,
                        user_id=user_id,
                        top_k=DEFAULT_ATTACHMENT_TOP_K,
                    )
                ]
                channel_status["attachment_dense"] = "ok"
            except Exception as exc:
                channel_status["attachment_dense"] = f"degraded:{type(exc).__name__}"
            stage_ms["attachment"] = round((time.perf_counter() - t0) * 1000.0, 2)

    t0 = time.perf_counter()
    merged = (
        list(lexical_refs)
        + list(todo_lexical_refs)
        + list(notion_lexical_refs)
        + list(entry_dense_refs)
        + list(knowledge_refs)
        + list(attachment_refs)
    )
    kept, dropped = dedupe_by_evidence_origin(merged)
    kept = _attach_judge_evidence(kept, terms)
    kept = annotate_with_canonical_keys(kept)

    def _rank_key(ref: Dict[str, Any]) -> tuple:
        return _selection_sort_key(ref)

    kept = sorted(kept, key=_rank_key)
    candidates, budget_meta = select_candidates_for_budget(kept, budget=cap)
    from app.services.entry_family import enrich_references_with_parent_context

    candidates = enrich_references_with_parent_context(
        db,
        user_id,
        candidates,
        inject_parent_into_content=True,
    )
    stage_ms["evidence"] = round((time.perf_counter() - t0) * 1000.0, 2)
    stage_ms["total"] = round((time.perf_counter() - t_total0) * 1000.0, 2)

    return {
        "candidates": candidates,
        "candidate_count": len(candidates),
        # Diagnostic only — must not alone become user-facing 503.
        "candidate_pool_truncated": bool(budget_meta.get("candidate_budget_applied")),
        "candidate_budget_applied": bool(budget_meta.get("candidate_budget_applied")),
        "result_scope": str(budget_meta.get("result_scope") or "exhaustive"),
        "pre_cap_count": len(kept),
        "pre_budget_count": int(budget_meta.get("pre_budget_count") or len(kept)),
        "selected_count": int(budget_meta.get("selected_count") or len(candidates)),
        "candidate_cap": cap,
        "budget_selection": budget_meta,
        "selection_reason": budget_meta.get("selection_reason"),
        "channel_selected": budget_meta.get("channel_selected") or {},
        "dropped_mirror_count": len(dropped),
        "channel_counts": {
            "lexical_union": len(lexical_refs),
            "todo_lexical": len(todo_lexical_refs),
            "notion_lexical": len(notion_lexical_refs),
            "entry_dense": len(entry_dense_refs),
            "knowledge_dense": len(knowledge_refs),
            "attachment_dense": len(attachment_refs),
        },
        "channel_status": channel_status,
        "lexical_scan": lexical_scan_meta,
        "stage_ms": stage_ms,
        "query_normalize": {
            "token_count": len(nq.tokens),
            "expanded_count": len(nq.expanded_terms),
            "synonym_hit_count": len(nq.synonym_hits),
            "query_vector_reused": reused_query_vector,
            "embedding_attempted": embedding_attempted,
        },
        "query_vector_reused": reused_query_vector,
        "embedding_attempted": embedding_attempted,
        "degraded": any(str(v).startswith("degraded:") for v in channel_status.values()),
    }
