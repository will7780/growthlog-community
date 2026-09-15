"""
Static coverage scopes for keyword-only / vector-only / hybrid.

These reflect the current production call graphs (as of Phase D.1 fix), not
desired future coverage. Comparisons across unequal scopes must be marked
unsupported.

OCR text is indexed as attachment_chunk content; it is a modality/tag, not a
separate reference_key source_type / corpus facet.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, List, Set

# Retrieval corpus facets (coverage_scope). OCR is NOT a separate facet.
CORPUS_FACETS = frozenset({"entry", "attachment_chunk", "knowledge_source"})

# Tags/modalities that may annotate attachment_chunk qrels without changing
# reference_key source_type.
ATTACHMENT_MODALITY_TAGS = frozenset({"ocr", "ocr_chunk", "image", "pdf", "pptx"})

RETRIEVER_COVERAGE: Dict[str, FrozenSet[str]] = {
    # LIKE over Entry.content / label only.
    "keyword-only": frozenset({"entry"}),
    # FAISS entry index only (search_similar_entries).
    "vector-only": frozenset({"entry"}),
    # retrieve_rag_context: knowledge dense (+ entry fallback) + keyword entries
    # + attachment_chunk dense (OCR text lives inside those chunks).
    "hybrid": frozenset({"entry", "attachment_chunk", "knowledge_source"}),
}

RETRIEVER_NAMES = ("keyword-only", "vector-only", "hybrid")


def coverage_scope_for(retriever: str) -> FrozenSet[str]:
    key = (retriever or "").strip().lower()
    aliases = {
        "keyword": "keyword-only",
        "dense": "vector-only",
        "vector": "vector-only",
    }
    key = aliases.get(key, key)
    if key not in RETRIEVER_COVERAGE:
        raise KeyError(f"unknown retriever: {retriever}")
    return RETRIEVER_COVERAGE[key]


def required_facets_from_source_types(source_types: Iterable[str]) -> Set[str]:
    """
    Map qrel source_type (must match reference_key prefix) to corpus facets.

    Labels like ocr/ocr_chunk are modalities: they map to attachment_chunk and
    must not invent a distinct coverage facet.
    """
    out: Set[str] = set()
    for raw in source_types:
        st = (raw or "").strip().lower()
        if st in {"entry"}:
            out.add("entry")
        elif st in {"attachment_chunk", "attachment", "ocr", "ocr_chunk"}:
            out.add("attachment_chunk")
        elif st in {"knowledge_source", "knowledge_chunk", "knowledge"}:
            out.add("knowledge_source")
        elif st:
            out.add(st)
    return out


def retriever_supports(retriever: str, needed_facets: Iterable[str]) -> tuple[bool, List[str]]:
    scope = coverage_scope_for(retriever)
    lack = sorted(set(needed_facets) - set(scope))
    return (not lack), lack


def comparison_supported(
    retrievers: Iterable[str],
    needed_facets: Iterable[str],
) -> tuple[bool, List[str]]:
    """True only when every listed retriever covers every needed facet."""
    needed = set(needed_facets)
    missing: List[str] = []
    for name in retrievers:
        ok, lack = retriever_supports(name, needed)
        if not ok:
            missing.append(f"{name}:missing={','.join(lack)}")
    return (not missing), missing
