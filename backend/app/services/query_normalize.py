"""
Configurable query normalization for R5.2.

CJK/Latin/digit boundary split, case/width normalize, synonym expansion.
Does not mutate production RRF. Synonyms are registry-driven, not UI hardcodes.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_DEFAULT_SYNONYMS: Dict[str, List[str]] = {
    "prompt": ["提示词"],
    "提示词": ["prompt"],
}

_BOUNDARY_RE = re.compile(
    r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])(?=[\u4e00-\u9fff])"
)


@dataclass
class NormalizedQuery:
    original: str
    normalized: str
    tokens: List[str]
    expanded_terms: List[str]
    synonym_hits: List[str] = field(default_factory=list)
    diagnostics: Dict[str, object] = field(default_factory=dict)


def _to_halfwidth(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def load_synonym_registry(path: Optional[Path] = None) -> Dict[str, List[str]]:
    """Load synonym map; env RAG_QUERY_SYNONYMS_JSON overrides file/defaults."""
    env_raw = (os.getenv("RAG_QUERY_SYNONYMS_JSON") or "").strip()
    if env_raw:
        data = json.loads(env_raw)
        if isinstance(data, dict):
            return {str(k).lower(): [str(x) for x in (v or [])] for k, v in data.items()}
    if path and path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k).lower(): [str(x) for x in (v or [])] for k, v in data.items()}
    default_path = Path(__file__).resolve().parent / "query_synonyms.json"
    if default_path.is_file():
        data = json.loads(default_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k).lower(): [str(x) for x in (v or [])] for k, v in data.items()}
    return dict(_DEFAULT_SYNONYMS)


def split_script_boundaries(text: str) -> str:
    return _BOUNDARY_RE.sub(" ", text)


def tokenize_normalized(text: str) -> List[str]:
    cleaned = []
    for ch in text:
        if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff"):
            cleaned.append(ch)
        else:
            cleaned.append(" ")
    tokens: List[str] = []
    for tok in "".join(cleaned).split():
        if not tok:
            continue
        tokens.append(tok)
        # light CJK n-grams for sticky compounds already split
        cjk = [c for c in tok if "\u4e00" <= c <= "\u9fff"]
        if len(cjk) >= 2 and len(cjk) == len(tok):
            for size in (2, 3):
                for i in range(0, max(0, len(tok) - size + 1)):
                    tokens.append(tok[i : i + size])
    # stable unique
    seen = set()
    out: List[str] = []
    for t in tokens:
        tl = t.lower()
        if tl in seen:
            continue
        seen.add(tl)
        out.append(tl)
    return out


def expand_synonyms(tokens: Sequence[str], registry: Dict[str, List[str]]) -> Tuple[List[str], List[str]]:
    expanded: List[str] = []
    hits: List[str] = []
    seen = set()
    for tok in tokens:
        key = tok.lower()
        if key not in seen:
            seen.add(key)
            expanded.append(key)
        for syn in registry.get(key) or []:
            s = str(syn).lower()
            if s not in seen:
                seen.add(s)
                expanded.append(s)
                hits.append(f"{key}->{s}")
    return expanded, hits


def normalize_query(
    query: str,
    *,
    synonym_registry: Optional[Dict[str, List[str]]] = None,
) -> NormalizedQuery:
    original = query or ""
    registry = synonym_registry if synonym_registry is not None else load_synonym_registry()
    step = unicodedata.normalize("NFKC", original)
    step = _to_halfwidth(step)
    step = step.strip().lower()
    step = split_script_boundaries(step)
    step = re.sub(r"\s+", " ", step).strip()
    tokens = tokenize_normalized(step)
    expanded, hits = expand_synonyms(tokens, registry)
    # Retrieval-facing normalized string keeps boundary spaces + synonym terms
    normalized = " ".join(expanded) if expanded else step
    return NormalizedQuery(
        original=original,
        normalized=normalized,
        tokens=tokens,
        expanded_terms=expanded,
        synonym_hits=hits,
        diagnostics={
            "token_count": len(tokens),
            "expanded_count": len(expanded),
            "synonym_hit_count": len(hits),
            "boundary_split_applied": bool(_BOUNDARY_RE.search(original or "")),
            # never echo original/normalized into public reports by default
            "has_original": bool(original.strip()),
        },
    )


def retrieval_query_text(nq: NormalizedQuery) -> str:
    """Text passed to keyword/dense channels (normalized + synonyms)."""
    return nq.normalized or nq.original


# Exhaustive/list operators — not topical content terms for lexical union.
_LOOKUP_OPERATOR_TERMS = {
    "列出",
    "罗列",
    "全部",
    "所有",
    "有哪些",
    "哪些",
    "找一个",
    "找一下",
    "帮我找",
    "帮我",
    "哪一条",
    "哪篇",
    "是什么",
    "什么",
    "要点",
    "相关",
    "记录",
    "list",
    "show",
    "all",
    "every",
    "find",
    "one",
    "what",
}

# Multi-character presentation phrases stripped before topic fingerprinting.
_PRESENTATION_PHRASES = (
    "列出全部",
    "列出所有",
    "有哪些要点",
    "有哪些",
    "找一个关于",
    "找一个",
    "找一下",
    "帮我找",
    "是什么",
    "相关的记录",
    "的记录",
)


def primary_topic_terms(
    nq: NormalizedQuery,
    *,
    synonym_registry: Optional[Dict[str, List[str]]] = None,
) -> List[str]:
    """
    Content-bearing terms for lookup lexical union / judge evidence.

    Prefer synonym-registry seeds found in the query (e.g. prompt↔提示词),
    not exhaustive operators or incidental CJK n-grams like 列出/全部.
    """
    registry = synonym_registry if synonym_registry is not None else load_synonym_registry()
    registry_l = {str(k).lower(): [str(x).lower() for x in (v or [])] for k, v in registry.items()}
    syn_values = {s for vals in registry_l.values() for s in vals}

    seeds: List[str] = []
    seen = set()
    for tok in nq.tokens:
        key = tok.lower()
        if key in _LOOKUP_OPERATOR_TERMS:
            continue
        if key in registry_l or key in syn_values:
            if key not in seen:
                seen.add(key)
                seeds.append(key)

    if seeds:
        expanded, _hits = expand_synonyms(seeds, registry_l)
        return expanded

    # Fallback: expanded terms minus operators / ultra-short tokens.
    out: List[str] = []
    seen = set()
    for tok in nq.expanded_terms:
        key = (tok or "").strip().lower()
        if len(key) < 2 or key in _LOOKUP_OPERATOR_TERMS:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def extract_retrieval_topic(query: str) -> Dict[str, object]:
    """
    R11.7: presentation operators stripped; topic shared across ordinary/find/list.

    Returns topic_terms, canonical_topic, and retrieval_query (text for embedding /
    lexical / pool — never includes find-one/list-all presentation wording).
    """
    raw = (query or "").strip().lower()
    stripped = raw
    for phrase in sorted(_PRESENTATION_PHRASES, key=len, reverse=True):
        stripped = stripped.replace(phrase.lower(), " ")
    stripped = re.sub(r"\s+", " ", stripped).strip()
    nq = normalize_query(stripped if stripped else raw)
    terms = primary_topic_terms(nq) or list(nq.expanded_terms)
    topic_terms = sorted(
        {
            str(t).strip().lower()
            for t in terms
            if str(t).strip()
            and str(t).strip().lower() not in _LOOKUP_OPERATOR_TERMS
            and len(str(t).strip()) >= 2
        }
    )
    retrieval_query = " ".join(topic_terms) if topic_terms else (nq.normalized or stripped or raw)
    return {
        "topic_terms": topic_terms,
        "canonical_topic": "|".join(topic_terms),
        "retrieval_query": retrieval_query,
        "token_count": len(nq.tokens),
    }
