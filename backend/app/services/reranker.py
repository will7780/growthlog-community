"""
Optional cross-encoder reranking.

Disabled by default. Set RAG_RERANK_MODEL to a local/available sentence-
transformers cross-encoder model name to enable. If loading or scoring fails,
the original ranking is returned unchanged.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Dict, List

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_cross_encoder(model_name: str):
    from sentence_transformers import CrossEncoder  # type: ignore

    return CrossEncoder(model_name)


def rerank_references(query: str, references: List[Dict], top_k: int) -> List[Dict]:
    model_name = (os.getenv("RAG_RERANK_MODEL") or "").strip()
    if not model_name or not references:
        return references[:top_k]

    try:
        model = _load_cross_encoder(model_name)
        pairs = [(query, ref.get("content") or ref.get("snippet") or ref.get("title") or "") for ref in references]
        scores = model.predict(pairs)
        reranked = []
        for ref, score in zip(references, scores):
            item = dict(ref)
            item["rerank_score"] = float(score)
            item["retrieval_method"] = item.get("retrieval_method") or "rerank"
            item.setdefault("metadata", {})["reranker_model"] = model_name
            reranked.append(item)
        reranked.sort(key=lambda item: item.get("rerank_score", 0.0), reverse=True)
        return reranked[:top_k]
    except Exception as exc:
        logger.warning("rerank disabled after failure: %s", exc)
        return references[:top_k]
