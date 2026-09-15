"""
Canonical reference identity shared by retrieval, generation, eval, and API.

Canonical form matches rank_fusion.result_key / frozen RAG qrels:
  entry:source:<id>
  attachment_chunk:chunk:<id>
  knowledge_source:source:<id>  (or knowledge_source:chunk:<id> when chunk_id set)

Legacy short forms remain accepted as aliases for matching only:
  entry:<id>
  attachment_chunk:<id>
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.services.rank_fusion import result_key

RESULT_KEY_RE = re.compile(
    r"^(?P<source_type>[a-z][a-z0-9_]{0,62}):(?P<kind>chunk|source|entry):(?P<id>\d+)$"
)
SHORT_KEY_RE = re.compile(
    r"^(?P<source_type>[a-z][a-z0-9_]{0,62}):(?P<id>\d+)$"
)
# Rare legacy: attachment_chunk:<attachment_id>:<index>
LEGACY_ATTACH_INDEX_RE = re.compile(
    r"^attachment_chunk:(?P<aid>\d+):(?P<idx>\d+)$"
)

# Explicit proposal / source-set identities (no ambiguous source_id shortcuts).
PROPOSAL_REFERENCE_KEY_FORMS = frozenset(
    {
        ("entry", "source"),
        ("attachment", "source"),
        ("attachment_chunk", "chunk"),
        ("knowledge_source", "chunk"),
        ("todo", "source"),
        ("notion_page", "source"),
    }
)


def parse_canonical_reference_key(raw: str) -> Optional[Tuple[str, str, int]]:
    """
    Strict parser for canonical reference_key.
    Returns (source_type, kind, id) or None. Never invents IDs.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    m = RESULT_KEY_RE.fullmatch(text)
    if not m:
        return None
    return m.group("source_type"), m.group("kind"), int(m.group("id"))


def parse_proposal_reference_key(raw: str) -> Optional[Tuple[str, str, int]]:
    """
    Strict parser for explainer source-set proposal keys.
    Only the unambiguous production forms are accepted.
    """
    parsed = parse_canonical_reference_key(raw)
    if parsed is None:
        return None
    source_type, kind, _oid = parsed
    if (source_type, kind) not in PROPOSAL_REFERENCE_KEY_FORMS:
        return None
    return parsed


def canonical_reference_key(ref: Dict[str, Any], index: int = 0) -> str:
    """
    Produce canonical key for a reference dict.
    Prefer existing canonical reference_key; else compute via result_key.
    Never invent unknown keys that collide across types.
    """
    if not isinstance(ref, dict):
        return f"unknown:entry:{int(index)}"

    explicit = ref.get("reference_key")
    if isinstance(explicit, str) and explicit.strip():
        text = explicit.strip()
        # Already canonical
        if RESULT_KEY_RE.fullmatch(text):
            return text
        # Upgrade known legacy shorts to canonical for *new output*
        upgraded = upgrade_legacy_key(text)
        if upgraded:
            return upgraded

    # Normalize field-level source_type before result_key / fallbacks.
    source_type = str(ref.get("source_type") or "entry").strip() or "entry"
    if source_type == "attachment":
        # Knowledge mirror of attachments (NOT attachment_chunks table).
        source_type = "knowledge_source"
    elif source_type == "knowledge":
        source_type = "knowledge_source"

    if source_type == "todo":
        todo_id = ref.get("todo_id") or ref.get("source_id")
        if todo_id is not None:
            return f"todo:source:{int(todo_id)}"
        return f"todo:entry:{int(index)}"
    if source_type == "notion_page":
        page_id = ref.get("notion_page_id") or ref.get("source_id")
        if page_id is not None:
            return f"notion_page:source:{int(page_id)}"
        return f"notion_page:entry:{int(index)}"

    # Field-based canonical (result_key) with normalized type
    try:
        normalized = dict(ref)
        normalized["source_type"] = source_type
        key = result_key(normalized)
        upgraded = upgrade_legacy_key(key)
        if upgraded and RESULT_KEY_RE.fullmatch(upgraded):
            return upgraded
        if RESULT_KEY_RE.fullmatch(key):
            return key
    except Exception:
        pass

    # Fallbacks for incomplete refs
    if source_type == "attachment_chunk":
        chunk_id = ref.get("chunk_id")
        if chunk_id is not None:
            return f"attachment_chunk:chunk:{int(chunk_id)}"
        attachment_id = ref.get("attachment_id")
        if attachment_id is not None:
            # No chunk_id: unstable index-based key (eval/debug only)
            return f"attachment_chunk:source:{int(attachment_id)}"
        return f"attachment_chunk:entry:{int(index)}"
    if source_type == "knowledge_source":
        chunk_id = ref.get("chunk_id")
        if chunk_id is not None:
            return f"knowledge_source:chunk:{int(chunk_id)}"
        source_id = ref.get("source_id") or ref.get("knowledge_source_id")
        if source_id is not None:
            return f"knowledge_source:source:{int(source_id)}"
        return f"knowledge_source:entry:{int(index)}"

    entry_id = ref.get("entry_id") or ref.get("source_id")
    if entry_id is not None:
        return f"entry:source:{int(entry_id)}"
    return f"entry:entry:{int(index)}"


def upgrade_legacy_key(key: str) -> Optional[str]:
    """Map known legacy short keys to canonical; None if unknown shape."""
    text = str(key or "").strip()
    if not text:
        return None
    m_canon = RESULT_KEY_RE.fullmatch(text)
    if m_canon:
        # Mis-typed knowledge hits historically used attachment:chunk:<kc_id>
        if m_canon.group("source_type") in {"attachment", "knowledge"}:
            return f"knowledge_source:{m_canon.group('kind')}:{m_canon.group('id')}"
        return text
    m = SHORT_KEY_RE.fullmatch(text)
    if m:
        st, sid = m.group("source_type"), m.group("id")
        if st == "entry":
            return f"entry:source:{sid}"
        if st == "attachment_chunk":
            return f"attachment_chunk:chunk:{sid}"
        if st in {"knowledge_source", "knowledge", "attachment"}:
            # short "attachment:<id>" is ambiguous; treat as knowledge_source source id
            if st == "attachment":
                return f"knowledge_source:source:{sid}"
            return f"knowledge_source:source:{sid}"
        return f"{st}:source:{sid}"
    m2 = LEGACY_ATTACH_INDEX_RE.fullmatch(text)
    if m2:
        # attachment without chunk_id historically used index; map to source kind
        return f"attachment_chunk:source:{m2.group('aid')}"
    return None


def key_match_aliases(key: str) -> Set[str]:
    """All forms that should match the same identity for offline scoring."""
    text = str(key or "").strip()
    if not text:
        return set()
    aliases = {text}
    upgraded = upgrade_legacy_key(text)
    if upgraded:
        aliases.add(upgraded)
    m = RESULT_KEY_RE.fullmatch(text)
    if m:
        st, kind, sid = m.group("source_type"), m.group("kind"), m.group("id")
        aliases.add(f"{st}:{sid}")
        if kind == "source" and st == "entry":
            aliases.add(f"entry:entry:{sid}")
        if kind == "entry" and st == "entry":
            aliases.add(f"entry:source:{sid}")
        if kind == "chunk":
            aliases.add(f"{st}:{sid}")
        # Historical knowledge hit mislabel
        if st == "knowledge_source":
            aliases.add(f"attachment:{kind}:{sid}")
            aliases.add(f"knowledge:{kind}:{sid}")
            aliases.add(f"attachment:{sid}")
        if st == "attachment":
            aliases.add(f"knowledge_source:{kind}:{sid}")
    m2 = SHORT_KEY_RE.fullmatch(text)
    if m2:
        st, sid = m2.group("source_type"), m2.group("id")
        if st == "entry":
            aliases.add(f"entry:source:{sid}")
            aliases.add(f"entry:entry:{sid}")
        elif st == "attachment_chunk":
            aliases.add(f"attachment_chunk:chunk:{sid}")
        else:
            aliases.add(f"{st}:source:{sid}")
            aliases.add(f"{st}:chunk:{sid}")
    return aliases


def keys_intersect(left: Iterable[str], right: Iterable[str]) -> Set[str]:
    right_alias: Set[str] = set()
    for key in right:
        right_alias |= key_match_aliases(str(key))
    matched: Set[str] = set()
    for key in left:
        if key_match_aliases(str(key)) & right_alias:
            matched.add(str(key))
    return matched


def resolve_key_against_known(key: str, known_canonical: Set[str]) -> Optional[str]:
    """
    Resolve a judge-returned key to a known canonical key via aliases.
    Returns None if unknown (must be rejected).
    """
    aliases = key_match_aliases(key)
    for cand in known_canonical:
        if key_match_aliases(cand) & aliases:
            return cand
    return None


def normalize_judge_key_list(
    raw_keys: Sequence[Any],
    known_canonical: Set[str],
) -> Tuple[List[str], List[str], List[str]]:
    """
    Returns (accepted_canonical_deduped, unknown_keys, duplicate_keys).
    Order preserved for first occurrence of each canonical key.
    """
    accepted: List[str] = []
    seen: Set[str] = set()
    unknown: List[str] = []
    duplicates: List[str] = []
    for raw in raw_keys or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip()
        resolved = resolve_key_against_known(text, known_canonical)
        if resolved is None:
            unknown.append(text)
            continue
        if resolved in seen:
            duplicates.append(text)
            continue
        seen.add(resolved)
        accepted.append(resolved)
    return accepted, unknown, duplicates


def evidence_origin_key(ref: Dict[str, Any], index: int = 0) -> str:
    """
    Cross-source evidence identity for mirror dedupe.

    attachment_chunk and knowledge mirrors of the same attachment page share:
      attachment_origin:<attachment_id>:<chunk_index>
    Entries use their canonical entry key.
    """
    if not isinstance(ref, dict):
        return f"unknown:{int(index)}"
    meta = ref.get("metadata") if isinstance(ref.get("metadata"), dict) else {}
    source_type = str(ref.get("source_type") or "").strip()

    attachment_id = ref.get("attachment_id")
    if attachment_id is None and meta.get("origin_type") == "attachment":
        attachment_id = meta.get("origin_id")
    if attachment_id is None and source_type in {"attachment_chunk", "attachment"}:
        attachment_id = ref.get("source_id")

    chunk_index = ref.get("chunk_index")
    if chunk_index is None:
        chunk_index = meta.get("chunk_index")

    if attachment_id is not None and chunk_index is not None:
        try:
            return f"attachment_origin:{int(attachment_id)}:{int(chunk_index)}"
        except (TypeError, ValueError):
            pass

    # Fall back to canonical reference key (distinct evidence).
    return canonical_reference_key(ref, index)


def _prefer_attachment_chunk(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    """Prefer attachment_chunk over knowledge_source mirror of same origin."""
    lt = str(left.get("source_type") or "")
    rt = str(right.get("source_type") or "")
    if lt == "attachment_chunk" and rt != "attachment_chunk":
        return left
    if rt == "attachment_chunk" and lt != "attachment_chunk":
        return right
    return left


def dedupe_by_evidence_origin(
    references: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Dedupe mirrors by evidence_origin_key.
    Returns (kept, dropped_duplicates) where dropped are duplicate_evidence_mirror.
    """
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    by_origin: Dict[str, int] = {}
    for index, ref in enumerate(references):
        if not isinstance(ref, dict):
            continue
        item = dict(ref)
        item["reference_key"] = canonical_reference_key(item, index)
        origin = evidence_origin_key(item, index)
        item["evidence_origin_key"] = origin
        if origin in by_origin:
            pos = by_origin[origin]
            winner = _prefer_attachment_chunk(kept[pos], item)
            loser = item if winner is kept[pos] else kept[pos]
            kept[pos] = winner
            dropped.append({
                **loser,
                "duplicate_of": winner.get("reference_key"),
                "reason": "duplicate_evidence_mirror",
            })
        else:
            by_origin[origin] = len(kept)
            kept.append(item)
    return kept, dropped


def annotate_with_canonical_keys(
    references: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for index, ref in enumerate(references):
        item = dict(ref) if isinstance(ref, dict) else {}
        item["reference_key"] = canonical_reference_key(item, index)
        item["evidence_origin_key"] = evidence_origin_key(item, index)
        out.append(item)
    return out
