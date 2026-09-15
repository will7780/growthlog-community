"""
Central generation eval schema.

Branches:
  - generation_v1: legacy reviewed/formal rows with fixed answer/references
  - generation_spec_v2: formal *specs* only (query + qrels); answers at live run

Stages mirror RAG: candidate → reviewed → frozen (formal).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.eval.rag_schema import (
    ALLOWED_RELEVANCE,
    ALLOWED_REVIEWED_STATUS,
    ALLOWED_SPLITS,
    E_DRAFT_NOT_PROMOTABLE,
    E_EXAMPLE_NOT_PROMOTABLE,
    E_PENDING_NOT_PROMOTABLE,
    E_SCHEMA_INVALID,
    REFERENCE_KEY_RE,
    CaseValidationError,
)

SPEC_VERSION_V2 = "generation_spec_v2"
ALLOWED_PROVENANCE = frozenset(
    {"manual", "codex_assisted", "cursor_assisted", "imported", "unknown"}
)

SPEC_V2_REQUIRED_FIELDS = (
    "case_id",
    "query",
    "qrels",
    "expected_answerable",
    "split",
    "tags",
    "difficulty",
    "query_type",
    "annotation_provenance",
    "manual_label_status",
    "spec_version",
)

# Live-only fields forbidden on formal specs.
SPEC_V2_FORBIDDEN_LIVE_FIELDS = (
    "answer",
    "references",
    "valid_references",
    "expected_reference_keys",
    "claims",
)


def detect_generation_branch(item: Dict[str, Any]) -> str:
    """Return 'spec_v2' or 'v1'."""
    if not isinstance(item, dict):
        return "v1"
    version = str(item.get("spec_version") or "").strip()
    if version in {SPEC_VERSION_V2, "2", "v2"}:
        return "spec_v2"
    # Heuristic: qrels present and no fixed answer → treat as v2 draft.
    if "qrels" in item and "answer" not in item and "references" not in item:
        return "spec_v2"
    return "v1"


def _validate_qrels(qrels: Any, *, expected_answerable: bool) -> List[str]:
    errors: List[str] = []
    if not isinstance(qrels, list):
        return ["qrels must be a list"]
    if expected_answerable and len(qrels) == 0:
        errors.append("expected_answerable=true requires non-empty qrels")
    for idx, row in enumerate(qrels):
        if not isinstance(row, dict):
            errors.append(f"qrels[{idx}] must be an object")
            continue
        key = str(row.get("reference_key") or "").strip()
        if not key or not REFERENCE_KEY_RE.fullmatch(key):
            errors.append(f"qrels[{idx}].reference_key invalid")
        rel = row.get("relevance")
        try:
            rel_i = int(rel)
        except (TypeError, ValueError):
            errors.append(f"qrels[{idx}].relevance must be int in {{1,2,3}}")
            continue
        if rel_i not in ALLOWED_RELEVANCE:
            errors.append(f"qrels[{idx}].relevance must be in {{1,2,3}}")
        st = str(row.get("source_type") or "").strip()
        if not st:
            errors.append(f"qrels[{idx}].source_type is required")
    return errors


def normalize_generation_spec_v2(
    item: Dict[str, Any],
    *,
    stage: str = "reviewed",
) -> Dict[str, Any]:
    """Validate and normalize a generation_spec_v2 row."""
    if bool(item.get("example")):
        raise CaseValidationError(
            f"{E_EXAMPLE_NOT_PROMOTABLE}: example=true cannot be promoted",
            code=E_EXAMPLE_NOT_PROMOTABLE,
        )
    status = str(item.get("manual_label_status") or "").strip()
    if status in {"pending"}:
        raise CaseValidationError(
            f"{E_PENDING_NOT_PROMOTABLE}: pending cannot be promoted",
            code=E_PENDING_NOT_PROMOTABLE,
        )
    if status in {"draft"}:
        raise CaseValidationError(
            f"{E_DRAFT_NOT_PROMOTABLE}: draft cannot be promoted",
            code=E_DRAFT_NOT_PROMOTABLE,
        )
    if stage in {"reviewed", "frozen"} and status not in ALLOWED_REVIEWED_STATUS:
        raise CaseValidationError(
            f"manual_label_status must be reviewed/approved, got {status!r}",
            code=E_SCHEMA_INVALID,
        )

    missing = [f for f in SPEC_V2_REQUIRED_FIELDS if f not in item]
    if missing:
        raise CaseValidationError(
            f"missing required fields: {', '.join(missing)}",
            code=E_SCHEMA_INVALID,
        )

    for live in SPEC_V2_FORBIDDEN_LIVE_FIELDS:
        if live in item and item.get(live) not in (None, "", [], {}):
            raise CaseValidationError(
                f"spec_v2 forbids live field {live!r}",
                code=E_SCHEMA_INVALID,
            )

    case_id = str(item.get("case_id") or "").strip()
    query = str(item.get("query") or "").strip()
    if not case_id:
        raise CaseValidationError("case_id is required", code=E_SCHEMA_INVALID)
    if not query:
        raise CaseValidationError("query is required", code=E_SCHEMA_INVALID)

    if not isinstance(item.get("expected_answerable"), bool):
        raise CaseValidationError(
            "expected_answerable must be a boolean", code=E_SCHEMA_INVALID
        )
    split = str(item.get("split") or "").strip()
    if split not in ALLOWED_SPLITS:
        raise CaseValidationError(
            f"split must be one of {sorted(ALLOWED_SPLITS)}", code=E_SCHEMA_INVALID
        )
    tags = item.get("tags")
    if not isinstance(tags, list):
        raise CaseValidationError("tags must be a list", code=E_SCHEMA_INVALID)
    difficulty = str(item.get("difficulty") or "").strip()
    if not difficulty:
        raise CaseValidationError("difficulty is required", code=E_SCHEMA_INVALID)
    query_type = str(item.get("query_type") or "").strip()
    if not query_type:
        raise CaseValidationError("query_type is required", code=E_SCHEMA_INVALID)
    provenance = str(item.get("annotation_provenance") or "").strip()
    if provenance not in ALLOWED_PROVENANCE:
        raise CaseValidationError(
            f"annotation_provenance must be one of {sorted(ALLOWED_PROVENANCE)}",
            code=E_SCHEMA_INVALID,
        )
    version = str(item.get("spec_version") or "").strip()
    if version not in {SPEC_VERSION_V2, "2", "v2"}:
        raise CaseValidationError(
            f"spec_version must be {SPEC_VERSION_V2!r}", code=E_SCHEMA_INVALID
        )

    qrel_errors = _validate_qrels(
        item.get("qrels"), expected_answerable=bool(item["expected_answerable"])
    )
    if qrel_errors:
        raise CaseValidationError("; ".join(qrel_errors), code=E_SCHEMA_INVALID)

    qrels_out: List[Dict[str, Any]] = []
    for row in item.get("qrels") or []:
        if not isinstance(row, dict):
            continue
        qrels_out.append(
            {
                "reference_key": str(row["reference_key"]).strip(),
                "relevance": int(row["relevance"]),
                "source_type": str(row.get("source_type") or "").strip(),
            }
        )

    return {
        "case_id": case_id,
        "query": query,
        "qrels": qrels_out,
        "expected_answerable": bool(item["expected_answerable"]),
        "split": split,
        "tags": list(tags),
        "difficulty": difficulty,
        "query_type": query_type,
        "annotation_provenance": provenance,
        "manual_label_status": status,
        "spec_version": SPEC_VERSION_V2,
    }


def validate_generation_spec_v2_item(
    item: Dict[str, Any],
    *,
    stage: str = "reviewed",
) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    try:
        normalize_generation_spec_v2(item, stage=stage)
    except CaseValidationError as exc:
        errors.append(str(exc))
    return errors, warnings


def case_to_formal_spec_v2(item: Dict[str, Any]) -> Dict[str, Any]:
    return normalize_generation_spec_v2(item, stage="frozen")
