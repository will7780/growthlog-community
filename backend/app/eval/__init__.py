"""Offline evaluation helpers (no production retrieval side effects)."""

from app.eval.rag_coverage import RETRIEVER_COVERAGE, coverage_scope_for
from app.eval.rag_schema import (
    STAGE_CANDIDATE,
    STAGE_FROZEN,
    STAGE_REVIEWED,
    normalize_case,
    stable_legacy_case_id,
    validate_case,
)

__all__ = [
    "RETRIEVER_COVERAGE",
    "STAGE_CANDIDATE",
    "STAGE_FROZEN",
    "STAGE_REVIEWED",
    "coverage_scope_for",
    "normalize_case",
    "stable_legacy_case_id",
    "validate_case",
]
